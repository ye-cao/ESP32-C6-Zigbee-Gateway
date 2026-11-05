#!/usr/bin/env python3
"""Firmware in den add-on hochladen und per esptool 直刷.

为什么不用 curl -F：早先的 add-on（<=1.4.0）在 do_POST 里把
Content-Type 整个 .lower() 了 -> "----WebKitFormBoundary..." 被毁掉，
body 作为**一个** part 写进文件（1000 B -> 1218 B），
OTA 失败而且不报错。

所以这里手工拼 multipart，boundary **全小写**（----x）。
这样坏掉的 add-on 和修好的 add-on 都能用。
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("ADDON_BASE", "http://192.168.101.5:8082")

# ★ 沙箱把 http_proxy/https_proxy 设成 127.0.0.1:3919。urllib 会挂在
#   就卡在这儿（getaddrinfo failed）—— 本地局域网必须直连。
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
