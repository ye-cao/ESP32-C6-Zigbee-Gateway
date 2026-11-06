"""通过 USB 串口给 ESP32-C6 下发一条 JSON 命令，并打印它的回执。

用在没有 add-on 的场合（比如板子直接插在 Windows 电脑上做验证）。
固件在串口上就是一行 JSON 收、一行 JSON 发，所以这个脚本本质上就是个
最小的「半个网关」。

用法举例：

    # 看当前通道状态
    python tools/c6_serial_cmd.py --cmd "{\"cmd\":\"net_status\"}"

    # 写入 Wi-Fi 账号 + TCP 目标，并切到 Wi-Fi 通道
    python tools/c6_serial_cmd.py --net-cfg \
        --ssid yecao --pass yecao@163.com --host 192.168.100.114 --port 12345 --tr 1

    # 改回串口通道（清掉 tr）
    python tools/c6_serial_cmd.py --net-cfg --tr 0

★ 打开串口时 DTR/RTS 都必须保持 False：
  RTS 拉低就是 EN（复位），DTR 会连到 strapping 脚 —— 一旦被拉进下载模式，
  固件就不跑了，串口上只剩 bootloader，看起来像"设备挂了"。
"""
import argparse
import json
import sys
import time

import serial
import serial.tools.list_ports as list_ports


def pick_port(explicit=None):
    """没指定就自己找：优先描述里带 Espressif 的，其次 VID 0x303a。"""
    if explicit:
        return explicit
    cands = []
    for p in list_ports.comports():
        desc = (p.description or "")
        hwid = (p.hwid or "").upper()
        if "Espressif" in desc or "303A:" in hwid:
            cands.append(p.device)
    if len(cands) == 1:
        return cands[0]
    if not cands:
        sys.exit("找不到 Espressif 串口 —— 板子的 USB 插好了吗？"
                 "（也可以直接用 --port COMx 指定）")
    sys.exit(f"找到多个候选串口 {cands}，请用 --port 指定其中一个")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="串口号，如 COM7；不给就自动找")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--cmd", help="要下发的完整 JSON 行")
    ap.add_argument("--net-cfg", action="store_true",
                    help="按下面的参数拼一条 net_cfg 下发")
    ap.add_argument("--ssid")
    ap.add_argument("--pass", dest="password")
    ap.add_argument("--host")
    ap.add_argument("--tcp-port", type=int, dest="tcp_port")
    ap.add_argument("--tr", type=int, choices=[0, 1],
                    help="1 = 切到 Wi-Fi/TCP，0 = 留在串口")
    ap.add_argument("--wait", type=float, default=6.0,
                    help="发完之后听多少秒回执（默认 6 s）")
    args = ap.parse_args()

    if args.net_cfg:
        msg = {"cmd": "net_cfg"}
        for key, val in (("ssid", args.ssid), ("pass", args.password),
                         ("host", args.host), ("port", args.tcp_port),
                         ("tr", args.tr)):
            if val is not None:
                msg[key] = val
        payload = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
    elif args.cmd:
        payload = args.cmd
    else:
        sys.exit("要么给 --cmd，要么给 --net-cfg 加参数")

    port = pick_port(args.port)
    print(f"[串口] {port} @ {args.baud}", flush=True)

    # ★ 先设 dtr/rts 再 open：pyserial 在未打开时只记状态，open 后才应用。
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = args.baud
    ser.timeout = 0.3
    ser.dtr = False
    ser.rts = False
    ser.open()
    time.sleep(0.4)
    try:
        ser.dtr = False
        ser.rts = False
    except Exception:                                   # noqa: BLE001
        pass

    print(f"[→C6] {payload}", flush=True)
    ser.write(payload.encode("utf-8") + b"\n")
    ser.flush()

    deadline = time.time() + args.wait
    buf = b""
    seen = 0
    while time.time() < deadline:
        try:
            chunk = ser.read(4096)
        except Exception as e:                          # noqa: BLE001
            print(f"[串口] 读失败：{e}", flush=True)
            break
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                print(f"[←C6] {text}", flush=True)
                seen += 1

    ser.close()
    print(f"[完成] 收到 {seen} 行", flush=True)


if __name__ == "__main__":
    main()
