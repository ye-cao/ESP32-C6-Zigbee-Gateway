"""一条命令判定「装壳状态下 Wi-Fi 到底能不能用」。

为什么单独写一个：`wifi_scan` 的原始回执是一长串 JSON，
`{"t":"wifi_scan","n":3,"got":3,"aps":[{"s":"yecao","r":-71,...}]}`，
要人眼去数 AP 个数、再比对 RSSI 门限，焊完天线当场判断很容易看错。
这个脚本把它变成一个 PASS / FAIL。

判据（2026-11-06 实测确定，见 `Wi-Fi通道-测试报告-2026-11-06.md` §5.1）：

    PASS = 「**装壳状态**下能扫到目标 SSID，且 RSSI ≥ -75 dBm」

★ 关键在"装壳状态"四个字。拆着外壳能连不算通过 —— 那天就是拆壳连上、
  装壳断掉，白高兴了一场。所以跑之前**务必把外壳装好**。

★ 顺带把 Zigbee 的 LQI 一起打出来当参照：
  LQI 是**链路余量的相对指示器**，不是通/不通的判据。
  同环境里装壳 45、拆壳 66 的跳变，恰好能定位衰减来自哪里。

用法：

    python tools/c6_wifi_check.py                 # 自动找串口，目标 SSID 取 yecao
    python tools/c6_wifi_check.py --port COM9 --ssid yecao
    python tools/c6_wifi_check.py --json          # 只输出一行结果，便于脚本消费

退出码：0 = PASS，1 = FAIL，2 = 串口/通信异常。
"""
import argparse
import json
import sys
import time

import serial
import serial.tools.list_ports as list_ports

# ★ RSSI 门限：-75 dBm 是本次定下的「可用」下限。
#   ESP32-C6 在 -80 附近还能勉强关联，但 TCP 吞吐会掉到几百 B/s 且频繁重连，
#   对我们这种"一行 JSON 一个回执"的交互式协议就是不可用，所以取 -75。
RSSI_OK = -75

# 扫描本身约 2.5~2.9 s；Wi-Fi 若是懒启动的（tr=0 时首次调用），
# 还要加上驱动初始化 + 等 STA_START 的时间，留足 20 s。
SCAN_WAIT = 20.0


def pick_port(explicit=None):
    """没指定就找 Espressif 的串口（VID 0x303A）。"""
    if explicit:
        return explicit
    cands = []
    for p in list_ports.comports():
        if "Espressif" in (p.description or "") or "303A:" in (p.hwid or "").upper():
            cands.append(p.device)
    if len(cands) == 1:
        return cands[0]
    if not cands:
        sys.exit("找不到 Espressif 串口 —— 板子的 USB 插好了吗？可用 --port 指定")
    sys.exit(f"找到多个候选串口 {cands}，请用 --port 指定其中一个")


