#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ota_probe2.py – saubere Trennung von "Laenge" und "Befehl".

Eine Port-Session:
  1. Reset erzwingen, auf {"t":"boot"} warten  -> s_active ist sicher false
  2. fw_ota_data KURZ  (47 Zeichen) -> muss "error not_active" liefern
  3. fw_ota_data LANG  (199 Zeichen) -> muss dasselbe liefern
  4. dev_cmd LANG      (199 Zeichen) -> Kontrolle: kommt der Befehl an?
"""
import json
import time

import serial

ser = serial.Serial()
ser.port, ser.baudrate, ser.timeout = "COM9", 115200, 0.05
ser.rtscts = ser.dsrdtr = False
ser.open()
# Reset-Sequenz wie beim Oeffnen mit gesetzten Leitungen beobachtet
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
    # ohne Reset ist s_active unbekannt -> Aussage unbrauchbar
    print("   WARNUNG: kein Reset erkannt, Ergebnis evtl. verfaelscht")

print("\n2) fw_ota_data KURZ (data='00')")
ln = send({"cmd": "fw_ota_data", "off": 0, "data": "00"})
print(f"   {len(ln)} Zeichen")
print("   <", hit(pump(5), ("fw_ota",)) or "(nichts)")

print("\n3) fw_ota_data LANG (data=160 Hex)")
ln = send({"cmd": "fw_ota_data", "off": 0, "data": "ab" * 80})
print(f"   {len(ln)} Zeichen")
print("   <", hit(pump(5), ("fw_ota",)) or "(nichts)")

print("\n4) dev_cmd LANG (199 Zeichen, Kontrolle)")
base = {"cmd": "dev_cmd", "addr": "0x07da", "ep": 16, "sw": "on", "pad": ""}
bl = len(json.dumps(base, separators=(",", ":")))
base["pad"] = "A" * (199 - bl)
ln = send(base)
print(f"   {len(ln)} Zeichen")
print("   <", hit(pump(6), ("dev_cmd", "aps")) or "(nichts)")

ser.close()
