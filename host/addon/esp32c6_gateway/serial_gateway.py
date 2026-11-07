#!/usr/bin/env python3
"""
serial_gateway.py —— 读取 ESP32-C6 的 JSON-Lines，转成 MQTT 发到 broker，
同时写进 MariaDB。Web 面板在 8080 端口。

「线」有两条，协议完全相同（一行一条 JSON）：
  uart —— USB 串口（/dev/ttyACM*），C6 插在主机 USB 口上；
  tcp  —— 本进程当服务端监听，C6 通过 Wi-Fi 主动连过来。
用 --link 选，默认 uart（和原来的行为一字不差）。

用法：
    python3 serial_gateway.py
    python3 serial_gateway.py --port /dev/ttyACM1 --broker 192.168.178.218
    python3 serial_gateway.py --link tcp --tcp-port 12345
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import serial
import paho.mqtt.client as mqtt
import pymysql

# ── 日志时间戳：用本地时区，不是容器 UTC ─────────────────────────────────
# HA 加载项容器默认没有 TZ，于是日志写 02:33，而墙上的钟是 10:33。
# 所以按这个顺序找时区来源：
#   1. LOG_TZ  （由加载项从 "timezone" 选项设置）
#   2. TZ      （仅当不是 UTC/GMT 时）
#   3. /etc/timezone 或符号链接 /etc/localtime
#   4. 系统时间（原样使用）
def _resolve_local_tz():
    """返回 (tzinfo|None, 显示名)。None = 使用系统时间。"""
    name = (os.getenv("LOG_TZ") or "").strip()
    if not name:
        env_tz = (os.getenv("TZ") or "").strip()
        if env_tz and env_tz.upper() not in ("UTC", "ETC/UTC", "GMT", "UCT"):
            name = env_tz
    if not name:
        try:
            with open("/etc/timezone", encoding="utf-8") as f:
                cand = f.read().strip()
            if cand and cand.upper() not in ("UTC", "ETC/UTC"):
                name = cand
        except OSError:
            pass
    if not name:
        try:
            link = os.readlink("/etc/localtime")
            for marker in ("zoneinfo/", "posix/"):
                if marker in link:
                    name = link.split(marker, 1)[1]
                    break
        except OSError:
            pass
    if not name:
        return None, "Systemzeit"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), name
    except Exception:
        pass
    # 镜像里没有 tzdata：只好拿 TZ + tzset() 顶一下
    try:
        if hasattr(time, "tzset"):
            os.environ["TZ"] = name
            time.tzset()
            return None, name + " (via TZ)"
    except Exception:
        pass
    return None, "Systemzeit"


_LOG_TZ, _LOG_TZ_NAME = _resolve_local_tz()


def _ts() -> str:
    """Log-Zeitstempel [HH:MM:SS] in Ortszeit."""
    if _LOG_TZ is not None:
        return datetime.now(_LOG_TZ).strftime("[%H:%M:%S]")
    return time.strftime("[%H:%M:%S]")


_print_raw = print
def print(*args, **kwargs):   # noqa: A001 —— 故意全局覆盖
    kwargs.setdefault("flush", True)
    _print_raw(_ts(), *args, **kwargs)

_dt0 = datetime.now(_LOG_TZ) if _LOG_TZ else datetime.now().astimezone()
print(f"[Log] 时区 {_LOG_TZ_NAME} · UTC{_dt0.strftime('%z')} · "
      f"现在 {_dt0.strftime('%Y-%m-%d %H:%M:%S')}")

# ── Konfiguration ─────────────────────────────────────────────────────────
DEFAULT_PORT   = "/dev/ttyACM0"
DEFAULT_BAUD   = 115200
DEFAULT_BROKER = "192.168.178.218"
DEFAULT_MQPORT = 1883
WEB_PORT       = int(os.getenv("WEB_PORT", "8080"))

DB_HOST = os.getenv("DB_HOST", "")   # empty = no database (e.g. HAOS add-on; HA records history itself)
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "gh")
DB_PASS = os.getenv("DB_PASS", "a12345")
DB_NAME = os.getenv("DB_NAME", "wagodb")

# ── 命令行参数 ─────────────────────────────────────────────────────────────
ap = argparse.ArgumentParser()
ap.add_argument("--port",   default=DEFAULT_PORT)
ap.add_argument("--baud",   default=DEFAULT_BAUD,   type=int)
ap.add_argument("--link",   default="uart", choices=["uart", "tcp"],
                help="传输通道：uart=USB 串口（默认），tcp=Wi-Fi/TCP 服务端")
ap.add_argument("--tcp-port", default=12345, type=int, dest="tcp_port",
                help="--link tcp 时监听的端口，要和 C6 里 net_cfg 用的端口一致")
ap.add_argument("--tcp-bind", default="0.0.0.0", dest="tcp_bind",
                help="--link tcp 时绑定的地址，默认全部网卡")
ap.add_argument("--broker", default=DEFAULT_BROKER)
ap.add_argument("--mqport", default=DEFAULT_MQPORT, type=int)
ap.add_argument("--user",   default=None)
ap.add_argument("--pass",   default=None, dest="passwd")
ap.add_argument("--base",   default="gw/c6serial",
                help="MQTT 基础主题（例如 gw/coordinator）")
ap.add_argument("--topo-interval", default=60, type=int, dest="topo_interval",
                help="自动采集网络拓扑的间隔秒数，0 = 关闭（拓扑页从此自动更新）")
args = ap.parse_args()

BASE   = args.base
DB_MAC = BASE.split("/")[-1][:8]

# 必须跟 config.json 里的版本号一致。它出现在启动横幅和网页上 ——
# 这样一眼就能看出容器是不是真的在跑新代码。
GW_VERSION = "1.5.7"

# ── Home Assistant MQTT Auto-Discovery ────────────────────────────────────
# 每个 Zigbee 设备/端点都会在第一条数据帧到达时自动注册成 HA 实体 ——
# configuration.yaml 里什么都不用写（前提是 HA 的 MQTT 集成已启用）。
HA_PREFIX  = os.getenv("HA_PREFIX", "homeassistant")
HA_ENABLED = os.getenv("HA_DISCOVERY", "1").lower() not in ("0", "no", "off", "false")
# 每台设备独立的 availability（见 _ha_avail / _avail_sync）。
# 关掉就退回"只看网关在线"，出问题时可以一条环境变量回滚。
DEVICE_AVAIL = os.getenv("DEVICE_AVAIL", "1").lower() not in ("0", "no", "off", "false")

# 型号数据库，来自 Zigbee2MQTT 的 converter（由 gen_device_db.py 生成）。
# 键 = ZCL Basic 0x0005（ModelIdentifier）。有了它，刚入网设备的端点
# 和能力立刻就能判对。
#
# ★ 免重建通道：run.sh 本来就会用 /share/serial_gateway.py 覆盖镜像副本，
#   所以这里也优先读 /share/device_db.json —— 以后 Z2M 出了新型号，
#   只要把新库拷到 /share 再重启 add-on 即可，不必重建整个镜像。
#   校验过是「非空字典」才采用；半截文件或手滑写坏就安静地回退到镜像内版本。
_ha_db = {}
_db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "device_db.json")
_share_db_path = "/share/device_db.json"
try:
    if os.path.isfile(_share_db_path):
        with open(_share_db_path, encoding="utf-8") as _sf:
            _probe_db = json.load(_sf)
        if isinstance(_probe_db, dict) and _probe_db:
            _db_path = _share_db_path
except Exception:
    pass                      # /share 那份坏掉就当它不存在
try:
    with open(_db_path, encoding="utf-8") as _f:
        _ha_db = json.load(_f)
    print(f"[HA] 设备数据库：已载入 {len(_ha_db)} 个型号（{_db_path}）")
except Exception as _e:
    print(f"[HA] 警告：device_db.json 载入失败（{_e}）—— 只用通用 Discovery")

# ── C6 链路存活判定 ────────────────────────────────────────────────────────
# ★ 2026-11-07 修复：以前 _state["status"] 只在收到 boot / heartbeat 时被置
#   "online"，从此再也不会变回 "offline" —— 结果 Web 页上「协调器在线」那个
#   徽标只要连过一次就永远是绿的：拔掉串口、TCP 断开、C6 断电，它照样显示
#   在线。排查时非常误导（会让人误以为链路没问题，转头去怀疑射频）。
#   现在改用「最近一次收到 C6 消息的时刻」做判据：C6 每 60 s 发一次心跳，
#   超过 2 个周期（130 s）没消息就判离线；链路断开的地方还会立即置一次，
#   不必干等超时。
_last_c6_ts = 0.0          # time.monotonic()；0 = 本次启动以来还没收到过
C6_STALE_S  = 130.0        # 心跳 60 s × 2 + 10 s 余量


def _c6_alive() -> bool:
    """C6 是否还在正常说话（收到过消息，且没超过 2 个心跳周期）。"""
    return _last_c6_ts > 0 and (time.monotonic() - _last_c6_ts) < C6_STALE_S


def _state_view() -> dict:
    """给页面和 /api/state 用的状态快照。

    只修正 status 并补一个 c6_age_s，**不改 _state 本身** —— 免得跟
    dispatch() 里的写入互相打架。
    """
    with _state_lock:
        s = json.loads(json.dumps(_state))
    if s.get("status") == "online" and not _c6_alive():
        s["status"] = "offline"
    s["c6_age_s"] = (round(time.monotonic() - _last_c6_ts, 1)
                     if _last_c6_ts > 0 else None)
    # 通道跟随的可观测性：link_echo 是 C6 自报「把命令归到了哪条通道」，
    # 也就是它接下来会把上报往哪发；hb_age_s 太久说明心跳没到（通道断了）。
    s["link_echo"] = _hb_link_echo
    s["hb_age_s"] = (round(time.monotonic() - _hb_last_pong, 1)
                     if _hb_last_pong > 0 else None)
    return s


# ── In-Memory State ────────────────────────────────────────────────────────
_state_lock = threading.Lock()
_state = {
    "status":       "offline",
    "uptime":       0,
    "channel":      0,
    "pan":          "–",
    "permit_join":  {"open": False, "seconds": 0},
    # 射频发送链路："want" 是设定值，"eff" 是 PA 回读的实际值。
    # 只有 "eff" 才是测量值（见 §21）。
    "tx_power":     {"want": None, "eff": None, "ts": ""},
    "devices":      {},   # addr → {ieee, name, last_seen, clusters: {name: value}}
    # 中继自报的父节点：addr → 父节点地址。来自中继固件把
    # zb_nwk_get_parent() 写进协调器 0xFC00/0x0002（设备自己说的，最权威）。
    "parent":       {},
    # 中继功率下发记录：addr → {dbm, ts, tsn}
    "wr_power":     {},
    # 网络拓扑（直连 / 经中继）。coordinator 的三张表直接镜像过来：
    #   nbr = 邻居表（我直接听得到谁）  rt = 路由表（到某地址的下一跳）
    #   rr  = 源路由（完整中继链路）    done = 本轮是否到齐
    "topo": {
        "pan": "", "ch": 0, "addr": "0x0000",
        "nbr": [], "rt": [], "rr": [],
        # mesh: addr → {"st":…, "tot":…, "nb":[…]}，每台设备**自己上报**的
        # 邻居表（Mgmt_Lqi_rsp）。这是判父节点的权威数据。
        "mesh": {},
        # round: 本轮采集的序号。每轮 +1，设备自报表里记下自己是第几轮采到的，
        # 收尾时把「不是本轮」的旧条目剔掉 —— 这样采集过程中图不会被清空
        # （旧图一直用到新图到齐），又不会留下已经掉线的设备。
        "round": 0,
        "ts": "", "done": False,
    },
    "ota": {
        "status":      "idle",   # idle | pending | transferring | done | error
        "bin_path":    "",
        "size":        0,
        "version":     1,
        "transferred": 0,
        # image type：这次推给哪一类设备
        #   0x0001 = 终端节点（client/，switch2wifi）
        #   0x0002 = Repeater (router/)
        "image_type":  0x0001,
        # 网关侧 OTA server 的内部计数（窗口命中/未命中/超时）
        "dbg":         {},
        # 交付对账（见 _ota_handle_req 注释）
        "sent":        0,
        "sent_hash":   "0x%08x" % 2166136261,
    },
    "fw_ota": {
        "status":   "idle",   # idle | pending | transferring | done | error
        "bin_path": "",
        "size":     0,
        "off":      0,
    },
}

# ── OTA state machine ───────────────────────────────────────────────────────
_ota_file = None   # open file handle during transfer
# ★ 交付对账：对「按顺序真正发出去的字节」算 FNV-1a，网关侧对「真正交给
#   协议栈的字节」算同一个哈希。两边一致 ⇒ 中继收到的数据逐字节正确。
#   只看 offset == _ota_next 的块，重复请求不会污染哈希。
_OTA_FNV0   = 2166136261
_ota_hash   = _OTA_FNV0
_ota_sent   = 0
_ota_next   = 0

# ── 协调器固件 OTA（走串口）─────────────────────────────────────────────────
# bin 放在树莓派上；按块经 UART 送给 C6，C6 用 esp_ota 写进下一个 OTA 分区。
# 流控：每批 FW_BATCH 块，C6 每收满 FW_ACK_EVERY（=16）块回一次 st="write"。
_fw_ota_file = None
_fw_ota_size = 0
_fw_ota_off  = 0

# ★ 2026-10-25：最后一次 {"t":"boot"} 的时间戳。
# C6 在 esp_zb_start() **之前**就起了 uart_rx 任务；这个窗口期内到达的命令
# 曾把它打进重启循环（zb_schedule_alarm 里 Load access fault）。固件已修 ——
# 这段冷却时间额外保护老镜像，也避免传输被 OTA 中途的一次重启彻底卡死。
_gw_boot_ts = 0.0
_GW_BOOT_COOLDOWN = 8.0
_fw_ota_restarts = 0

# ── 通道心跳：C6 的「通道跟随」靠它定位 ────────────────────────────────────
# C6 的规则是「哪个通道最近收到过命令，就把上报往哪个通道发」（20 s 窗口）。
# add-on 每 5 s 说一句 {"cmd":"ping"}，就能把当前通道一直钉住 —— 于是
# `--link serial` 和 `--link tcp` 都不用改 C6 的 NVS，切换后最多一个周期
# 就自动跟上，不会出现「换了端口、数据还往老通道发」这种哑火。
_HB_INTERVAL_S = 5.0
_hb_last_pong  = 0.0     # time.monotonic()；0 = 本次启动以来还没收到过
_hb_link_echo  = ""      # C6 回执里报的通道：uart / tcp
_hb_sent       = 0
_hb_started    = False


def _hb_loop():
    global _hb_sent
    while True:
        time.sleep(_HB_INTERVAL_S)
        try:
            # OTA 传输期间不插话：那边每批都有 st="write" 回执，通道已经明确，
            # 再挤一行只会白占带宽、还可能插进 fw_ota_data 中间。
            if _fw_ota_file is not None:
                continue
            # ★ 必须带 who="addon"：C6 只认带身份的心跳做通道归属。
            #   不带的话它仍会回 pong，但不会把上报切到这条通道 ——
            #   这样插着 USB 调试时，正走 TCP 的 add-on 不会被抢走。
            ser.write(b'{"cmd":"ping","who":"addon"}\n')
            _hb_sent += 1
        except Exception:                  # noqa: BLE001
            pass                           # 通道断着就等下一轮，不刷屏


def _hb_start():
    global _hb_started
    if _hb_started:
        return
    _hb_started = True
    threading.Thread(target=_hb_loop, daemon=True).start()
    print(f"[HB] 通道心跳已启动：每 {_HB_INTERVAL_S:.0f} s 一次"
          f"（C6 据此选择上报通道）")


def _env_int(name, default, lo, hi):
    try:
        v = int(os.getenv(name, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


# 帧长：一行是 43 + len(str(off)) + 2*chunk 个字符。
# C6 用 fgets(buf, BUF_SIZE) 读命令。老固件 BUF_SIZE=256 —— 一条 ~950 字符的行
# 会被切成碎片然后**静默丢弃**（症状正是：begin 到了，却永远等不到 "write" ACK）。
# 用 chunk=96 时一行约 241 字符，老缓冲也装得下 —— 代价是传输要多花几分钟。
FW_CHUNK = _env_int("FW_CHUNK", 80, 32, 512)
FW_BATCH = _env_int("FW_BATCH", 16, 1, 64)

# 启动横幅：没有这一行，说明容器里跑的还是旧构建。
print(f"[GATEWAY] v{GW_VERSION} · OTA-Frame {FW_CHUNK} B × {FW_BATCH} = "
      f"{FW_CHUNK * FW_BATCH} B/批 · 紧凑 JSON")

def _js(obj) -> str:
    """紧凑 JSON + 换行（用于日志输出和 .encode()）。"""
    return json.dumps(obj, separators=(",", ":")) + "\n"


def _j(obj) -> bytes:
    """给 UART 用的紧凑 JSON —— ':' 和 ',' 后面都不加空格。

    注意：老固件是用 `strstr(buf, "\\"data\\":\\"")` 解析的。
    `json.dumps()` 默认写 `"data": "…"`（冒号后带空格）—— 这种死板的
    strstr 永远找不到，帧就被静默丢掉了。写成紧凑形式（`"data":"…"`）可以
    同时满足两种解析器：老的那套严格的，和新的那套宽松的（json_val）。
    """
    return _js(obj).encode()


def _fw_ota_close():
    global _fw_ota_file, _fw_ota_off
    if _fw_ota_file:
        _fw_ota_file.close()
        _fw_ota_file = None
    _fw_ota_off = 0

def _save_upload(ctype: str, body: bytes, dst: str = None):
    """保存 multipart/form-data 请求体里的第一个文件。

    需要这样做，是因为 /data 在 add-on 容器里，HA 主机上看不到 ——
    不装 Samba 的话，从浏览器上传就是唯一的路。
    返回写好的路径（或 None）。

    dst：可选目标路径。对 OTA 很重要 —— 每类设备都要有自己的镜像，
    否则中继上传会把终端设备的镜像覆盖掉（反过来也一样），
    而那时还没人刷过。
    """
    import re as _re
    m = _re.search(r"boundary=([^;]+)", ctype)
    if not m:
        return None
    boundary = b"--" + m.group(1).strip().strip('"').encode()
    for part in body.split(boundary):
        if b"filename=" not in part:
            continue
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep or not data:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]                       # 去掉这一部分结尾的 CRLF
        if not data:
            continue
        if not dst:
            dst = "/data/fw_upload.bin" if os.path.isdir("/data") else "/tmp/fw_upload.bin"
        with open(dst, "wb") as f:
            f.write(data)
        print(f"[OTA] 上传：{len(data)} 字节 → {dst}")
        return dst
    return None

def _mp_field(ctype: str, body: bytes, name: str):
    """读取 multipart/form-data 请求体里的一个普通文本字段。

    需要这样做，是因为 do_POST 遇到 multipart 会把 `params` 置成 `{}`
    （parse_qs 会弄坏二进制）—— 可这样一来同一个表单里的 <select> 字段
    也就看不见了。而 /api/ota 要的正是它（image type）。
    """
    import re as _re
    m = _re.search(r"boundary=([^;]+)", ctype)
    if not m:
        return None
    boundary = b"--" + m.group(1).strip().strip('"').encode()
    needle = ('name="%s"' % name).encode()
    for part in body.split(boundary):
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep or needle not in head or b"filename=" in head:
            continue
        return data.rstrip(b"\r\n").decode("utf-8", "replace")
    return None


def _ota_image_type(raw_val, default=0x0001) -> int:
    """把 '0x0001' / '1' / None 稳妥地转成 int。"""
    if raw_val is None:
        return default
    s = str(raw_val).strip()
    if not s:
        return default
    try:
        return int(s, 0)
    except ValueError:
        try:
            return int(s, 16)
        except ValueError:
            return default


def _fw_ota_watchdog():
    """C6 不再回数据帧时发出告警。

    没有它，传输就那么静静地停住，只看到 "7168/789804" ——
    完全不知道为什么走不下去。
    """
    last, still = -1, 0
    while True:
        time.sleep(5)
        if _fw_ota_file is None:
            return
        if _fw_ota_off == last:
            still += 5
            if still == 20:
                print(f"[OTA] 警告：20 s 没收到 C6 的回执 "
                      f"（停在 {_fw_ota_off}/{_fw_ota_size}）。")
                if FW_CHUNK <= 128:
                    print("[OTA] 帧长只有 "
                          f"{FW_CHUNK} B（约 {2*FW_CHUNK+50} 个字符），已经小到 "
                          "256 字节的缓冲都够用 ⇒ 正在跑的固件 "
                          "解析不了 fw_ota_data。那就只能 "
                          "用 esptool 走 USB 了。")
                else:
                    print("[OTA] 多半是运行中的固件里 BUF_SIZE=256："
                          f"一帧是 {FW_CHUNK} B ≈ {2*FW_CHUNK+50} 个字符。"
                          "把加载项选项 'fw_chunk' 设成 80 再重启。")
        else:
            last, still = _fw_ota_off, 0


def _fw_ota_start(bin_path: str):
    """让 C6 自己写另一个 ota 分区（App 级在线升级，esp_ota）。

    走的是当前通道（--link serial 或 tcp），不经 esptool、不需要 USB。
    与 flash_apply() 的区别：那个是 bootloader 级直刷，必须物理接触。
    """
    global _fw_ota_file, _fw_ota_size, _fw_ota_off
    import os as _os
    if not bin_path:
        bin_path = "/data/zigbee-vital-sensor.bin"
    if not _os.path.isfile(bin_path):
        print(f"[OTA] 找不到协调器固件文件：{bin_path}")
        with _state_lock:
            _state["fw_ota"]["status"] = "error"
        return
    _fw_ota_close()
    _fw_ota_size = _os.path.getsize(bin_path)
    _fw_ota_off  = 0
    _fw_ota_file = open(bin_path, "rb")
    with _state_lock:
        _state["fw_ota"].update({
            "status": "pending", "bin_path": bin_path,
            "size": _fw_ota_size, "off": 0,
        })
    ser.write(_j({"cmd": "fw_ota_start", "size": _fw_ota_size}))
    frames  = -(-_fw_ota_size // FW_CHUNK)          # 向上取整
    batches = -(-frames // FW_BATCH)
    # ★ 估算公式按**串口 115200**标定（每批约 0.55 s，实测约 920 B/s）。
    #   TCP 通道下瓶颈变成 C6 写 flash + 每帧 hex 解码，实际会明显快于这个
    #   数字 —— 所以这里算出来的是**上界**，偏大不是 bug。
    est_s   = batches * 0.55 + frames * 0.004
    print(f"[OTA] 协调器在线升级：{bin_path} size={_fw_ota_size}（通道 {args.link}）")
    print(f"[OTA] 计划：{frames} 帧 × {FW_CHUNK} B，每批 {FW_BATCH} 帧 ⇒ "
          f"{batches} 批，预计 {est_s/60:.1f} 分钟")
    threading.Thread(target=_fw_ota_watchdog, daemon=True).start()

def _fw_ota_send_batch():
    """从当前偏移开始发 FW_BATCH 个块；最后一批发出后就触发结束。"""
    global _fw_ota_off
    if _fw_ota_file is None:
        return
    sent = 0
    while sent < FW_BATCH and _fw_ota_off < _fw_ota_size:
        _fw_ota_file.seek(_fw_ota_off)
        data = _fw_ota_file.read(FW_CHUNK)
        if not data:
            break
        ser.write(_j({"cmd": "fw_ota_data", "off": _fw_ota_off,
                      "data": data.hex()}))
        _fw_ota_off += len(data)
        sent += 1
    with _state_lock:
        _state["fw_ota"]["off"] = _fw_ota_off
        _state["fw_ota"]["status"] = "transferring"
    pct = (_fw_ota_off * 100 // _fw_ota_size) if _fw_ota_size else 0
    print(f"[OTA] 协调器 {_fw_ota_off}/{_fw_ota_size} 字节，已发送 ({pct} %)")
    if _fw_ota_off >= _fw_ota_size:
        ser.write(b'{"cmd":"fw_ota_end"}\n')
        print("[OTA] 协调器：全部字节已发完 —— 结束 …")

def _now():
    """本地时区的日期/时间（与日志时间戳一致）。"""
    if _LOG_TZ is not None:
        return datetime.now(_LOG_TZ).strftime("%d.%m. %H:%M:%S")
    return datetime.now().strftime("%d.%m. %H:%M:%S")

# ── MariaDB ────────────────────────────────────────────────────────────────
_db_conn = None
_db_lock = threading.Lock()

def _db_connect():
    if not DB_HOST:
        raise RuntimeError("DB_HOST not set - database disabled")
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS,
        database=DB_NAME,
        autocommit=True,
        connect_timeout=5,
    )

def _db_cursor():
    global _db_conn
    try:
        _db_conn.ping(reconnect=True)
    except Exception:
        _db_conn = _db_connect()
    return _db_conn.cursor()

def db_init():
    global _db_conn
    if not DB_HOST:
        print("[DB] disabled (DB_HOST empty)")
        return
    try:
        _db_conn = _db_connect()
        cur = _db_conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS esp32_gateways (
                mac        CHAR(8)      NOT NULL,
                status     VARCHAR(16)  NOT NULL DEFAULT 'offline',
                last_seen  DATETIME,
                PRIMARY KEY (mac)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS esp32_zigbee_devices (
                mac        CHAR(8)      NOT NULL,
                addr       VARCHAR(8)   NOT NULL,
                ieee       VARCHAR(24)  NOT NULL DEFAULT '',
                name       VARCHAR(64)  NOT NULL DEFAULT '',
                last_seen  DATETIME,
                PRIMARY KEY (mac, addr)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS esp32_chan_scan (
                id      INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                mac     CHAR(8)      NOT NULL,
                ts      DATETIME(3)  NOT NULL,
                ch      TINYINT UNSIGNED NOT NULL,
                count   TINYINT UNSIGNED NOT NULL DEFAULT 0,
                nets    TEXT,
                INDEX (mac, ts)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS esp32_zigbee_data (
                id         INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                mac        CHAR(8)      NOT NULL,
                addr       VARCHAR(8)   NOT NULL,
                cluster    VARCHAR(32)  NOT NULL,
                ts         DATETIME(3)  NOT NULL,
                value      DOUBLE,
                raw_json   TEXT,
                INDEX (mac, addr, cluster, ts)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        # Bekannte Devices aus DB in State laden
        cur.execute("""
            SELECT addr, ieee, name, last_seen FROM esp32_zigbee_devices WHERE mac=%s
        """, (DB_MAC,))
        with _state_lock:
            for row in cur.fetchall():
                addr, ieee, name, last_seen = row
                ts = last_seen.strftime("%d.%m. %H:%M:%S") if last_seen else "–"
                _state["devices"].setdefault(addr, {
                    "ieee": ieee or "", "name": name or addr,
                    "last_seen": ts, "clusters": {}
                })
        cur.close()
        print(f"[DB] 已连接 {DB_HOST}/{DB_NAME} mac={DB_MAC} devices={len(_state['devices'])}")
    except Exception as e:
        print(f"[DB] 初始化出错：{e}")
        _db_conn = None

def db_insert_chan_scan(ch, count, nets_json):
    if not DB_HOST:
        return
    with _db_lock:
        try:
            cur = _db_cursor()
            cur.execute("""
                INSERT INTO esp32_chan_scan (mac, ts, ch, count, nets)
                VALUES (%s, UTC_TIMESTAMP(3), %s, %s, %s)
            """, (DB_MAC, ch, count, nets_json))
            cur.close()
        except Exception as e:
            print(f"[DB] chan_scan insert: {e}")

def db_upsert_gateway(status):
    if not DB_HOST:
        return
    with _db_lock:
        try:
            cur = _db_cursor()
            cur.execute("""
                INSERT INTO esp32_gateways (mac, status, last_seen)
                VALUES (%s, %s, UTC_TIMESTAMP())
                ON DUPLICATE KEY UPDATE status=VALUES(status), last_seen=UTC_TIMESTAMP()
            """, (DB_MAC, status[:16]))
            cur.close()
        except Exception as e:
            print(f"[DB] gateway upsert: {e}")

def db_upsert_zigbee_device(addr, ieee=""):
    if not DB_HOST:
        return
    with _db_lock:
        try:
            cur = _db_cursor()
            cur.execute("""
                INSERT INTO esp32_zigbee_devices (mac, addr, ieee, last_seen)
                VALUES (%s, %s, %s, UTC_TIMESTAMP())
                ON DUPLICATE KEY UPDATE ieee=VALUES(ieee), last_seen=UTC_TIMESTAMP()
            """, (DB_MAC, addr, ieee))
            cur.close()
        except Exception as e:
            print(f"[DB] zigbee device upsert: {e}")

def _extract_value(cluster, raw):
    # 多联设备：子 topic 可能是 "ep2/on_off" 之类 → 比对基础名
    base = cluster.rsplit("/", 1)[-1] if "/" in cluster else cluster
    if base == "temperature":
        r = raw.get("raw")
        return None if (r is None or r == -32768) else r / 100.0
    if base == "humidity":     return raw.get("raw", 0) / 100.0
    if base == "illuminance":  return raw.get("raw")
    if base == "on_off":       return raw.get("v")
    if base == "occupancy":    return raw.get("occ")
    return None

def db_insert_zigbee_data(addr, cluster, raw):
    if not DB_HOST:
        return
    value = _extract_value(cluster, raw)
    with _db_lock:
        try:
            cur = _db_cursor()
            cur.execute("""
                INSERT INTO esp32_zigbee_data (mac, addr, cluster, ts, value, raw_json)
                VALUES (%s, %s, %s, UTC_TIMESTAMP(3), %s, %s)
            """, (DB_MAC, addr, cluster, value, json.dumps(raw)))
            cur.execute("""
                UPDATE esp32_zigbee_devices SET last_seen=UTC_TIMESTAMP()
                WHERE mac=%s AND addr=%s
            """, (DB_MAC, addr))
            cur.close()
        except Exception as e:
            print(f"[DB] zigbee data insert: {e}")

# ── MQTT ───────────────────────────────────────────────────────────────────
mq = mqtt.Client(client_id="serial-gateway")
if args.user:
    mq.username_pw_set(args.user, args.passwd)
mq.will_set(f"{BASE}/status", "offline", retain=True, qos=1)

# ── MQTT 出口：统一的发布路径 ────────────────────────────────────────────
# 复刻 Zigbee2MQTT（lib/mqtt.ts）里四个当时缺失的机制：
#
#  1. Topic 校验。带 '+' 或 '#' 的 publish topic 不是 topic，而是过滤器 ——
#     broker 会回一个协议错误并断开连接。调试时正踩了这个坑（mqtt_probe.py
#     用 'c6_#'：立刻掉线）。所以设备名里带 '#' 会直接把网关连接打死
#     → 必须提前拦下。
#  2. retained 消息登记表。broker 若不落盘，它一重启所有 discovery 配置就没了，
#     HA 也就不再认识这些实体。Z2M 因此每次连上之后 2 s 再发一遍 retained
#     消息（lib/mqtt.ts, onConnect）。
#  3. 回声保护。我们订阅 {BASE}/cmd/+、{BASE}/set/#，以及启动窗口期内的
#     {HA_PREFIX}/+/+/+/config。而 {BASE}/cmd/* 是我们自己发的（网页表单），
#     所以这一支明确豁免回声抑制（见 on_message）。
#  4. 单一出口。所有 publish 都走 mq_put()；这样校验和记账只在一处维护。
_retained = {}       # topic → payload（我们以 retain=true 发过的内容）
_self_pub = {}       # topic → 忽略回声的截止时间

# HA 启动完成后会在这里发 'online'。Z2M 就挂在这个事件上重发全部状态
# （lib/extension/homeassistant.ts）。
_HA_STATUS_TOPIC = "homeassistant/status"


def mq_put(topic, payload, retain=False, qos=1):
    """所有 publish 的唯一出口。"""
    if "+" in topic or "#" in topic:
        print(f"[MQTT] topic '{topic}' 含通配符 —— 未发送")
        return
    if retain:
        if payload:
            _retained[topic] = payload
        else:
            _retained.pop(topic, None)
    if len(_self_pub) > 400:
        _self_pub.clear()          # 纯粹是回声保护，不丢数据
    _self_pub[topic] = time.time() + 1.0
    try:
        mq.publish(topic, payload, retain=retain, qos=qos)
    except Exception as e:
        print(f"[MQTT] publish '{topic}' 失败：{e}")


def _republish_retained():
    """连接建立后，把所有 retained 消息重发一遍。

    第一次连上时登记表是空的 —— 那就什么也不做。
    真正需要它，是 broker 重启且没有落盘之后。
    """
    n = 0
    for topic, payload in list(_retained.items()):
        mq_put(topic, payload, retain=True, qos=1)
        n += 1
    if n:
        print(f"[MQTT] 连接建立后重发了 {n} 条 retained 消息")


def _ha_republish_all(why=""):
    """重发所有已知状态（HA 重启之后用）。

    没有它，HA 一重启实体就停在 'unknown'（前提是 broker 没落盘）：
    HA 手里没有状态了，网关这边又不知情 —— 设备自己什么都没变。
    Z2M 的做法恰恰相反：先发 bridge 状态，再把缓存的每个设备状态都发一遍。
    """
    mq_put(f"{BASE}/status", "online", retain=True, qos=1)
    n = 0
    for addr, subs in list(_ha_last.items()):
        for sub, p in list(subs.items()):
            mq_put(f"{BASE}/zigbee/{addr}/{sub}", json.dumps(p), retain=True, qos=1)
            n += 1
    print(f"[MQTT] HA {why or '重新启动'}：重发了 {n} 条状态")


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"[MQTT] 已连接 → {BASE}")
        mq_put(f"{BASE}/status", "online", retain=True, qos=1)
        client.subscribe(f"{BASE}/cmd/+", qos=1)
        client.subscribe(f"{BASE}/set/#", qos=1)   # HA-Entity-Befehle
        client.subscribe(_HA_STATUS_TOPIC, qos=0)  # HA-Neustart erkennen
        db_upsert_gateway("online")
        with _state_lock:
            _state["status"] = "online"
        # Z2M 在连接建立 2 s 后重发 retained 消息；
        # Discovery 配置则另外从启动窗口读回来。
        threading.Timer(2.0, _republish_retained).start()
        _disc_begin_window()
    else:
        print(f"[MQTT] 出错 rc={rc}")

# ── 命令确认与重发 ───────────────────────────────────────────────────────
# 设备收到远程命令只回一个 Default Response，**不发** Report 属性。
# 命令到底有没有到，只能靠一次主动的 Read Attributes（read_onoff）来确认。
#
# LQI 15–30 时 —— 设备埋在墙里、协调器在配电箱里 —— 命令和读请求偶尔会丢。
# 这时 HA 显示的是假定值，而设备压根没动。2026-10-17 就量到过：
# ep17 在收到 "off" 之后 0.78 s 又报 v=1。
#
# 所以：发完命令就读，连追两次；一旦值不对或者没回应，
# 就把命令重发，最多 _MAX_TRIES 次。
_MAX_TRIES   = 3
_READ_DELAY  = 0.7     # 秒，到第一次 Read-Attributes 的间隔
_READ_DELAY2 = 1.5     # 第二次读的延迟秒数（防止第一次丢了）
_RETRY_AFTER = 2.6     # 完全没有回应的秒数 ⇒ 重发命令

_pending      = {}     # (addr, ep) → {"want": 0|1, "tries": n, "ts": t}
_pending_lock = threading.Lock()

# ── 状态不可回读的设备 ─────────────────────────────────────────────────────
# 不是每台设备都能回答自己的开/关状态。情景面板 FTB56+ZSN18ZY1.1（0x747d）
# 和 HGZB-043 同为 Feibit 结构，但没有继电器：它接受 On/Off 命令、
# 回 Default-Response SUCCESS，而 Read-Attributes 0x0006/0x0000 却答
# 0x86（UNSUPPORTED_ATTRIBUTE）—— 它压根没有这个属性，因为没什么可开关的。
#   2026-10-17 实测：  0x07da → "186c010000002001"  (status 00, type 20, v=1)
#                      0x747d → "185001000086"      (status 86)
# 这类端点原则上不可能给出确认。没有这张表，每次点击都会发三条命令、
# 六次读取，最后还是报"没能生效"—— 永远如此。
_noread = set()        # "addr/ep" —— 状态不可回读
_ctrl   = set()        # "addr/ep" —— 纯发送方（按键面板，没有继电器）


def _ep_key(addr, ep):
    return f"{addr}/{int(ep)}"


def _noread_add(addr, ep, why):
    k = _ep_key(addr, ep)
    if k in _noread:
        return
    _noread.add(k)
    _nodes_save()
    print(f"[HA] {addr} ep{ep}：状态无法回读（{why}）—— "
          f"从现在起**不再**重发命令")
    # 端点如果已经判定为发送端，那把 switch 就彻底推翻了 →
    # 现在就去注销（见 _classify）。
    _classify(addr, ep, f"没有 OnOff 属性（{why}）")


def _noread_del(addr, ep):
    k = _ep_key(addr, ep)
    if k in _noread:
        _noread.discard(k)
        _nodes_save()
        print(f"[HA] {addr} ep{ep}：居然能报状态了 —— "
              f"回读重新启用")


def _send_read_onoff(addr, ep):
    """向设备查询真实的 On/Off 状态（Read Attributes）。"""
    if time.monotonic() - _gw_boot_ts < _GW_BOOT_COOLDOWN:
        # 网关刚启动，Zigbee 协议栈可能还没起来。
        return
    try:
        msg = _js({"cmd": "read_onoff", "addr": addr, "ep": ep})
        ser.write(msg.encode())
        print(f"[→C6] {msg.strip()}")
    except Exception as e:
        print(f"[HA] 回读 {addr} ep{ep} 失败：{e}")


def _sub_ep(sub):
    """'ep17/on_off' → 17，'on_off' → 1（C6 在 EP1 上会省掉前缀）。"""
    if sub.startswith("ep") and "/" in sub:
        try:
            return int(sub.split("/", 1)[0][2:])
        except ValueError:
            return None
    return 1


def _pending_ack(addr, sub, p):
    """收到设备的真实值了 —— 检查待确认项。"""
    if not sub.endswith("on_off"):
        return
    v = (p or {}).get("v")
    if v is None:
        return
    ep = _sub_ep(sub)
    if ep is None:
        return
    # 收到真实状态，就证明这个端点可读。
    _probe_seen(addr, ep)
    key = (addr, ep)
    with _pending_lock:
        pend = _pending.get(key)
        if not pend:
            return
        nr = bool(pend.get("noread"))
        if int(v) == int(pend["want"]):
            _pending.pop(key, None)
            print(f"[HA] {addr} ep{ep}：设备已确认 v={int(v)}")
            _noread_del(addr, ep)
            return
        if nr or pend["tries"] >= _MAX_TRIES:
            _pending.pop(key, None)
            if nr:
                print(f"[HA] {addr} ep{ep}：设备报告 v={int(v)} "
                      f"（无法回读）—— 已修正显示")
            else:
                print(f"[HA] {addr} ep{ep}：试了 {_MAX_TRIES} 次仍未 "
                      f"生效（设备报告 v={int(v)}）")
            # 把显示改成事实 —— 不要继续挂着假定值，
            # 否则 HA 会一直显示错误状态。
            _ha_state(addr, f"ep{ep}/on_off" if ep > 1 else "on_off",
                      {"v": int(v)})
            return
        pend["tries"] += 1
        want = pend["want"]
        n = pend["tries"]
        pend["ts"] = time.time()
    print(f"[HA] {addr} ep{ep}：设备报告 v={int(v)}，期望 {want} "
          f"⇒ 重发命令（{n}/{_MAX_TRIES}）")
    _issue_cmd(addr, ep, want, "on" if want else "off", n)


def _issue_cmd(addr, ep, want, act, attempt=1):
    """发出 dev_cmd 并登记确认（带回读定时器）。"""
    noread = _ep_key(addr, ep) in _noread
    uart_msg = _js({"cmd": "dev_cmd", "addr": addr, "ep": ep, "sw": act})
    ser.write(uart_msg.encode())
    print(f"[→C6] {uart_msg.strip()}"
          + (f"   (Versuch {attempt}/{_MAX_TRIES})" if attempt > 1 else ""))
    with _pending_lock:
        # 对 "noread" 直接把 tries 设成 _MAX_TRIES：这样
        # _retry_if_silent 和异常上报都不会再触发重发 ——
        # 一次点击就一条命令，不多发。
        _pending[(addr, ep)] = {"want": want,
                                "tries": _MAX_TRIES if noread else attempt,
                                "noread": noread,
                                "ts": time.time()}
    try:
        # 即便是 "noread" 也保留**一次**回读尝试：只花一帧，
        # 却是以后能自动纠正判定的唯一机会（设备一旦真的报了值，
        # _pending_ack 就会把这个标记清掉）。
        threading.Timer(_READ_DELAY, _send_read_onoff, (addr, ep)).start()
        if not noread:
            if attempt == 1:
                threading.Timer(_READ_DELAY2, _send_read_onoff, (addr, ep)).start()
            # 超时看门狗要挂在**每一次**尝试上，不能只挂第一次：
            # 只在第一次挂时，一条没有回应的链路会在第 2 次尝试后结束，
            # 而 _pending 里的条目就永远躺在那儿了（2026-10-17 测试发现：
            # 只发了 2 条而不是 3 条，pending 也没清掉）。
            # _retry_if_silent 自己会判断是不是还太早。
            threading.Timer(_RETRY_AFTER, _retry_if_silent, (addr, ep)).start()
    except Exception as e:
        print(f"[HA] 确认定时器没启动起来：{e}")


def _retry_if_silent(addr, ep):
    """完全没有回应？那多半是命令本身就丢了。"""
    key = (addr, ep)
    with _pending_lock:
        pend = _pending.get(key)
        if not pend:
            return                        # 已经确认过了
        if pend.get("noread"):
            return                        # 这里重发没有意义
        if time.time() - pend["ts"] < _RETRY_AFTER * 0.5:
            return                        # gerade erst wiederholt
        if pend["tries"] >= _MAX_TRIES:
            _pending.pop(key, None)
            print(f"[HA] {addr} ep{ep}：设备无应答，"
                  f"试了 {_MAX_TRIES} 次都没成功")
            # 三轮都没成功就别再试了：要么设备不支持 Read-Attributes
            # （见 _noread），要么射频链路差到重发也没用。两种都算"无法确认" ——
            # 这时候一次点击应该只发**一条**命令，而不是三条。
            _noread_add(addr, ep, "Read Attributes 没有应答")
            return
        pend["tries"] += 1
        want = pend["want"]
        n = pend["tries"]
        pend["ts"] = time.time()
    print(f"[HA] {addr} ep{ep}：没有确认 ⇒ 重发命令 "
          f"({n}/{_MAX_TRIES})")
    _issue_cmd(addr, ep, want, "on" if want else "off", n)


def _ha_handle_set(topic, payload):
    """HA 的开关命令 → 转成 dev_cmd 发给协调器。

    Topic:   {BASE}/set/{addr}/{epN}/on_off
    Payload: "1" | "0" | "toggle"
    """
    parts = topic[len(BASE) + 1:].split("/")   # ["set", addr, epN, on_off]
    if len(parts) < 4 or parts[0] != "set":
        return
    addr = parts[1]
    ep   = int("".join(c for c in parts[2] if c.isdigit()) or 1)
    # HA 根据配置会发 1/0、ON/OFF 或 true/false —— 全都要能映射。
    # 认不出来的一律按 toggle 处理。
    pl   = payload.strip().upper()
    if   pl in ("1", "ON", "TRUE"):  act = "on"
    elif pl in ("0", "OFF", "FALSE"): act = "off"
    else:                              act = "toggle"

    # 立刻把状态回显出去。设备（例如 HGZB-043）收到 On/Off 只回
    # Default-Response，**不发** Report 属性 —— 没有这次回显，
    # HA 的卡片会一直停在 "unknown"，而且每次点击后开关都会弹回去（"无法控制"）。
    # 之后要是真来了 Report，会把这个值覆盖掉。
    #
    # 重要：命名规则跟 C6 里的 ha_mqtt_publish_zigbee_ep() 保持一致 ——
    # 端点 1 **不带** "ep1/" 前缀。否则回显会发到跟实体 state_topic
    # 不同的 topic 上，单端点设备的开关就会在命令其实送达的情况下弹回去。
    sub = f"ep{ep}/on_off" if ep > 1 else "on_off"
    old = (_ha_last.get(addr, {}).get(sub) or {}).get("v")
    if act == "on":
        new = 1
    elif act == "off":
        new = 0
    else:
        new = 0 if old == 1 else 1
    _ha_state(addr, sub, {"v": new})

    # 发命令 + 登记确认（见上面的段落）。
    _issue_cmd(addr, ep, new, act)

def on_message(client, userdata, msg):
    """把主机发来的命令转发给 C6（走串口）。"""
    # 回声保护：我们刚刚自己发出去的东西不算命令。
    #
    # 例外 {BASE}/cmd/*：网页表单把命令发到这里
    # （permit_join、tx_power 等），好让网页和 MQTT 走同一条路径。
    # 没有这个例外，保护机制正好会吃掉这些命令 —— 网页上的按钮按下去
    # 什么都不发生，而且哪里都看不到报错。
    if not msg.topic.startswith(f"{BASE}/cmd/") and _self_pub.get(msg.topic, 0) > time.time():
        return
    payload = msg.payload.decode(errors="replace")

    # HA 启动完成 → 重发状态。
    if msg.topic == _HA_STATUS_TOPIC:
        if payload.strip().lower() == "online":
            print("[HA] 报告重启（homeassistant/status）⇒ 重发状态")
            threading.Timer(1.0, _ha_republish_all, ("重新启动",)).start()
        return

    # 启动窗口：现有的 Discovery 配置只读进来、不往外发。
    if not _disc_ready and msg.topic.startswith(f"{HA_PREFIX}/") and msg.topic.endswith("/config"):
        _disc_remote[msg.topic] = payload
        return

    # HA-Discovery-Schaltbefehle: {BASE}/set/0x07da/ep16/on_off
    if msg.topic.startswith(f"{BASE}/set/"):
        _ha_handle_set(msg.topic, payload)
        return
    cmd = msg.topic.split("/")[-1]
    if cmd == "permit_join":
        uart_msg = _js({"cmd": "permit_join", "sec": int(payload)})
        ser.write(uart_msg.encode())
        print(f"[→C6] {uart_msg.strip()}")
    elif cmd == "set_channel":
        ch = int(payload)
        if 11 <= ch <= 26:
            uart_msg = _js({"cmd": "set_channel", "ch": ch})
            ser.write(uart_msg.encode())
            print(f"[→C6] {uart_msg.strip()}")
        else:
            print(f"[WARN] 无效的 Zigbee 信道：{ch}（11-26）")
    elif cmd == "scan_chan":
        uart_msg = _js({"cmd": "scan_chan"})
        ser.write(uart_msg.encode())
        print(f"[→C6] {uart_msg.strip()}")
    elif cmd == "switch2wifi":
        uart_msg = _js({"cmd": "switch2wifi"})
        ser.write(uart_msg.encode())
        print(f"[→C6] {uart_msg.strip()}")
    elif cmd == "probe":
        # 型号查询，给已经入网却什么都不吭声的设备用
        # （没有 Announce、没有上报）→ 直接读 Basic 簇。
        # Payload: "0x07da,16"  (EP optional, Default 1)
        try:
            parts = payload.split(",")
            addr = parts[0].strip()
            ep = int(parts[1]) if len(parts) > 1 else 1
            uart_msg = _js({"cmd": "probe", "addr": addr, "ep": ep})
            ser.write(uart_msg.encode())
            print(f"[→C6] {uart_msg.strip()}")
        except (ValueError, IndexError):
            print(f"[WARN] probe：无效载荷 {payload}")
    elif cmd == "set_sleep":
        try:
            secs = max(10, int(payload))
            uart_msg = _js({"cmd": "set_sleep", "s": secs})
            ser.write(uart_msg.encode())
            print(f"[→C6] {uart_msg.strip()}")
        except ValueError:
            print(f"[WARN] set_sleep：无效值 {payload}")
    elif cmd == "set_dev_power":
        # 中继发射功率：payload 是 JSON {"addr":"0x1234","ep":1,"dbm":14}
        # 网关会翻成 ZCL Write Attribute 0xFC00/0x0001 发给那台中继；
        # 中继收到即改 PA 并落自己的 NVS，所以以后不用重新刷固件。
        try:
            o = json.loads(payload)
            addr = str(o.get("addr", "")).strip()
            ep   = int(o.get("ep", 1) or 1)
            dbm  = max(-24, min(20, int(o.get("dbm", 8))))
            if not addr:
                raise ValueError("缺少 addr")
        except (ValueError, TypeError, json.JSONDecodeError):
            print(f"[WARN] set_dev_power：无效载荷 {payload!r}")
            return
        uart_msg = _js({"cmd": "set_dev_power", "addr": addr, "ep": ep, "dbm": dbm})
        ser.write(uart_msg.encode())
        print(f"[→C6] {uart_msg.strip()}")
    elif cmd == "set_tx_power":
        # Sendepfad des C6 in dBm. Payload: "-4" 或 "{\"dbm\":-4}"
        try:
            raw = payload.strip()
            if raw.startswith("{"):
                raw = str(json.loads(raw).get("dbm", ""))
            dbm = int(raw)
        except (ValueError, TypeError, json.JSONDecodeError):
            print(f"[WARN] set_tx_power：无效值 {payload!r}")
            return
        dbm = max(-24, min(20, dbm))
        uart_msg = _js({"cmd": "set_tx_power", "dbm": dbm})
        ser.write(uart_msg.encode())
        _txp_save(dbm)          # 能扛过 C6 重启和加载项重启
        print(f"[→C6] {uart_msg.strip()}   (回读值会以 {{\"t\":\"txpower\"}} 报回来)")
    elif cmd == "lqi":
        # 即时取一次：否则 LQI/RSSI 只能等 60 s 心跳。
        uart_msg = _js({"cmd": "lqi"})
        ser.write(uart_msg.encode())
        print(f"[→C6] {uart_msg.strip()}")
    elif cmd == "read_onoff":
        # Payload "0x747d,2" → Read Attributes auf On/Off.
        # 唯一能证明下行帧真的到达设备的硬证据，就是设备自己回话。
        # 发射功率对比（txpower_ab.py）要的正是这个：
        # LQI/RSSI 描述的是相反方向，改网关功率时几乎不变。
        try:
            parts = payload.split(",")
            addr = parts[0].strip()
            ep   = int(parts[1]) if len(parts) > 1 else 1
        except (ValueError, IndexError):
            print(f"[WARN] read_onoff：无效载荷 {payload!r}")
            return
        uart_msg = _js({"cmd": "read_onoff", "addr": addr, "ep": ep})
        ser.write(uart_msg.encode())
        print(f"[→C6] {uart_msg.strip()}")
    elif cmd == "wifi_scan":
        # 让 C6 用它自己的射频扫一遍 2.4 GHz，回执是一条 {"t":"wifi_scan",…}。
        # ★ 这是判断"连不上 Wi-Fi 到底怪谁"的关键：主机的扫描结果不能当依据
        #   —— 电脑的天线、频段能力、Windows 的扫描缓存都和板子不同。
        #   2026-11-06 就是靠它确认"板子本身一个 AP 都收不到"的。
        uart_msg = _js({"cmd": "wifi_scan"})
        ser.write(uart_msg.encode())
        print(f"[→C6] {uart_msg.strip()}")
    elif cmd == "net_status":
        # 查当前传输通道 / Wi-Fi 状态。回执是一条 {"t":"net_status",…}。
        # ★ 这条也是「固件里到底跑的是不是新版本」最省事的判据：
        #   老固件不认识它，会回 {"t":"error","why":"unknown_cmd"}。
        uart_msg = _js({"cmd": "net_status"})
        ser.write(uart_msg.encode())
        print(f"[→C6] {uart_msg.strip()}")
    elif cmd == "net_cfg":
        # 配置 Wi-Fi。payload 直接给一个 JSON 对象：
        #   {"ssid":"…","pass":"…","host":"<ha-ip>","port":12345,"tr":1}
        # 只写想改的字段即可（空/缺省 = 这一项不动）。
        # ★ 这里整包转发，不在 add-on 侧拆字段 —— 拆了就又变成「两处解析」，
        #   而固件那边（ha_mqtt.c）已经在拆。让它一处说了算。
        try:
            body = json.loads(payload)
            if not isinstance(body, dict) or not body:
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            print(f"[WARN] net_cfg：载荷必须是 JSON 对象，收到 {payload!r}")
            return
        body["cmd"] = "net_cfg"
        uart_msg = _js(body)
        ser.write(uart_msg.encode())
        print(f"[→C6] {uart_msg.strip()}")
        print("[NET] 配置已下发；固件会回一条 net_status，"
              "之后按 tr 的值决定是否切到 Wi-Fi")

mq.on_connect = on_connect
mq.on_message = on_message

# ── Serial ─────────────────────────────────────────────────────────────────
_write_err = 0        # 累计写失败次数（只用于限速日志，见 _note_write_fail）


def _note_write_fail(why):
    """记一次写失败，并按 1/10/200 次限速打印。

    ★ 为什么要专门写一句「上面那行 [→C6] 并没有真的发出去」：
      2026-11-06 排查 Wi-Fi 通道时，日志里明明有
          [→C6] {"ssid":"yecao",...,"cmd":"net_cfg"}
      看起来命令已下发，实际 ser 是 _NullSerial（端口早死了），
      写进去等于扔进黑洞 —— 白白多排查了半小时。下发回显和写入结果
      必须绑在一起，否则这条日志就是在骗人。
    """
    global _write_err
    _write_err += 1
    if _write_err in (1, 10) or _write_err % 200 == 0:
        print(f"[Serial] 写失败 {_write_err} 次：{why}"
              + ("  → 上面那行 [→C6] 实际上没有发出去，等通道重开"
                 if _write_err == 1 else ""))


class CoordinatorSerial:
    """持久串口连接，不触发 ESP32-C6 复位。

    与 CoordinatorTcp 的公共接口：read / write / close / in_waiting / can_reset。
    主循环只认这几个名字，因此换通道不需要改主循环。
    """
    can_reset = True      # 有 RTS ⇒ 能硬复位芯片（见 _serial_kick_reset）

    def __init__(self, port, baudrate):
        # 在 pyserial 打开之前关掉 HUPCL ⇒ WCH 芯片不会因 DTR 触发复位
        import subprocess
        subprocess.run(["stty", "-F", port, "-hupcl"], check=False)
        self.ser = serial.Serial()
        self.ser.port     = port
        self.ser.baudrate = baudrate
        self.ser.timeout  = 1
        self.ser.dtr      = False
        self.ser.rts      = False
        self.ser.open()
        time.sleep(0.5)
        try:
            self.ser.dtr = False
        except Exception:
            pass
        print(f"[Serial] {port} @ {baudrate} 已打开（DTR/RTS=LOW，HUPCL=off）")

    def read(self, n):   return self.ser.read(n)

    def write(self, d):
        """写串口。★ 异常必须在这里吞掉。

        串口 EIO 时如果把 SerialException 抛出去，会顺着 paho 的 on_message
        一路炸穿 MQTT 回调线程 —— 2026-10-26 实测日志：
            _issue_cmd → ser.write → SerialException: write failed: [Errno 5]
        之后 HA 发来的开关命令全部失灵，日志只刷 traceback，而真正的病根
        （端口已死）没人处理。这里只记数，端口重开统一交给读循环的看门狗。
        """
        try:
            return self.ser.write(d)
        except Exception as e:                          # noqa: BLE001
            _note_write_fail(e)
            return 0

    def close(self):     self.ser.close()

    @property
    def in_waiting(self): return self.ser.in_waiting

# ── 用 esptool 应急刷机（走 bootloader）──────────────────────────────────
# App 级 OTA（fw_ota_*）的前提是：**正在运行**的固件认识 fw_ota_data。
# 老构建不认识 —— 它们用 fgets(buf, 256) 读命令，和/或写死
# strstr("\"data\":\"") 匹配 —— 于是永远不会有 ACK，传完第一批就卡住。
# 这种情况只剩 bootloader：esptool 经 USB-Serial-JTAG 把 C6 复位进下载模式，
# 直接写镜像。
APP_OFFSET = "0x20000"   # ota_0 – boot-Log: "Loaded app from partition at offset 0x20000"
FLASH_BAUD = 460800

_serial_pause      = threading.Event()   # 暂停读取循环
_flash_mutex       = threading.Lock()    # 同一时间只允许一个刷写
_flash_state_lock  = threading.Lock()
_flash_state       = {"status": "idle", "bin": "", "tail": []}


class _NullSerial:
    """占位用的"死"串口：开机打不开端口时顶上，让 Web UI / MQTT 照常起来。

    读一律抛 OSError，所以主循环的看门狗会自动走「重开」分支，等设备回来了
    自己接上 —— 总比开机就 traceback 退出、被 Supervisor 反复重启好。
    """
    can_reset = True

    def read(self, n):   raise OSError("串口未打开（占位通道）")
    def close(self):     pass

    def write(self, d):
        # ★ 必须显式记一次失败：这里返回 0 是"静默丢弃"，调用方看到 0
        #   之前就已经把 [→C6] 回显打出去了（见 _note_write_fail 的说明）。
        _note_write_fail("串口未打开（占位通道）")
        return 0

    @property
    def in_waiting(self):
        raise OSError("串口未打开（占位通道）")


# ── TCP 通道（C6 走 Wi-Fi 连过来）──────────────────────────────────────────
class CoordinatorTcp:
    """TCP **服务端**：等 ESP32-C6 主动连上来。

    为什么由主机监听、C6 当客户端：
      - C6 的目标地址是固定的（HA 主机），主机 IP 变了改一处就行；
      - 主机这边是被动方，DHCP 换地址也不会让 C6「找不到人」；
      - 一个端口只能有一路连接，旧连接会被新连接顶掉 —— 正好匹配
        「一块板子一个网关」的部署方式。

    接口与 CoordinatorSerial 一致（read/write/close/in_waiting/can_reset），
    所以主循环一个字都不用改。

    ★ can_reset = False：TCP 通道没有 RTS，没法给芯片硬复位。主循环里的
      「45 s 没数据就复位」看门狗必须跳过它，否则日志会一直喊「强制复位」
      却什么也没发生，误导排查方向。
    """
    can_reset = False

    def __init__(self, bind="0.0.0.0", port=12345):
        self.bind = bind
        self.port = port
        self.conn = None
        self.peer = None
        self._err_logged = False
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.srv.bind((bind, port))
            self.srv.listen(1)
        except OSError:
            self.srv.close()
            raise
        self.srv.settimeout(0.5)      # accept 不能无限等，否则主循环卡死
        print(f"[Link] 已监听 {bind}:{port} —— 等 C6 通过 Wi-Fi 连上来")

    # -- 内部 ---------------------------------------------------------------
    def _accept(self):
        try:
            c, addr = self.srv.accept()
        except socket.timeout:
            return False
        except OSError as e:
            print(f"[Link] accept 出错：{e}")
            return False
        try:
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            c.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            pass
        c.settimeout(1.0)
        old, self.conn = self.conn, c
        self.peer = addr
        self._err_logged = False
        if old:
            try:
                old.close()
            except Exception:                           # noqa: BLE001
                pass
            print("[Link] 有新的 C6 连接进来，旧的已断开")
        print(f"[Link] C6 已连接：{addr[0]}:{addr[1]}")
        return True

    def drop(self):
        """丢掉当前连接（保留监听套接字，下轮 read 会重新 accept）。"""
        c, self.conn = self.conn, None
        self.peer = None
        if c:
            try:
                c.close()
            except Exception:                           # noqa: BLE001
                pass
        # ★ TCP 一断就立刻把状态置离线，不用干等 130 s 心跳超时。
        #   C6 重连后会补发 {"t":"boot"}，那时又会置回 online。
        with _state_lock:
            _state["status"] = "offline"

    # -- 与串口一致的接口 ---------------------------------------------------
    def read(self, n):
        if self.conn is None:
            self._accept()
            return b""
        try:
            # ★ 至少读 4 KB：主循环传进来的 n 是 in_waiting or 1，也就是 1。
            #   真按 1 字节读的话，一次 4 KB 的拓扑 dump 要 4000 次 recv，
            #   白白多出几十毫秒延迟。调用方本来就只是往 buf 里追加。
            d = self.conn.recv(max(int(n), 4096))
        except socket.timeout:
            return b""
        except OSError as e:
            print(f"[Link] 连接出错：{e} → 断开，等 C6 重连")
            self.drop()
            return b""
        if not d:
            print("[Link] C6 断开了，等它重连（板上会自动重试）")
            self.drop()
        return d

    def write(self, d):
        global _write_err
        if self.conn is None:
            return 0
        try:
            self.conn.sendall(d)
            return len(d)
        except OSError as e:
            _write_err += 1
            # 断线期间 HA 的命令会一直打到这里，日志必须限速 ——
            # 否则一次网络抖动就能刷满整个日志缓冲（HA 只回最后 100 行）。
            if not self._err_logged:
                self._err_logged = True
                print(f"[Link] 写失败：{e} → 连接已断开，等 C6 重连")
            self.drop()
            return 0

    def close(self):
        self.drop()
        try:
            self.srv.close()
        except Exception:                               # noqa: BLE001
            pass

    @property
    def in_waiting(self):
        # 恒返回 0：让主循环走 read(1) 这条阻塞路径，由 recv 自己等数据。
        return 0


def _tcp_open(bind, port, attempts=3):
    """起监听。失败重试；全失败时返回 _NullSerial 顶着，不抛异常退出。

    ★ 不抛异常是为了保住「开机不让 Supervisor 反复重启」这条底线：
      端口被占 / 网卡还没起来，都会在第 10 次读失败后由 _link_reopen 重试。
    """
    for i in range(1, attempts + 1):
        try:
            return CoordinatorTcp(bind, port)
        except Exception as e:                          # noqa: BLE001
            print(f"[Link] 监听 {bind}:{port} 失败（第 {i}/{attempts} 次）：{e}")
            time.sleep(1.0)
    print(f"[Link] !! {bind}:{port} 监听不起来，先用占位通道顶着，稍后自动重试")
    return _NullSerial()


def _serial_other_ports(preferred):
    """除配置端口之外，还值得一试的串口节点。

    ★ 为什么需要这个兜底：`/dev/serial/by-id/...` 是 udev 生成的**符号链接**，
      设备重新枚举（刷机、换 USB 口、主机重启、板子掉电重连）之后它可能
      指向新的 /dev/ttyACMx，甚至变成断链。2026-11-06 就卡在这儿：
      按 by-id 打开一直报
          [Errno 1] Operation not permitted
      而换 /dev/ttyACM0 就正常 —— 日志里只有一句 EPERM，完全看不出是
      「路径失效」而不是「设备没了」。

      所以重开失败到一定次数后，主动扫一遍常见的串口节点试一次；
      打通了就把实际用的路径打进日志，提示把选项改过去。
    """
    import glob
    out = []
    for p in sorted(glob.glob("/dev/ttyACM*")) + sorted(glob.glob("/dev/ttyUSB*")):
        if p != preferred and p not in out:
            out.append(p)
    return out


def _serial_open(port, baud, attempts=3, first_delay=0.0):
    """打开串口，失败就重试几次。全失败时返回 _NullSerial（不抛异常）。"""
    delay = first_delay
    for i in range(1, attempts + 1):
        if delay:
            time.sleep(delay)
        try:
            s = CoordinatorSerial(port, baud)
            return s
        except Exception as e:                          # noqa: BLE001
            print(f"[Serial] 打开 {port} 失败（第 {i}/{attempts} 次）: {e}")
            delay = min(max(delay * 2, 1.0), 10.0)
    print(f"[Serial] !! {port} 打不开，先用占位串口顶着，稍后自动重试")
    return _NullSerial()


_last_reopen_ts = 0.0     # 上次尝试重开的时间（节流用，见 _serial_reopen）


def _serial_reopen(reason: str = "") -> bool:
    """掉线自愈：关掉旧 fd 重新打开端口。

    ★ 为什么必须"重开"而不是"重试"：USB-CDC 端点在设备重新枚举后会失效，
      **旧 fd 会永远返回 `[Errno 5] I/O error`**。2026-10-26 重建 add-on 后
      实测：连续 100+ 次读全是 EIO，网关一台设备都收不到（页面空列表），
      而 read() 里原来那句「sleep 0.5 继续」永远不会自己好 —— 只能换 fd。

    ★ 10 秒内只尝试一次：端口彻底不在时（比如板子被拔去接电脑），
      主循环的看门狗每 6 秒就会喊一次重开，每次都刷三段失败日志，
      日志直接被淹没，真正有用的行反而看不见了。

    返回 True 表示端口已恢复。与 esptool 刷机互斥：正在刷就让开。
    """
    global ser, _last_reopen_ts
    now = time.time()
    if now - _last_reopen_ts < 10:
        return False
    _last_reopen_ts = now
    if _serial_pause.is_set():
        return False                       # esptool 正在用端口
    if not _flash_mutex.acquire(blocking=False):
        # ★ 措辞要说实话：这把锁除了刷机，还被**另一个正在跑的重开流程**占着
        #   （那串退避合计 45 s）。原来一律写"端口被刷机占用"，会把排查方向
        #   直接带偏到 esptool 上。
        print("[Serial] 另一个重开/刷机流程正占着端口，本次跳过")
        return False
    try:
        try:
            ser.close()
        except Exception:
            pass
        # 前 3 次只试配置端口（偶发 EIO 重开一次就好，不必乱换）；
        # 都失败说明这个路径本身有问题，再把候选节点排进队列。
        ports = [args.port] * 3 + _serial_other_ports(args.port)
        delay = 1.0
        for i, port in enumerate(ports, 1):
            time.sleep(delay)
            try:
                ser = CoordinatorSerial(port, args.baud)
                global _write_err
                _write_err = 0
                if port != args.port:
                    print(f"[Serial] ★ 配置的 {args.port} 打不开，已改用 {port}"
                          f"（建议把 add-on 选项 serial_port 改成这个路径）")
                print(f"[Serial] 端口已重开（{reason} · {port} · 第 {i} 次尝试）")
                return True
            except Exception as e:                      # noqa: BLE001
                print(f"[Serial] 重开失败 第 {i} 次（{port}）: {e}")
                delay = min(delay * 2, 15.0)
        print(f"[Serial] !! {len(ports)} 个候选端口全打不开，稍后再试")
        return False
    finally:
        _flash_mutex.release()


def _serial_kick_reset(reason: str = "") -> bool:
    """硬复位 C6：拉一下 RTS（=EN），让它重新启动。

    什么时候用：端口能打开、但**一个字节都收不到**。这时问题在芯片侧
    （固件挂了 / USB-JTAG 端点停了），重开 fd 没用，只能复位。

    ★ RTS=True 拉低 EN、False 放开 —— 和 esptool 的默认复位序列一致。
      DTR 保持 False，否则会被 strapping 带进下载模式（那样就再也收不到
      JSON 上报了，只剩 bootloader）。
    """
    try:
        s = getattr(ser, "ser", None)
        if s is None:
            return False
        print(f"[Serial] 强制复位 C6（{reason}）")
        s.rts = True
        time.sleep(0.15)
        s.rts = False
        return True
    except Exception as e:                              # noqa: BLE001
        print(f"[Serial] 复位失败: {e}")
        return False


# ── 通道分发：主循环只调 _link_*，不直接碰串口 ─────────────────────────────
# 这样加一条新通道（Wi-Fi/TCP）不需要动主循环里任何一行，
# 也就不会出现「串口分支修好了、TCP 分支忘了改」这种半吊子状态。
def _link_open():
    """按 --link 打开当前通道。开机只走这一处。"""
    if args.link == "tcp":
        return _tcp_open(args.tcp_bind, args.tcp_port)
    return _serial_open(args.port, args.baud)


def _link_reopen(reason: str = "") -> bool:
    """掉线自愈。串口要换 fd；TCP 只要丢掉连接重新 accept。"""
    global ser
    if args.link != "tcp":
        return _serial_reopen(reason)

    if _serial_pause.is_set():
        return False
    if not _flash_mutex.acquire(blocking=False):
        print("[Link] 另一个重开/刷机流程正占着通道，本次跳过")
        return False
    try:
        try:
            ser.close()
        except Exception:                           # noqa: BLE001
            pass
        ser = _tcp_open(args.tcp_bind, args.tcp_port)
        global _write_err
        _write_err = 0
        print(f"[Link] 监听已重建（{reason}）")
        return not isinstance(ser, _NullSerial)
    finally:
        _flash_mutex.release()


def _link_kick_reset(reason: str = "") -> bool:
    """硬复位。只有串口能做到 —— TCP 通道没有 RTS，可能连芯片都不在一块板子上。"""
    if args.link == "tcp":
        return False
    return _serial_kick_reset(reason)


def _esptool_cmd(bin_path: str) -> list:    return [sys.executable, "-m", "esptool",
            "--chip", "esp32c6",
            "--port", args.port,
            "--baud", str(FLASH_BAUD),
            "--before", "default_reset",
            "--after", "hard_reset",
            "write_flash", APP_OFFSET, bin_path]


def flash_status() -> dict:
    with _flash_state_lock:
        return {"status": _flash_state["status"],
                "bin": _flash_state["bin"],
                "tail": list(_flash_state["tail"])}


def flash_is_running() -> bool:
    return _flash_state["status"] == "running"


def flash_apply(bin_path: str) -> bool:
    """通过 bootloader 写入 bin_path。阻塞到 esptool 跑完。"""
    global ser
    if args.link == "tcp":
        # ★ 这条路径要靠 esptool 走 USB-Serial-JTAG 进下载模式，本质上是
        #   **物理接触**，TCP 通道根本够不着。别让它半途失败，
        #   直接拒绝并说清原因 —— 换成 App 级 OTA（/api/fw_ota）就行。
        print("[FLASH] 拒绝：当前是 TCP 通道，esptool 走不了网络。"
              "请改用 App 级 OTA（固件在线升级），或者插上 USB 切回串口。")
        with _flash_state_lock:
            _flash_state.update({"status": "error", "bin": bin_path,
                                 "tail": ["当前是 TCP 通道，esptool 需要 USB 串口"]})
        return False
    if not _flash_mutex.acquire(blocking=False):
        print("[FLASH] 已经在跑了 —— 忽略本次请求")
        return False
    _serial_pause.set()                 # 暂停主循环
    with _flash_state_lock:
        _flash_state.update({"status": "running", "bin": bin_path, "tail": []})
    ok = False
    try:
        time.sleep(0.3)
        try:
            ser.close()
        except Exception:
            pass
        cmd = _esptool_cmd(bin_path)
        print("[FLASH] 启动：" + " ".join(cmd[1:]))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                errors="replace", bufsize=1)
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            print(f"[FLASH] {line}")
            with _flash_state_lock:
                _flash_state["tail"] = (_flash_state["tail"] + [line])[-40:]
        rc = proc.wait()
        ok = rc == 0
        print(f"[FLASH] esptool 结束，rc={rc}"
              + ("" if ok else "  （详情见上；镜像里是不是缺 esptool？）"))
    except FileNotFoundError:
        print("[FLASH] 找不到 esptool/python")
    except Exception as e:
        print(f"[FLASH] 出错：{e}")
    finally:
        # 刷完恢复串口。用 _serial_open：刷机后设备正在重新枚举，一次打不开
        # 很正常，别让整个 finally 抛异常（会把 _flash_state 卡在 running）。
        ser = _serial_open(args.port, args.baud, attempts=5, first_delay=1.0)
        print("[FLASH] 串口已恢复，桥接继续")
        with _flash_state_lock:
            _flash_state["status"] = "done" if ok else "error"
        _serial_pause.clear()
        _flash_mutex.release()
    return ok


# ── HA Auto-Discovery ─────────────────────────────────────────────────────
_ha_devices = {}     # addr → {"ieee", "mfr", "model"}
_ha_nodes   = {}     # addr → node（会话内缓存，见 _ha_node）
_ha_last    = {}     # addr → {sub: payload} 最后一次已知状态

# ── Discovery-Cache ───────────────────────────────────────────────────────
# 以前 _ha_sent 是个集合 (node, obj_id, addr, _DISC_VER)。两个毛病都实际踩到过：
#
#   * 版本计数器 _DISC_VER 每次改内容都得手工加一。忘了加，新配置就永远发不出去，
#     HA 悄无声息地继续用旧实体 —— 而且完全看不出来。
#   * 每次加载项重启集合就空了，于是所有配置全量重发，HA 每次都把全部实体
#     重新处理一遍。
#
# 照 Z2M 的做法（lib/extension/homeassistant.ts），现在按 topic 比较成品 JSON：
#     if (!discoveredMessage || discoveredMessage.payload !== payloadStr)
# 这样内容一变就自动重发，而内容没变的重启一条都不会发。_DISC_VER 取消。
_ha_sent   = {}      # topic → 上一次发布的 JSON 串
_sent_addr  = {}     # topic → addr（仅用于 _purge_addr / 清理）

# 启动窗口：Z2M 启动时订阅 Discovery topic 5 秒，把 broker 上 retained 的
# 配置读进来，然后只发布会缺的或不一样的。这样重启能省掉几百次 publish ——
# 也是唯一能知道 broker 上到底已经有什么的机会。
_DISC_WAIT   = 5.0
_disc_ready  = True     # 第一次连接之前：直接发布
_disc_remote = {}       # topic → payload（从 broker 读到的）
_disc_defer  = {}       # topic → payload（窗口期内先扣住不发）

# 现在只作记录用：这个计数器不再参与键的计算。
#   1 → 第一版
#   2 → "has_entity_name"（名字重复了："HGZB-043 HGZB-043 上 开关"）
#   3 → 按键面板的 event 实体（_ha_publish_event）
_DISC_VER = 3

# 实体的来源标记 —— HA 会在设备对话框里显示。Z2M 也是这么设的，
# 字段也一样（origin: {name, sw, url}）。
def _gw_version():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "config.json"), encoding="utf-8") as f:
            return json.load(f).get("version") or "dev"
    except Exception:
        return "dev"

_GW_ORIGIN = {"name": "ESP32-C6 Gateway", "sw": _gw_version(),
              "url": "https://github.com/Koenkk/zigbee-herdsman-converters"}


def _ha_avail(addr=None):
    """当前格式的可用性（列表）—— 取代旧的单字段。

    ★ 2026-10-26: 以前只有网关自己这一个 Topic ⇒ 网关在线时，**已经掉线的
      开关也照样显示 "关"**（HA 只是显示最后一次 payload），用户点了没反应
      还以为是网络问题。现在每台设备再挂一个自己的 Topic，模式改 "all"：
      网关在线 **并且** 这台设备在线，实体才可用。

    为什么是 "all" 而不是原来的 "latest"：latest 是"最后收到的那个消息说了算"，
    两个来源会互相覆盖，取决于到达顺序；all 是"所有来源都说 online 才算可用"，
    结果确定的。只有一个 Topic 时两者等价。
    """
    av = {
        "availability": [{
            "topic": f"{BASE}/status",
            "payload_available":     "online",
            "payload_not_available": "offline",
        }],
        "availability_mode": "all",
    }
    if addr and DEVICE_AVAIL:
        # 注意顺序：gateway 必须在 [0]。_disc_purge_orphans() 靠
        # availability[0].topic 判断"这条 discovery 是不是我们发的"。
        av["availability"].append({
            "topic": _avail_topic(addr),
            "payload_available":     "online",
            "payload_not_available": "offline",
        })
    return av


# ── 每台设备的可用性（device availability）──────────────────────────────────
# 判活用"记分板"而不是单一时钟：任何能证明设备存在的证据都打一个时间戳，
# 取最大值为"最后一次被证实存活"。
#   · 它发来的任意 APS/ZCL 帧            → 最硬的证据
#   · ZDO node_desc 探测成功              → 拓扑轮询每 60 s 就探一次，全覆盖
#   · 出现在协调器邻居表里                → 协调器刚听见它
#   · 自己上报邻居表（Mgmt_Lqi_rsp）
# 反证只有一条：node_desc 探测**失败**（0x85 超时等），且比最后一次成功更晚。
# ★ 为什么不用"多久没上报"这一条判：只在按键时才上报的开关（0x023b 那种）
#   会长期没帧，但它们每 60 s 的探测都答得上 —— 靠超时会误杀。
_avail_last_ok  = {}     # addr → 最后一次被证实存活的 time.time()
_avail_last_bad = {}     # addr → 最后一次探测失败的 time.time()
_avail_pub      = {}     # addr → 已发布的 "online"/"offline"（去抖）

# 启动宽限：刚开机设备表/邻居表都还是空的，这时候判死必然是误杀。
_AVAIL_GRACE   = 300.0
_AVAIL_NBR_MAX = 600.0   # 邻居表超过这么久没更新，就不拿它当"看不见"的证据
_avail_boot    = time.time()


def _avail_topic(addr: str) -> str:
    return f"{BASE}/dev/{addr}/status"


def _avail_touch(addr) -> None:
    """记下「这台设备刚刚被证实存活」。"""
    if addr:
        _avail_last_ok[addr] = time.time()


def _avail_timeout(power) -> float:
    """多久没有任何证据就判掉线。市电 30 min（探测每 60 s 一次）、电池 24 h。"""
    p = (power or "").lower()
    if "battery" in p or "batt" in p:
        return 24 * 3600.0
    if "mains" in p:
        return 30 * 60.0
    return 6 * 3600.0


def _avail_vanished(addr) -> bool:
    """历史上有实体，但本次启动以来毫无音讯，而且协调器也听不见它。

    这是"设备真的不在了"的唯一可判定形态：
      · 地址还在实体表里（有过 discovery）→ 说明不是陌生地址；
      · 启动宽限期已过（设备表/邻居表都已建立）；
      · 不在设备表里（没有任何上报、也没有被拓扑采集补进来）；
      · 不在协调器邻居表里，而邻居表本身是新鲜的。
    换过地址的设备不在这里：_purge_addr() 会把旧地址从 _addr_nodes 删掉。
    """
    if addr not in _addr_nodes:
        return False
    if time.time() - _avail_boot < _AVAIL_GRACE:
        return False
    if addr in (_state.get("devices") or {}):
        return False
    tp = _state.get("topo") or {}
    if (time.time() - (tp.get("done_at") or 0.0)) > _AVAIL_NBR_MAX:
        return False                        # 邻居表太旧，不敢据此判死
    nbr = tp.get("nbr") or []
    return not any(n.get("a") == addr for n in nbr)


def _avail_online(addr) -> bool:
    ok  = _avail_last_ok.get(addr, 0.0)
    bad = _avail_last_bad.get(addr, 0.0)
    if ok == 0.0 and bad == 0.0:
        # 一点证据都没有（刚启动、设备表还没建立）→ **不主动判死**，
        # 保持过去的行为（可用）。宁可不显示不可用，也不要误杀。
        # 例外：实体还在、但启动后彻底没影的设备（见 _avail_vanished）。
        return not _avail_vanished(addr)
    if bad > ok:
        return False
    nd = (_state.get("node_desc") or {}).get(addr) or {}
    return (time.time() - ok) <= _avail_timeout(nd.get("power"))


def _avail_sync(addr, force: bool = False) -> None:
    """按当前判断发布 availability（retained；状态没变就不发）。"""
    if not HA_ENABLED or not addr or not DEVICE_AVAIL:
        return
    st = "online" if _avail_online(addr) else "offline"
    if not force and _avail_pub.get(addr) == st:
        return
    was = _avail_pub.get(addr)
    _avail_pub[addr] = st
    mq_put(_avail_topic(addr), st, retain=True, qos=1)
    if was != st:
        print(f"[AVAIL] {addr} → {st}")


def _avail_task(interval: float = 30.0):
    """定期重算并发布每台设备的 availability。

    探测结果由拓扑轮询（每 60 s 对每台设备发 node_desc）持续刷新，
    这里只负责"把记分板换算成 online/offline 并推给 HA"。
    """
    while True:
        try:
            with _state_lock:
                addrs = set((_state.get("devices") or {}).keys())
                nbr = (_state.get("topo") or {}).get("nbr") or []
            addrs |= {n.get("a") for n in nbr if n.get("a")}
            # 历史上发过 discovery 的地址也要算进去：只有把它们纳进来，
            # "设备已经不在网络上"才能从 online 翻成 offline（否则那条
            # retained 的 online 永远没人去改，HA 里就是一条僵尸实体）。
            addrs |= set(_addr_nodes.keys())
            for a in sorted(addrs):
                _avail_sync(a)
        except Exception as e:                          # noqa: BLE001
            print(f"[AVAIL] 循环出错: {e}")
        time.sleep(interval)


def _ago(ts) -> str:
    """把 time.time() 时间戳说成「多久以前」。"""
    if not ts:
        return "—"
    d = max(0, int(time.time() - ts))
    if d < 60:
        return f"{d} 秒前"
    if d < 3600:
        return f"{d // 60} 分钟前"
    if d < 86400:
        return f"{d // 3600} 小时前"
    return f"{d // 86400} 天前"



def _disc_begin_window():
    """打开启动窗口：先读入现有的 Discovery 配置。"""
    global _disc_ready
    if not HA_ENABLED:
        return
    _disc_ready = False
    _disc_remote.clear()
    try:
        mq.subscribe(f"{HA_PREFIX}/+/+/+/config", qos=0)
        print(f"[HA] 读取 broker 里已有的 Discovery 配置（最多等 {_DISC_WAIT:.0f} s）")
        threading.Timer(_DISC_WAIT, _disc_flush).start()
    except Exception as e:
        print(f"[HA] 打不开启动窗口：{e}")
        _disc_ready = True


def _disc_flush():
    """关闭启动窗口：只真正发布有差异的配置。"""
    global _disc_ready
    _disc_ready = True
    try:
        mq.unsubscribe(f"{HA_PREFIX}/+/+/+/config")
    except Exception:
        pass
    neu = gleich = 0
    for topic, payload in list(_disc_defer.items()):
        if _disc_remote.get(topic) == payload:
            _ha_sent[topic] = payload      # broker 已经有了，不用再发
            gleich += 1
            continue
        mq_put(topic, payload, retain=True, qos=1)
        _ha_sent[topic] = payload
        neu += 1
    _disc_defer.clear()
    print(f"[HA] Discovery 核对：{neu} 个新增/变化，{gleich} 个未变 "
          f"({len(_disc_remote)} Configs im Broker)")
    _disc_purge_orphans()
    _disc_upgrade_avail()


def _disc_purge_orphans():
    """删掉那些已经不属于任何设备的 retained discovery 配置。

    两种情况都真遇到过：
      * 设备已经不在了（短地址未知）→ 否则整套配置会变成孤儿留在 broker 里，
        在 HA 里留一张"不可用"的僵尸卡片。
      * 端点被判定为纯发送端 → 那里不该（再）存在 switch。
    """
    if not HA_ENABLED or not _disc_remote:
        return
    if not _addr_nodes and not _ieee_nodes:
        # 如果对自己的设备一无所知（ha_nodes.json 不存在、全新安装），
        # 就分不清"未知节点"和"设备还没重新上线"。这种时候宁可什么都不删 ——
        # 删错一条，用户会丢掉那一整套实体。
        print("[HA] 不知道有哪些设备 —— 跳过清理")
        return
    bekannt = set(_addr_nodes.values()) | set(_ieee_nodes.values())
    rueck   = {v: k for k, v in _addr_nodes.items()}
    eigen   = f"{BASE}/status"
    weg = 0
    for topic, payload in list(_disc_remote.items()):
        parts = topic.split("/")
        if len(parts) < 5:
            continue
        comp, node, obj_id = parts[1], parts[2], parts[3]
        # 归属不能按节点名前缀判断，要看配置自身的 availability topic。
        # Z2M 也是这么判的
        # (message.availability[0].topic.startsWith(baseTopic))。否则同一个
        # broker 上的**第二个**网关实例会把第一台的实体清掉 ——
        # 它们的节点名同样叫 "c6_…"。
        try:
            cfg = json.loads(payload)
        except Exception:
            continue
        av = cfg.get("availability")
        if isinstance(av, list) and av and isinstance(av[0], dict):
            owns_it = av[0].get("topic") == eigen
        else:
            owns_it = cfg.get("availability_topic") == eigen      # 老版本的字段名
        if not owns_it:
            continue
        grund = None
        if node not in bekannt:
            grund = "设备未知"
        elif comp == "switch" and obj_id.startswith("ep") and "_" in obj_id:
            try:
                ep = int(obj_id[2:].split("_", 1)[0])
            except ValueError:
                ep = 0
            addr = rueck.get(node)
            if ep and addr and _ep_key(addr, ep) in _ctrl:
                grund = "端点是纯发送端"
        if grund:
            mq_put(topic, "", retain=True, qos=1)
            _ha_sent.pop(topic, None)
            _sent_addr.pop(topic, None)
            _disc_remote.pop(topic, None)
            weg += 1
            print(f"[HA] 已删除孤儿配置（{grund}）：{topic}")
    if weg:
        print(f"[HA] 清理了 {weg} 个过期 Discovery 配置")


def _node_addr_map() -> dict:
    """node-ID → 当前短地址（_addr_nodes / _ieee_nodes 的反查）。"""
    m = {n: a for a, n in _addr_nodes.items() if n and a}
    for ieee, node in _ieee_nodes.items():
        if node and node not in m:
            a = _ieee_addr.get(ieee)
            if a:
                m[node] = a
    return m


def _disc_upgrade_avail():
    """给遗留的旧 discovery 配置补上设备 availability。

    为什么需要：discovery 配置**只有**在设备再发一条电文时才会重写
    （_disc_send 挂在数据路径上）。一个只发原始 APS 按键帧的面板（0x747d），
    或者更新之后就再没说过话的设备，就会永远留着那份只带**一个**
    availability topic（网关）的旧配置。后果：设备早就没了，
    HA 里却还显示"关"而不是"不可用" ——
    一张跟真正的"关"分不出来的僵尸卡片。

    启动窗口期是 _disc_remote 里**装齐** broker 全部 retained 配置的唯一时刻。
    补发就在这里做。

    顺序：网关 topic 保持在 [0]，因为 _disc_purge_orphans() 是拿它判归属的。
    """
    if not HA_ENABLED or not DEVICE_AVAIL or not _disc_remote:
        return
    n2a  = _node_addr_map()
    eigen = f"{BASE}/status"
    gw = {"topic": eigen, "payload_available": "online",
          "payload_not_available": "offline"}
    auf = 0
    for topic, payload in list(_disc_remote.items()):
        parts = topic.split("/")
        if len(parts) < 5 or parts[4] != "config":
            continue
        node = parts[2]
        try:
            cfg = json.loads(payload)
        except Exception:
            continue
        if not isinstance(cfg, dict):
            continue
        av = cfg.get("availability")
        if isinstance(av, list) and av and isinstance(av[0], dict):
            if av[0].get("topic") != eigen:
                continue                    # 别的实例 —— 不要动
        elif cfg.get("availability_topic") == eigen:
            av = []                         # uralte Einzel-Topic-Fassung
        else:
            continue
        addr = n2a.get(node)
        if not addr:
            continue                        # 地址未知 —— 不要瞎猜
        want = _avail_topic(addr)
        if any(isinstance(x, dict) and x.get("topic") == want for x in av):
            continue
        rest = [x for x in av
                if isinstance(x, dict) and x.get("topic") not in (eigen, want)]
        cfg.pop("availability_topic", None)
        cfg["availability"] = [gw] + rest + [
            {"topic": want, "payload_available": "online",
             "payload_not_available": "offline"}]
        cfg["availability_mode"] = "all"
        neu = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
        if neu == payload:
            continue
        mq_put(topic, neu, retain=True, qos=1)
        _ha_sent[topic]    = neu
        _disc_remote[topic] = neu
        auf += 1
        print(f"[HA] 已补上 availability：{topic} → {want}")
    if auf:
        print(f"[HA] {auf} 个 Discovery 配置改成设备级 availability")
    else:
        print("[HA] availability：所有 Discovery 配置都是最新的")


def _disc_send(comp, node, obj_id, cfg, addr=""):
    """设置一条 Discovery 配置 —— 但只在它发生变化时才发。"""
    topic   = f"{HA_PREFIX}/{comp}/{node}/{obj_id}/config"
    payload = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
    if _ha_sent.get(topic) == payload:
        return
    _sent_addr[topic] = addr
    if not _disc_ready:
        # 还在启动窗口期：先记下来，对齐之后补发。
        # 没有这个队列，实体就丢了 —— discovery 是跟着来电文走的，不会重来一次。
        _disc_defer[topic] = payload
        return
    mq_put(topic, payload, retain=True, qos=1)
    _ha_sent[topic] = payload
    print(f"[HA] discovery {comp}/{node}/{obj_id} → {cfg.get('name')}")

# ── 持久化的节点归属 ───────────────────────────────────────────────────────
# 没有这个文件时，Node-ID 在每次 add-on 启动后都按**当时**的短地址重新拼。
# 设备一旦在 rejoin 时换了地址（停电、换插座、重新入网），就会生成第二套
# 实体；旧的 retained discovery 配置留在 broker 里成了孤儿，HA 里于是
# 出现重复实体。
#
# 两样都存：
#   addr_nodes: {addr: node}   – 冻结现有名字（不做迁移）
#   ieee_nodes: {ieee: node}   – 换地址后还能把设备认回来
#   addr_ieee : {addr: ieee}   – 学到手的对应关系
_DATA_DIR   = "/data" if os.path.isdir("/data") else "/tmp"
_NODES_FILE = os.path.join(_DATA_DIR, "ha_nodes.json")

# ── 记住发射功率，C6 重启后恢复 ────────────────────────────────────────────
# `set_tx_power` 在固件里只在**运行期**生效（main.c 不写 NVS）。断电或复位
# 之后功率回到 SDK 默认值 —— 而且没人会察觉，因为 `lqi`/`rssi` 描述的是
# **反方向**，改网关功率它们基本不动（§4）。"网关在用 20 dBm"这种误判
# 就是这么来的，而它其实早就跑在 0 dBm 上了。
# 所以：把最后一次期望值存进 /data，每次 C6 启动后重发，直到回读值
# （`eff`）确认为止。
_TXP_FILE = os.path.join(_DATA_DIR, "gw_txpower.json")
_TXP_DBM  = None
_TXP_LOCK = threading.Lock()


def _txp_load():
    global _TXP_DBM
    try:
        with open(_TXP_FILE, "r", encoding="utf-8") as f:
            _TXP_DBM = int(json.load(f).get("dbm"))
        print(f"[TX] gemerkte Sendeleistung: {_TXP_DBM} dBm")
    except FileNotFoundError:
        _TXP_DBM = None
    except Exception as e:                              # noqa: BLE001
        _TXP_DBM = None
        print(f"[TX] 发射功率读不出来（{_TXP_FILE}）：{e}")


def _txp_save(dbm):
    global _TXP_DBM
    _TXP_DBM = int(dbm)
    try:
        tmp = _TXP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"dbm": _TXP_DBM}, f)
        os.replace(tmp, _TXP_FILE)
    except Exception as e:                              # noqa: BLE001
        print(f"[TX] 发射功率存不下来：{e}")


def _txp_reapply(delay: float = 3.0, versuche: int = 3):
    """C6 启动后把记住的期望值重发一遍。

    要试多次，因为紧跟 `boot` 之后这条命令还可能打空
    （ZBOSS 之后才报 "ZDO Config Ready"）。一旦回读值确认了期望值就停。
    """
    if _TXP_DBM is None:
        return

    def _go():
        for i in range(versuche):
            try:
                with _TXP_LOCK:
                    ser.write(_js({"cmd": "set_tx_power", "dbm": _TXP_DBM}).encode())
            except Exception as e:                      # noqa: BLE001
                print(f"[TX] 恢复失败：{e}")
                return
            time.sleep(4.0)
            with _state_lock:
                tp = dict(_state.get("tx_power") or {})
            if tp.get("eff") == _TXP_DBM:
                print(f"[TX] C6 重启后已恢复发射功率："
                      f"{_TXP_DBM} dBm (Versuch {i + 1})")
                return
        print(f"[TX] 警告：C6 重启后 {_TXP_DBM} dBm 未被确认 "
              f"（eff={tp.get('eff')}）—— 请检查 /api/tx_power")

    threading.Thread(target=_go, daemon=True).start()


_addr_nodes = {}
_ieee_nodes = {}
_addr_ieee  = {}
_ieee_addr  = {}      # ieee → 当前 addr（用于清理旧地址）
_addr_subs  = {}      # addr → [sub, …]      (本 topic 上发布过哪些 sub)


def _nodes_load():
    global _addr_nodes, _ieee_nodes, _addr_ieee, _ieee_addr, _addr_subs
    try:
        with open(_NODES_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"[HA] 节点映射读不出来（{_NODES_FILE}）：{e}")
        return
    _addr_nodes = dict(d.get("addr_nodes") or {})
    _ieee_nodes = dict(d.get("ieee_nodes") or {})
    _addr_ieee  = dict(d.get("addr_ieee") or {})
    _ieee_addr  = dict(d.get("ieee_addr") or {})
    _addr_subs  = {k: list(v) for k, v in (d.get("addr_subs") or {}).items()}
    _noread.clear()
    _noread.update(d.get("noread") or [])
    _ctrl.clear()
    _ctrl.update(d.get("ctrl") or [])
    print(f"[HA] 节点映射：{len(_addr_nodes)} 个地址，"
          f"{len(_ieee_nodes)} IEEE ({_NODES_FILE})")
    if _noread or _ctrl:
        print(f"[HA] 已知：{len(_noread)} 个端点不可回读，"
              f"{len(_ctrl)} reine Sender: {sorted(_ctrl) or '-'}")


def _nodes_save():
    try:
        tmp = _NODES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"addr_nodes": _addr_nodes, "ieee_nodes": _ieee_nodes,
                       "addr_ieee": _addr_ieee, "ieee_addr": _ieee_addr,
                       "addr_subs": _addr_subs,
                       "noread": sorted(_noread), "ctrl": sorted(_ctrl)},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, _NODES_FILE)
    except Exception as e:
        print(f"[HA] 节点映射存不下来：{e}")


def _ha_slug(s):
    return "".join(c if c.isalnum() else "_" for c in str(s)).strip("_").lower()

def _purge_addr(addr, why):
    """把某个旧短地址上的 retained topic 全部清掉。

    没有它，每次 rejoin 之后 broker 里就多一整套：
    2026-10-17 就出现过 0xb2a2 和 0x747d 并排躺着（同一个 IEEE！），
    而且 ep1 除了 on_off 还额外留着一个过期的 ep1/on_off 值。
    """
    subs = _addr_subs.pop(addr, [])
    for sub in subs:
        mq_put(f"{BASE}/zigbee/{addr}/{sub}", "", retain=True, qos=1)
    gone = 0
    for topic in [t for t, a in _sent_addr.items() if a == addr]:
        mq_put(topic, "", retain=True, qos=1)
        _ha_sent.pop(topic, None)
        _sent_addr.pop(topic, None)
        gone += 1
    _addr_nodes.pop(addr, None)
    _ha_nodes.pop(addr, None)
    _addr_ieee.pop(addr, None)
    # 把这个地址上学到的东西一并带走 —— 否则旧地址会永远被当成"不可回读"，
    # 新地址又要重新判一遍。
    for k in [k for k in _noread if k.split("/", 1)[0] == addr]:
        _noread.discard(k)
    for k in [k for k in _ctrl if k.split("/", 1)[0] == addr]:
        _ctrl.discard(k)
    if subs or gone:
        print(f"[HA] {addr} verworfen ({why}): {len(subs)} Zustands-Topics, "
              f"已删除 {gone} 个 Discovery 配置")
    _nodes_save()


def _device_remove(addr, why="手动删除", tell_c6=True):
    """把设备彻底删掉 —— 列表、broker、HA。

    顺序很重要：
      1. 先记住节点名，因为 _purge_addr() 会把映射删掉。
      2. 撤掉 retained discovery 配置。_sent_addr 只知道**本次**会话发过
         什么 —— add-on 重启之后它就是空的。所以还要靠 _disc_remote
         （启动时 broker 的现状）按节点名再找一遍。
      3. _purge_addr()：状态 topic、映射、_noread/_ctrl、_nodes_save。
      4. availability 备忘 + retained 的 online/offline。
      5. 运行时状态（devices / node_desc）。
      6. 告诉 C6 让它也把这条记录删掉。少了第 6 步，删除只能维持 60 s：
         心跳会把完整设备表发过来，而 `t == "lqi"` 会用 setdefault
         把缺失的条目重新建出来（2026-11-05 在 0x023b 上正是这样）。
    """
    addr = (addr or "").strip()
    if not addr:
        return False
    node = _ha_nodes.get(addr) or _addr_nodes.get(addr)

    topics = set()
    if node:
        for t in list(_disc_remote):
            parts = t.split("/")
            if len(parts) >= 5 and parts[2] == node:
                topics.add(t)
    topics |= {t for t, a in list(_sent_addr.items()) if a == addr}
    for t in topics:
        mq_put(t, "", retain=True, qos=1)
        _ha_sent.pop(t, None)
        _sent_addr.pop(t, None)
        _disc_remote.pop(t, None)

    _purge_addr(addr, why)

    for d in (_avail_last_ok, _avail_last_bad, _avail_pub):
        d.pop(addr, None)
    mq_put(_avail_topic(addr), "", retain=True, qos=1)

    with _state_lock:
        _state["devices"].pop(addr, None)
        nd = _state.get("node_desc")
        if isinstance(nd, dict):
            nd.pop(addr, None)

    if tell_c6:
        try:
            ser.write(_js({"cmd": "dev_remove", "addr": addr}).encode())
            print(f"[→C6] dev_remove {addr}")
        except Exception as e:                          # noqa: BLE001
            print(f"[C6] dev_remove {addr} 没发出去：{e}")

    print(f"[Web] 已删除 {addr}：{len(topics)} 个 Discovery 配置，"
          f"状态 topic 和可用性一并清除（{why}）")
    return True


def _ha_learn_ieee(addr, ieee):
    """记住某个地址的 IEEE，并把它绑到已有的 Node-ID 上。

    这样设备以后以**新**短地址出现时，还能找回原来的实体
    （而不是又建一套）。旧地址会同时从 broker 里彻底清掉。

    每条带 ieee 的 zigbee 消息都会调它（on_off 上报也算）。所以只在真的
    有变化时才写文件 —— 否则每按一次键都要重写一遍 JSON。
    """
    if not ieee:
        return
    changed = _addr_ieee.get(addr) != ieee

    prev = _ieee_addr.get(ieee)
    if prev and prev != addr:
        prev_node = _addr_nodes.get(prev) or _ha_nodes.get(prev)
        if prev_node:
            _ieee_nodes[ieee] = prev_node      # 名字熬过换地址
            print(f"[HA] IEEE {ieee}: Kurzadresse {prev} → {addr}")
                # 我们学到的关于这台设备的东西继续有效 —— 只是挂在
                # 新地址下。否则面板每次 rejoin 之后，
                # 又会变成一次点击发三条命令。
            for s in (_noread, _ctrl):
                for k in [k for k in s if k.split("/", 1)[0] == prev]:
                    s.discard(k)
                    s.add(f"{addr}/{k.split('/', 1)[1]}")
            _purge_addr(prev, "设备换了新短地址")
        changed = True
    _ieee_addr[ieee] = addr

    _addr_ieee[addr] = ieee
    _ha_devices.setdefault(addr, {})["ieee"] = ieee
    node = _ha_nodes.get(addr) or _addr_nodes.get(addr)
    if node and _ieee_nodes.get(ieee) != node:
        _ieee_nodes[ieee] = node
        changed = True
    if changed:
        _nodes_save()


def _ha_node(addr):
    """Node-ID —— 按地址、也按 IEEE 保持稳定。

    顺序：
      1. 地址已认识（本次会话或文件里）→ 沿用这个名字。
         这样现有实体被冻结住；不迁移，也不产生孤儿。
      2. IEEE 已知且已分到过名字（设备换了地址回来）
         → 沿用**旧**的 Node-ID，并把新地址映射上去。
      3. 否则新建 —— 用**地址**，跟 1.2.0 之前一样。

    第 3 步为什么不用 IEEE？因为那会迁移现有安装：此前所有节点都叫
    c6_0x07da。若改成优先用 IEEE，更新之后**每台**设备都会拿到新的
    Node-ID —— 正好产生这个文件想避免的重复实体。IEEE 表只用来在换地址
    之后把已经发出去的名字找回来。
    """
    node = _ha_nodes.get(addr) or _addr_nodes.get(addr)
    if node:
        _ha_nodes[addr] = node
        return node

    ieee = _ha_devices.get(addr, {}).get("ieee") or _addr_ieee.get(addr, "")
    if ieee and ieee in _ieee_nodes:
        node = _ieee_nodes[ieee]
        print(f"[HA] {addr} 换了新短地址 ⇒ 现有实体"
              f"继续沿用 {node}（同一 IEEE）")
    else:
        node = "c6_" + _ha_slug(addr)

    _ha_nodes[addr]   = node
    _addr_nodes[addr] = node
    if ieee:
        _ieee_nodes[ieee] = node
        _addr_ieee[addr]  = ieee
        _ha_devices.setdefault(addr, {})["ieee"] = ieee
    _nodes_save()
    return node


def _ha_state(addr, sub, p, retain=True):
    """把状态发到 state_topic —— retained。

    以前是不带 retain 发的：HA 每次重启所有开关都回到 "unknown"，
    因为 broker 和 HA 都没有最后一个值。另外 _ha_last 会记住这个值，
    好让没有设备回报的 "toggle" 也能朝正确方向翻。
    """
    topic = f"{BASE}/zigbee/{addr}/{sub}"
    mq_put(topic, json.dumps(p), retain=retain, qos=1)
    if isinstance(p, dict):
        _ha_last.setdefault(addr, {})[sub] = p
    # 记下我们曾经设过哪些 topic —— 只有靠它，之后才能把一个
    # 旧短地址彻底清理干净（_purge_addr）。
    subs = _addr_subs.setdefault(addr, [])
    if sub not in subs:
        subs.append(sub)
        _nodes_save()

def _ha_device(addr):
    d  = _ha_devices.get(addr, {})
    db = d.get("db") or {}
    ids = ["c6_" + _ha_slug(addr)]
    if d.get("ieee"):
        ids.append("c6_" + _ha_slug(d["ieee"]))
    return {
        "identifiers": ids,
        "name": db.get("model") or d.get("model") or ("Zigbee " + addr),
        "manufacturer": db.get("vendor") or d.get("mfr") or "未知厂商",
        "model": db.get("model") or d.get("model") or "未知型号",
    }

# 端点名取自 Z2M 数据库（top/center/bottom/left/right …）→ 显示名
_EP_ZH = {"top": "上", "center": "中", "bottom": "下", "left": "左", "right": "右",
          "top_left": "左上", "top_right": "右上",
          "bottom_left": "左下", "bottom_right": "右下"}

def _ha_ep_label(addr, epn):
    """'ep16' → '上' wenn die Z2M-Definition diesen Endpunkt so benennt."""
    db = _ha_devices.get(addr, {}).get("db") or {}
    for name, num in db.get("endpoints", {}).items():
        if f"ep{num}" == epn:
            return _EP_ZH.get(name, name)
    return epn.upper()

# sub-Basisname → (HA-Komponente, Anzeigename, value_template, unit, device_class, state_class)
_HA_MAP = {
    "on_off":      ("switch",        "开关",   "{{ value_json.v }}",                                    None, None,           None),
    "temperature": ("sensor",        "温度",   "{{ (value_json.raw / 100) | round(1) }}",               "°C", "temperature", "measurement"),
    "humidity":    ("sensor",        "湿度",   "{{ (value_json.raw / 100) | round(1) }}",               "%",  "humidity",    "measurement"),
    "illuminance": ("sensor",        "光照度", "{{ (10 ** ((value_json.raw - 1) / 10000)) | round(1) }}", "lx", "illuminance", "measurement"),
    "occupancy":   ("binary_sensor", "占用",   "{{ value_json.occ }}",                                  None, "occupancy",   None),
    "ias_zone":    ("binary_sensor", "报警",   "{{ value_json.motion }}",                               None, None,          None),
}

def _ha_publish(addr, sub, p):
    """把 (addr, sub) 自动登记到 Home Assistant（MQTT Discovery）。

    每条进来的电文都会调用，但配置每个 (节点, 对象) 只发一次 ——
    带 retain，好熬过 HA 重启。不需要 YAML：HA 自己建实体。
    """
    if not HA_ENABLED:
        return

    # ★ discovery 里引用了 {BASE}/dev/<addr>/status 这条 availability 主题，
    #   如果主题还没消息，availability_mode="all" 会让实体一开始就是不可用。
    #   所以发 config 之前先把状态补上（retained；去抖，不会每帧都发）。
    _avail_sync(addr)

    base_name = sub.rsplit("/", 1)[-1]
    epn       = sub.split("/", 1)[0] if "/" in sub else "ep1"
    node      = _ha_node(addr)
    dev       = _ha_device(addr)
    topic     = f"{BASE}/zigbee/{addr}/{sub}"
        # 被判定为纯发送方的端点**不**再建开关 —— 否则下一帧电报会立刻
        # 把刚撤掉的开关卡片重新建出来（见 _ha_drop_switch）。
    if base_name == "on_off":
        try:
            _epn_num = int(epn[2:]) if epn.startswith("ep") else 1
        except ValueError:
            _epn_num = 1
        if _ep_key(addr, _epn_num) in _ctrl:
            return
        # 单端点设备的 "EP1" 只是名字里的噪声
    ep_label  = _ha_ep_label(addr, epn)
    ep_part   = "" if ep_label == "EP1" else ep_label + " "
    avail     = _ha_avail(addr)

    objects = []   # (component, object_id, config)

    if base_name == "battery":
        if "pct" in p:
            objects.append(("sensor", f"{epn}_battery_pct", {
                "name": f"{ep_part}电量",
                "has_entity_name": True,
                "state_topic": topic,
                "value_template": "{{ value_json.pct }}",
                "device_class": "battery", "unit_of_measurement": "%",
                "state_class": "measurement", **avail,
            }))
        if "voltage_mv" in p:
            objects.append(("sensor", f"{epn}_battery_volt", {
                "name": f"{ep_part}电池电压",
                "has_entity_name": True,
                "state_topic": topic,
                "value_template": "{{ value_json.voltage_mv }}",
                "device_class": "voltage", "unit_of_measurement": "mV",
                "state_class": "measurement", **avail,
            }))
    elif base_name in _HA_MAP:
        comp, label, tpl, unit, dc, sc = _HA_MAP[base_name]
            # "has_entity_name"：HA 会把设备名加在名字前面。原先设备名又重复
            # 出现在 name 字段里，结果出来是 "HGZB-043 HGZB-043 上 开关"。
            # 所以这里只写区分设备的那部分（端点 + 角色）。
        cfg = {
            "name": f"{ep_part}{label}",
            "has_entity_name": True,
            "state_topic": topic,
            "value_template": tpl,
            **avail,
        }
        if dc: cfg["device_class"] = dc
        if sc: cfg["state_class"]  = sc
        if unit: cfg["unit_of_measurement"] = unit
        if comp == "switch":
            cfg.update({
                "command_topic": f"{BASE}/set/{addr}/{epn}/on_off",
                "payload_on": "1", "payload_off": "0",
                "state_on": "1", "state_off": "0",
            })
        elif comp == "binary_sensor":
            cfg.update({"payload_on": "1", "payload_off": "0"})
        objects.append((comp, f"{epn}_{base_name}", cfg))

    for comp, obj_id, cfg in objects:
            # addr 也要一起记账：设备换了短地址时，state_topic/command_topic
            # 得重新发布，旧 topic 要彻底消失（_purge_addr）。
            # 到底发不发，由 _disc_send 按内容决定。
        cfg["unique_id"] = f"{node}_{obj_id}"
        cfg["device"]    = dev
        cfg["origin"]    = _GW_ORIGIN
            # HA 2025.10 用 default_entity_id 取代了 object_id；Z2M 两个都写，
            # 好让已有的实体 ID 熬过这次迁移。
        cfg["default_entity_id"] = f"{comp}.{_ha_slug(node)}_{_ha_slug(obj_id)}"
        _disc_send(comp, node, obj_id, cfg, addr)

def _db_kind_of(entry):
    """按设备库的 caps/description 归类：'panel' | 'maybe' | 'switch' | None。"""
    caps = entry.get("caps") or []
    desc = (entry.get("description") or "").lower()
    if ("scene" in desc or "selector" in desc) and ("switch" in caps or "action" in caps):
        return "panel"
    if "switch" in caps and "action" in caps:
        return "maybe"          # 有歧义，见 _ha_db_hint
    if "switch" in caps or "light" in caps:
        return "switch"
    return None


def _ha_db_family(model):
    """在 DB 里找同一系列 —— 给那些 DB 里没有的型号用。

    FTB56+ZSN18ZY1.1 不在 DB 里，FTB56+ZSN15HG1.0 和
    FTB56+ZSN16HG1.0 在（"Smart N key scene wall switch"）。用最长公共
    前缀就能把这套分类继承过来。

    前缀不能太短："FTB56+" 也会匹配到 FTB56+SKT1BCW1.0 —— 一个**带**继电器的
    插座。所以至少 9 个字符，而且只在整族一致同意时才用。
    """
    if not model:
        return None, []
    m = model.strip()
    for L in range(len(m) - 1, 8, -1):          # 9 Zeichen … len-1
        pre = m[:L]
        hits = [k for k in _ha_db if k.startswith(pre)]
        if hits:
            return pre, hits
    return None, []


def _ha_db_hint(addr):
    """Z2M 数据库对设备类型的说法 —— 只是**提示**。

    为什么不下结论：DB 只靠描述文字来区分 "Smart 2 key scene wall switch"
    和真正的开关。几个看着好用的字段都不行（2026-10-17 拿全部 3188 条验过）：

      * caps 里含 "action"：31 条命中，其中包括 MEAZON_BIZY_PLUG、
        DinRail 单相电表、Power switch S1、LK FUGA wiser 无线双联继电器 ——
        全都是有继电器的设备。按它判会把它们误判成情景面板。
      * endpoints 为空：219 个纯开关型号没有端点映射，但它们确实能开关
        （只是 Z2M 没写出来）。
      * components=switch：618 个型号，其中只有 7 个在文字里带 "scene"。

    而这里说的这台设备（FTB56+ZSN18ZY1.1）压根不在 DB 里 ——
    所以才绕道 _ha_db_family() 走系列。最终裁决仍然靠 Descriptor/探测，
    永远不看这段文字。
    """
    d = _ha_devices.get(addr, {})
    model = (d.get("model") or "").strip()
    entry = d.get("db") or _ha_db.get(model) or {}
    if entry:
        return _db_kind_of(entry), \
            f"DB: „{entry.get('description')}“ ({entry.get('model')})"

    pre, hits = _ha_db_family(model)
    if not pre:
        return None, ""
    kinds = {_db_kind_of(_ha_db[h]) for h in hits}
    kinds.discard(None)
    names = ", ".join(sorted(hits))[:56]
    if kinds == {"panel"}:
        return "panel", f"数据库同族「{pre}*」（只有情景面板：{names}）"
    if kinds and kinds <= {"panel", "maybe"}:
        return "maybe", f"DB-Familie „{pre}*“ (Panel-Verdacht: {names})"
    return None, f"数据库同族「{pre}*」混杂 {sorted(kinds)} —— 不作为判据"


def _ha_prepublish(addr):
    """把 Z2M DB 里已知的端点立刻登记上去，让设备
    刚入网就出现在 HA 里 —— 而不是等到第一次按键。
    只在有明确端点表时才做，否则会造出幽灵实体。"""
    db  = _ha_devices.get(addr, {}).get("db") or {}
    eps = db.get("endpoints") or {}
    if not eps or "switch" not in (db.get("caps") or []):
        return
    for _name, num in sorted(eps.items(), key=lambda kv: kv[1]):
        _ha_publish(addr, f"ep{num}/on_off", {"v": 0})

def _ha_forget(addr):
    """丢弃某台设备的 discovery 缓存（比如读过新的 Basic 之后），
    好让实体带着正确的型号名重新登记。"""
    node = _ha_node(addr)
    for t in [t for t in _ha_sent if f"/{node}/" in t]:
        _ha_sent.pop(t, None)


# ── 按键事件 ───────────────────────────────────────────────────────────────
# C6 把**每一个**收到的 APS 帧都原样输出成 {"t":"aps", …,"hex":…}。有了它，
# 按键就能在 add-on 里解码，C6 侧不必再写簇相关的接收逻辑：
#
#   cluster 0x0006 (On/Off)，帧类型 0x01（簇相关），
#   方向 Client→Server（0x08 位**未**置位），cmd 0x00/0x01/0x02
#   cluster 0x0005 (Scenes)，cmd 0x05 (recall)，负载 = 组(2) + 场景(1)
#
# 设备回的 Default-Response（cmd 0x0b）置了 0x08 位，因此永远不会被当成
# 按键 —— 否则我们自己每发一条命令都会触发一次按键事件。
_ONOFF_ACT = {0x00: "off", 0x01: "on", 0x02: "toggle"}


def _event_types():
    """HA event 实体允许的取值。

    ★ 只要值**不**在 `event_types` 里，HA 就丢弃该事件
    （日志里写 "Invalid event type"，实体保持不变）。所以这张表必须事先
    包含面板可能发出的所有值 —— 事后没法扩充，除非重建实体。

    这些值的来源是设备的上游转换器：
    `FTB56+ZSN15HG1.0` / `FTB56+ZSN16HG1.0`（nue_3a.ts）用的是
    `fromZigbee: [fz.command_recall]` → `action = "recall_<sceneid>"`。
    两键操作时，区分按键靠的是**场景 ID**；端点号则另外区分两个翘板。
    """
    t = ["on", "off", "toggle", "scene"]        # "scene" = Altbestand (1.2.2)
    t += ["recall_%d" % i for i in range(32)]   # genScenes recall
    t += ["store_%d" % i for i in range(32)]    # genScenes store
    return t


_EVENT_TYPES = _event_types()
ESP_ZB_CL_ON_OFF = 0x0006     # ZCL-Cluster On/Off
ESP_ZB_CL_SCENES = 0x0005     # ZCL-Cluster Scenes

# HA 分类：角色就是"开关"的 Device-ID（…Switch）。带这种 ID 的设备是
# 发送方，不是执行器。故意留得很窄，只当第二特征用 —— 这里判错也不会
# 删掉一个能用的开关，因为还额外要求"不可回读"。
_SWITCH_DEV_IDS = {0x0005, 0x0008, 0x0009, 0x000A}


def _ha_event_sub(ep):
    """命名规则到处一致：EP1 不带前缀。"""
    return f"ep{ep}/action" if ep > 1 else "action"


# ★ HA 要求 event 实体的负载是带 event_type 元素的 JSON
#   （MQTT Event 文档原文："The JSON payload should contain the
#   event_type element"）。裸的 "on" 不是 JSON → HA 会**静默**丢弃
#   （实体停在"未知"）。当初卡片什么都不显示、可消息明明已经到 broker，
#   原因就是这个。
#
# 不去重：重复事件（一次按键两帧）是设备的**故意设计** —— 用户 2026-10-17
# 亲口确认：极短的一次触碰不会触发翻转，但照样会上报。所以第二条一模一样
# 的事件是真实事件，不是假信号。
def _action_payload(act):
    return json.dumps({"event_type": act})


def _ha_publish_event(addr, ep):
    """给某个端点注册事件实体（按键）。"""
    if not HA_ENABLED:
        return
    _avail_sync(addr)          # availability 主题要先存在，否则实体一开始不可用
    epn    = f"ep{ep}" if ep > 1 else "ep1"
    label  = _ha_ep_label(addr, epn)
    ep_part = "" if label == "EP1" else label + " "
    node   = _ha_node(addr)
    obj_id = f"{epn}_action"
    cfg = {
        "name": f"{ep_part}按键",
        "has_entity_name": True,
        "state_topic": f"{BASE}/zigbee/{addr}/{_ha_event_sub(ep)}",
        "event_types": list(_EVENT_TYPES),
        "device_class": "button",
        "unique_id": f"{node}_{obj_id}",
        "device": _ha_device(addr),
        "origin": _GW_ORIGIN,
        "default_entity_id": f"event.{_ha_slug(node)}_{_ha_slug(obj_id)}",
        **_ha_avail(addr),
    }
    _disc_send("event", node, obj_id, cfg, addr)


def _ha_button_from_report(addr, sub, p, was_expected=False):
    """从 on/off 的 **Report** 推导按键。

    ★ 2026-10-17 18:53 在 0x747d（FTB56+ZSN18ZY1.1）上实测：按下时
    面板报的**不是**上游转换器里写的那个场景
    （`fromZigbee: [fz.command_recall]`），而是受影响端点的 OnOff 属性
    本身 —— ep2 → `{"v": 1}`，2 s 后 ep1 → `{"v": 1}`。

    Report 的帧类型是 0（通用）、方向是 Server→Client。
    `_ha_aps_action` 故意把这两种都丢掉（否则每条对我们命令的应答都会
    被当成按键）。也正因此按键在那里从来没出现过 —— 它根本不是命令报文。

    只对判为纯发送方的端点做（`_ctrl`）：对真开关来说，Report 是状态变化，
    不是按键，不能产生事件。
    """
    if not HA_ENABLED or not sub.endswith("on_off"):
        return
    ep = _sub_ep(sub)
    if ep is None:
        return
    k = _ep_key(addr, ep)
    if k not in _ctrl:
        # 第二条路：设备已经作为面板登记过了（_ctrl 里任一端点），**并且**
        # 型号族一致认定为情景面板。这覆盖了 Simple-Descriptor 只给了一个
        # 端点、而另一个翘板照样报按键的情况。族条件很严（2/2 一致），
        # 免得真继电器被误判成按键源。
        if _ha_db_hint(addr)[0] != "panel":
            return
        if not any(x.split("/", 1)[0] == addr for x in _ctrl):
            return
    if was_expected:
        # 是在回答我们自己的 Read-Attributes → 不算按键
        return
    v = (p or {}).get("v")
    if v is None:
        return
    act = "on" if int(v) else "off"
    topic = f"{BASE}/zigbee/{addr}/{_ha_event_sub(ep)}"
    mq_put(topic, _action_payload(act), retain=False, qos=1)
    print(f"[MQTT] {topic} {act}   （来自上报的按键 ep{ep}）")
    _ha_publish_event(addr, ep)


def _ha_drop_switch(addr, ep, why):
    """把纯发送方的 switch 实体撤掉。

    没有继电器的面板接受 On/Off 命令、回 SUCCESS，然后什么也不做 ——
    HA 里那张 switch 卡片就是在承诺一件不存在的事。所以撤掉它，
    换成 event 实体。
    想恢复：把 /data/ha_nodes.json 里 "ctrl" 下的 "addr/ep" 删掉，
    重启 add-on。
    """
    if not HA_ENABLED:
        return
    epn    = f"ep{ep}" if ep > 1 else "ep1"
    node   = _ha_node(addr)
    obj_id = f"{epn}_on_off"
    topic  = f"{HA_PREFIX}/switch/{node}/{obj_id}/config"
    mq_put(topic, "", retain=True, qos=1)
    _ha_sent.pop(topic, None)
    _sent_addr.pop(topic, None)
    print(f"[HA] {addr} ep{ep}: switch/{node}/{obj_id} 已注销 – {why}")


_dropped = set()       # "addr/ep" – 本次运行期已注销的 switch 实体


def _classify(addr, ep, why=""):
    """从两个特征推出正确的实体形态。

    规则（两个特征都自动采集，没有一个是手填的）：

      A  OnOff 属性可回读        → 真开关（switch）
      B  设备自己发按键命令      → 纯发送方（event）
      B + 不可回读（A 不成立）   → 无继电器面板：撤掉 switch
      B + 回读没问题             → 带继电器的设备，同时又发按键：
                                   switch 和 event 都留着

    调用点：_ha_mark_controller（新增特征 B）、_noread_add
    （特征 A 被推翻）和 _classify_from_desc（Simple-Descriptor）。
    """
    k = _ep_key(addr, ep)
    if k not in _ctrl:
        return
    _ha_publish_event(addr, ep)
    if k in _noread and k not in _dropped:
        _dropped.add(k)
        _ha_drop_switch(addr, ep, why or "设备是纯发送端（不可回读，"
                                        "但会发自己的按键命令）")


def _ha_mark_controller(addr, ep):
    """把端点判定为纯发送端（判据 B）。"""
    k = _ep_key(addr, ep)
    if k in _ctrl:
        _classify(addr, ep)
        return
    _ctrl.add(k)
    _nodes_save()
    if k in _noread:
        print(f"[HA] {addr} ep{ep}：会发自有的按键命令且 "
              f"不可回读 ⇒ 无继电器按键面板")
    else:
        print(f"[HA] {addr} ep{ep}：会发自有按键命令 —— "
              f"按按键注册（保留开关，回读可用）")
    _classify(addr, ep)


# ── 入网时自动分级 ───────────────────────────────────────────────────────
# 两个互相独立的来源，任何一项都不需要人工裁决：
#
#  1. Simple-Descriptor（配对时下发）：0x0006 出现在 INPUT 列表里，
#     说明设备是 On/Off 的 Server = 真开关。
#     0x0006/0x0005 只出现在 OUTPUT 列表里，说明它只能**发**命令 = 按键面板。
#  2. 探测：每个端点发一次 Read-Attributes。有值回来 → 开关。
#     什么都没回 → 根本没有这个属性 → 面板。
#
# 来源 1 明确，但只在配对时来一次。来源 2 一直跑，能把错判纠回来
# （见 _pending_ack → _noread_del）。
_PROBE_AFTER = 2.5     # 秒，从 "interview done" 算起
_PROBE_WAIT  = 4.0     # s，等回应的超时
_probe = {}            # (addr, ep) → 到期时间
_desc  = {}            # addr → {ep: (in_cluster, out_cluster)}，来自 Simple-Desc


def _probe_one(addr, ep):
    key = (addr, ep)
    with _pending_lock:
        if key in _probe:
            return
        _probe[key] = time.time() + _PROBE_WAIT
    _send_read_onoff(addr, ep)
    threading.Timer(_PROBE_WAIT + 0.2, _probe_done, (addr, ep)).start()


def _probe_start(addr, eps):
    """只探测带 On/Off 簇的端点。

    温度传感器没有 OnOff 属性；去问它，得到的只会是"不可回读"，
    还会让日志被一堆对它毫无意义的消息淹掉。Simple-Descriptor 事先就说了
    谁身上有 On/Off —— 只查这些。
    """
    if not eps:
        return
    known = _desc.get(addr) or {}
    hint, _why = _ha_db_hint(addr)
    todo  = []
    for ep in sorted(set(int(e) for e in eps)):
        if ep <= 0:
            continue
        cl = known.get(ep)
        if cl is None:
            # 没有 Descriptor（配对没走完）。只有 DB 把它标成面板时
            # 才照着算，否则不要猜。
            if hint == "panel":
                todo.append(ep)
            continue
        if ESP_ZB_CL_ON_OFF in cl[0] or ESP_ZB_CL_ON_OFF in cl[1]:
            todo.append(ep)
    if not todo:
        return
    print(f"[HA] {addr}：检查 {todo} 上的 OnOff 是否可回读")
    for i, ep in enumerate(todo):
        threading.Timer(_PROBE_AFTER + i * 0.4, _probe_one, (addr, ep)).start()


def _probe_seen(addr, ep):
    """收到了真实状态 ⇒ 这个端点可读。"""
    key = (addr, ep)
    with _pending_lock:
        hit = _probe.pop(key, None)
    if hit is not None:
        print(f"[HA] {addr} ep{ep}：会回 Read Attributes ⇒ 真开关")
    if _ep_key(addr, ep) in _noread:
        _noread_del(addr, ep)


def _probe_done(addr, ep):
    key = (addr, ep)
    with _pending_lock:
        if key not in _probe:
            return                       # 说明应答来了
        _probe.pop(key, None)
    _noread_add(addr, ep, "入网时 OnOff 属性读不到")


# ── 型号补读（ZCL Basic 0x0004 厂商 / 0x0005 型号）────────────────────────
# ★ 为什么需要：型号**只在入网那一刻**（interview）读一次，读完网关就把
#   interviewed=true 写进 NVS，之后永远不会再读。设备要是那一次没答上
#   （休眠端点在睡、刚入网就抖、或者当年固件还没有「逐端点扫 Basic」这段），
#   型号就**永久缺失**，而且没有任何重试。
#   实测（2026-10-26）：经中继的 0x159e 就是这样 —— 它不是通讯不通，
#   手动发一条 probe 型号立刻回来（Feibit Inc co. / FB56+ZSW1GKJ2.5），
#   纯粹是当年那次没读到、之后没人再问。
#   所以这里定期给「有 cluster 数据但没有 basic」的设备补一次 probe，
#   指数退避（90 s 起，最长 6 h 一轮），读到就自动停。
_MB_FIRST   = 90          # s，第一次失败后多久重试
_MB_MAXWAIT = 6 * 3600    # 最长退避
_MB_PER_ROUND = 3         # 每轮最多补几台，别把串口刷满
_mb_next  = {}            # addr → 下次允许尝试的 ts
_mb_tries = {}            # addr → 累计尝试次数


def _eps_of(dev):
    """从 cluster 键（`ep16/on_off`）抠出端点号；抠不到就按 ep1 试。"""
    eps = []
    for k in (dev.get("clusters") or {}):
        if k.startswith("ep"):
            try:
                eps.append(int(k[2:].split("/")[0]))
            except ValueError:
                pass
    return sorted(set(eps)) or [1]


def _model_missing(dev):
    b = (dev.get("clusters") or {}).get("basic")
    if isinstance(b, dict):
        if ((b.get("payload") or {}).get("model") or "").strip():
            return False
    return True


def _probe_basic(addr, eps=None):
    """让网关读一次 Basic（0x0004/0x0005）。逐端点、间隔 0.9 s 发。"""
    with _state_lock:
        dev = json.loads(json.dumps((_state.get("devices") or {}).get(addr) or {}))
    eps = sorted(set(int(e) for e in (eps or _eps_of(dev))))
    for i, ep in enumerate(eps):
        threading.Timer(i * 0.9, lambda e=ep: mq_put(
            f"{BASE}/cmd/probe", f"{addr},{e}", qos=1)).start()
    print(f"[PROBE] {addr} 读 Basic（厂商/型号）@ ep{eps}")
    return eps


def _model_backfill_once():
    now = time.time()
    todo = []
    with _state_lock:
        for a, d in (_state.get("devices") or {}).items():
            if not _model_missing(d):
                _mb_next.pop(a, None)     # 读到了，清掉退避状态
                _mb_tries.pop(a, None)
                continue
            if now < _mb_next.get(a, 0):
                continue
            todo.append(a)
    for a in todo[:_MB_PER_ROUND]:
        try:
            _probe_basic(a)
        except Exception as e:             # noqa: BLE001
            print(f"[PROBE] {a}: {e}")
            continue
        n = _mb_tries.get(a, 0) + 1
        _mb_tries[a] = n
        _mb_next[a]  = now + min(_MB_MAXWAIT, _MB_FIRST * (2 ** (n - 1)))
    if todo:
        print(f"[PROBE] 缺型号 {len(todo)} 台，本轮补 {min(len(todo), _MB_PER_ROUND)} 台")


def _model_backfill_task():
    time.sleep(45)          # 等串口就绪、设备表从 NVS 载入完
    while True:
        try:
            _model_backfill_once()
        except Exception as e:             # noqa: BLE001
            print(f"[PROBE] backfill: {e}")
        time.sleep(180)


def _classify_from_desc(addr, ep, in_cl, out_cl, dev_id=0):
    """按 Simple-Descriptor 分级（明确，配对时下发）。

    Descriptor 是 Zigbee 设备关于自己最可靠的说明 —— 比厂商/型号更可靠，
    因为它来自 ZDO 配对流程，而不是某个文本数据库：
        profile_id、device_id、Input 簇（Server）、Output 簇（Client）。

    关键在簇 0x0006 的**方向**：
        Input/Server  → 设备持有 On/Off 状态（可开关）
        Output/Client → 设备**发** On/Off，自己没有状态
    另外会把 Device-ID 一并记下；当簇列表说明不了问题时，
    它作为第二特征使用。
    """
    k = _ep_key(addr, ep)
    # 给配对探测记下谁身上有 On/Off。
    _desc.setdefault(addr, {})[ep] = (list(in_cl), list(out_cl))
    if ESP_ZB_CL_ON_OFF in in_cl:
        # On/Off 是 Server 簇：设备持有状态 → 是开关。
        if k in _ctrl:
            _ctrl.discard(k)
            _nodes_save()
            print(f"[HA] {addr} ep{ep}：On/Off 是 Server 簇 ⇒ "
                  f"确实是真开关（推翻原判定）")
        _noread_del(addr, ep)
        return
    if (ESP_ZB_CL_ON_OFF in out_cl or ESP_ZB_CL_SCENES in out_cl
            or dev_id in _SWITCH_DEV_IDS):
        _what = ("On/Off 只在 Client 簇" if ESP_ZB_CL_ON_OFF in out_cl
                 else "Scenes 只在 Client 簇" if ESP_ZB_CL_SCENES in out_cl
                 else "Device ID 属开关类")
        print(f"[HA] {addr} ep{ep}: {_what} "
              f"(device=0x{dev_id:04x}, in={in_cl}, out={out_cl}) → Tastenpanel")
        _ha_mark_controller(addr, ep)


def _zcl_head(b):
    """拆 ZCL 头 → (fc, mfr, tsn, cmd, payload_start)，失败返回 None。

    ★ Frame-Control 第 2 位（0x04）= 厂商专有。此时 2 字节厂商码
    夹在 Frame-Control 和 TSN **之间**：

        fc(1) | mfr(2, 低位在前) | tsn(1) | cmd(1) | payload…

    早先的版本一律从 `b[2]` 读 `cmd`。对厂商专有帧来说那是厂商码的
    低字节 —— 所以几乎永远不是 0x00/0x01/0x02（On/Off）或 0x05
    （Scene recall）。结果：按键被**静默丢弃**，日志一行都没有。
    这正好对上"按键读不到"的症状。
    """
    if len(b) < 3:
        return None
    fc = b[0]
    i = 1
    mfr = None
    if fc & 0x04:                      # manufacturer specific
        if len(b) < 5:
            return None
        mfr = b[1] | (b[2] << 8)
        i = 3
    if len(b) < i + 2:
        return None
    return fc, mfr, b[i], b[i + 1], i + 2


_aps_unbekannt = set()   # (cl, ftype, cmd) —— 已经记过日志（免得刷屏）


def _ha_aps_action(msg):
    """从原始 APS 帧里识别按键，并送到 HA。"""
    if not HA_ENABLED:
        return
    hx = msg.get("hex") or ""
    if len(hx) < 6:
        return
    try:
        b = bytes.fromhex(hx)
    except ValueError:
        return
    head = _zcl_head(b)
    if not head:
        return
    fc, mfr, tsn, cmd, ps = head
    if fc & 0x08:            # Server→Client = 在回答我们的命令
        return
    ftype = fc & 0x03        # 0 = allgemein, 1 = clusterspezifisch
    cl   = int(str(msg.get("cl") or "0"), 0)
    sep  = int(msg.get("sep") or 0)
    addr = msg.get("src") or ""
    if not addr or sep == 0:
        return
    act = None
    if cl == ESP_ZB_CL_ON_OFF and ftype == 0x01 and cmd in _ONOFF_ACT:
        act = _ONOFF_ACT[cmd]
    elif cl == ESP_ZB_CL_SCENES and ftype == 0x01 and len(b) >= ps + 3:
        # recall(0x05) / store(0x04): groupid(2, LSB first) + sceneid(1)
        grp = b[ps] | (b[ps + 1] << 8)
        sid = b[ps + 2]
        if cmd == 0x05:
            act = "recall_%d" % sid
        elif cmd == 0x04:
            act = "store_%d" % sid
        if act:
            print(f"[HA] {addr} ep{sep}: Szene {'recall' if cmd == 0x05 else 'store'}"
                  f" – Gruppe 0x{grp:04x}, Szene {sid}"
                  + (f", Herstellercode 0x{mfr:04x}" if mfr is not None else ""))
    if not act:
        # 被判为发送方的端点，本来就该有按键。认不出来的消息**只**记一次日志 ——
        # 否则下一条报错又变成瞎子。
        if _ep_key(addr, sep) in _ctrl or _ha_db_hint(addr)[0] == "panel":
            key = (cl, ftype, cmd)
            if key not in _aps_unbekannt:
                _aps_unbekannt.add(key)
                print(f"[HA] {addr} ep{sep}：无法识别的按键消息 "
                      f"cl=0x{cl:04x} type={ftype} cmd=0x{cmd:02x} "
                      f"mfr={'0x%04x' % mfr if mfr is not None else '-'} "
                      f"hex={hx} —— 不解析")
        return
    topic = f"{BASE}/zigbee/{addr}/{_ha_event_sub(sep)}"
    mq_put(topic, _action_payload(act), retain=False, qos=1)
    print(f"[MQTT] {topic} {act}")
    _ha_publish_event(addr, sep)
    _ha_mark_controller(addr, sep)

# ── Dispatcher ─────────────────────────────────────────────────────────────
# ── 网络拓扑：直连还是经中继 ───────────────────────────────────────────────
# 数据来自协调器的三张表（见 main/zb_topo.c）：
#   nbr 邻居表  = 协调器直接听得到谁（含 LQI/RSSI/关系/深度）
#   rt  路由表  = 到某地址的**下一跳**（最可靠的「经谁转发」证据）
#   rr  源路由  = 从协调器到目的的完整路径（多级中继）
#
# ★ 判读优先级：rt.next_hop > rr.path[0] > nbr.relationship。
#   原因：路由表是转发时真正用到的表项；邻居表只说明「听得到」，不代表
#   那台设备的父节点就是协调器（路由器的邻居关系是 sibling，不等于父子）。
#
# ★ Zigbee 是按需建路，路由表只保留近期用过的路径（几十秒后过期）。
#   所以刷新拓扑时要先给每台设备发一帧（node_desc）逼出路由，再 dump。

_TOPO_MAXW = 980          # SVG 画布最大宽
_TOPO_MINS = 440          # 节点很少时也别缩得太窄
_TOPO_PAD  = 56           # 左右留白（圆半径 + 型号文字外扩）
_TOPO_XGAP = 150          # 相邻同级节点的最小水平间距
_TOPO_YGAP = 130          # 层间距


def _topo_ingest(kind, msg):
    """把网关发来的四类表 + 汇总头收进 _state["topo"]。

    topo_mesh = 每台设备**自己上报**的邻居表（Mgmt_Lqi_rsp），这是拓扑的
    主证据：里面 rel=0 那条就是它的父节点，lqi 是这一跳的链路质量。
    其余三张表是协调器 NIB 的快照，只当兜底。
    """
    with _state_lock:
        tp = _state["topo"]
        if kind == "topo_nbr":
            tp["nbr"] = msg.get("n") or []
            # ★ 邻居表 = 协调器**刚刚听见的**名单。顺手在这里"种设备"：
            #   进程重启后设备表是空的（只由上报驱动），用户会以为"设备没了"。
            #   邻居表里有的先挂个占位，等它真上报再把 clusters 补齐。
            coord = tp.get("addr") or "0x0000"
            for n in tp["nbr"]:
                a = n.get("a")
                if not a or a == coord:
                    continue
                _avail_touch(a)          # 协调器刚听见它 = 存活的硬证据
                dev = _state["devices"].setdefault(a, {
                    "ieee": "", "name": a, "last_seen": "",
                    "clusters": {}, "synthetic": True,
                })
                # 邻居表里的 lqi/rssi 是**这一条链路**的实测值，比心跳里那个
                # "radio 最近一帧"可信。设备自己没有有效值时就用它补上。
                if dev.get("lqi") in (None, 0) and "lqi" in n:
                    dev["lqi"] = n["lqi"]
                    dev["lqi_src"] = "nbr"
                if dev.get("rssi") is None and "rssi" in n:
                    dev["rssi"] = n["rssi"]
        elif kind == "topo_rt":
            tp["rt"] = msg.get("n") or []
        elif kind == "topo_rr":
            tp["rr"] = msg.get("n") or []
        elif kind == "topo_mesh":
            a = msg.get("a")
            if a:
                _avail_touch(a)              # 它自己答了 Mgmt_Lqi = 活着
                tp.setdefault("mesh", {})[a] = {
                    "st":  msg.get("st", 0),
                    "tot": msg.get("tot", 0),
                    "nb":  msg.get("nb") or [],
                    "rd":  tp.get("round", 0),   # 本轮序号，收尾时用来淘汰旧条目
                }
        else:
            tp["pan"]  = msg.get("pan", tp["pan"])
            tp["ch"]   = msg.get("ch", tp["ch"])
            tp["addr"] = msg.get("addr", tp["addr"])
            if msg.get("done"):
                # ★ 只在「设备真的不在了」时才剔掉自报表。以前按「本轮有没有
                #   报到」剔，但 Mgmt_Lqi 的回包比汇总消息慢好几秒（实测 4 台
                #   里有 3 台在 done 之后才到）⇒ 每轮都把 4 台砍成 1 台，
                #   拓扑图上一半的连线来回闪。现在只清掉已经从设备表消失的地址。
                mesh = tp.get("mesh") or {}
                keep_addrs = set((_state.get("devices") or {}).keys())
                kept = {a: e for a, e in mesh.items() if a in keep_addrs}
                dropped = [a for a in mesh if a not in kept]
                tp["mesh"] = kept
                tp["done"] = True
                tp["ts"]   = _now()
                tp["done_at"] = time.time()   # 供拓扑页显示「几秒前采的」
                print("[TOPO] 邻居 %d / 路由 %d / 源路由 %d / 设备自报 %d%s"
                      % (len(tp["nbr"]), len(tp["rt"]), len(tp["rr"]),
                         len(tp["mesh"]),
                         ("（设备已不在表中、已剔除: %s）" % " ".join(dropped))
                         if dropped else ""))


def _topo_refresh(wait_per_dev=0.45):
    """让网关逐台做 Mgmt_Lqi 采集（顺便先发 node_desc 触发路由发现）。

    串口写不阻塞 HTTP 线程 → 整个流程放后台线程跑。
    """
    # ★ done=False 必须在**起线程之前**同步置好。自动采集靠轮询 done 判断
    #   本轮是否结束；如果还留在 _run() 里异步置位，"刚起线程就去检查"会读到
    #   上一轮遗留的 True，误判成本轮已完成 → 采集会越叠越多。
    with _state_lock:
        addrs = sorted(_state["devices"].keys())
        # 新一轮采集：**不清空 mesh**，只把序号 +1。旧条目还留着（图不会闪成
        # 空白），等本轮 done 时再淘汰没被重新采到的 —— 见 _topo_ingest()。
        _state["topo"]["round"] = int(_state["topo"].get("round", 0)) + 1
        _state["topo"]["done"] = False

    def _run():
        for a in addrs:
            try:
                ser.write(_js({"cmd": "node_desc", "addr": a}).encode())
            except Exception as e:                     # noqa: BLE001
                print(f"[TOPO] node_desc {a} 失败: {e}")
                break
            time.sleep(wait_per_dev)
        try:
            delay = int(len(addrs) * wait_per_dev * 1000) + 1200
            ser.write(_js({"cmd": "topo", "delay": delay}).encode())
            print(f"[TOPO] 已请求拓扑（{len(addrs)} 台，等 {delay} ms 后逐台采集）")
        except Exception as e:                         # noqa: BLE001
            print(f"[TOPO] topo 请求失败: {e}")

    threading.Thread(target=_run, daemon=True).start()


# ── 定时自动采集拓扑 ────────────────────────────────────────────────────────
# 为什么放服务端、而不是页面里 setInterval：
#   拓扑页自己有 15 s 整页重载，但重载只重新读 _state["topo"]，
#   **不会触发新的采集** —— 所以图上永远是旧数据，除非人手点一次刷新。
#   在前端放计时器也不行：15 s 的重载会把计时器一起清掉，永远到不了 60 s。
#   放在服务端就没这个矛盾，页面照常重载就能看到新值（效果类似 HA 的 BLE 拓扑）。
_topo_busy = threading.Lock()


def _topo_refresh_blocking(timeout: float = 40.0) -> bool:
    """触发一轮采集并等它结束；上一轮还没完就跳过，避免采集叠加。"""
    if not _topo_busy.acquire(blocking=False):
        print("[TOPO] 上一轮还没结束，跳过本轮自动采集")
        return False
    try:
        _topo_refresh()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with _state_lock:
                if _state["topo"].get("done"):
                    return True
            time.sleep(0.5)
        print("[TOPO] 自动采集超时（done 没置位）")
        return False
    finally:
        _topo_busy.release()


def _topo_auto_ready() -> str:
    """本轮能不能采。返回 "" = 可以，否则返回跳过原因（只用于日志）。"""
    with _state_lock:
        st    = _state.get("status")
        coord = (_state.get("topo") or {}).get("addr") or "0x0000"
        # 别把协调器自己算成"一台设备"
        n     = len([a for a in (_state.get("devices") or {}) if a != coord])
    if st != "online":
        return f"网关状态 {st!r}，还没上线"
    if n == 0:
        return "还没有任何设备上报，扫了也是空图"
    return ""


def _topo_auto_loop(interval: int):
    # 开机先等一轮心跳：否则 _state["devices"] 还是空的，会扫 0 台设备，
    # 而且要等满一个 interval 才有第一张图。
    time.sleep(min(interval, 30))
    while True:
        try:
            why = _topo_auto_ready()
            if why:
                print(f"[TOPO] 本轮跳过：{why}")
            elif _topo_refresh_blocking():
                print(f"[TOPO] 自动采集完成，{interval} s 后再来一轮")
        except Exception as e:                          # noqa: BLE001
            print(f"[TOPO] 自动采集出错: {e}")
        time.sleep(interval)


def _topo_links(tp):
    """把所有已知的「谁听得见谁」汇总成无向链路图。

    返回 {(a, b)} → {"lqi": int|None, "n": 见到这条链路的次数}，地址对按
    字典序归一（a < b），所以同一条链路从两端各报一次只会合并成一个键，
    n == 2 就说明**双方互相都听得见**（比单方面汇报可信）。

    来源：
      · 协调器自己的邻居表（它直接听得到的设备）
      · 每台设备自报的邻居表（Mgmt_Lqi_rsp）

    ★ 为什么不用 rel / depth 直接读父节点：实测（2026-10-26）本网络的
      Feibit 开关回 0x84 NOT_SUPPORTED（根本不支持 Mgmt_Lqi），而支持它的
      ESP32-C6 中继虽然答了，但 rel 一律是 2(sibling)、depth 一律是 0 ——
      ZBOSS 没维护这两个字段。所以只能退回「实测链路 + 图搜索」，
      这条路不依赖任何设备自愿汇报父子关系。
    """
    coord = tp.get("addr") or "0x0000"
    links = {}

    def _add(x, y, lqi):
        if not x or not y or x == y:
            return
        k = (x, y) if x < y else (y, x)
        e = links.setdefault(k, {"lqi": None, "n": 0})
        e["n"] += 1
        if isinstance(lqi, int) and lqi > 0:
            e["lqi"] = lqi if e["lqi"] is None else max(e["lqi"], lqi)

    for n in tp.get("nbr") or []:
        _add(coord, n.get("a"), n.get("lqi"))
    for a, m in (tp.get("mesh") or {}).items():
        for e in m.get("nb") or []:
            _add(a, e.get("a"), e.get("lqi"))
    return links


def _topo_rel(tp, claims=None):
    """算出 addr → 父节点。

    返回 (parent, how, hop_lqi, depth, links)。

    算法（每次刷新都重算，纯函数，没有隐藏状态）：
      ① 在**实测链路图**上从协调器做 BFS：父节点 = 深度最小的可达邻居，
         同一层里挑 **LQI 最高**的那条（链路越好越可能真的在用它转发）。
      ② 设备**明确自报** rel==0 的父节点，只在「更靠近协调器」时才用来改写
         ①的结果 —— 自报值不校验会造出环（实测用例：某路由器自报自己的
         终端子节点是爹，整棵树倒挂）。
      ③ 完全不可达的设备 → 不在 parent 里，页面显示「未定位」。
      ④ `claims`（中继固件自报的父节点，0xFC00/0x0002）优先级最高，
         直接改写 ①，只做防环校验。

    ★ 都不依赖路由表。旧实现死磕路由表和邻居表，而这两张表按 Zigbee
      的设计就拿不到父子关系：邻居表只装一跳，路由表按需建、直接邻居
      根本不建表项（`rt` 常为 0）。
    """
    coord = tp.get("addr") or "0x0000"
    parent, how, hop_lqi, depth = {}, {}, {}, {}
    mesh = tp.get("mesh") or {}
    links = _topo_links(tp)

    # ① 建邻接表（LQI 高的在前 = 同层择优），从协调器 BFS 出生成树
    adj = {}
    for (x, y), e in links.items():
        adj.setdefault(x, []).append((y, e))
        adj.setdefault(y, []).append((x, e))
    for v in adj.values():
        v.sort(key=lambda t: (t[1].get("lqi") or 0), reverse=True)

    depth[coord] = 0
    queue = [coord]
    seen = {coord}
    while queue:
        cur = queue.pop(0)
        for nxt, e in adj.get(cur, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            parent[nxt]  = cur
            how[nxt]     = "链路图·直连" if cur == coord else "链路图·经中继"
            hop_lqi[nxt] = e.get("lqi")
            depth[nxt]   = depth.get(cur, 0) + 1
            queue.append(nxt)

    # ② 设备自报的父节点只能「改写」已测到的父节点，必须更靠近协调器
    for a, m in mesh.items():
        if not a or a == coord or a not in depth:
            continue
        for e in m.get("nb") or []:
            p = e.get("a")
            if (e.get("rel") == 0 and p and p != a
                    and p in depth and depth[p] < depth[a]):
                parent[a]  = p
                how[a]     = "设备自报父节点"
                hop_lqi[a] = e.get("lqi")
                break

    # ③ 中继固件**自己报**的父节点（zb_nwk_get_parent → 0xFC00/0x0002）。
    #    这是最终裁决：设备自己说的，比任何推断都准。唯一要防的是成环。
    def _cyc(a, p):
        cur, n = p, 0
        while cur and n < 32:
            if cur == a:
                return True
            cur = parent.get(cur)
            n += 1
        return False

    for a, p in sorted((claims or {}).items()):
        if not a or a == coord or not p or p == "0xffff" or _cyc(a, p):
            continue
        parent[a]  = p
        how[a]     = "设备自报父节点（中继自己报的）"
        hop_lqi[a] = (links.get((a, p) if a < p else (p, a)) or {}).get("lqi")

    # 改写后重算深度（沿 parent 链上溯，最多 32 层防环）
    for a in list(parent):
        d, cur, n = 1, parent.get(a), 0
        while cur and cur != coord and n < 32:
            d += 1
            cur = parent.get(cur)
            n += 1
        depth[a] = d

    parent.pop(coord, None)
    depth.pop(coord, None)
    return parent, how, hop_lqi, depth, links


def _topo_svg(tp, names=None, meta=None, claims=None):
    """把 parent 关系画成一棵树。返回 (svg, parent, how, hop_lqi, depth, links)。

    边的标注用**子设备自己上报的**这一跳 LQI（Mgmt_Lqi_rsp），而不是协调器
    邻居表里那条「最后一帧」的旧值 —— 后者只能反映协调器能不能听见它，
    中间隔了一台路由器时完全不对。

    names: {addr: 型号}，节点标签优先显示型号（短、能认人），没有就退回地址；
           地址永远在鼠标悬停卡片里（和蓝牙设备列表一个习惯）。
    meta:  {addr: {"ieee","rssi"}}，只给悬停卡片补充信息，缺了不影响画图。
    """
    names = names or {}
    meta = meta or {}
    coord = tp.get("addr") or "0x0000"
    parent, how, hop_lqi, depth, links = _topo_rel(tp, claims)
    nbr  = {n["a"]: n for n in (tp.get("nbr") or []) if n.get("a")}
    mesh = tp.get("mesh") or {}

    nodes = {coord} | set(parent) | set(nbr) | set(mesh)
    for (x, y) in links:
        nodes.add(x)
        nodes.add(y)
    # 完全不可达的设备（既不在协调器邻居表、也没被任何设备报过）：BFS 到不了它，
    # parent 里没有它。旧实现直接 get(a, coord) 会把它画成协调器的直连子节点 ——
    # 那是**编造出来的边**。这里单独标出来：只放位置、不画线。
    orphan = {a for a in nodes if a != coord and a not in parent}
    kids = {a: [] for a in nodes}
    for a in sorted(nodes):
        if a == coord:
            continue
        p = parent.get(a, coord)
        if p not in kids:
            p = coord
        kids[p].append(a)

    # 子树宽度（以「一个叶位」为单位）
    width = {}

    def _w(a):
        ch = kids.get(a, [])
        width[a] = sum(_w(c) for c in ch) if ch else 1
        return width[a]

    _w(coord)

    scale = min(_TOPO_XGAP, (_TOPO_MAXW - 80) / max(1, width[coord]))
    pos, maxdepth = {}, [0]

    def _place(a, depth_, x0):
        w = width[a]
        pos[a] = (40 + (x0 + w / 2.0) * scale, 46 + depth_ * _TOPO_YGAP)
        maxdepth[0] = max(maxdepth[0], depth_)
        cx = x0
        for c in kids.get(a, []):
            _place(c, depth_ + 1, cx)
            cx += width[c]

    _place(coord, 0, 0)

    # ★ 画布固定 980 宽、而小网络只占左边一小块（4 台设备仅 300 px），
    #   右边全是大片空白、整张图缩在左上角。改成按实际跨度收窄画布并水平居中。
    xs = [x for x, _ in pos.values()]
    span = max(xs) - min(xs)
    W = int(min(_TOPO_MAXW, max(_TOPO_MINS, span + 2 * _TOPO_PAD)))
    shift = (W - span) / 2.0 - min(xs)
    pos = {a: (x + shift, y) for a, (x, y) in pos.items()}

    H = 46 + maxdepth[0] * _TOPO_YGAP + 70

    def _type_of(a):
        if a == coord:
            return "coord"
        n = nbr.get(a) or {}
        if not n:
            # 协调器邻居表里没有（隔了一台路由器），退回设备自报的类型
            for e in (mesh.get(a) or {}).get("nb") or []:
                if e.get("a") == a and isinstance(e.get("ty"), int):
                    return {0: "coord", 1: "router", 2: "ed"}.get(e["ty"], "ed")
        return {0: "coord", 1: "router", 2: "ed"}.get(n.get("ty"), "ed")

    fill = {"coord": "#c0392b", "router": "#2980b9", "ed": "#6c7a89"}

    parts = [
        f'<svg viewBox="0 0 {W} {H}" width="100%" '
        f'style="max-width:{W}px;background:#12121f;border-radius:6px" '
        f'xmlns="http://www.w3.org/2000/svg">'
    ]

    # 先画「额外链路」：两端都听得见、但不在生成树上的那条，画得很淡。
    # 这是 mesh 的真实样子 —— 树只为了让层级看得懂，链路才是实测数据。
    for (x, y), e in links.items():
        if (parent.get(y) == x) or (parent.get(x) == y):
            continue
        if x not in pos or y not in pos:
            continue
        (x1, y1), (x2, y2) = pos[x], pos[y]
        lqi = e.get("lqi")
        parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                     f'stroke="#2e4a6a" stroke-width="1" '
                     f'stroke-dasharray="2 5"/>')
        if isinstance(lqi, int):
            parts.append(f'<text x="{(x1 + x2) / 2:.1f}" y="{(y1 + y2) / 2 + 10:.1f}" '
                         f'fill="#4a6a8a" font-size="9" text-anchor="middle">{lqi}</text>')

    # 再画生成树的边，后画节点（节点盖住线头）
    for a in sorted(nodes):
        if a == coord:
            continue
        if a in orphan:
            continue                      # 没有实测链路 → 不画线（不编造边）
        p = parent.get(a, coord)
        if p not in pos:
            p = coord
        x1, y1 = pos[p]
        x2, y2 = pos[a]
        relayed = (p != coord)
        dash = ' stroke-dasharray="6 4"' if relayed else ""
        col = "#e6b422" if relayed else "#4a6aaa"
        lbl = hop_lqi.get(a)
        # LQI 低于 40 连不成样子了，画细画红
        w = "2"
        if isinstance(lbl, int):
            if lbl < 40:
                col = "#e74c3c"
            w = "3" if lbl >= 150 else ("2" if lbl >= 60 else "1.5")
        parts.append(f'<line x1="{x1:.1f}" y1="{y1 + 20:.1f}" '
                     f'x2="{x2:.1f}" y2="{y2 - 20:.1f}" '
                     f'stroke="{col}" stroke-width="{w}"{dash}/>')
        if isinstance(lbl, int):
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            parts.append(f'<text x="{mx:.1f}" y="{my:.1f}" fill="#8fa8c8" '
                         f'font-size="10" text-anchor="middle">LQI {lbl}</text>')

    # 标签最多能放几个字：相邻叶位间距就是 scale，等宽字体约 6.2 px/字
    lbl_max = max(8, int(scale / 6.2))

    for a in sorted(nodes):
        x, y = pos[a]
        ty = _type_of(a)
        r = 20 if ty == "coord" else 17
        # 标签：有型号显示型号（短、能认人），没型号就退回地址
        model = (names.get(a) or "").strip()
        if a == coord:
            plain = "协调器"
        elif model:
            plain = model if len(model) <= lbl_max else model[:lbl_max - 1] + "…"
        else:
            plain = a
        # 采集失败（st != 0）圈起来：0x84=不支持算「灰」，其它算真失败「暗红」
        m = mesh.get(a) or {}
        st = m.get("st") or 0
        stroke = "#0d0d1f"
        if a != coord:
            if st == 0x84:
                stroke = "#55556a"
            elif st:
                stroke = "#a03a3a"
        if a in orphan:
            stroke = "#5a3a5a"
        mm = meta.get(a) or {}
        via = parent.get(a)
        if a == coord:
            via_t = "—"
        elif a in orphan:
            via_t = "未知（没采到任何实测链路）"
        elif via == coord:
            via_t = "直连协调器"
        else:
            via_t = f"经中继 {via}"
        coll_t = ("未采集" if a not in mesh else
                  "不支持 Mgmt_Lqi" if st == 0x84 else
                  f"失败 st=0x{st:02x}" if st else f"✓ {m.get('tot', 0)} 条")
        tip = json.dumps({
            "name": model or plain, "addr": a,
            "ieee": mm.get("ieee") or "", "role": _DEV_TYPE.get(ty, ty),
            "via": via_t,
            "lqi": hop_lqi.get(a) if isinstance(hop_lqi.get(a), int) else
                   (nbr.get(a) or {}).get("lqi") or "",
            "depth": depth.get(a, 0),
            "rssi": mm.get("rssi") if isinstance(mm.get("rssi"), int) else "",
            "coll": coll_t,
        }, ensure_ascii=False)
        tip = (tip.replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;").replace('"', "&quot;"))
        parts.append(f'<g class="tn" data-tip="{tip}" style="cursor:pointer">')
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" '
                     f'fill="{fill[ty]}" stroke="{stroke}" stroke-width="2"/>')
        parts.append(f'<text x="{x:.1f}" y="{y + r + 14:.1f}" fill="#dfe6f0" '
                     f'font-size="11" text-anchor="middle">{plain}</text>')
        if a in orphan:
            parts.append(f'<text x="{x:.1f}" y="{y + r + 26:.1f}" '
                         f'fill="#9a7a9a" font-size="9" '
                         f'text-anchor="middle">未定位</text>')
        elif a != coord and parent.get(a, coord) != coord:
            parts.append(f'<text x="{x:.1f}" y="{y + r + 26:.1f}" '
                         f'fill="#e6b422" font-size="9" '
                         f'text-anchor="middle">中继</text>')
        parts.append("</g>")

    parts.append("</svg>")
    return "".join(parts), parent, how, hop_lqi, depth, links


_DEV_TYPE = {"coord": "协调器", "router": "路由器", "ed": "终端设备"}

# 拓扑图悬停卡片。单独放成常量是为了不写进 f-string（里面全是花括号，转义太容易错）。
_TOPO_TIP_JS = r"""
<div id="tt"></div>
<script>
(function () {
  var tt = document.getElementById('tt'), cur = null;
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }
  function row(k, v) {
    if (v === '' || v === null || v === undefined) return '';
    return '<div><span class="k">' + k + '</span><span class="v">' + esc(v) + '</span></div>';
  }
  document.querySelectorAll('.tn').forEach(function (g) {
    g.addEventListener('mouseenter', function () {
      var d;
      try { d = JSON.parse(g.dataset.tip); } catch (e) { return; }
      cur = JSON.stringify(d);
      tt.innerHTML = '<div class="h">' + esc(d.name || d.addr) + '</div>'
        + row('地址：', d.addr)
        + row('型号：', d.name && d.name !== d.addr ? d.name : '')
        + row('IEEE：', d.ieee)
        + row('角色：', d.role)
        + row('连接：', d.via)
        + row('该跳 LQI：', d.lqi)
        + row('深度：', d.depth)
        + row('RSSI：', d.rssi === '' ? '' : d.rssi + ' dBm')
        + row('自报采集：', d.coll);
      tt.style.display = 'block';
    });
    g.addEventListener('mousemove', function (ev) {
      if (!tt.style.display || tt.style.display === 'none') return;
      var x = ev.clientX + 16, y = ev.clientY + 16, r = tt.getBoundingClientRect();
      if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 16;
      if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 16;
      tt.style.left = Math.max(4, x) + 'px';
      tt.style.top = Math.max(4, y) + 'px';
    });
    g.addEventListener('mouseleave', function () { tt.style.display = 'none'; cur = null; });
  });
})();
</script>
"""


def _dev_model(d):
    """从设备状态里取 ZCL Basic.model（设备型号），取不到返回 ""。"""
    cl = (d or {}).get("clusters") or {}
    b = cl.get("basic")
    if isinstance(b, dict):
        return ((b.get("payload") or {}).get("model") or "").strip()
    return ""


def _lqi_cell(lqi, width: int = 50, ts=None) -> str:
    """渲染 LQI。★ LQI == 0 不是「信号为 0」。

    802.15.4 的 LQI 由收到的帧算相关性得到，取值范围 1..255；
    **0 表示这一帧没带有效的 LQI 值**（网关的 `ind.lqi` 在弱链路 / 某些
    帧类型下就是 0）。以前直接把 0 画成一个空 meter，用户看到「LQI 0」
    会以为显示坏了 —— 这里明确标成"未测"，真实强度请看同一行的 RSSI。

    `ts` = 这个值是**什么时候**测的（epoch 秒，来自固件心跳里的 `lqia`）。
    固件现在只在 LQI 有效时才覆盖（不再被 0 抹掉），所以静默设备的 LQI
    会一直保留旧值 —— 超过 2 分钟就标上年龄，别让人把陈旧值当实时值。
    """
    if not isinstance(lqi, int):
        return "–"
    if lqi == 0:
        return ('<span style="color:#888" title="最后几帧没有带有效 LQI，'
                '不是信号为 0；实际强度看 RSSI">– （未测）</span>')
    col = "#f66" if lqi < 40 else ("#fc6" if lqi < 100 else "#7fff7f")
    age = ""
    if ts:
        secs = int(time.time() - ts)
        if secs >= 120:
            age = (' <span style="color:#888;font-size:.85em" '
                   f'title="固件只在 LQI 有效时更新，这个值是最后一次测到的">'
                   f'（{_ago(ts)}测的）</span>')
    return (f'<meter value="{lqi}" min="0" max="255" '
            f'style="width:{width}px;vertical-align:middle"></meter> '
            f'<span style="color:{col}">{lqi}</span>{age}')


def _rssi_cell(rssi) -> str:
    """渲染 RSSI。这个是实测 dBm，LQI 拿不到时它才是可信的强度指标。

    顺手给个粗档位，省得用户自己把 dBm 换算成"到底算好不好"：
    经验阈值（2.4 GHz、板载天线）≥-60 优 / ≥-70 良 / ≥-80 弱 / 更低很差。
    """
    if not isinstance(rssi, int):
        return "–"
    if rssi >= -60:
        q, col = "优", "#7fff7f"
    elif rssi >= -70:
        q, col = "良", "#c8e06a"
    elif rssi >= -80:
        q, col = "弱", "#fc6"
    else:
        q, col = "很差", "#f66"
    return f'<span style="color:{col}">{rssi} dBm · {q}</span>'


def _topo_html():
    with _state_lock:
        tp = json.loads(json.dumps(_state.get("topo") or {}))
        devs = json.loads(json.dumps(_state.get("devices") or {}))
        # 中继自报的父节点（0xFC00/0x0002）—— 权威数据，判定优先级最高
        tp["claims"] = json.loads(json.dumps(_state.get("parent") or {}))

    # ★ 「这一轮是什么时候采的」必须显示出来：自动采集每 60 s 一轮，页面每
    #   15 s 重载，图里的连线/数字其实一直在这两个节奏之间变；不写时间戳，
    #   用户会觉得"刷新了也没变"，只能靠手动点。
    if tp.get("done_at"):
        topo_stamp = (f"本轮采集完成于 {tp.get('ts') or '?'}"
                      f"（{_ago(tp.get('done_at'))}，第 {tp.get('round', 0)} 轮）")
    elif tp.get("round"):
        topo_stamp = f"第 {tp.get('round', 0)} 轮采集中……（上一轮的图先留着）"
    else:
        topo_stamp = "还没有采集过拓扑"
    topo_iv = int(getattr(args, "topo_interval", 0) or 0)

    # 节点标签用型号（认设备），地址放到悬停卡片里；没型号的节点标签就退回地址
    names = {a: _dev_model(d) for a, d in devs.items() if _dev_model(d)}
    meta  = {a: {"ieee": d.get("ieee") or "", "rssi": d.get("rssi")}
             for a, d in devs.items()}
    svg, parent, how, hop_lqi, depth, links = _topo_svg(
        tp, names, meta, (tp.get("claims") or {}))
    coord = tp.get("addr") or "0x0000"
    nbr  = {n["a"]: n for n in (tp.get("nbr") or []) if n.get("a")}
    mesh = tp.get("mesh") or {}

    rows = ""
    for a in sorted(set(list(parent) + list(nbr) + list(devs) + list(mesh))):
        if a == coord:
            continue
        p = parent.get(a)
        if p == coord:
            way = '<span style="color:#7fff7f">直连</span>'
            ptxt = "–"
        elif p:
            way = '<span style="color:#e6b422">经中继</span>'
            ptxt = f'<a href="/device/{p}" style="color:#e6b422">{p}</a>'
        else:
            way = '<span style="color:#888">未知</span>'
            ptxt = "–"
        n = nbr.get(a) or {}
        m = mesh.get(a) or {}
        d = devs.get(a) or {}
        model = _dev_model(d)
        tyv = n.get("ty")
        if tyv is None:
            for e in m.get("nb") or []:
                if e.get("a") == a and isinstance(e.get("ty"), int):
                    tyv = e["ty"]
                    break
        ty = {0: "协调器", 1: "路由器", 2: "终端设备"}.get(tyv, "终端设备")
        # 该跳 LQI：优先设备自报（隔了中继也准），否则协调器邻居表的旧值
        hl = hop_lqi.get(a)
        if not isinstance(hl, int):
            hl = n.get("lqi")
        rssi = n.get("rssi")
        if a not in mesh:
            coll = '<span style="color:#666">未采集</span>'
        elif m.get("st") == 0x84:
            coll = '<span style="color:#888">不支持</span>'
        elif m.get("st"):
            coll = f'<span style="color:#f66">失败 st=0x{m.get("st"):02x}</span>'
        else:
            coll = f'<span style="color:#7fff7f">✓ {m.get("tot", 0)} 条</span>'
        # 这台设备总共被几条链路看到（含它自己报的和别人报的）
        n_lnk = sum(1 for (x, y) in links if a in (x, y))
        dv = depth.get(a)
        rows += f"""
        <tr>
          <td><a href="/device/{a}" style="color:#7ecbff;text-decoration:none">{a}</a></td>
          <td>{ty}</td>
          <td>{model or "–"}</td>
          <td>{way}</td>
          <td>{ptxt}</td>
          <td>{dv if isinstance(dv, int) else "–"}</td>
          <td>{_lqi_cell(hl)}</td>
          <td>{_rssi_cell(rssi)}</td>
          <td>{coll}</td>
          <td>{n_lnk}</td>
          <td style="color:#888;font-size:.85em">{how.get(a, "")}</td>
        </tr>"""

    n_relay = sum(1 for a, p in parent.items() if p and p != coord)
    n_mesh_ok  = sum(1 for m in mesh.values() if not m.get("st"))
    n_mesh_bad = len(mesh) - n_mesh_ok
    ts = tp.get("ts") or "（还没有数据，点下面的「刷新拓扑」）"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<title>网络拓扑 – ESP32-C6 网关</title>
<style>
  body{{background:#1a1a2e;color:#e0e0e0;font-family:monospace;margin:2em}}
  h1{{color:#00d4ff;font-size:1.4em}} h2{{color:#a0c4ff;border-bottom:1px solid #333;padding-bottom:.3em}}
  table{{border-collapse:collapse;width:100%;margin:.5em 0}}
  th,td{{border:1px solid #444;padding:.35em .7em;text-align:left}}
  th{{background:#2a2a4a}} tr:nth-child(even){{background:#1e1e3a}}
  button{{background:#2a4a7a;color:#e0e0e0;border:1px solid #4a6a9a;
          padding:.4em 1em;cursor:pointer;border-radius:3px}}
  button:hover{{background:#3a6aaa}}
  .legend span{{display:inline-block;margin-right:1.2em;font-size:.9em}}
  .dot{{display:inline-block;width:10px;height:10px;border-radius:50%;
        margin-right:.35em;vertical-align:middle}}
  section{{background:#16213e;padding:1.2em;margin:1.2em 0;border-radius:6px}}
  a{{color:#7ecbff}}
  /* 悬停卡片：跟蓝牙设备列表一个习惯 —— 图上只写型号，地址等信息放这里 */
  #tt{{position:fixed;z-index:50;pointer-events:none;display:none;
       background:#20202e;border:1px solid #3d3d60;border-radius:8px;
       padding:.65em .9em;font-size:.85em;line-height:1.65;
       box-shadow:0 8px 24px #000c;max-width:340px;white-space:nowrap}}
  #tt .h{{color:#fff;font-weight:bold;font-size:1.1em;margin-bottom:.15em}}
  #tt .k{{color:#8a8aa8;display:inline-block;min-width:5.6em}}
  #tt .v{{color:#dfe6f0}}
  .tn:hover circle{{filter:brightness(1.35)}}
</style></head>
<body>
<h1>网络拓扑 <small style="font-size:.6em;color:#666">
  PAN {tp.get("pan") or "–"} · 信道 {tp.get("ch") or "–"} ·
  邻居 {len(tp.get("nbr") or [])} · 路由 {len(tp.get("rt") or [])} ·
  源路由 {len(tp.get("rr") or [])} ·
  设备自报 {n_mesh_ok}/{len(mesh)}{(" · 失败 " + str(n_mesh_bad)) if n_mesh_bad else ""}</small></h1>
<p><a href="/">← 返回仪表盘</a> &nbsp;|&nbsp;
   <a href="/api/topo">JSON</a> &nbsp;|&nbsp;
   采集时间 {ts}</p>

<form method="POST" action="/api/topo">
  <button type="submit">刷新拓扑（逐台 Mgmt_Lqi 采集，约 {max(3, len(devs) * 2)} 秒）</button>
</form>
<p style="color:#888;font-size:.85em">
  <b>怎么算的</b>：先向每台设备发 <code>Mgmt_Lqi_Req</code>（ZDO 0x0031）收它的邻居表，
  再把「协调器的邻居表 + 各设备自报的邻居表」合并成一张<b>实测链路图</b>
  （每条边带 LQI），最后从协调器做 BFS 生成树：父节点 = 深度最小的可达邻居，
  同层挑 LQI 最高的那条。<br>
  <b>为什么不用设备自报的父子关系</b>：本网络的 Feibit 开关回
  <code>0x84 NOT_SUPPORTED</code>（不支持 Mgmt_Lqi）；支持它的 ESP32-C6 中继虽然答了，
  但 ZBOSS 把 <code>relationship</code> 一律填 2、<code>depth</code> 一律填 0，
  不可用。所以只能靠实测链路推 —— 好处是不依赖设备配合。<br>
  虚线 = 这台的流量要过一台中继；线越粗 LQI 越好，&lt;40 画红色。
  <span style="color:#2e4a6a">点线</span> = 两端都听得见、但没用在生成树上的额外链路。<br>
  <b>节点上写的是型号</b>（ZCL Basic.model，取不到型号就写地址）；
  <b>鼠标悬停</b>看地址 / IEEE / 角色 / 连接方式 / 该跳 LQI / RSSI。
  圈的颜色：<span style="color:#55556a">灰</span>=不支持 Mgmt_Lqi、
  <span style="color:#a03a3a">暗红</span>=采集失败、
  <span style="color:#5a3a5a">紫</span>=未定位（没采到任何实测链路）。
</p>

<section>
<h2>拓扑图</h2>
{svg}
<p class="legend">
  <span><i class="dot" style="background:#c0392b"></i>协调器</span>
  <span><i class="dot" style="background:#2980b9"></i>路由器（可转发）</span>
  <span><i class="dot" style="background:#6c7a89"></i>终端设备</span>
  <span style="color:#e6b422">— — 虚线 = 经中继</span>
  <span style="color:#2e4a6a">···· 点线 = 额外链路（实测能听见，未用于转发路径）</span>
</p>
<p style="color:#888;font-size:.85em">共 {n_relay} 台设备的流量经过中继 · 实测链路 {len(links)} 条。</p>
</section>

<section>
<h2>明细</h2>
<table>
<tr><th>地址</th><th>类型</th><th>型号</th><th>连接方式</th><th>父节点/下一跳</th>
    <th>深度</th><th>该跳 LQI</th><th>本机 RSSI</th><th>自报采集</th><th>链路</th><th>判定依据</th></tr>
{rows or '<tr><td colspan="11"><em>还没有数据</em></td></tr>'}
</table>
</section>
<footer style="margin-top:2em;color:#666;font-size:.85em">
  <span id="rl" style="color:#666">自动刷新 15 秒</span> &nbsp;|&nbsp;
  <a href="javascript:location.reload()">立即刷新</a> &nbsp;|&nbsp;
  <a href="/api/topo">JSON</a>
  <br>
  <span style="color:#8ab4f8">{topo_stamp}</span>
  &nbsp;·&nbsp; 服务端每 {topo_iv} 秒自动重采一轮（0 = 关闭）
</footer>
{_autoreload_js(15)}
{_TOPO_TIP_JS}
</body></html>"""


