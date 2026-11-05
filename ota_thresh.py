#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ota_thresh.py – 找出 fw_ota_data 从多长的 data 开始就不吭声了。

没有会话时，handler 永远回 {"st":"error","why":"not_active"}。
若应答干脆没了，说明那一行没被（完整地）处理。
"""
import json
import time

import serial

ser = serial.Serial()
ser.port, ser.baudrate, ser.timeout = "COM9", 115200, 0.05
ser.rtscts = ser.dsrdtr = False
ser.open()
try:
    ser.dtr = False
    ser.rts = False
    time.sleep(0.15)
    ser.rts = True
    time.sleep(0.15)
    ser.rts = False
except Exception:
    pass

buf = b""


def send(obj):
    line = json.dumps(obj, separators=(",", ":"))
    ser.write(line.encode() + b"\n")
    ser.flush()
    return len(line)


def pump(secs):
    global buf
    out = []
    end = time.time() + secs
    while time.time() < end:
        try:
            c = ser.read(ser.in_waiting or 1)
        except Exception:
            return out
        if not c:
            continue
        buf += c
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            t = raw.decode("utf-8", errors="replace").strip()
            if t.startswith("{"):
                try:
                    out.append(json.loads(t))
                except Exception:
                    pass
    return out


pump(6)   # Boot

print(f"{'data B':>7} {'行':>6}  回应")
print("-" * 42)
rows = []
for nb in (1, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 128):
    ln = send({"cmd": "fw_ota_data", "off": 0, "data": "ab" * nb})
    msgs = pump(2.5)
    ok = any(m.get("t") == "fw_ota" for m in msgs)
    rows.append((nb, ln, ok))
    print(f"{nb:>7} {ln:>6}  {'antwortet' if ok else 'STILL'}")

ser.close()
good = [nb for nb, _, ok in rows if ok]
bad = [nb for nb, _, ok in rows if not ok]
print("\nfunktioniert bis data =", max(good) if good else "-", "B")
print("still ab data =", min(bad) if bad else "-", "B")
