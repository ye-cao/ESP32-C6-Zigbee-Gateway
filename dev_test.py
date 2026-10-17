#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dev_test.py – dev_cmd / probe gegen bekannte Adresse testen.

Ein Port-Öffnen für die ganze Sequenz (jedes Öffnen löst einen Reset aus).
"""
import argparse
import json
import sys
import time

import serial

EP_LIST = (16, 17, 18)


def open_port(port, baud=115200):
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 0.2
    ser.rtscts = False
    ser.dsrdtr = False
    ser.open()
    try:
        ser.dtr = False
        ser.rts = False
    except Exception:
        pass
    return ser


def pump(ser, secs, sink):
    t_end = time.time() + secs
    buf = b""
    while time.time() < t_end:
        try:
            chunk = ser.read(ser.in_waiting or 1)
        except Exception as e:
            print(f"[serial] {e}")
            return
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            txt = raw.decode("utf-8", errors="replace").strip()
            if not txt:
                continue
            # Boot-Log-Rauschen ausblenden
            if txt.startswith(("I (", "W (", "E (", "esp_image", "boot:", "load:",
                               "entry ", "ESP-ROM", "Build:", "rst:", "Saved PC",
                               "SPIWP", "mode:", "ELF file")):
                continue
            print(f"  {txt}")
            sink.append(txt)


def send(ser, obj):
    line = json.dumps(obj, separators=(",", ":"))
    print(f">>> {line}")
    ser.write((line + "\n").encode())
    ser.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM9")
    ap.add_argument("--addr", default="0x07da")
    ap.add_argument("--wait-boot", type=float, default=4.0)
    ap.add_argument("--watch", type=float, default=6.0)
    ap.add_argument("--skip-probe", action="store_true")
    args = ap.parse_args()

    ser = open_port(args.port)
    sink = []
    print(f"--- warte {args.wait_boot:.0f}s auf Boot ---")
    pump(ser, args.wait_boot, sink)

    if not args.skip_probe:
        for ep in (1,) + EP_LIST:
            print(f"\n=== probe {args.addr} ep{ep} ===")
            send(ser, {"cmd": "probe", "addr": args.addr, "ep": ep})
            pump(ser, args.watch, sink)

    for ep in EP_LIST:
        print(f"\n=== dev_cmd toggle {args.addr} ep{ep} ===")
        send(ser, {"cmd": "dev_cmd", "addr": args.addr, "ep": ep, "sw": "toggle"})
        pump(ser, args.watch, sink)

    print("\n=== dev_cmd on  (alle EP) ===")
    for ep in EP_LIST:
        send(ser, {"cmd": "dev_cmd", "addr": args.addr, "ep": ep, "sw": "on"})
        pump(ser, 2.0, sink)

    print("\n=== dev_cmd off (alle EP) ===")
    for ep in EP_LIST:
        send(ser, {"cmd": "dev_cmd", "addr": args.addr, "ep": ep, "sw": "off"})
        pump(ser, 2.0, sink)

    ser.close()
    print("\n--- fertig ---")


if __name__ == "__main__":
    main()