def dispatch(line: str):
    mq_put(f"{BASE}/console", line, qos=0)

    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return

    t = msg.get("t")
    if not t:
        return

    # ★ 任何一条 C6 消息都算「链路还活着」—— 心跳、属性上报、命令回执都算。
    #   统一在这里更新，比在每个分支里各写一次可靠，也不会漏掉新加的消息类型。
    global _last_c6_ts
    _last_c6_ts = time.monotonic()

    if t == "boot":
        global _gw_boot_ts, _fw_ota_restarts
        _gw_boot_ts = time.monotonic()
        mq_put(f"{BASE}/status", "online", retain=True, qos=1)
        db_upsert_gateway("online")
        with _state_lock:
            _state["status"] = "online"
            fw = dict(_state.get("fw_ota") or {})
        print("[C6] boot")
        # 发射功率在固件里**不**持久化（没写 NVS）——
        # 每次 C6 启动后都要把记住的期望值重设一遍。
        _txp_reapply()
        # ★ C6 若在固件传输途中重启，OTA 会话就没了 —— 剩下的帧会悄无声息
        # 地蒸发（当时正是这样：传输停在 30720/656560）。所以从头发一遍，
        # 但最多 3 次。
        if (fw.get("status") == "transferring"
                and fw.get("bin_path")
                and (fw.get("off") or 0) < (fw.get("size") or 0)
                and _fw_ota_restarts < 3):
            _fw_ota_restarts += 1
            print(f"[OTA] 传输中途 C6 重启了 —— 重新开始 "
                  f"({_fw_ota_restarts}/3): {fw['bin_path']}")
            threading.Timer(4.0, _fw_ota_start, (fw["bin_path"],)).start()

    elif t == "pong":
        # C6 对心跳的回执。`link` 说明它把这条命令归到了哪条通道 ——
        # 也就是它之后会把上报往哪条发。切过通道 5 s 内这里就会跟着变。
        global _hb_last_pong, _hb_link_echo
        _hb_last_pong = time.monotonic()
        _hb_link_echo = msg.get("link", "?")
        return

    elif t == "vitals":
        p = msg.get("p")
        if p:
            mq_put(f"{BASE}/mr60bha2", json.dumps(p))

    elif t == "zigbee":
        addr = "0x{:04x}".format(msg.get("addr", 0))
        sub  = msg.get("sub", "raw")
        p    = msg.get("p")
        if p is not None:
            _ha_state(addr, sub, p)          # retained —— 熬得过 HA 重启
            print(f"[MQTT] {BASE}/zigbee/{addr}/{sub} {p}")
            # 这个值是我们自己要来的吗？必须在 _pending_ack **之前**判断 ——
            # 之后那条记录就已经清掉了。
            _erwartet = False
            if sub.endswith("on_off"):
                _e = _sub_ep(sub)
                _erwartet = _e is not None and (addr, _e) in _pending
            # 真实的设备值会结束一次待确认的命令，
            # 或者触发一次重发（见 _pending_ack）。
            _pending_ack(addr, sub, p)
            # 从 Report 里推导按键（面板报的是 on/off，不是 Scene ——
            # 见 _ha_button_from_report）。
            _ha_button_from_report(addr, sub, p, _erwartet)
            ieee = msg.get("ieee", "")
            if ieee:
                _ha_learn_ieee(addr, ieee)
            # Basic-Read（厂商/型号）→ 给 discovery 用的设备信息。
            # 之后要丢弃缓存，让实体带着真实型号重新登记
            # （之前它们还叫 "Zigbee 0x…"）。
            if sub == "basic":
                d = _ha_devices.setdefault(addr, {})
                if p.get("mfr"):   d["mfr"]   = p["mfr"]
                if p.get("model"): d["model"] = p["model"]
                # ★★★ 2026-11-07：必须同步一份到 _state["devices"]。
                #   设备表和 /api/state 都是从 _state["devices"] 取字段的，而型号
                #   以前只写进 _ha_devices —— 两份数据互不相通，于是页面
                #   「制造商 / 型号」整列永远显示 "–"，连 payload 里明明已经读到
                #   型号的设备（Feibit FTB56）也一样。这是用户指出的那个"设备
                #   列表不对"。
                with _state_lock:
                    sd = _state["devices"].setdefault(addr, {
                        "ieee": "", "name": addr,
                        "last_seen": "", "clusters": {}})
                    if d.get("mfr"):   sd["mfr"]   = d["mfr"]
                    if d.get("model"): sd["model"] = d["model"]
                hit = _ha_db.get((p.get("model") or "").strip())
                if hit:
                    d["db"] = hit
                    print(f"[HA] 识别到型号：{p.get('model')} → {hit['model']} "
                          f"({hit['vendor']}) eps={hit.get('endpoints')}")
                else:
                    print(f"[HA] 型号未知：{p.get('model')} —— 用通用 Discovery")
                _knd, _why = _ha_db_hint(addr)
                if _knd == "panel":
                    print(f"[HA] {addr}：线索 {_why} ⇒ 猜测是按键面板 "
                          f"（最终由 Descriptor/Probe 决定）")
                elif _knd == "maybe":
                    print(f"[HA] {addr}：线索 {_why} —— 有歧义，"
                          f"entscheidet Descriptor/Probe")
                elif _knd is None and _why:
                    print(f"[HA] {addr}: {_why}")
                _ha_forget(addr)
                _ha_prepublish(addr)
            else:
                _ha_publish(addr, sub, p)
            db_upsert_zigbee_device(addr, ieee)
            db_insert_zigbee_data(addr, sub, p)
            with _state_lock:
                # 删掉 IEEE 相同的旧条目（rejoin 后地址变了）
                if ieee:
                    old = [a for a, d in _state["devices"].items()
                           if d.get("ieee") == ieee and a != addr]
                    for a in old:
                        del _state["devices"][a]
                dev = _state["devices"].setdefault(addr, {
                    "ieee": ieee, "name": addr, "last_seen": "", "clusters": {}
                })
                dev["ieee"] = ieee
                # 温度簇：0x8000 表示无效 → raw:null
                if sub == "temperature" and isinstance(p, dict) and p.get("raw") == -32768:
                    p = dict(p); p["raw"] = None
                dev["clusters"][sub] = {"payload": p, "ts": _now()}
                dev["last_seen"] = _now()   # 固件心跳批次时间（整批一起刷，别当真）
                dev["seen"]      = time.time()   # 这台设备**自己**最后一次说话的时刻
                dev.pop("synthetic", None)       # 真上报了，占位标记可以摘了
            _avail_touch(addr)
            _avail_sync(addr)

    elif t == "parent":
        # 中继自报父节点（固件把 zb_nwk_get_parent() 写进 0xFC00/0x0002）。
        # 这是**设备自己说的**，拓扑里优先级最高，不用再靠邻居表猜。
        a = msg.get("addr")
        p = msg.get("p")
        if a and p and a != p:
            with _state_lock:
                _state.setdefault("parent", {})[a] = p
            print(f"[PARENT] {a} 自报父节点 {p}")

    elif t == "heartbeat":
        mq_put(f"{BASE}/heartbeat", line, retain=True, qos=1)
        db_upsert_gateway("online")
        with _state_lock:
            _state.update({
                "status":  "online",
                "uptime":  msg.get("uptime", 0),
                "channel": msg.get("ch", 0),
                "pan":     msg.get("pan", "–"),
            })
            for d in msg.get("dev", []):
                addr = d.get("addr")
                if addr:
                    dev = _state["devices"].setdefault(addr, {
                        "ieee": "", "name": addr, "last_seen": "", "clusters": {}
                    })
                    if "lqi"  in d: dev["lqi"]  = d["lqi"]
                    if "rssi" in d: dev["rssi"] = d["rssi"]
                    # lqia = 固件说"这个 lqi 是几秒前测的"。换成绝对时刻存，
                    # 之后页面自己算年龄，不用每轮刷新也能显示对不对。
                    if "lqia" in d:
                        _age = d["lqia"]
                        dev["lqi_ts"] = (0 if _age is None or _age >= 0xFFFFFFFF
                                         else time.time() - _age)
                    dev["last_seen"] = _now()
                    db_upsert_zigbee_device(addr)
        devs = msg.get("dev", [])
        lqi_str = " ".join(f"{d['addr']}={d['lqi']}" for d in devs) if devs else "–"
        print(f"[HB] uptime={msg.get('uptime')}s ch={msg.get('ch')} pan={msg.get('pan')} lqi=[{lqi_str}]")

    elif t == "permit_join":
        p = msg.get("p", {})
        if p:
            mq_put(f"{BASE}/permit_join", json.dumps(p), qos=1)
            with _state_lock:
                _state["permit_join"] = p
            print(f"[MQTT] {BASE}/permit_join {p}")

    elif t == "scan":
        ch = msg.get("ch")
        if ch is not None:
            nets = msg.get("nets", [])
            db_insert_chan_scan(ch, msg.get("count", 0), json.dumps(nets))
            print(f"[SCAN] ch={ch} count={msg.get('count', 0)} nets={nets}")
        else:
            print(f"[SCAN] state={msg.get('state')}")

    elif t == "interview":
        # Join-Interview: Active-EP → Simple-Desc → Bind → Configure-Reporting
        act = msg.get("act")
        # C6 在 interview 对象里把地址当字符串发（"0x07da"），
        # 在 zigbee 对象里当数字发。这里统一成同一种写法，
        # 否则 _ctrl/_noread 里的键对不上。
        _raw = msg.get("addr")
        try:
            a = "0x{:04x}".format(int(_raw, 0) if isinstance(_raw, str) else int(_raw))
        except (TypeError, ValueError):
            a = str(_raw)
        if act:
            print(f"[IV] {act} addr={a} ep={msg.get('ep')} "
                  f"cluster={msg.get('cluster')} status={msg.get('status')}")
        elif msg.get("in") is not None or msg.get("out") is not None:
            # Simple-Descriptor：簇列表是关于"这个端点能开关、还是只能发
            # 命令"最权威的答案（见 _classify_from_desc）。
            print(f"[IV] desc addr={a} ep={msg.get('ep')} "
                  f"device={msg.get('device')} in={msg.get('in')} out={msg.get('out')}")
            try:
                _dev = msg.get("device") or "0x0000"
                _classify_from_desc(a, int(msg.get("ep") or 0),
                                    [int(x, 0) for x in (msg.get("in") or [])],
                                    [int(x, 0) for x in (msg.get("out") or [])],
                                    int(_dev, 0) if isinstance(_dev, str) else int(_dev))
            except (TypeError, ValueError) as e:
                print(f"[IV] desc 解析不了：{e}")
        else:
            print(f"[IV] {msg.get('state')} addr={a} "
                  f"eps={msg.get('eps')} status={msg.get('status')}")
            if msg.get("state") == "done":
                # Ein Read-Attributes je Endpunkt entscheidet automatisch,
                # 那里有没有状态存在（见 _probe_*）。
                _probe_start(a, msg.get("eps") or [])

    elif t in ("wifi_scan", "wifi_bootscan"):
        # C6 自己扫频的结果。n/got 分开报：n 是驱动说扫到几个，
        # got 是实际取回来几个 —— 两者不等说明是取记录失败，不是没信号。
        aps = msg.get("aps") or []
        with _state_lock:
            _state[t] = dict(msg)
        if msg.get("err"):
            print(f"[SCAN] 扫描失败：{msg.get('err')}")
        else:
            print(f"[SCAN] C6 扫到 {msg.get('got', 0)} 个 AP"
                  f"（驱动计数 {msg.get('n', 0)}）：")
            for a in aps:
                print(f"       {a.get('s')!r:24} rssi={a.get('r')} dBm "
                      f"ch={a.get('c')} auth={a.get('a')}")
            if not aps:
                print("[SCAN] 一个都没扫到 —— 先确认板子的 Wi-Fi 接收是否正常，"
                      "别急着改 SSID/密码")

    elif t in ("net", "net_status"):
        # Wi-Fi/TCP 通道状态。C6 在「Wi-Fi 连上 / TCP 连上 / 断开 / 配置变更」
        # 时主动发一条；{"cmd":"net_status"} 也会回一条同格式的。
        # 这里只做两件事：记进 _state（/api/state 里能看到）和打一行日志 ——
        # 验证「拔掉 USB 之后还通不通」时，这行是唯一的直接证据。
        with _state_lock:
            _state["net"] = dict(msg)
        if t == "net_status":
            # 查询回执：把真实状态照实说。这里**不能**当成「已连通」——
            # 串口模式下 Transport=uart、ip 是 0.0.0.0，说「已连通」会误导排查。
            print(f"[NET] 状态：通道={msg.get('transport')} "
                  f"state={msg.get('state')} ssid={msg.get('ssid')!r} "
                  f"ip={msg.get('ip')} rssi={msg.get('rssi')} "
                  f"→ {msg.get('host')}:{msg.get('port')}")
        elif msg.get("up"):
            print(f"[NET] 通道已连通 ip={msg.get('ip')} rssi={msg.get('rssi')} "
                  f"→ {msg.get('host')}:{msg.get('port')}")
        else:
            print("[NET] TCP 断开，等待 C6 重连（板上会自动重试）")

    elif t == "aps":
        # 原始 APS 帧：里面含按键（见 _ha_aps_action）。
        # 能收到它的帧 = 这台设备刚刚真的发了东西，判活最硬的证据。
        _src = msg.get("src")
        if _src:
            _avail_touch(_src)
            with _state_lock:
                _d = _state["devices"].get(_src)
                if _d is None:
                    # 纯面板只发按键的裸 APS 帧，永远不会有 zigbee/cluster
                    # 上报。旧代码只给"已存在"的设备打时间戳，于是这类设备
                    # 在网页设备表和可用性检查里**完全不存在**（0x747d 就是
                    # 这么消失的）。这里给它建档，标 synthetic 等真上报来摘。
                    _d = _state["devices"].setdefault(_src, {
                        "ieee": "", "name": _src, "last_seen": "",
                        "clusters": {}, "synthetic": True,
                    })
                _d["seen"] = time.time()
            _avail_sync(_src)
        _ha_aps_action(msg)

    elif t == "txpower":
        # set_tx_power 的回执：期望值，以及 PA 回读到的实际值。两者不一致
        # 就说明改动没生效 —— 那么用这个功率做的任何测量都没意义
        # （见报告 §21）。
        want = msg.get("want")
        eff  = msg.get("eff")
        with _state_lock:
            _state["tx_power"] = {"want": want, "eff": eff, "ts": _now()}
        mq_put(f"{BASE}/tx_power", line, retain=True, qos=1)
        abw = ""
        if want is not None and eff is not None and want != eff:
            abw = f"   (!) 设定 {want} dBm，实际 {eff} dBm —— 调整没生效"
        print(f"[TX] Sendeleistung: Wunsch {want} dBm, wirksam {eff} dBm{abw}")

    elif t == "dev_removed":
        # 我们那条 "dev_remove" 的回执：C6 已经把它从表里 + NVS 里删了。
        # 如果这里还有残留（比如是从别处触发删除的），现在补上。
        a = msg.get("addr")
        noch_da = a and (a in (_state.get("devices") or {}) or a in _addr_nodes)
        print(f"[C6] 已删除设备 {a}（IEEE {msg.get('ieee')}，"
              f"表内：{msg.get('found')}，Leave：{msg.get('leave')}）")
        if noch_da:
            _device_remove(a, "由协调器删除", tell_c6=False)

    elif t == "lqi":
        # 按需取快照（不必等到 60 s 心跳）。
        # 发射功率对比要用它：同一个位置、同一台设备，只有功率不同。
        devs = msg.get("dev") or []
        with _state_lock:
            for d in devs:
                addr = d.get("addr")
                if not addr:
                    continue
                dev = _state["devices"].setdefault(addr, {
                    "ieee": "", "name": addr, "last_seen": "", "clusters": {}
                })
                if "lqi"  in d: dev["lqi"]  = d["lqi"]
                if "rssi" in d: dev["rssi"] = d["rssi"]
                if "lqia" in d:
                    _age = d["lqia"]
                    dev["lqi_ts"] = (0 if _age is None or _age >= 0xFFFFFFFF
                                     else time.time() - _age)
        mq_put(f"{BASE}/lqi", line, retain=True, qos=1)
        if devs:
            print("[LQI] " + " ".join(
                f"{d.get('addr')}={d.get('lqi')}/{d.get('rssi')}dBm" for d in devs))
        else:
            print("[LQI] 还没有已知设备")

    elif t == "keepalive":
        # Keep-Alive 测试：授权之后周期性发 Read-Attributes
        print(f"[KA] addr={msg.get('addr')} ep={msg.get('ep')} "
              f"round={msg.get('round')}")

    elif t == "node_desc":
        # ZDO Node-Descriptor（0x0002）的回执。决定这台设备能不能作为
        # 路由器（ZR）转发别人的流量，还是只是终端设备。
        # ★ 顺便当"活体探测"：拓扑轮询每 60 s 对每台设备发一次，答了就是活着，
        #   回错误（0x85 超时等）就是掉线的**反证** —— 见 _avail_* 记分板。
        a = msg.get("addr", "?")
        # 广播/组播地址永远不会有 node_desc 应答（0xffff 实测回 0x85 超时）。
        # 若当成设备记录，会污染 node_desc 表、把"探测失败"计数抬高，
        # 还会在可用性记分板上留下一条假的"掉线设备"。直接丢弃。
        if str(a).lower() in ("0xffff", "0xfffe", "0xfffd", "0xfffc", "?"):
            return
        if msg.get("error"):
            print(f"[ND] {a}：查询失败（{msg.get('error')}）")
            _avail_last_bad[a] = time.time()
        else:
            print(f"[ND] {a}: role={msg.get('role')} power={msg.get('power')} "
                  f"rx_idle={msg.get('rx_idle')} cap={msg.get('cap')}")
            _avail_touch(a)
        with _state_lock:
            nd = dict(msg)
            nd["_ts"] = time.time()
            _state.setdefault("node_desc", {})[a] = nd
        _avail_sync(a)

    elif t == "fw_ota":
        st = msg.get("st")
        if st == "begin":
            print(f"[OTA] coordinator: begin – Part {msg.get('part','?')}, "
                  f"size={msg.get('size','?')} —— 传输中 …")
            _fw_ota_send_batch()
        elif st == "write":
            with _state_lock:
                _state["fw_ota"]["off"] = msg.get("off", 0)
            if _fw_ota_off < _fw_ota_size:
                _fw_ota_send_batch()
        elif st == "done":
            # written = C6 实际写进分区的字节数。
            # 和文件大小不一致，说明上传/分帧环节出了岔子
            # （例如尾部多 44 B）。只要 esp_ota_end 认了就不致命，
            # 但必须看得见，不能静默。
            written = msg.get("written")
            if isinstance(written, int) and written != _fw_ota_size:
                print(f"[OTA] 警告：已写 {written} B，文件 {_fw_ota_size} B "
                      f"（差 {written - _fw_ota_size:+d} B）")
            print("[OTA] 协调器：更新完成 —— 重启中 …")
            with _state_lock:
                _state["fw_ota"]["status"] = "done"
            _fw_ota_close()
        elif st == "error":
            why = msg.get("why")
            print(f"[OTA] 协调器出错：{why}")
            if why == "busy":
                print("[OTA] C6 那边还有一个没结束的会话（s_active=true）。"
                      "新固件会在断流 120 s 后自动接管旧会话，等一下再点一次即可；"
                      "老固件只能重启 C6。")
            with _state_lock:
                _state["fw_ota"]["status"] = "error"
            _fw_ota_close()

    elif t == "zb_signal":
        print(f"[ZB] {msg.get('sig')} addr={msg.get('addr')} "
              f"status={msg.get('status')} id={msg.get('id')} err={msg.get('err')}")

    elif t == "cmd":
        print(f"[CMD] addr={msg.get('addr')} ep={msg.get('ep')} "
              f"cluster={msg.get('cluster')} v={msg.get('v')}")

    elif t == "ota_req":
        _ota_handle_req(msg.get("off", 0), msg.get("sz", 64))

    elif t == "ota_status":
        st = msg.get("status", "?")
        with _state_lock:
            _state["ota"]["status"] = st
        print(f"[OTA] coordinator: {st} size={msg.get('size')} ver={msg.get('ver')}")

    elif t == "ota_srv_status":
        st = msg.get("status", "?")
        addr = msg.get("addr", "?")
        print(f"[OTA] client {addr}: {st}")
        if st == "done":
            with _state_lock:
                _state["ota"]["status"] = "done"
            _ota_close()

    elif t == "ota_dbg":
        # ★ 网关 OTA server 的内部计数（来自 main/zb_ota_server.c）。
        #   命中/未命中/超时是判断「窗口缓存够不够、串口来不来得及」的唯一依据。
        with _state_lock:
            d = _state["ota"].setdefault("dbg", {})
            for k in ("n", "hit", "miss", "to", "seq", "idx", "sz", "off",
                      "win", "load", "len", "timeout", "stop", "src", "cmd"):
                if k in msg:
                    d[k] = msg[k]
        loud = ("miss" in msg or "timeout" in msg or "stop" in msg)
        if loud or (msg.get("load", -1) % 8192 == 0):
            print("[OTA-gw] " + ", ".join(
                f"{k}={msg[k]}" for k in msg if k != "t"))

    elif t in ("topo_nbr", "topo_rt", "topo_rr", "topo_mesh", "topo"):
        _topo_ingest(t, msg)

    elif t == "wr_power":
        # 网关已把 0xFC00/0x0001 写给了中继；中继是否真的改了，
        # 只能看它自己的串口日志（属性写入不回读）。
        addr = msg.get("addr", "?")
        with _state_lock:
            _state["wr_power"][addr] = {
                "dbm": msg.get("dbm"),
                "tsn": msg.get("tsn"),
                "ts":  _now(),
            }
        print(f"[PWR] → {addr} ep{msg.get('ep')} {msg.get('dbm')} dBm "
              f"(tsn {msg.get('tsn')})")


