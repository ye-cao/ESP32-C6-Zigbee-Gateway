#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rep_final.py – entscheidender Test fuer die Zustands-Rueckmeldung.

Wichtig: Jedes Oeffnen des Ports resettet den C6 und damit die Interview-Flags.
Deshalb:
  1. Boot abwarten
  2. toggle #1 -> loest (falls noetig) IEEE-Abfrage + Interview aus
  3. warten, bis das Interview "done" meldet und Configure Reporting mit
     SUCCESS quittiert ist
  4. toggle #2 -> DAS ist die Zustandsaenderung, auf die ein Report folgen muss
  5. 60 s auf ZCL 0x0A (Report Attributes) warten
"""
import json
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM9"
ADDR = "0x07da"

ser = serial.Serial(PORT, 115200, timeout=0.05)
ser.rtscts = ser.dsrdtr = False
ser.dtr = False
ser.rts = False
buf = b""
t0 = time.time()
reports = []
iv_done = False


def pump(secs):
    global buf, iv_done
    end = time.time() + secs
    while time.time() < end:
        c = ser.read(ser.in_waiting or 1)
        if not c:
            continue
        buf += c
        while b"\n" in buf:
            r, buf = buf.split(b"\n", 1)
            t = r.decode("utf-8", "replace").strip()
            if not t.startswith("{"):
                continue
            try:
                m = json.loads(t)
            except Exception:
                continue
            typ = m.get("t")
            if typ == "interview":
                print(f"[{time.time() - t0:6.1f}] IV  {t}", flush=True)
                if m.get("state") == "done":
                    iv_done = True
            elif typ == "aps":
                hx = m.get("hex", "")
                cmd = hx[4:6] if len(hx) >= 6 else "?"
                if cmd == "0a":
                    reports.append(m)
                    print(f"[{time.time() - t0:6.1f}] >>> REPORT cl={m.get('cl')} "
                          f"sep={m.get('sep')} {hx}", flush=True)
                elif cmd in ("07", "0b"):
                    print(f"[{time.time() - t0:6.1f}] APS sep={m.get('sep')} "
                          f"cl={m.get('cl')} cmd=0x{cmd} {hx}", flush=True)
            elif typ == "zigbee" and str(m.get("sub", "")).endswith("on_off"):
                print(f"[{time.time() - t0:6.1f}] ZB  {t}", flush=True)


def toggle(n):
    o = {"cmd": "dev_cmd", "addr": ADDR, "ep": 16, "sw": "toggle"}
    print(f"\n>>> toggle #{n}", flush=True)
    ser.write((json.dumps(o, separators=(",", ":")) + "\n").encode())
    ser.flush()


pump(5)                     # Boot
print("--- 8 s Stabilisierung ---", flush=True)
pump(8)

toggle(1)
print("--- warte auf Interview (max 45 s) ---", flush=True)
end = time.time() + 45
while time.time() < end and not iv_done:
    pump(1)
print(f"--- Interview done={iv_done}, 3 s nachlaufen ---", flush=True)
pump(3)

toggle(2)
print("--- 60 s: Report erwartet ---", flush=True)
pump(60)

ser.close()
print(f"\n=== {len(reports)} Report-Attributes-Frames ===", flush=True)
for m in reports:
    print("   ", json.dumps(m), flush=True)
