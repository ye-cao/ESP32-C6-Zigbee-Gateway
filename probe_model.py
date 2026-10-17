#!/usr/bin/env python3
"""Fragt das Basic-Cluster (Hersteller/Modell) eines bekannten Geräts ab.

Nützlich für Geräte, die schon im Netz sind, aber nichts von sich hören lassen
(kein Announce, keine Reports) – ohne sie neu pairen zu müssen.

    python probe_model.py COM9 0x07da 1 16 17 18
"""
import sys
import time
import json
import serial

port = sys.argv[1] if len(sys.argv) > 1 else "COM9"
addr = sys.argv[2] if len(sys.argv) > 2 else "0x07da"
eps = sys.argv[3:] or ["1"]

s = serial.Serial(port, 115200, timeout=0.3)
s.dtr = False
s.rts = False
time.sleep(3)   # Boot abwarten

for ep in eps:
    cmd = json.dumps({"cmd": "probe", "addr": addr, "ep": int(ep)}) + "\n"
    s.write(cmd.encode())
    print(f"# probe {addr} ep{ep}", flush=True)
    time.sleep(2)

print("# ---- Antworten ----", flush=True)
t0 = time.time()
while time.time() - t0 < 12:
    raw = s.readline()
    if not raw:
        continue
    line = raw.decode("utf-8", "replace").strip()
    if line.startswith("{") and '"t"' in line:
        print(f"[{time.strftime('%H:%M:%S')}] {line}", flush=True)
s.close()
print("# ende", flush=True)