def _ota_start(bin_path: str, image_type: int = 0x0001):
    """启动 OTA：打开镜像文件，向协调器发 ota_start。

    image_type 决定这份镜像适用于哪类设备：
        0x0001 = 终端设备（client/）   0x0002 = 中继（router/）
    协调器在 Image-Notify 里带上这个类型；只有同类型的客户端才会应答。
    类型错了 = 没人应（不出事，但也不会有更新）。
    """
    global _ota_file
    import os as _os
    if not bin_path:
        if image_type == 0x0002:
            print("[OTA] 中继镜像没有路径 —— 需要先上传")
            with _state_lock:
                _state["ota"]["status"] = "error"
            return
        bin_path = _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
            "client", "build", "zigbee-client.bin"
        )
    if not _os.path.isfile(bin_path):
        print(f"[OTA] 找不到文件：{bin_path}")
        with _state_lock:
            _state["ota"]["status"] = "error"
        return
    size = _os.path.getsize(bin_path)
    # ★ 合理性检查：ESP App 镜像开头是魔术字节 0xE9。
    #   没有它就说明文件被弄坏了（典型：镜像前面混进了 multipart 头），
    #   或者这是 .zip/.ota 包装。这种情况宁可大声失败，也别花 20 分钟
    #   发一堆设备最后照样丢弃的块。
    try:
        with open(bin_path, "rb") as _mf:
            _magic = _mf.read(1)
        if _magic != b"\xE9":
            print(f"[OTA] 中止：{bin_path} 开头是 0x{_magic.hex() or '??'}，"
                  f"应为 0xe9（这不是 ESP 应用镜像）。")
            with _state_lock:
                _state["ota"]["status"] = "error"
            return
    except OSError as _e:
        print(f"[OTA] 中止：{bin_path} 读不出来（{_e}）")
        with _state_lock:
            _state["ota"]["status"] = "error"
        return
    # 版本号由文件 mtime 推出（Unix 时间戳，32 位）
    version = int(_os.path.getmtime(bin_path)) & 0xFFFFFFFF
    _ota_close()
    _ota_file = open(bin_path, "rb")
    global _ota_hash, _ota_sent, _ota_next
    _ota_hash = _OTA_FNV0
    _ota_sent = 0
    _ota_next = 0
    with _state_lock:
        _state["ota"].update({
            "status":      "pending",
            "bin_path":    bin_path,
            "size":        size,
            "version":     version,
            "transferred": 0,
            "image_type":  image_type,
            "sent":        0,
            "sent_hash":   "0x%08x" % _OTA_FNV0,
        })
    ser.write(_j({"cmd": "ota_start", "size": size, "ver": version,
                  "type": image_type}))
    print(f"[OTA] 已开始：{bin_path} size={size} ver=0x{version:08x} "
          f"type=0x{image_type:04x}")


