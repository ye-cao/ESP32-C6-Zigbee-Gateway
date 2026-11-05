#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ota_probe2.py – 把"长度"和"命令"这两件事干净地分开。

Eine Port-Session:
  1. 强制复位，等 {"t":"boot"}  -> s_active 确定是 false
  2. fw_ota_data 短  (47 字符)  -> 必须回 "error not_active"
  3. fw_ota_data 长  (199 字符) -> 必须回同样的
  4. dev_cmd 长      (199 字符) -> 对照：命令到底能不能到？
"""
import json
import time

import serial

ser = serial.Serial()
ser.port, ser.baudrate, ser.timeout = "COM9", 115200, 0.05
ser.rtscts = ser.dsrdtr = False
ser.open()
# 复位时序，与"带着 DTR/RTS 打开端口"时观测到的相同
try:
    ser.dtr = False
    ser.rts = False
    time.sleep(0.15)
    ser.rts = True
    time.sleep(0.15)
    ser.rts = False
except Exception as e:
    print("[serial] reset:", e)

buf = b""


def send(obj):
    line = json.dumps(obj, separators=(",", ":"))
    ser.write(line.encode() + b"\n")
    ser.flush()
    return line


def pump(secs):
    global buf
    out = []
    end = time.time() + secs
    while time.time() < end:
        try:
            c = ser.read(ser.in_waiting or 1)
        except Exception as e:
            print("  [serial]", e)
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


def hit(msgs, tags):
    return [m for m in msgs if m.get("t") in tags]


print("1) Reset + Boot abwarten")
msgs = pump(6)
boot = [m for m in msgs if m.get("t") == "boot"]
print("   boot-Frame:", "ja" if boot else "nein", f"({len(msgs)} Frames)")
if not boot:
    # 不复位的话 s_active 未知 -> 结论不可用
    print("   警告：没检测到复位，结果可能失真")

print("\n2) fw_ota_data KURZ (data='00')")
ln = send({"cmd": "fw_ota_data", "off": 0, "data": "00"})
print(f"   {len(ln)} Zeichen")
print("   <", hit(pump(5), ("fw_ota",)) or "（无）")

print("\n3) fw_ota_data LANG (data=160 Hex)")
ln = send({"cmd": "fw_ota_data", "off": 0, "data": "ab" * 80})
print(f"   {len(ln)} Zeichen")
print("   <", hit(pump(5), ("fw_ota",)) or "（无）")

print("\n4) dev_cmd LANG (199 Zeichen, Kontrolle)")
base = {"cmd": "dev_cmd", "addr": "0x07da", "ep": 16, "sw": "on", "pad": ""}
bl = len(json.dumps(base, separators=(",", ":")))
base["pad"] = "A" * (199 - bl)
ln = send(base)
print(f"   {len(ln)} Zeichen")
print("   <", hit(pump(6), ("dev_cmd", "aps")) or "（无）")

ser.close()
