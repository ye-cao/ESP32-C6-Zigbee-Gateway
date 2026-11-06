"""刷写固件后**立刻**接管串口，抓开机前几秒的输出。

为什么需要它：板子的 USB 是 ESP32-C6 自带的 USB-Serial-JTAG，
RTS 并没有接到 EN，所以"打开串口再拉 RTS 复位"是无效的 ——
唯一可靠的复位点是 esptool 刷写结束时的那次 hard_reset。
而开机日志（尤其是 app_main 里、Zigbee 起来之前打的那几行）
只在复位后几秒内出现，用两次独立调用去抓必然错过。

所以把两件事放进同一个进程：先跑 esptool 刷写，再马上 open 串口。

用法：
    python tools/c6_flash_and_capture.py --bin build/zigbee-vital-sensor.bin \
        --secs 16 [--port COM9] [--out _boot.log]
"""
import argparse
import os
import subprocess
import sys
import time

import serial
import serial.tools.list_ports as list_ports

# esptool 必须用 IDF 自带的那套 python，否则模块找不到
IDF_PY = r"C:\Espressif\python_env\idf5.5_py3.12_env\Scripts\python.exe"


def pick_port(explicit=None):
    if explicit:
        return explicit
    for p in list_ports.comports():
        if "Espressif" in (p.description or "") or "303A:" in (p.hwid or "").upper():
            return p.device
    raise SystemExit("找不到 Espressif 串口")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port")
    ap.add_argument("--bin", required=True, help="要写到 0x20000 的 app 镜像")
    ap.add_argument("--baud", type=int, default=460800)
    ap.add_argument("--secs", type=float, default=16.0)
    ap.add_argument("--out", default="_boot.log")
    args = ap.parse_args()

    port = pick_port(args.port)
    py = IDF_PY if os.path.exists(IDF_PY) else sys.executable

    cmd = [py, "-m", "esptool", "--chip", "esp32c6", "-p", port,
           "-b", str(args.baud), "--before", "default_reset",
           "--after", "hard_reset", "write_flash", "0x20000", args.bin]
    print("[刷写] " + " ".join(cmd[2:]), flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    tail = [l for l in r.stdout.splitlines() if "Hash of data" in l or "Wrote" in l]
    print("[刷写] " + (" | ".join(tail) if tail else f"rc={r.returncode} 无输出"))
    if r.returncode != 0:
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        sys.exit(1)

    # ★ 立刻接管：中间不留任何多余动作，否则会漏掉开机日志
    s = serial.Serial()
    s.port = port
    s.baudrate = 115200
    s.timeout = 0.2
    s.dtr = False
    s.rts = False
    s.open()

    deadline = time.time() + args.secs
    buf = b""
    out = []
    while time.time() < deadline:
        try:
            chunk = s.read(4096)
        except Exception as e:                          # noqa: BLE001
            print(f"[串口] 读失败：{e}", flush=True)
            break
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            out.append(line.decode("utf-8", "replace").rstrip())
    s.close()

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"[完成] 抓到 {len(out)} 行 → {args.out}", flush=True)


if __name__ == "__main__":
    main()
