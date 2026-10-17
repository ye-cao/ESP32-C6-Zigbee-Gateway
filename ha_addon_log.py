# -*- coding: utf-8 -*-
"""ha_addon_log.py – Add-on-Log des C6-Gateways über den Supervisor-Proxy lesen.

Warum nicht REST /api/hassio/...?  Der REST-Proxy antwortet einem
Long-Lived-Token mit 401 (nur Session-Tokens werden durchgelassen).
Der WebSocket-Befehl `supervisor/api` geht dagegen durch.

Aufruf:
    python ha_addon_log.py list
    python ha_addon_log.py log   [SLUG] [N]
    python ha_addon_log.py follow [SLUG] [SEK]
"""
import asyncio
import json
import os
import sys
import time

import websockets

HERE  = os.path.dirname(os.path.abspath(__file__))
URL   = "ws://192.168.101.5:8123/api/websocket"
TOKEN = open(os.path.join(HERE, "_ha_token.tmp"), "r", encoding="utf-8").read().strip()


class Ha:
    def __init__(self, ws):
        self.ws = ws
        self.i = 0

    async def call(self, typ, **kw):
        self.i += 1
        mid = self.i
        await self.ws.send(json.dumps({"id": mid, "type": typ, **kw}))
        while True:
            m = json.loads(await self.ws.recv())
            if m.get("id") != mid:
                continue
            if not m.get("success"):
                raise RuntimeError("%s -> %s" % (typ, m.get("error")))
            return m.get("result")

    async def sup(self, endpoint, method="get"):
        return await self.call("supervisor/api", endpoint=endpoint, method=method)


def match(a):
    s = (a.get("slug") or "").lower()
    n = (a.get("name") or "").lower()
    return any(k in s for k in ("esp32", "c6", "zigbee", "gateway")) or \
           any(k in n for k in ("esp32", "zigbee", "gateway"))


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "log"
    slug = sys.argv[2] if len(sys.argv) > 2 else None
    n    = int(sys.argv[3]) if len(sys.argv) > 3 else 200

    async with websockets.connect(URL, max_size=None, ping_interval=None,
                                  open_timeout=20) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        if json.loads(await ws.recv()).get("type") != "auth_ok":
            sys.exit("auth fehlgeschlagen")
        ha = Ha(ws)

        if mode == "list":
            res = await ha.sup("/addons")
            for a in res.get("addons", []):
                if not match(a):
                    continue
                print("%-40s %-30s state=%-8s ver=%s" % (
                    a.get("slug"), a.get("name"), a.get("state"),
                    a.get("version")))
            return

        if slug in (None, "-"):
            res = await ha.sup("/addons")
            cand = [a for a in res.get("addons", []) if match(a)]
            if not cand:
                sys.exit("kein Add-on gefunden – erst 'list'")
            slug = cand[0]["slug"]

        if mode == "log":
            res = await ha.sup("/addons/%s/logs" % slug)
            # Supervisor liefert {"content": "..."} – je nach Version str
            txt = res.get("content") if isinstance(res, dict) else None
            if txt is None:
                print(json.dumps(res)[:800])
                return
            for line in txt.splitlines()[-n:]:
                print(line)
            return

        if mode == "follow":
            res = await ha.sup("/addons/%s/logs" % slug)
            base = (res.get("content") or "").splitlines()
            seen = len(base)
            print("[follow] %s – Start bei Zeile %d, %d s" % (slug, seen, n),
                  file=sys.stderr, flush=True)
            t0 = time.time()
            while time.time() - t0 < n:
                await asyncio.sleep(1.0)
                try:
                    r = await ha.sup("/addons/%s/logs" % slug)
                except Exception as e:
                    print("[follow] %s" % e, file=sys.stderr, flush=True)
                    continue
                cur = (r.get("content") or "").splitlines()
                if len(cur) < seen:
                    seen = 0
                for line in cur[seen:]:
                    print(line, flush=True)
                seen = len(cur)


asyncio.run(main())
