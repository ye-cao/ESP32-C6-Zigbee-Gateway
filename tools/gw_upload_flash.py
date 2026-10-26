#!/usr/bin/env python3
"""Firmware in den add-on hochladen und per esptool 直刷.

Warum nicht curl -F: das fruehere add-on (<=1.4.0) hat in do_POST den
Content-Type komplett .lower() genommen -> "----WebKitFormBoundary..." wurde
zerstoert, der Body landete als EIN Part in der Datei (1000 B -> 1218 B) und
OTA schlug ohne Fehlermeldung fehl.

Deshalb hier: multipart von Hand, boundary **komplett klein** (----x).
Das funktioniert sowohl mit dem kaputten als auch mit dem gefixten add-on.
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("ADDON_BASE", "http://192.168.101.5:8082")

# ★ Sandbox setzt http_proxy/https_proxy auf 127.0.0.1:3919. urllib haengt sich
#   daran auf (getaddrinfo failed) – lokales LAN muss direkt gehen.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_OPENER)
BND = "----x"


def post_file(path, endpoint):
    with open(path, "rb") as f:
        data = f.read()
    body = (
        ("--" + BND + "\r\n"
         'Content-Disposition: form-data; name="file"; filename="'
         + os.path.basename(path) + '"\r\n'
         "Content-Type: application/octet-stream\r\n\r\n").encode()
        + data
        + ("\r\n--" + BND + "--\r\n").encode()
    )
    req = urllib.request.Request(
        BASE + endpoint,
        data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + BND},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        print(endpoint, "->", r.status, len(data), "B gesendet")


def state():
    with urllib.request.urlopen(BASE + "/api/state", timeout=15) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


if __name__ == "__main__":
    p = sys.argv[1]
    ep = sys.argv[2] if len(sys.argv) > 2 else "/api/fw_flash"
    post_file(p, ep)
    for _ in range(60):
        time.sleep(3)
        try:
            s = state()
        except Exception as e:            # noqa: BLE001
            print("state?", e)
            continue
        fl = s.get("fw_flash") or {}
        st = s.get("status")
        print("status=%s uptime=%s flash=%s %s" % (
            st, s.get("uptime"), fl.get("status"), fl.get("bin", "")))
        tail = fl.get("tail") or []
        if tail:
            print("   " + "\n   ".join(tail[-4:]))
        if fl.get("status") in ("ok", "error", "done"):
            break
