#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_dump.py – jede Zeile vom C6 mit Zeitstempel, Befehle klar abgesetzt.

Zweck: sauber zuordnen, welche APS-Antwort zu welchem Befehl gehoert
(Default Response enthaelt Status + urspruengliche Command-ID).
"""
import argparse
import json
import sys
import time

import serial


class Link:
    def __init__(self, ser):
        self.ser, self.buf, self.t0 = ser, b"", time.time()

    def send(self, obj):
        raw = json.dumps(obj, separators=(",", ":")).encode() + b"\n"
        print(f"\n>>> {raw.decode().strip()}   (len={len(raw) - 1})")
        self.ser.write(raw)

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
                if not txt:
                    continue
                dt = time.time() - self.t0
                if txt.startswith("{"):
                    print(f"  [{dt:6.2f}s] {txt}")
                    try:
                        out.append(json.loads(txt))
                    except Exception:
                        pass
                else:
                    print(f"  [{dt:6.2f}s] LOG {txt[:120]}")
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
    ap.add_argument("--boot-timeout", type=float, default=15)
    args = ap.parse_args()

    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = args.port, 115200, 0.05
    ser.rtscts = ser.dsrdtr = False
    ser.open()
    try:
        ser.dtr = False
        ser.rts = False
    except Exception:
        pass

    L = Link(ser)
    print(f"--- {args.port} offen, warte auf boot ---")
    L.wait(lambda m: m.get("t") == "boot", args.boot_timeout)

    print("\n=== 1) probe ===")
    L.send({"cmd": "probe", "addr": args.addr, "ep": args.ep})
    L.pump(6)

    print("\n=== 2) dev_cmd toggle ===")
    L.send({"cmd": "dev_cmd", "addr": args.addr, "ep": args.ep, "sw": "toggle"})
    L.pump(8)

    print("\n=== 3) dev_cmd on ===")
    L.send({"cmd": "dev_cmd", "addr": args.addr, "ep": args.ep, "sw": "on"})
    L.pump(8)

    print("\n=== 4) dev_cmd off ===")
    L.send({"cmd": "dev_cmd", "addr": args.addr, "ep": args.ep, "sw": "off"})
    L.pump(8)

    print("\n=== 5) 20 s passiv (Reports abwarten) ===")
    L.pump(20)

    ser.close()
    print("--- ende ---")


if __name__ == "__main__":
    main()
