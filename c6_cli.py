#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""c6_cli.py – kleiner Serien-CLI fuer den ESP32-C6-Zigbee-Koordinator.

Beispiele:
    python c6_cli.py listen --secs 10
    python c6_cli.py send '{"cmd":"permit_join","sec":180}' --watch 15
    python c6_cli.py send '{"cmd":"dev_cmd","addr":"0x07da","ep":16,"sw":"toggle"}' --watch 8

Wichtig: Beim Oeffnen werden DTR/RTS auf False gesetzt, sonst kann der
USB-Serial-JTAG des C6 einen Reset ausloesen.
"""
import argparse
import json
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial fehlt: pip install pyserial")


def open_port(port, baud):
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


def pump(ser, secs, tag="C6"):
    t_end = time.time() + secs
    buf = b""
    lines = []
    while time.time() < t_end:
        try:
            chunk = ser.read(ser.in_waiting or 1)
        except Exception as e:
            print(f"[serial] {e}")
            break
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            txt = raw.decode("utf-8", errors="replace").strip()
            if not txt:
                continue
            print(f"[{tag}] {txt}")
            lines.append(txt)
    if buf.strip():
        txt = buf.decode("utf-8", errors="replace").strip()
        print(f"[{tag}] {txt} (ohne Zeilenende)")
        lines.append(txt)
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM9")
    ap.add_argument("--baud", type=int, default=115200)
    sub = ap.add_subparsers(dest="mode", required=True)

    p_l = sub.add_parser("listen")
    p_l.add_argument("--secs", type=float, default=10.0)

    p_s = sub.add_parser("send")
    p_s.add_argument("payload")
    p_s.add_argument("--watch", type=float, default=8.0)
    p_s.add_argument("--pre", type=float, default=0.5,
                     help="Sekunden Sammeln vor dem Senden")

    args = ap.parse_args()
    ser = open_port(args.port, args.baud)

    if args.mode == "listen":
        print(f"--- hoere {args.secs:.0f}s auf {args.port} ---")
        pump(ser, args.secs)
        print("--- ende ---")
        ser.close()
        return

    if args.pre > 0:
        pump(ser, args.pre)

    payload = args.payload.strip()
    if not payload.startswith("{"):
        payload = '{"cmd":"' + payload + '"}'
    if not payload.endswith("\n"):
        payload += "\n"
    print(f"--- sende: {payload.strip()} ---")
    ser.write(payload.encode("utf-8"))
    ser.flush()
    pump(ser, args.watch)
    print("--- ende ---")
    ser.close()


if __name__ == "__main__":
    main()