def _ota_handle_req(offset: int, size: int):
    """回答 ota_req：从镜像文件里读一块，转成 hex 发出去。

    ★ 判「有没有在传」只看 _ota_file（打开着就说明 _ota_start 成功过）。
      旧实现白名单只写了 pending/transferring，而网关在 ota_start 之后会先
      发一条 status="started" —— 于是客户端第一次 Query Next Image 就被判
      「kein Transfer aktiv」，10 秒后客户端 abort，**OTA 永远起不来**。
      （实测 2026-10-26：中继每 45 s 问一次，每次都卡在这里。）"""
    global _ota_file
    if _ota_file is None:
        print(f"[OTA] ota_req off={offset} 但当前没有打开的镜像（先上传/选择 bin）")
        return

    global _ota_hash, _ota_sent, _ota_next
    try:
        _ota_file.seek(offset)
        data = _ota_file.read(size)
        hex_data = data.hex()
        cmd = json.dumps({"cmd": "ota_data", "off": offset, "data": hex_data})
        ser.write((cmd + "\n").encode())
        # 只对「连续的、第一次发」的块累加，重发不重复计
        if offset == _ota_next and data:
            h = _ota_hash
            for b in data:
                h = ((h ^ b) * 16777619) & 0xFFFFFFFF
            _ota_hash = h
            _ota_sent += len(data)
            _ota_next = offset + len(data)
        with _state_lock:
            _state["ota"]["status"] = "transferring"
            _state["ota"]["transferred"] = offset + len(data)
            _state["ota"]["sent"] = _ota_sent
            _state["ota"]["sent_hash"] = "0x%08x" % _ota_hash
    except Exception as e:
        print(f"[OTA] 读取出错：{e}")
        _ota_close()
        with _state_lock:
            _state["ota"]["status"] = "error"


