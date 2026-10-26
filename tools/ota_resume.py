#!/usr/bin/env python3
"""重启一次中继 OTA 会话，并当场判定「服务端游标和客户端 fileOffset 是否同步」。

背景（2026-10-26）：协调器在传输中途重启会让服务端游标归零，而客户端还记着
自己的 offset。服务端于是把文件开头的字节当成"下一块"发出去，客户端照单写进
它 flash 的 offset 位置 ⇒ 镜像中段错位，传再多也过不了 esp_ota_end。

所以每次起会话必须核对两件事：
  ① 客户端第一个块请求的 offset 是不是 0（不是 0 说明它还惦记着上一包，先停服务端逼它超时）；
  ② 网关 stat 行的 "sync" 是不是 0（非 0 = 中途又失步了）。

用法：
    python tools/ota_resume.py                # 默认 /data/ota_0x0002.bin, type=2
    python tools/ota_resume.py <watch_s>      # 观察秒数，默认 90
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

BASE = os.environ.get("ADDON_BASE", "http://192.168.101.5:8082")
HA = os.environ.get("HA_BASE", "http://192.168.101.5:8123")
SLUG = "local_esp32c6_gateway"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_OPENER)

RE_APS = re.compile(r'"cl":"0x0019","sec":\d+,"len":(\d+),"hex":"([0-9a-f]+)"')
RE_STAT = re.compile(r'\[(\d\d:\d\d:\d\d)\][^"]*?(\{"t":"ota_dbg","stat".*)')


def get_state():
    with urllib.request.urlopen(BASE + "/api/state", timeout=10) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def ota_start(path="/data/ota_0x0002.bin", image_type=2):
    body = urllib.parse.urlencode({"path": path, "type": image_type}).encode()
    req = urllib.request.Request(BASE + "/api/ota", data=body, method="POST",
                                headers={"Content-Type": "application/x-www-form-urlencoded"})
    urllib.request.urlopen(req, timeout=20).read()


def addon_log():
    tok_path = os.path.join(ROOT, "_ha_token.tmp")
    if not os.path.isfile(tok_path):
        return ""
    tok = open(tok_path, encoding="utf-8").read().strip()
    req = urllib.request.Request(HA + "/api/hassio/addons/%s/logs" % SLUG,
                                headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", "replace")


def client_offsets(text):
    """从原始 ASDU 里解客户端的 fileOffset（[12..15] 小端）与 maxDataSize（[16]）。"""
    out = []
    for m in RE_APS.finditer(text):
        h = m.group(2)
        if len(h) < 34:
            continue
        b = bytes.fromhex(h)
        if b[2] != 0x03:                       # 只要 Image Block Request
            continue
        if (b[4] | (b[5] << 8)) != 0x131B:     # 只认自己的 manufacturer
            continue
        off = int.from_bytes(b[12:16], "little")
        out.append((off, b[16]))
    return out


def main():
    watch = float(sys.argv[1]) if len(sys.argv) > 1 else 90.0

    s = get_state()
    print("刷机后 uptime = %s s（新固件刚刷完这个数会很小）" % s.get("uptime"))
    print("当前 ota     =", (s.get("ota") or {}).get("status"),
          (s.get("ota") or {}).get("transferred"))

    print("\n① 起会话 …")
    ota_start()
    time.sleep(6)

    print("② 观察 %.0f s" % watch)
    first = None
    t0 = time.time()
    last_print = 0
    while time.time() - t0 < watch:
        try:
            st = get_state()
            o = st.get("ota") or {}
            try:
                txt = addon_log()
            except Exception:
                txt = ""
            offs = client_offsets(txt)
            if offs and first is None:
                first = offs[0]
                print("   客户端第一个块: offset=%d, maxDataSize=%d" % first)
            if time.time() - last_print >= 10:
                last_print = time.time()
                stats = RE_STAT.findall(txt)
                print("   t+%3.0fs  transferred=%-7s 客户端=%s  %s" % (
                    time.time() - t0, o.get("transferred"),
                    ("%d→%d" % (offs[0][0], offs[-1][0])) if offs else "—",
                    (stats[-1][1] if stats else "")))
        except Exception as e:              # noqa: BLE001
            print("   ?", e)
        time.sleep(3)

    print("\n── 判定 ──")
    if first is None:
        print("✗ 没抓到客户端的块请求：可能 add-on 没在传（先确认 /data/ota_0x0002.bin 在），")
        print("  或客户端还没醒（它 5 分钟才查一次，必要时等一会儿再跑）。")
    elif first[0] == 0:
        print("✓ 客户端从 0 开始 —— 这一包是干净的，让它跑完即可。")
    else:
        print("✗ 客户端仍从 %d 开始（惦记着上一包）⇒ 这一包会错位，必须作废。" % first[0])
        print("  处理：停掉服务端让它彻底超时，再重试本脚本：")
        print("    curl -X POST http://192.168.101.5:8082/api/ota/stop   # 需要新的 add-on")
        print("  旧版 add-on 没有这个路由时，直接给协调器断电/刷机重启同样能逼它放弃。")
    if first:
        print("  提示：maxDataSize=%d（64 = 老中继固件；223 = 新固件已生效）" % first[1])


if __name__ == "__main__":
    main()
