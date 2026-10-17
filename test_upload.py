#!/usr/bin/env python3
"""Testet den multipart-Parser für den Firmware-Upload (ohne HTTP-Server)."""
import os
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "host", "addon", "esp32c6_gateway", "serial_gateway.py")
src = open(SRC, encoding="utf-8").read()
i = src.index("def _save_upload")
j = src.index("def _fw_ota_start", i)

ns = {"os": os}
exec(src[i:j], ns)

B = b"----WebKitFormBoundaryABC123"
payload = bytes(range(256)) * 4          # 1 KiB Binärdaten inkl. CRLF-Bytes

body = (
    b"--" + B + b"\r\n"
    b'Content-Disposition: form-data; name="file"; filename="fw.bin"\r\n'
    b"Content-Type: application/octet-stream\r\n\r\n"
    + payload + b"\r\n"
    b"--" + B + b"--\r\n"
)

dst = ns["_save_upload"]("multipart/form-data; boundary=" + B.decode(), body)
print("gespeichert:", dst)
saved = open(dst, "rb").read()
print("Bytes:", len(saved))

ok = True
if saved != payload:
    print("FAIL Inhalt weicht ab")
    ok = False
    # zeige erste Abweichung
    for k in range(min(len(saved), len(payload))):
        if saved[k] != payload[k]:
            print(f"  erste Abweichung bei Byte {k}: {saved[k]} != {payload[k]}")
            break
else:
    print("OK  Binärdaten 1:1 (inkl. 0x00/0x0d/0x0a)")

# Fehlerfälle
if ns["_save_upload"]("text/plain", body) is not None:
    print("FAIL: ohne boundary darf nichts gespeichert werden")
    ok = False
else:
    print("OK  ohne boundary → None")

if ns["_save_upload"]("multipart/form-data; boundary=" + B.decode(),
                      b"--" + B + b"\r\nContent-Disposition: form-data; name=\"x\"\r\n\r\n" +
                      b"--" + B + b"--\r\n") is not None:
    print("FAIL: Part ohne filename darf nichts speichern")
    ok = False
else:
    print("OK  Part ohne filename → None")

os.remove(dst)
sys.exit(0 if ok else 1)