def _ota_close():
    global _ota_file
    if _ota_file:
        _ota_file.close()
        _ota_file = None

# ── Web-Dashboard ──────────────────────────────────────────────────────────
def _autoreload_js(secs: int) -> str:
    """条件自动刷新。

    旧实现用 <meta http-equiv="refresh">，它会在固定秒数后无条件整页重载，
    从而把 <input type="file"> 里已选中的文件清空（页面回到"未选择任何文件"），
    用户根本来不及点上传。这里改成脚本驱动：只要页面上有"未提交的输入"
    （已选文件 / 输入框有焦点 / 输入框内容与初始值不同）就暂停刷新，
    并允许用户手动点击"立即刷新"。
    """
    return f"""<script>
(function () {{
  var SECS = {secs};
  /* 记录页面加载时的输入框内容作为"未修改"基线。
     不能直接用 defaultValue：Firefox 整页重载后会恢复用户已输入的内容，
     那样 defaultValue 与 value 会一直不相等，页面将永远不再刷新。 */
  function snapshot() {{
    var ins = document.querySelectorAll('input, textarea');
    for (var j = 0; j < ins.length; j++) {{
      var t = (ins[j].type || '').toLowerCase();
      if (t === 'file' || t === 'submit' || t === 'button' || t === 'checkbox' || t === 'radio') continue;
      ins[j].setAttribute('data-init', ins[j].value || '');
    }}
  }}
  function busy() {{
    var fs = document.querySelectorAll('input[type=file]');
    for (var i = 0; i < fs.length; i++) {{
      if (fs[i].files && fs[i].files.length) return 'file';
    }}
    var a = document.activeElement;
    if (a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.tagName === 'SELECT')) return 'focus';
    var ins = document.querySelectorAll('input, textarea');
    for (var j = 0; j < ins.length; j++) {{
      var t = (ins[j].type || '').toLowerCase();
      if (t === 'file' || t === 'submit' || t === 'button' || t === 'checkbox' || t === 'radio') continue;
      var init = ins[j].getAttribute('data-init');
      if (init === null) continue;
      if ((ins[j].value || '') !== init) return 'dirty';
    }}
    return '';
  }}
  function label(msg, color) {{
    var e = document.getElementById('rl');
    if (e) {{ e.textContent = msg; e.style.color = color; }}
  }}
  function tick() {{
    var why = busy();
    if (why === 'file')  {{ label('已选择文件，自动刷新已暂停', '#7fff7f'); return; }}
    if (why === 'focus') {{ label('正在输入，自动刷新已暂停', '#ffcc66'); return; }}
    if (why === 'dirty') {{ label('有未提交的输入，自动刷新已暂停', '#ffcc66'); return; }}
    location.reload();
  }}
  setInterval(tick, SECS * 1000);
  document.addEventListener('DOMContentLoaded', function () {{
    snapshot();
    var fs = document.querySelectorAll('input[type=file]');
    for (var i = 0; i < fs.length; i++) {{
      fs[i].addEventListener('change', function () {{
        if (this.files && this.files.length) {{
          label('已选择 ' + this.files[0].name + '（' + this.files[0].size + ' 字节），自动刷新已暂停', '#7fff7f');
        }} else {{
          snapshot();
        }}
      }});
    }}
  }});
  /* 从往返缓存恢复时重新取基线 */
  window.addEventListener('pageshow', function (ev) {{ if (ev.persisted) snapshot(); }});
}})();
</script>"""