def open_port(port, baud=115200):
    """打开串口。

    ★ DTR/RTS 必须都保持 False：RTS 接 EN，拉低就是复位；
      DTR 连着 strapping 脚，被拉进下载模式后固件就不跑了，
      串口上只剩 bootloader，表现成"设备挂了"。
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 0.3
    ser.dtr = False
    ser.rts = False
    ser.open()
    time.sleep(0.3)
    try:
        ser.dtr = False
        ser.rts = False
    except Exception:                                   # noqa: BLE001
        pass
    ser.reset_input_buffer()
    return ser


def send_and_collect(ser, payload, wait, want, seen=None):
    """下发一行，收集 want 秒，返回匹配 `want` 的那条 JSON（取最后一条）。

    `seen` 若给一个 list，会把期间收到的所有 JSON 对象塞进去 ——
    用来"顺手"捡心跳里的 Zigbee LQI，不必再等下一个心跳周期。
    """
    ser.write(payload.encode("utf-8") + b"\n")
    ser.flush()

    deadline = time.time() + wait
    buf = b""
    hit = None
    while time.time() < deadline:
        try:
            chunk = ser.read(4096)
        except Exception as e:                          # noqa: BLE001
            print(f"[串口] 读失败：{e}", file=sys.stderr)
            break
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("utf-8", "replace").strip()
            # 日志开关打开时这一行前面会粘着 IDF 日志，取最后一个 '{' 起的部分
            cut = text.rfind('{"t"')
            if cut < 0:
                continue
            try:
                obj = json.loads(text[cut:])
            except ValueError:
                continue
            if seen is not None:
                seen.append(obj)
            if obj.get("t") == want:
                hit = obj
                return hit
    return hit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="串口号，如 COM9；不给就自动找")
    ap.add_argument("--ssid", default="yecao", help="期望扫到的 SSID（默认 yecao）")
    ap.add_argument("--json", action="store_true", help="只输出一行 JSON 结果")
    args = ap.parse_args()

    port = pick_port(args.port)
    if not args.json:
        print(f"[串口] {port}")
        print(f"[判据] 目标 SSID「{args.ssid}」且 RSSI ≥ {RSSI_OK} dBm（需装壳）")

    ser = open_port(port)
    try:
        # 先确认固件在线。设备刚上电时 app_main 还没跑完，等它一下。
        st = send_and_collect(ser, '{"cmd":"net_status"}', 6.0, "net_status")
        if st is None:
            print("✗ 没有回执 —— 固件没在跑，或者串口被别的程序占着", file=sys.stderr)
            return 2
        if not args.json:
            print(f"[状态] transport={st.get('transport')} state={st.get('state')} "
                  f"ssid={st.get('ssid')} ip={st.get('ip')} why={st.get('why')}")

        # 扫频。tr=0 时 Wi-Fi 是这条命令才懒启动的，第一次会慢。
        seen = []
        scan = send_and_collect(ser, '{"cmd":"wifi_scan"}', SCAN_WAIT, "wifi_scan", seen)
        if scan is None:
            print("✗ 扫描没有回执", file=sys.stderr)
            return 2
        if scan.get("err"):
            print(f"✗ 扫描失败：{scan['err']}", file=sys.stderr)
            return 2

        aps = scan.get("aps") or []
        # 顺手捡一次心跳里的 Zigbee LQI 当参照（心跳约 60 s 一次，扫频窗口里通常就有）
        hb = next((o for o in reversed(seen) if o.get("t") == "heartbeat"), None)
    finally:
        ser.close()

    # ---- 判定 ----
    target = None
    for ap_rec in aps:
        if ap_rec.get("s") == args.ssid:
            target = ap_rec
            break

    best = max(aps, key=lambda a: a.get("r", -127)) if aps else None
    ok = target is not None and target.get("r", -127) >= RSSI_OK

    result = {
        "port": port,
        "ssid": args.ssid,
        "scanned": scan.get("n", 0),
        "got": scan.get("got", 0),
        "found": target is not None,
        "rssi": target.get("r") if target else None,
        "ch": target.get("c") if target else None,
        "threshold": RSSI_OK,
        "verdict": "PASS" if ok else "FAIL",
        "aps": [{"s": a.get("s"), "r": a.get("r"), "c": a.get("c")} for a in aps],
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0 if ok else 1

    print(f"[扫描] 驱动报 {scan.get('n', 0)} 个 / 实取 {scan.get('got', 0)} 个")
    if aps:
        for a in aps:
            mark = " ←目标" if a.get("s") == args.ssid else ""
            name = a.get("s") or "(隐藏)"
            print(f"       {name:<24} {a.get('r'):>4} dBm  ch{a.get('c')}{mark}")
    else:
        print("       （一个都没有 —— 射频链路仍然不通）")

    if hb and hb.get("dev"):
        lqis = [d.get("lqi") for d in hb["dev"] if d.get("lqi") is not None]
        if lqis:
            print(f"[参照] Zigbee 设备 {len(lqis)} 台，LQI {'/'.join(str(x) for x in lqis)}"
                  f"（仅作链路余量参考，不是通/不通的判据）")

    print()
    if ok:
        print(f"✓ PASS —— 「{args.ssid}」RSSI {target.get('r')} dBm，"
              f"优于 {RSSI_OK} dBm，可以接着把 tr 改 1 验证 TCP 通路")
    elif target is not None:
        print(f"✗ FAIL —— 能看到「{args.ssid}」但 RSSI {target.get('r')} dBm，"
              f"低于 {RSSI_OK} dBm，连带上了也不稳")
    elif best is not None:
        print(f"✗ FAIL —— 能扫到别的 AP（最强 {best.get('s') or '(隐藏)'} "
              f"{best.get('r')} dBm），但看不到「{args.ssid}」")
        print("         说明射频是通的，问题在 AP 本身（频段/隐藏/信道）")
    else:
        print("✗ FAIL —— 一个 AP 都扫不到。射频链路仍然不可用：")
        print("         ① 外壳是否真的装回去了？（装壳才是要验的状态）")
        print("         ② 天线焊点/净空；③ 换块板子交叉验证")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
