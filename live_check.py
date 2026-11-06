#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_check.py – OTA 之后给 C6 拍一张快照。

只打开 COM9 一次，等启动完成，再等一条设备表非空的心跳
（设备表要从 zb_storage 恢复之后才会填上），然后检查：

  1. probe ep16  -> basic 簇（厂商/型号）  == 配对完好
  2. dev_cmd toggle -> Default Response 0x00   == 命令通路 OK
  3. 之后来一条入向的 Report Attributes  == repcfg 修复生效
     （APS 负载里有 0x0a），也就是设备自己上报状态。

用法：  python live_check.py [--port COM9] [--addr 0x07da]
"""
import argparse
import json
import sys
import time

import serial


def open_port(port, baud=115200):
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = port, baud, 0.05
    ser.rtscts = ser.dsrdtr = False
    ser.open()
    try:
        ser.dtr = False
        ser.rts = False
    except Exception:
        pass
    return ser


class Link:
    def __init__(self, ser):
        self.ser, self.buf, self.msgs = ser, b"", []

    def send(self, obj):
        self.ser.write(json.dumps(obj, separators=(",", ":")).encode() + b"\n")

    def pump(self, secs):
        out, end = [], time.time() + secs
        while time.time() < end:
            try:
                chunk = self.ser.read(self.ser.in_waiting or 1)
            except Exception as e:
                print(f"[serial] {e}")
                break
            if not chunk:
                continue
            self.buf += chunk
            while b"\n" in self.buf:
                raw, self.buf = self.buf.split(b"\n", 1)
                txt = raw.decode("utf-8", errors="replace").strip()
                if not txt.startswith("{"):
                    continue
                try:
                    m = json.loads(txt)
                except Exception:
                    continue
                out.append(m)
                self.msgs.append(m)
        return out

    def wait(self, pred, timeout):
        end = time.time() + timeout
        while time.time() < end:
            for m in self.pump(0.1):
                if pred(m):
                    return m
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM9")
    ap.add_argument("--addr", default="0x07da")
    ap.add_argument("--ep", type=int, default=16)
    args = ap.parse_args()

    addr_i = int(args.addr, 16)
    ser = open_port(args.port)
    L = Link(ser)
    ok, fail = [], []

    def chk(cond, label, detail=""):
        (ok if cond else fail).append(label)
        print(("  OK   " if cond else "  FAIL ") + label + (f"   {detail}" if detail else ""))

    print(f"--- {args.port} 已打开，等待启动 ---")
    boot = L.wait(lambda m: m.get("t") == "boot", 15)
    chk(boot is not None, "Boot-Meldung empfangen")

    hb = L.wait(lambda m: m.get("t") == "heartbeat" and m.get("p", {}).get("dev"),
                25)
    if hb:
        p = hb["p"]
        print(f"  heartbeat: uptime={p.get('uptime')}s ch={p.get('ch')} "
              f"pan={p.get('pan')} dev={p.get('dev')}")
    chk(hb is not None, "心跳里的设备表非空",
        "" if hb else "（启动后设备表还是空的）")

    # 1) Pairing
    L.send({"cmd": "probe", "addr": args.addr, "ep": args.ep})
    basic = L.wait(lambda m: m.get("t") == "zigbee" and m.get("sub") == "basic", 10)
    if basic:
        print(f"  basic: {basic.get('p')}")
    chk(basic is not None and bool(basic.get("p", {}).get("model")),
        "probe ep{} 能取到 basic 簇".format(args.ep),
        (basic or {}).get("p", {}).get("model", ""))

    # 2) Steuerbefehl
    L.send({"cmd": "dev_cmd", "addr": args.addr, "ep": args.ep, "sw": "toggle"})
    dr = L.wait(lambda m: m.get("t") == "aps" and m.get("src", "").lower()
                == args.addr.lower() and str(m.get("hex", "")).startswith("18"),
                10)
    if dr:
        hx = dr.get("hex", "")
        # ZCL-Header = [frame-control][seq][command]  →  command 0x0B = Default
        # Response 的负载 = [command_id][status]，status 就落在
        # Byte 4 (hex[8:10]), 不是 Byte 2.
        st = hx[8:10] if len(hx) >= 10 else "?"
        print(f"  Default Response: {hx}  (command_id=0x{hx[6:8]}, status=0x{st})")
    chk(dr is not None and len(dr.get("hex", "")) >= 10
        and dr.get("hex", "")[8:10] == "00",
        "dev_cmd toggle -> Default Response status 0x00",
        dr.get("hex", "") if dr else "（没有回应）")

    # 3) 终端设备自己上报（repcfg 修复）
    rep = L.wait(lambda m: m.get("t") == "aps" and "082e0a" in m.get("hex", ""), 12)
    onoff = L.wait(lambda m: m.get("t") == "zigbee"
                   and m.get("sub", "").endswith("/on_off"), 8)
    if rep:
        print(f"  Report Attributes: {rep.get('hex')}")
    if onoff:
        print(f"  on_off-Report: sub={onoff.get('sub')} p={onoff.get('p')}")
    chk(rep is not None or onoff is not None,
        "终端设备自己上报状态（Report Attributes / on_off）")

    ser.close()
    print(f"\n=== {len(ok)} OK / {len(fail)} FAIL ===")
    for f in fail:
        print("  - " + f)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