def _html():
    # 用 _state_view()：它会按「C6 最近有没有说话」把 status 修正成
    # online / offline，避免徽标只要连过一次就永远显示在线。
    s = _state_view()

    online = s["status"] == "online"
    badge  = ("online" if online else "offline")

    pj     = s["permit_join"]
    pj_txt = f"🔓 已开启 ({pj.get('seconds',0)} 秒)" if pj.get("open") else "🔒 已关闭"

    # 发射功率：want=命令值，eff=PA 回读值。只有 eff 是实测。
    _tx = s.get("tx_power") or {}
    if _tx.get("eff") is None:
        tx_txt = "– （固件还没上报；旧固件无此功能）"
    else:
        tx_txt = f"<b>{_tx['eff']} dBm</b>"
        if _tx.get("want") is not None and _tx["want"] != _tx["eff"]:
            tx_txt += f"（命令 {_tx['want']}，未生效）"
        if _tx.get("ts"):
            tx_txt += f" · {_tx['ts']}"

    import os as _os

    # 时长估算：每批 0.55 s（含传输 + ACK 往返）。
    # ★ 0.55 s/批 是按 **UART 115200** 实测标定的（约 920 B/s）。TCP 通道下
    #   瓶颈变成 C6 写 flash，实际比这快 —— 所以这里给的是**上界**。
    #   镜像尺寸优先取固件目录里的实际文件，取不到再退回上版观测值。
    _img_candidates = [
        _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                      "firmware", "zigbee-vital-sensor.bin"),
        "/share/zigbee-vital-sensor.bin",
    ]
    _img_size = 1330896
    for _p in _img_candidates:
        try:
            if _os.path.isfile(_p):
                _img_size = _os.path.getsize(_p)
                break
        except OSError:
            pass
    _frames  = -(-_img_size // FW_CHUNK)
    _batches = -(-_frames // FW_BATCH)
    _est_min = _batches * 0.55 / 60
    _flash   = flash_status()

    # TCP 通道下 esptool 直刷必然失败：它要走 USB-Serial-JTAG 进下载模式。
    # 事先在页面上说清楚，省得用户点了之后以为是固件坏了。
    _tcp_warn = ""
    if args.link == "tcp":
        _tcp_warn = ('<p style="font-size:.85em;color:#ff7a7a">⚠ 当前通道是 TCP：'
                     'esptool 必须经 USB-Serial-JTAG 进下载模式，网络够不着芯片，'
                     '这个按钮会直接失败并打印原因。请改用上面的在线 OTA。</p>')

    default_bin = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "client", "build", "zigbee-client.bin"
    )

    dev_rows = ""
    for addr, d in s["devices"].items():
        # ★ 型号有两个来源：_state["devices"]（新代码会同步过去）和 _ha_devices
        #   （老路径只写这里）。两处都查，保证升级前后都已读到的型号立刻能显示。
        _hd    = _ha_devices.get(addr) or {}
        _mfr   = (d.get("mfr") or _hd.get("mfr") or "").strip()
        _model = (d.get("model") or _hd.get("model") or "").strip()
        if not _mfr and not _model:
            _mfr = "–"          # 没读到就明确显示占位，别留空白让人以为是渲染坏了
        clusters_html = ""
        for k, v in d["clusters"].items():
            if isinstance(v, dict) and "payload" in v:
                payload_str = json.dumps(v["payload"], indent=2, ensure_ascii=False)
                ts_str = v.get("ts", "")
            else:
                payload_str = json.dumps(v, indent=2, ensure_ascii=False)
                ts_str = ""
            clusters_html += (
                f'<div style="margin-bottom:.6em">'
                f'<b>{k}</b>'
                + (f' <span style="color:#888;font-size:.85em">{ts_str}</span>' if ts_str else "")
                + f'<pre style="margin:.2em 0 0 0;background:#0d0d1f;padding:.4em;'
                  f'white-space:pre-wrap;word-break:break-all;border-left:2px solid #4a6aaa">'
                  f'{payload_str}</pre></div>'
            )
        clusters_html = clusters_html or "<em>–</em>"
        lqi  = d.get("lqi",  "–")
        rssi = d.get("rssi", "–")
        lqi_html  = _lqi_cell(lqi, 50, d.get("lqi_ts"))
        rssi_html = _rssi_cell(rssi)
        dev_rows += f"""
        <tr style="cursor:pointer" onclick="location.href='/device/{addr}'"
            title="查看详情">
          <td><a href="/device/{addr}" style="color:#7ecbff;text-decoration:none">{addr}</a></td>
          <td style="font-size:.85em;color:#aaa">{_mfr} {('<br>' + _model) if _model else ''}</td>
          <td>{clusters_html}</td>
          <td>{lqi_html}</td>
          <td>{rssi_html}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
{_autoreload_js(10)}
<title>ESP32-C6 网关</title>
<style>
  body{{background:#1a1a2e;color:#e0e0e0;font-family:monospace;margin:2em}}
  h1{{color:#00d4ff}} h2{{color:#a0c4ff;border-bottom:1px solid #333;padding-bottom:.3em}}
  h3{{color:#7ecbff;margin-top:1.2em}}
  table{{border-collapse:collapse;width:100%;margin:.5em 0}}
  th,td{{border:1px solid #444;padding:.4em .8em;text-align:left}}
  th{{background:#2a2a4a}} tr:nth-child(even){{background:#1e1e3a}}
  .badge{{padding:.2em .6em;border-radius:4px;font-size:.85em;margin-left:.5em}}
  .online{{background:#1a5c1a;color:#7fff7f}}
  .offline{{background:#5c1a1a;color:#ff7f7f}}
  button{{background:#2a4a7a;color:#e0e0e0;border:1px solid #4a6a9a;
          padding:.3em .8em;cursor:pointer;border-radius:3px;margin-right:.4em}}
  button:hover{{background:#3a6aaa}}
  section{{background:#16213e;padding:1.2em;margin:1.2em 0;border-radius:6px}}
  footer{{margin-top:2em;color:#666;font-size:.85em}}
</style></head>
<body>
<h1>ESP32-C6 Zigbee Gateway <small style="font-size:.5em;color:#666">v{GW_VERSION}</small></h1>
<p><span id="rl" style="color:#666">自动刷新 10 秒</span> &nbsp;|&nbsp;
   <a href="javascript:location.reload()" style="color:#7ecbff">立即刷新</a> &nbsp;|&nbsp;
   <a href="/topology" style="color:#7ecbff">🗺 网络拓扑</a> &nbsp;|&nbsp;
   <a href="/api/state" style="color:#7ecbff">JSON</a></p>
<section>
  <h2>协调器 <code>{BASE}</code>
    <span class="badge {badge}">{'在线' if online else '离线'}</span>
  </h2>
  <table>
    <tr><th>运行时间</th><td>{s['uptime']} 秒</td></tr>
    <tr><th>信道</th><td>{s['channel']}</td></tr>
    <tr><th>PAN</th><td>{s['pan']}</td></tr>
    <tr><th>允许加入</th><td>{pj_txt}</td></tr>
    <tr><th>发射功率</th><td>{tx_txt}</td></tr>
  </table>
  <h3>允许加入</h3>
  <form method="POST" action="/api/permit_join">
    <button name="secs" value="180">🔓 开启 180 秒</button>
    <button name="secs" value="0">🔒 关闭</button>
  </form>
  <h3>发射功率（dBm）</h3>
  <p style="font-size:.85em;color:#888">改完看 <code>{BASE}/tx_power</code> 的
     <code>eff</code>：那才是 PA 真正在跑的档位。<br>
     注意方向：<b>LQI/RSSI 反映的是设备→网关</b>（网关收到的帧强度），
     改网关发射功率<b>不会</b>改变它。降档唯一能影响的是
     <b>网关→设备</b>这一侧，也就是「命令还能不能送到」。
     要判断哪档合适，看命令成功率，不要看 LQI。</p>
  <form method="POST" action="/api/tx_power">
    <button name="dbm" value="20">20（最大）</button>
    <button name="dbm" value="14">14</button>
    <button name="dbm" value="8">8</button>
    <button name="dbm" value="2">2</button>
    <button name="dbm" value="0">0</button>
  </form>
  <form method="POST" action="/api/lqi">
    <button type="submit">📶 立即取一次 LQI/RSSI</button>
  </form>
  <h3>OTA 固件升级（Zigbee 无线推送）</h3>
  <table>
    <tr><th>Status</th><td>{s['ota']['status']}</td></tr>
    <tr><th>Binary</th><td>{s['ota']['bin_path'] or '–'}</td></tr>
    <tr><th>Image type</th><td>0x{s['ota']['image_type']:04x}</td></tr>
    <tr><th>进度</th><td>{s['ota']['transferred']} / {s['ota']['size']} Byte</td></tr>
  </table>
  <p style="font-size:.85em;color:#888">
    目标类型必须和设备的固件一致：<code>0x0001</code> 终端节点（client/）·
    <code>0x0002</code> 中继器（router/）。选错不会刷坏，只是没人应答。<br>
    设备页里也能单独上传 —— 每个 image type 各存一份，互不覆盖。
  </p>
  <form method="POST" action="/api/ota" enctype="multipart/form-data">
    <select name="type" style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em">
      <option value="0x0001">0x0001 · 终端节点 (client)</option>
      <option value="0x0002">0x0002 · 中继器 (router)</option>
    </select>
    <input type="file" name="file" accept=".bin"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    <button type="submit">&#x2B06; 上传并开始 OTA</button>
  </form>
  <form method="POST" action="/api/ota">
    <select name="type" style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em">
      <option value="0x0001">0x0001 · 终端节点</option>
      <option value="0x0002">0x0002 · 中继器</option>
    </select>
    <input name="path" value="{s['ota']['bin_path'] or default_bin}" size="50"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    <button type="submit">⬆ 按路径开始 OTA</button>
  </form>
  <h3>协调器固件升级（在线 OTA，走当前通道）</h3>
  <p style="font-size:.85em;color:#888">帧 {FW_CHUNK} B ≈ {2*FW_CHUNK+50} 字符/行
     （旧固件 256 字节缓冲也能装下）· {FW_BATCH} 帧/批 = {FW_CHUNK*FW_BATCH} B
     · 镜像 {_img_size/1048576:.1f} MB 预计约 {_est_min:.0f} 分钟
     （按串口 115200 标定；TCP 通道更快，实际更短）</p>
  <form method="POST" action="/api/fw_ota" enctype="multipart/form-data">
    <input type="file" name="file" accept=".bin"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    <button type="submit">&#x2B06; 上传并升级协调器</button>
  </form>
  <p style="font-size:.85em;color:#888">或填写容器内已有文件的路径（<code>/share/…</code> 可由 Samba 访问）：</p>
  <form method="POST" action="/api/fw_ota">
    <input name="path" value="" placeholder="/share/zigbee-vital-sensor.bin" size="50"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    <button type="submit">按路径升级</button>
  </form>
  <p style="font-size:.85em;color:#888">状态: {s['fw_ota']['status']}
     &nbsp;|&nbsp; {s['fw_ota']['off']} / {s['fw_ota']['size']} 字节</p>

  <h3>应急：esptool 直刷（应用层 OTA 卡死时用）</h3>
  <p style="font-size:.85em;color:#ffcc66">绕过应用层 OTA，直接用 Bootloader 通过 USB
     写入 <code>{APP_OFFSET}</code>（ota_0）。刷写期间串口会被独占、桥接暂停，
     完成后自动重开。适用于旧固件收不到 <code>fw_ota_data</code>（一直停在 1280 字节）的情况。</p>
  {_tcp_warn}
  <form method="POST" action="/api/fw_flash" enctype="multipart/form-data">
    <input type="file" name="file" accept=".bin"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    <button type="submit">&#x26A1; 上传并用 esptool 直刷</button>
  </form>
  <p style="font-size:.85em;color:#888">或按容器内路径直刷：</p>
  <form method="POST" action="/api/fw_flash">
    <input name="path" value="" placeholder="/share/zigbee-vital-sensor-devcmd-fix.bin" size="50"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    <button type="submit">按路径直刷</button>
  </form>
  <p style="font-size:.85em;color:#888">直刷状态: <b>{_flash['status']}</b>
     {('· ' + _flash['bin']) if _flash['bin'] else ''}</p>
  {('<pre style="background:#0d0d1f;padding:.6em;font-size:.8em;max-height:10em;overflow:auto;white-space:pre-wrap">' + chr(10).join(_flash['tail']) + '</pre>') if _flash['tail'] else ''}
  <h3>Zigbee 设备 ({len(s['devices'])})</h3>
  <table>
    <tr><th>地址</th><th>制造商 / 型号</th><th>Payload</th><th>LQI</th><th>RSSI</th></tr>
    {dev_rows or '<tr><td colspan="5"><em>暂无设备</em></td></tr>'}
  </table>
</section>
<section>
  <h2>原始状态</h2>
  <pre style="background:#0d0d1f;padding:1em;overflow-x:auto;white-space:pre-wrap;word-break:break-all;font-size:.85em">{json.dumps(s, indent=2, ensure_ascii=False)}</pre>
</section>
<footer>
  主题前缀: <code>{BASE}</code> &nbsp;|&nbsp; 数据库: <code>{DB_HOST}/{DB_NAME}</code>
  &nbsp;|&nbsp; OTA 帧: <code>{FW_CHUNK} B</code> &nbsp;|&nbsp; 版本: <code>v{GW_VERSION}</code>
</footer>
</body></html>"""


def _device_html(addr):
    """单个 Zigbee 设备的详情页。"""
    with _state_lock:
        s = json.loads(json.dumps(_state))
    dev = s["devices"].get(addr)
    if dev is None:
        return None

    clusters_html = ""
    for k, v in dev["clusters"].items():
        if isinstance(v, dict) and "payload" in v:
            payload_str = json.dumps(v["payload"], indent=2, ensure_ascii=False)
            ts_str = v.get("ts", "")
        else:
            payload_str = json.dumps(v, indent=2, ensure_ascii=False)
            ts_str = ""
        clusters_html += f"""
        <section style="margin-bottom:1.2em">
          <h3 style="color:#7ecbff;margin-bottom:.4em">{k}
            {"<span style='color:#888;font-size:.8em;margin-left:.5em'>" + ts_str + "</span>" if ts_str else ""}
          </h3>
          <pre style="background:#0d0d1f;padding:1em;white-space:pre-wrap;
               word-break:break-all;border-left:3px solid #4a6aaa;margin:0;
               font-size:.9em">{payload_str}</pre>
        </section>"""

    lqi  = dev.get("lqi",  "–")
    rssi = dev.get("rssi", "–")
    # 型号/厂商来自 Basic 簇（0x0005 / 0x0004）。只在入网那一刻读过一次，
    # 那一次没读到的设备这里就是空的 —— 给个按钮再问一遍。
    _bsc  = (dev.get("clusters") or {}).get("basic")
    _bpl  = (_bsc.get("payload") or {}) if isinstance(_bsc, dict) else {}
    model = (_bpl.get("model") or "").strip()
    mfr   = (_bpl.get("mfr") or "").strip()
    model_html = (model if model else
                  '<span style="color:#f90">未读到</span> '
                  '<span style="color:#888;font-size:.85em">（入网时那次 Basic 没答上，'
                  '点下面「重新读型号」）</span>')
    lqi_bar = _lqi_cell(lqi, 80, dev.get("lqi_ts"))

    # 这台设备**自己**最后一次发帧的时刻（不是心跳批次时间）
    _seen = dev.get("seen")
    _seen_txt = (_ago(_seen) if _seen else
                 '<span style="color:#888">—（还没有帧 / 刚重启）</span>')
    if _avail_online(addr):
        _avail_txt = ('<b style="color:#6f6">在线</b> '
                      '<span style="color:#888">（HA 实体可用）</span>')
    else:
        _avail_txt = ('<b style="color:#f66">不可用</b> '
                      '<span style="color:#888">（ZDO 探测失败或长时间没有证据，'
                      'HA 里对应实体会变灰）</span>')

    # Node-Descriptor 结果（路由器还是终端设备？）—— 只在查过之后才有
    nd = (_state.get("node_desc") or {}).get(addr)
    if nd and nd.get("role"):
        role_txt = ("<b style='color:#6f6'>路由器（ZR）—— 会转发他人流量</b>"
                    if nd["role"] == "router" else
                    "<b style='color:#f90'>终端设备（ZED）—— 不转发</b>")
        role_html = (f'{role_txt}<br><span style="color:#888">cap={nd.get("cap")} · '
                     f'{nd.get("power")} · rx_idle={nd.get("rx_idle")}</span>')
    elif nd and nd.get("error"):
        role_html = f'<span style="color:#f66">查询失败 ({nd.get("error")})</span>'
    else:
        role_html = '<span style="color:#888">未查询（下方“命令”里选 node_desc）</span>'

    # 连接方式：直连还是经中继。数据来自 /topology 采集的三张表，
    # 没采集过就提示去点一下（Zigbee 按需建路，不探测就查不到）。
    _tp = s.get("topo") or {}
    _parent, _how, _hlqi, _depth, _links = _topo_rel(_tp, s.get("parent") or {})
    _coord = _tp.get("addr") or "0x0000"
    _hl = _hlqi.get(addr)
    _hl_txt = f" · 该跳 LQI {_hl}" if isinstance(_hl, int) else ""
    if addr == _coord:
        link_html = '<b style="color:#c0392b">协调器本身</b>'
    elif _parent.get(addr) == _coord:
        link_html = ('<b style="color:#6f6">直连协调器</b> '
                     f'<span style="color:#888">（一跳，不经过中继{_hl_txt}）</span>')
    elif _parent.get(addr):
        _p = _parent[addr]
        link_html = (f'<b style="color:#e6b422">经中继 {_p}</b> '
                     f'<span style="color:#888">（依据：{_how.get(addr, "?")}{_hl_txt}）</span> '
                     f'· <a href="/topology" style="color:#7ecbff">看拓扑图</a>')
    else:
        link_html = ('<span style="color:#888">未知 – 先到 '
                     '<a href="/topology" style="color:#7ecbff">网络拓扑</a>'
                     ' 点一次「刷新拓扑」</span>')

    # Image-Typ vorbelegen:中继固件的 model 是 ESP32-C6-Repeater。
    # 认不出来时默认终端节点（0x0001），用户可以自己改。
    model_l = (dev.get("model") or "").lower()
    default_type = 0x0002 if ("repeater" in model_l or "中继" in model_l) else 0x0001
    sel_ed = "selected" if default_type == 0x0001 else ""
    sel_rp = "selected" if default_type == 0x0002 else ""

    wr = (s.get("wr_power") or {}).get(addr)
    if wr:
        last_wr = (f"{wr.get('ts','')} 已下发 <b>{wr.get('dbm')} dBm</b>"
                   f"（tsn {wr.get('tsn','?')}；以中继自己的串口日志为准）")
    else:
        last_wr = '<span style="color:#888">本次运行还没下发过</span>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
{_autoreload_js(15)}
<title>设备 {addr}</title>
<style>
  body{{background:#1a1a2e;color:#e0e0e0;font-family:monospace;margin:2em}}
  h1{{color:#00d4ff}} h2{{color:#a0c4ff;border-bottom:1px solid #333;padding-bottom:.3em}}
  h3{{color:#7ecbff;margin-top:1.2em}}
  table{{border-collapse:collapse;width:100%;margin:.5em 0}}
  th,td{{border:1px solid #444;padding:.4em .8em;text-align:left}}
  th{{background:#2a2a4a}} tr:nth-child(even){{background:#1e1e3a}}
  section{{background:#16213e;padding:1.2em;margin:1.2em 0;border-radius:6px}}
  a{{color:#7ecbff}}
  button{{background:#2a4a7a;color:#e0e0e0;border:1px solid #4a6a9a;
          padding:.3em .8em;cursor:pointer;border-radius:3px}}
  button:hover{{background:#3a6aaa}}
</style></head>
<body>
<p><a href="/">← 返回仪表盘</a></p>
<h1>设备 <code>{addr}</code></h1>
<section>
<table>
  <tr><th>地址</th><td>{addr}</td></tr>
  <tr><th>Name</th><td>{dev.get('name', addr)}</td></tr>
  <tr><th>型号</th><td>{model_html}</td></tr>
  <tr><th>厂商</th><td>{mfr or '–'}</td></tr>
  <tr><th>IEEE</th><td>{dev.get('ieee', '–')}</td></tr>
  <tr><th>LQI</th><td>{lqi_bar}</td></tr>
  <tr><th>RSSI</th><td>{_rssi_cell(rssi)}</td></tr>
  <tr><th>角色</th><td>{role_html}</td></tr>
  <tr><th>连接方式</th><td>{link_html}</td></tr>
  <tr><th>最后上报</th><td>{_seen_txt}
    <span style="color:#888;font-size:.85em">（这台设备<b>自己</b>最后一次发帧的时刻；
    重启 add-on 会清零，所以重启后显示「—」是正常的，不代表掉线）</span></td></tr>
  <tr><th>可用性</th><td>{_avail_txt}</td></tr>
  <tr><th>状态刷新</th><td>{dev.get('last_seen', '–')}
    <span style="color:#888;font-size:.85em">（固件心跳批次时间：整批设备一起刷新，
    不是这台设备的通信时刻）</span></td></tr>
</table>
</section>
<h2>Cluster 数据 ({len(dev["clusters"])})</h2>
{clusters_html or "<em>暂无数据</em>"}
<section>
  <h2>命令</h2>
  <h3 style="color:#7ecbff;margin-top:.2em">重新读型号（ZCL Basic 0x0004 / 0x0005）</h3>
  <p style="font-size:.85em;color:#888;line-height:1.6;margin:.2em 0 .5em">
    型号<b>只在入网那一刻读一次</b>，网关随后把「已面试」写进 NVS，不会自动重读 ——
    那次没答上（休眠端点 / 刚入网抖动 / 当年固件还没逐端点扫 Basic）就永久缺失。
    这里手动补问一次；补读按端点逐个发，2~3 秒后刷新页面看结果。
  </p>
  <form method="POST" action="/api/device/{addr}/probe">
    <button type="submit">重新读型号</button>
  </form>
  <h3 style="color:#7ecbff;margin-top:1em">其他命令</h3>
  <form method="POST" action="/api/device/{addr}/cmd">
    <select name="cmd" style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em">
      <option value="switch2wifi">switch2wifi</option>
      <option value="node_desc">node_desc（查角色：Router / End Device）</option>
    </select>
    <button type="submit">发送</button>
  </form>
  <form method="POST" action="/api/device/{addr}/delete"
        onsubmit="return confirm('彻底移除设备 {addr}？\\n\\n会做四件事：\\n① 让协调器把这条记录从表里和 NVS 里删掉（并向设备补发一次 Mgmt_Leave）\\n② 删除全部 MQTT retained 状态话题\\n③ 删除 HA 的 Discovery 配置（实体变成“已移除”）\\n④ 清掉可用性标记\\n\\n设备若仍在本网络中，它会掉网；要重新用就重新配对。')">
    <button type="submit" style="margin-top:.5em;background:#5a1a1a;color:#ffaaaa;border:1px solid #aa3333;padding:.25em .75em">&#x1F5D1; 从网关彻底移除（含 HA 实体）</button>
  </form>
  <h3 style="color:#7ecbff;margin-top:1em">休眠间隔</h3>
  <form method="POST" action="/api/sleep">
    <input name="addr" type="hidden" value="{addr}">
    <input name="secs" type="number" value="600" min="10" max="86400"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;
                  padding:.25em;width:80px"> 秒
    <button type="submit">设置</button>
  </form>
  <h3 style="color:#7ecbff;margin-top:1em">OTA 固件升级</h3>
  <p style="font-size:.85em;color:#888;line-height:1.6">
    不同设备跑的是不同固件，<b>必须先选对目标类型</b>：网关只把该 image type 的
    镜像通知给对应设备，类型不符的设备不会应答（不会刷坏，但也没反应）。<br>
    <code>0x0001</code> = 终端节点（client/，switch2wifi）&nbsp;·&nbsp;
    <code>0x0002</code> = 中继器（router/）<br>
    版本号取镜像文件的 mtime —— 重新编译出来的镜像自动就算是“更新”，不用手工填版本。
  </p>
  <form method="POST" action="/api/ota" enctype="multipart/form-data">
    <p style="margin:.3em 0">
      <select name="type" style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em">
        <option value="0x0001" {sel_ed}>0x0001 · 终端节点 (client)</option>
        <option value="0x0002" {sel_rp}>0x0002 · 中继器 (router)</option>
      </select>
      <input type="file" name="file" accept=".bin"
             style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    </p>
    <button type="submit">&#x2B06; 上传并开始 OTA</button>
  </form>
  <p style="font-size:.85em;color:#888;margin-top:.9em">或使用容器内已有的路径（<code>/share/…</code> 可由 Samba 访问）：</p>
  <form method="POST" action="/api/ota">
    <select name="type" style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em">
      <option value="0x0001" {sel_ed}>0x0001 · 终端节点</option>
      <option value="0x0002" {sel_rp}>0x0002 · 中继器</option>
    </select>
    <input name="path" value="" placeholder="/share/zigbee-repeater.bin" size="40"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    <button type="submit">按路径开始</button>
  </form>
  <p style="font-size:.85em;color:#888;margin-top:.8em">
    OTA 状态: <b>{s['ota']['status']}</b>
    &nbsp;·&nbsp; image type 0x{s['ota']['image_type']:04x}
    &nbsp;·&nbsp; {s['ota']['transferred']} / {s['ota']['size']} Byte<br>
    {s['ota']['bin_path'] or '（尚未选择镜像）'}<br>
    中继是常电 Router，不涉及休眠设备的轮询窗口；64 B/块的速度下
    569 KB 大约 10~30 分钟，期间别断电也别关这个页面所在的 add-on。
  </p>
  <h3 style="color:#7ecbff;margin-top:1em">中继发射功率（运行时，不用重刷固件）</h3>
  <p style="font-size:.85em;color:#888;line-height:1.6">
    写入中继固件里的厂商专有簇 <code>0xFC00 / 0x0001</code>（S8，dBm）：
    中继收到就立刻改 PA 并写进自己的 NVS，掉电再上电还是这个值。<br>
    <b>只对中继固件有效</b>（普通开关没有这个簇，写了也不会有应答）。<br>
    20 dBm 电流峰值最高，供电偏弱会触发 Brownout 反复复位 —— 遇到就降到 8/14。
  </p>
  <form method="POST" action="/api/device/{addr}/tx_power">
    <button name="dbm" value="20">20（最大）</button>
    <button name="dbm" value="14">14</button>
    <button name="dbm" value="8">8</button>
    <button name="dbm" value="2">2</button>
    <button name="dbm" value="0">0</button>
  </form>
  <p style="font-size:.85em;color:#888;margin-top:.5em">
    下发记录: {last_wr}
  </p>
</section>
<footer style="margin-top:2em;color:#666;font-size:.85em">
  <span id="rl" style="color:#666">自动刷新 15 秒</span> &nbsp;|&nbsp;
  <a href="javascript:location.reload()">立即刷新</a> &nbsp;|&nbsp; <a href="/api/state">JSON 状态</a>
</footer>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass   # stdout 里不要访问日志

    def _send(self, code, ctype, body):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(data))
        # ★ 禁止浏览器缓存：拓扑页每 15 s 用 location.reload() 自刷新，
        #   一旦被缓存就会"看起来一直不变"，非得手动点刷新才更新。
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _html())
        elif path.startswith("/device/"):
            addr = path[len("/device/"):]
            page = _device_html(addr)
            if page:
                self._send(200, "text/html; charset=utf-8", page)
            else:
                self._send(404, "text/html; charset=utf-8",
                           f"<h2>设备 {addr} 不存在</h2><a href='/'>← 返回</a>")
        elif path == "/api/state":
            # 也走 _state_view()：JSON 里的 status 必须和页面徽标一致，否则
            # 「页面说离线、API 说在线」又得多排查一轮。
            self._send(200, "application/json",
                       json.dumps(_state_view(), indent=2))
        elif path in ("/topology", "/topo"):
            self._send(200, "text/html; charset=utf-8", _topo_html())
        elif path == "/api/topo":
            with _state_lock:
                self._send(200, "application/json",
                           json.dumps(_state.get("topo") or {}, indent=2))
        else:
            self._send(404, "text/plain", "Not Found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        # ★ 做 multipart 解析时**不能**把 Content-Type 转小写：
        #   boundary 参数是**大小写敏感**的。Chrome 和 curl 发的名字都是
        #   混合大小写（----WebKitFormBoundary…、----2YSIdO…）。
        #   一旦 .lower()，body.split(boundary) 就切不开，整个 body 会被当成
        #   一个 part —— 后果：image_type 落回 0x0001，而且文件开头多出
        #   ~218 字节的 part 头（OTA 随后在镜像校验处失败）。
        raw_ctype = self.headers.get("Content-Type") or ""
        ctype  = raw_ctype.lower()
        raw    = self.rfile.read(length) if length else b""
        # 文件上传（multipart）不要走 parse_qs —— 那会把二进制数据弄坏。
        if ctype.startswith("multipart/form-data"):
            params = {}
        else:
            params = parse_qs(raw.decode("utf-8", errors="replace"))
        # 顺便把 query string 也收进来：parse_qs(raw) 只读 body，
        # 于是 /api/device/x/tx_power?dbm=20 悄悄落回了默认值（8 dBm），
        # 而且没有任何报错。
        for _k, _v in parse_qs(urlparse(self.path).query).items():
            params.setdefault(_k, _v)
        path   = urlparse(self.path).path
        if path == "/api/topo":
            # 刷新拓扑：后台逐台探测 + dump，立即 303 回拓扑页
            _topo_refresh()
            self.send_response(303)
            self.send_header("Location", "/topology")
            self.end_headers()
            return

        if path.startswith("/api/device/") and path.endswith("/probe"):
            # 手动补读型号：型号只在入网时读一次，错过就永久缺失，
            # 这里给一次「再问一遍」的机会（读 Basic 0x0004/0x0005）。
            addr = path[len("/api/device/"):-len("/probe")]
            _mb_next.pop(addr, None)
            _mb_tries.pop(addr, None)
            _probe_basic(addr)
            self.send_response(303)
            self.send_header("Location", f"/device/{addr}")
            self.end_headers()
            return

        if path.startswith("/api/device/") and path.endswith("/cmd"):
            addr = path[len("/api/device/"):-len("/cmd")]
            cmd  = params.get("cmd", [""])[0]
            if cmd == "switch2wifi":
                uart_msg = _js({"cmd": "switch2wifi"})
                ser.write(uart_msg.encode())
                print(f"[→C6] switch2wifi → {addr}")
            elif cmd == "node_desc":
                # ZDO 0x0002：MAC 能力位能看出设备是否作为路由器工作
                # （bit1）、是否接着市电（bit2）。
                uart_msg = _js({"cmd": "node_desc", "addr": addr})
                ser.write(uart_msg.encode())
                print(f"[→C6] {uart_msg.strip()}")
            # （早先那个 "leave" 是空包弹：固件从来没实现过这个命令。
            #  已换成自己的 dev_remove 路径，
            #  见 /api/device/<addr>/delete。）
            self.send_response(302)
            self.send_header("Location", f"/device/{addr}")
            self.end_headers()
            return

        if path.startswith("/api/device/") and path.endswith("/tx_power"):
            addr = path[len("/api/device/"):-len("/tx_power")]
            dbm = params.get("dbm", ["8"])[0].strip()
            try:
                dbm_i = int(dbm)
            except ValueError:
                dbm_i = 8
            dbm_i = max(-24, min(20, dbm_i))
            # 网关把它翻成 ZCL Write Attribute 0xFC00/0x0001 发给中继
            mq_put(f"{BASE}/cmd/set_dev_power",
                   json.dumps({"addr": addr, "ep": 1, "dbm": dbm_i},
                              separators=(",", ":")), qos=1)
            print(f"[Web] set_dev_power {addr} → {dbm_i} dBm")
            self.send_response(303)
            self.send_header("Location", f"/device/{addr}")
            self.end_headers()
            return

        if path.startswith("/api/device/") and path.endswith("/delete"):
            addr = path[len("/api/device/"):-len("/delete")]
            _device_remove(addr, "在网页上删除")
            try:
                with _db_lock:
                    cx = _db_connect()
                    if cx:
                        with cx.cursor() as c:
                            c.execute("DELETE FROM esp32_zigbee_devices WHERE mac=%s AND addr=%s",
                                      (DB_MAC, addr))
                        cx.commit()
            except Exception as e:
                print(f"[DB] delete {addr}: {e}")
            print(f"[Web] device {addr} deleted")
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return

        # ★★★ 2026-11-07：下面这三条以前走 mq_put(f"{BASE}/cmd/…")，
        #   而 **C6 固件里根本没有 MQTT 订阅**（`ha_mqtt.c` 里既没有
        #   `esp_mqtt_client_subscribe` 也没有 `MQTT_EVENT_DATA` 分支）——
        #   PUBLISH 出去的消息没有任何接收方，按钮点了等于没点。
        #   实测代价：为了排查设备在不在网，我在 Web 上开了 180 s 入网窗口，
        #   其实一条命令都没到芯片上，白等了三分钟还误判了一次。
        #   现在统一走 ser.write()，和 /api/wifi_scan、/api/ota 一样。
        if path == "/api/permit_join":
            secs = params.get("secs", ["0"])[0].strip() or "0"
            try:
                secs_i = int(secs)
            except ValueError:
                secs_i = 0
            ser.write(_js({"cmd": "permit_join", "secs": secs_i}).encode())
            print(f"[Web] permit_join {secs_i}s（已下发到通道）")

        elif path == "/api/tx_power":
            dbm = params.get("dbm", [""])[0].strip()
            body = {"cmd": "set_tx_power", "dbm": dbm}
            try:
                body["dbm"] = int(dbm)
            except ValueError:
                pass                      # 让 C6 自己判非法值并回错误
            ser.write(_js(body).encode())
            print(f"[Web] set_tx_power {dbm} dBm（已下发，回读值稍后给出）")

        elif path == "/api/lqi":
            ser.write(b'{"cmd":"lqi"}\n')
            print("[Web] 已请求一次 LQI 快照（已下发到通道）")

        elif path == "/api/cmd":
            # 通用命令透传：把一条 JSON 原样写进当前通道。
            #   curl -X POST -d '{"cmd":"probe","addr":"0x8e4e","ep":1}' \
            #        http://<addon>:8082/api/cmd
            # 为什么需要它：Web UI 只硬编码了少数几个按钮，而 C6 支持的命令
            # 有几十条（node_desc / probe / topo / read_onoff / dev_remove …）。
            # 以前每查一次就得改一次脚本、重建一次镜像；有了这个口子，
            # 排查时不用再动代码。
            body = (params.get("json", [""])[0].strip()
                    or raw.decode("utf-8", errors="replace").strip())
            if body:
                try:
                    obj = json.loads(body)
                    ser.write(_js(obj).encode())
                    print(f"[Web] 命令透传 → {body}")
                except (ValueError, TypeError) as e:
                    print(f"[Web] /api/cmd JSON 解析失败：{e}；收到的是 {body[:120]}")
            else:
                print("[Web] /api/cmd 没收到内容（需要在 body 里给一条 JSON）")

        elif path == "/api/fw_ota":
            if ctype.startswith("multipart/form-data"):
                bin_path = _save_upload(raw_ctype, raw) or ""
            else:
                bin_path = params.get("path", [""])[0].strip()
            if bin_path:
                # 手动启动会把自动重启的次数预算清零
                globals()["_fw_ota_restarts"] = 0
                _fw_ota_start(bin_path)
            else:
                print("[OTA] 没有固件路径 / 上传为空 —— 已中止")

        elif path == "/api/fw_flash":
            # 走 bootloader（esptool）的应急通道，不管当前固件
            # 认不认 fw_ota_data。
            if ctype.startswith("multipart/form-data"):
                bin_path = _save_upload(raw_ctype, raw) or ""
            else:
                bin_path = params.get("path", [""])[0].strip()
            if bin_path and os.path.isfile(bin_path):
                size = os.path.getsize(bin_path)
                print(f"[FLASH] 收到刷机请求：{bin_path}（{size} B @ {APP_OFFSET}）")
                threading.Thread(target=flash_apply, args=(bin_path,),
                                 daemon=True).start()
            else:
                print("[FLASH] 没有固件路径 / 上传为空 —— 已中止")

        elif path == "/api/wifi_scan":
            # 让 C6 自己扫一遍 2.4 GHz。结果会以 [SCAN] 打出来，也进 /api/state。
            # ★ 排查 Wi-Fi 连不上时的第一条命令：主机扫描结果不能当依据。
            ser.write(_js({"cmd": "wifi_scan"}).encode())
            print("[SCAN] 已请求 C6 扫频，结果稍后以 [SCAN] 打印")

        elif path == "/api/ota/stop":
            # ★ 中止 OTA：否则一次误点会白占一个多小时的无线带宽（~110 B/s）。
            # 网关侧 zb_ota_server_stop() 会停掉预取任务，客户端等不到数据就自己放弃。
            ser.write(_js({"cmd": "ota_stop"}).encode())
            _ota_close()
            with _state_lock:
                _state["ota"]["status"] = "idle"
            print("[OTA] 已中止（ota_stop 已下发）")

        elif path == "/api/serial_reopen":
            # 手动兜底：通道卡住时不用重启整个 add-on。
            # 串口：先重开 fd；再硬复位一次 C6（端口活着但芯片挂了也一并治好）。
            # TCP：丢掉旧连接重新监听（C6 那边会自动重连）。
            # ⚠ 串口路径最多阻塞 ~45 s（6 次指数退避重试），浏览器可能先超时，属正常。
            print("[Link] 收到手动重开请求")
            ok = _link_reopen("手动 /api/serial_reopen")
            print(f"[Link] 手动重开结果：{'成功' if ok else '失败（见上面的重试日志）'}")
            if ok:
                _link_kick_reset("手动 /api/serial_reopen 配套复位")

        elif path == "/api/ota":
            # image type: 0x0001 终端节点 / 0x0002 中继器
            if ctype.startswith("multipart/form-data"):
                image_type = _ota_image_type(_mp_field(raw_ctype, raw, "type"))
                # 每个类型独立的落盘文件：否则上传中继镜像会覆盖终端节点镜像
                base = "/data" if os.path.isdir("/data") else "/tmp"
                dst = f"{base}/ota_0x{image_type:04x}.bin"
                bin_path = _save_upload(raw_ctype, raw, dst=dst) or ""
            else:
                image_type = _ota_image_type(params.get("type", [None])[0])
                bin_path = params.get("path", [""])[0].strip()
            print(f"[OTA] /api/ota type=0x{image_type:04x} path={bin_path or '(空)'}")
            _ota_start(bin_path, image_type)

        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()


class _ReuseHTTPServer(HTTPServer):
    allow_reuse_address = True

def _run_web():
    srv = _ReuseHTTPServer(("0.0.0.0", WEB_PORT), _Handler)
    print(f"[Web] Dashboard → http://0.0.0.0:{WEB_PORT}/")
    srv.serve_forever()

# ── Start ───────────────────────────────────────────────────────────────────
db_init()

if args.link == "tcp":
    print(f"[Link] 传输通道 = Wi-Fi/TCP，监听 {args.tcp_bind}:{args.tcp_port}")
else:
    print(f"[Link] 传输通道 = 串口 {args.port} @ {args.baud}")
# ★ 开机打不开端口时**不要**直接抛异常退出：那会被 Supervisor 判定成
#   "启动失败" 反复重启（2026-10-26 重建后就是这样）。先用占位通道把
#   Web UI / MQTT 拉起来，主循环的看门狗会自己重连。
ser = _link_open()

# 加载持久化的节点归属 —— 必须在第一次 discovery 发布**之前**，
# 否则重启后会生成新的 Node-ID，也就生成了新实体。
_nodes_load()
_txp_load()

print(f"[MQTT] 正在连接 {args.broker}:{args.mqport} …")
mq.connect(args.broker, args.mqport, keepalive=60)
mq.loop_start()

threading.Thread(target=_run_web, daemon=True).start()
# 通道心跳：C6 靠「最近从哪条通道收到命令」决定上报往哪发。
# 没有它，C6 在两条通道都静默时就只能双发；有了它，一条通道双向都能用。
_hb_start()
# 型号补读：入网那次没读到 Basic 的设备，定期补问（指数退避，读到就停）
threading.Thread(target=_model_backfill_task, daemon=True).start()
# 拓扑自动采集：让拓扑页像 HA 的 BLE 图那样自己更新，不用手点刷新
if args.topo_interval and int(args.topo_interval) > 0:
    threading.Thread(target=_topo_auto_loop,
                     args=(int(args.topo_interval),), daemon=True).start()
    print(f"[TOPO] 自动采集已开启：每 {int(args.topo_interval)} s 一轮")
else:
    print("[TOPO] 自动采集已关闭（--topo-interval 0）")
# 每台设备的可用性：把"记分板"换算成 online/offline 推给 HA
if DEVICE_AVAIL and HA_ENABLED:
    threading.Thread(target=_avail_task, daemon=True).start()
    print("[AVAIL] 设备级可用性已开启（掉线设备的 HA 实体会变灰）")
else:
    print("[AVAIL] 设备级可用性已关闭（所有实体只看网关在线）")

# ★ 只在 boot 时重发还不够：如果 C6 没重启、只有 add-on 重启了，C6 那边
#   其实还留着上次设的功率，但 `_state["tx_power"]` 是空的、网页显示 "—"。
#   这里补一发：功率本来就在，属于幂等操作；顺便把回读值填上。
_txp_reapply(8.0)

# ── Hauptschleife ───────────────────────────────────────────────────────────
print("[Bridge] 运行中 —— Ctrl+C 退出")
buf = b""
read_err  = 0        # 连续读失败次数（成功一次就归零）
err_since = 0.0      # 第一连败的时刻，用来限速日志
last_rx   = time.time()   # 最后一次收到任意字节的时刻（判断"端口活着但没数据"）
last_kick = 0.0      # 上一次强制复位的时刻（限速，别把 C6 反复复位）
try:
    while True:
        # esptool 刷写期间端口是关着的、或被 bootloader 占着 ——
        # 读取循环先停一会儿。
        if _serial_pause.is_set():
            time.sleep(0.2)
            continue
        # ★ 饿死看门狗：通道不报错但**一个字节都没有**。协调器正常情况下
        #   每几秒就有心跳/上报，连续 45 s 静默 = 芯片侧挂了（固件卡死、
        #   USB-JTAG 端点停了），重开 fd 治不好，只能硬复位。见 _serial_kick_reset。
        #   ★ TCP 通道没有 RTS，也没有「端口」这个实体，这条看门狗对它无意义
        #     （can_reset=False）—— 断线由 CoordinatorTcp.read 自己发现并重连。
        if (getattr(ser, "can_reset", True)
                and time.time() - last_rx > 45 and time.time() - last_kick > 90):
            last_kick = time.time()
            _link_kick_reset(f"已 {int(time.time() - last_rx)} s 没收到任何字节")
            last_rx = time.time()      # 给它 45 s 重新起来
        try:
            raw = ser.read(ser.in_waiting or 1)
        except Exception as e:
            # ★ 关键：**重试同一个 fd 是没用的**。USB-CDC 端点失效后旧 fd
            #   永远返回 EIO（实测 100+ 次连败），必须关掉重开。见 _serial_reopen。
            read_err += 1
            now = time.time()
            if read_err == 1:
                err_since = now
                print(f"[Link] 读取出错：{e} —— 累计到 10 次就重开通道")
            elif read_err == 10 or (read_err % 60 == 0):
                print(f"[Link] 读失败 {read_err} 次 / {now - err_since:.0f} s"
                      f"（最后一次：{e}）→ 重开通道")
            if read_err >= 10 and not _serial_pause.is_set():
                if _link_reopen(f"连续 {read_err} 次读失败: {e}"):
                    buf = b""       # 半行必须丢掉，否则和下一行粘成非法 JSON
                    read_err = 0
                    last_rx  = time.time()
            time.sleep(0.5)
            continue
        if read_err:
            print(f"[Link] 读取恢复正常（此前连续失败 {read_err} 次）")
            read_err = 0
        if raw:
            last_rx = time.time()
            buf += raw
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line:
                    decoded = line.decode("utf-8", errors="replace")
                    print(f"[C6] {decoded}")
                    dispatch(decoded)
except KeyboardInterrupt:
    print("\n已退出。")
finally:
    mq_put(f"{BASE}/status", "offline", retain=True, qos=1)
    db_upsert_gateway("offline")
    mq.loop_stop()
    ser.close()
