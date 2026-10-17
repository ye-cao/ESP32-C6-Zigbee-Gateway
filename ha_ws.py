# -*- coding: utf-8 -*-
"""ha_ws.py – HA WebSocket API: Registries + MQTT-Discovery-Zustand auslesen."""
import asyncio, json, os, sys
import websockets

URL   = os.environ["HA_URL"].replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
TOKEN = os.environ["HA_TOKEN"]


async def main():
    async with websockets.connect(URL, max_size=None, ping_interval=None, open_timeout=20) as ws:
        hello = json.loads(await ws.recv())
        print("hello:", hello.get("type"), hello.get("ha_version"))
        await ws.send(json.dumps({"type": "auth", "access_token": TOKEN}))
        a = json.loads(await ws.recv())
        print("auth:", a.get("type"))
        if a.get("type") != "auth_ok":
            sys.exit("auth fehlgeschlagen: %s" % a)

        async def call(i, typ, **kw):
            await ws.send(json.dumps({"id": i, "type": typ, **kw}))
            while True:
                m = json.loads(await ws.recv())
                if m.get("id") == i:
                    if not m.get("success"):
                        print("  FEHLER", typ, m.get("error"))
                    return m.get("result")

        ent = await call(1, "config/entity_registry/list")
        dev = await call(2, "config/device_registry/list")
        print("entities: %d   devices: %d" % (len(ent), len(dev)))
        json.dump(ent, open("ha_entreg.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        json.dump(dev, open("ha_devreg.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

        # MQTT-Entities mit c6_-Praefix
        c6 = [e for e in ent if "c6_" in (e.get("unique_id") or "")]
        print("\n=== unique_id enthaelt 'c6_' : %d ===" % len(c6))
        for e in sorted(c6, key=lambda x: x["entity_id"]):
            print("  %-56s uid=%-34s dis=%-6s hid=%-6s plat=%s" % (
                e["entity_id"], e.get("unique_id", ""), e.get("disabled_by"),
                e.get("hidden_by"), e.get("platform")))

        # alle mqtt-Entities, deren unique_id NICHT mit bekannten Integrationen kollidiert
        mq = [e for e in ent if e.get("platform") == "mqtt"]
        print("\nmqtt-Entities insgesamt: %d" % len(mq))

        # Devices, die zu c6_-Entities gehoeren
        dids = {e.get("device_id") for e in c6 if e.get("device_id")}
        print("\n=== zugehoerige Devices ===")
        for d in dev:
            if d["id"] in dids:
                print("  %-40s name=%-28s mfr=%s model=%s" % (
                    d["id"], d.get("name_by_user") or d.get("name"),
                    d.get("manufacturer"), d.get("model")))


asyncio.run(main())
