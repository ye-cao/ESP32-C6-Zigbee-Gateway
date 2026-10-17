#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e2e_local.py – End-to-End-Test der Produktionskette ohne Home Assistant.

    echter Broker (amqtt, 127.0.0.1:18830)
      + echtes serial_gateway.py als Prozess
      + echter ESP32-C6 auf COM9
      + ein "Fake-HA", das genau das tut, was die HA-MQTT-Integration tut:
        Discovery-Config lesen, auf command_topic publizieren, state_topic lesen

Damit ist die Kette geprüft, die auf dem HA-Host läuft – nur eben lokal:
  MQTT-Befehl → add-on → UART → Gerät → Antwort → UART → add-on → state_topic

Aufruf:
    python e2e_local.py [--port COM9] [--addr 0x07da]
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, "host", "addon", "esp32c6_gateway", "serial_gateway.py")
VENV_PY = sys.executable

MQTT_PORT = 18830
WEB_PORT = 18082
BASE = "gw/coordinator"
HA_PREFIX = "homeassistant"

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("paho-mqtt fehlt")

try:
    from amqtt.broker import Broker
except ImportError:
    sys.exit("amqtt fehlt – mit --target ./.mqttenv installiert (PYTHONPATH setzen)")


# ── Broker im Hintergrund-Thread ──────────────────────────────────────────
def start_broker(ready: threading.Event):
    config = {
        "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{MQTT_PORT}"}},
        "plugins": {
            "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True},
            "amqtt.plugins.sys.broker.BrokerSysPlugin": {"sys_interval": 0},
        },
    }

    async def _run():
        broker = Broker(config)
        await broker.start()
        ready.set()
        while True:
            await asyncio.sleep(3600)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run())
    except Exception as e:
        print(f"[broker] {e}")
        ready.set()


# ── Fake-HA-Client ────────────────────────────────────────────────────────
class FakeHA:
    def __init__(self):
        self.msgs = []
        self.connected = threading.Event()
        self.c = mqtt.Client(client_id="fake-ha")
        self.c.on_connect = self._on_connect
        self.c.on_message = self._on_message

    def _on_connect(self, cl, ud, flags, rc):
        cl.subscribe([("homeassistant/#", 1), (BASE + "/#", 1)])
        self.connected.set()

    def _on_message(self, cl, ud, msg):
        try:
            pl = json.loads(msg.payload.decode())
        except Exception:
            pl = msg.payload.decode(errors="replace")
        self.msgs.append((msg.topic, pl, time.time()))

    def start(self):
        self.c.connect("127.0.0.1", MQTT_PORT, 30)
        self.c.loop_start()
        if not self.connected.wait(8):
            raise RuntimeError("Fake-HA konnte nicht verbinden")

    def stop(self):
        self.c.loop_stop()
        self.c.disconnect()

    def publish(self, topic, payload, retain=False):
        self.c.publish(topic, payload, qos=1, retain=retain)

    def find(self, topic, timeout=10.0, pred=None):
        end = time.time() + timeout
        while time.time() < end:
            for t, p, _ in reversed(self.msgs):
                if t == topic and (pred is None or pred(p)):
                    return p
            time.sleep(0.15)
        return None

    def all_on(self, topic):
        return [p for t, p, _ in self.msgs if t == topic]


def retained_value(topic, timeout=3.0):
    """Frischer Client: beweist, dass der Wert retained im Broker liegt."""
    got = {}

    def on_c(cl, ud, flags, rc):
        cl.subscribe(topic, 1)

    def on_m(cl, ud, msg):
        try:
            got["v"] = json.loads(msg.payload.decode())
        except Exception:
            got["v"] = msg.payload.decode(errors="replace")

    c = mqtt.Client(client_id="retain-check-%d" % int(time.time() * 1000))
    c.on_connect = on_c
    c.on_message = on_m
    c.connect("127.0.0.1", MQTT_PORT, 30)
    c.loop_start()
    end = time.time() + timeout
    while time.time() < end and "v" not in got:
        time.sleep(0.1)
    c.loop_stop()
    c.disconnect()
    return got.get("v")


OK = 0
FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  OK   " + name + ((": " + str(extra)) if extra else ""))
    else:
        FAIL += 1
        print("  FAIL " + name + ((": " + str(extra)) if extra else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM9")
    ap.add_argument("--addr", default="0x07da")
    ap.add_argument("--boot-wait", type=float, default=18.0)
    args = ap.parse_args()

    ep = 16
    state_topic = "%s/zigbee/%s/ep%d/on_off" % (BASE, args.addr, ep)
    cmd_topic = "%s/set/%s/ep%d/on_off" % (BASE, args.addr, ep)
    disc_topic = "%s/switch/c6_%s/ep%d_on_off/config" % (HA_PREFIX, args.addr, ep)

    print("== 1. Broker starten ==")
    ready = threading.Event()
    threading.Thread(target=start_broker, args=(ready,), daemon=True).start()
    if not ready.wait(10):
        sys.exit("Broker startet nicht")
    print("  Broker auf 127.0.0.1:%d" % MQTT_PORT)

    print("\n== 2. serial_gateway.py als Prozess starten ==")
    env = dict(os.environ)
    env.update({"WEB_PORT": str(WEB_PORT), "DB_HOST": "", "HA_DISCOVERY": "1",
                "LOG_TZ": "Asia/Shanghai"})
    log = open(os.path.join(HERE, "e2e_addon.log"), "wb")
    proc = subprocess.Popen(
        [VENV_PY, "-u", ADDON, "--port", args.port, "--broker", "127.0.0.1",
         "--mqport", str(MQTT_PORT), "--base", BASE],
        cwd=os.path.dirname(ADDON), env=env, stdout=log, stderr=subprocess.STDOUT)
    print("  PID %d, Log: e2e_addon.log" % proc.pid)

    ha = FakeHA()
    try:
        ha.start()
        st = ha.find(BASE + "/status", timeout=15)
        check("add-on meldet online", st == "online", st)

        print("\n== 3. %.0f s warten (Zigbee-Netz braucht Anlauf) ==" % args.boot_wait)
        time.sleep(args.boot_wait)

        print("\n== 4. Gerät per MQTT abfragen (probe) -> Discovery ==")
        ha.publish(BASE + "/cmd/probe", "%s,%d" % (args.addr, ep))
        cfg = ha.find(disc_topic, timeout=15)
        check("Discovery-Config fuer ep%d angekommen" % ep, cfg is not None, disc_topic)
        if cfg:
            check("state_topic korrekt", cfg.get("state_topic") == state_topic, cfg.get("state_topic"))
            check("command_topic korrekt", cfg.get("command_topic") == cmd_topic, cfg.get("command_topic"))
            check("payload_on/off", (cfg.get("payload_on"), cfg.get("payload_off")) == ("1", "0"),
                  "%s/%s" % (cfg.get("payload_on"), cfg.get("payload_off")))
            check("unique_id", bool(cfg.get("unique_id")), cfg.get("unique_id"))

        print("\n== 5. HA schaltet EIN (set -> on) ==")
        ha.publish(cmd_topic, "1")
        v = ha.find(state_topic, timeout=8, pred=lambda p: p == {"v": 1})
        check("state_topic = {v:1}", v == {"v": 1}, v)
        rv = retained_value(state_topic)
        check("Wert ist retained (neuer Client sieht ihn sofort)", rv == {"v": 1}, rv)

        print("\n== 6. HA schaltet AUS (set -> off) ==")
        ha.publish(cmd_topic, "0")
        v = ha.find(state_topic, timeout=8, pred=lambda p: p == {"v": 0})
        check("state_topic = {v:0}", v == {"v": 0}, v)

        print("\n== 7. toggle dreht weiter ==")
        ha.publish(cmd_topic, "toggle")
        v = ha.find(state_topic, timeout=8, pred=lambda p: p == {"v": 1})
        check("toggle aus 0 -> {v:1}", v == {"v": 1}, v)

        print("\n== 8. Geraet hat die Befehle wirklich quittiert ==")
        # console-Zeilen sind JSON-Strings – json.loads() im FakeHA macht daraus
        # dicts. Deshalb hier beide Formen wieder auf Text normalisieren.
        # Der Zustands-Echo des add-ons kommt sofort, das Funk-ACK des Geräts
        # erst ein paar hundert ms später -> bis zu 8 s nachpollen.
        acked = []
        end = time.time() + 8
        while time.time() < end:
            console = [c if isinstance(c, str) else json.dumps(c, separators=(",", ":"))
                       for c in ha.all_on(BASE + "/console")]
            acked = [c for c in console
                     if '"cl":"0x0006"' in c and '"hex":"' in c and "0b0" in c]
            if len(acked) >= 3:
                break
            time.sleep(0.3)
        check("mind. 3 Default-Response-Frames vom Geraet", len(acked) >= 3, len(acked))
        for c in acked[:3]:
            print("      " + c)

        print("\n== 9. Bulk: alle drei Endpunkte bekommen Entities ==")
        ha.publish(BASE + "/cmd/probe", "%s,17" % args.addr)
        time.sleep(3)
        ha.publish(BASE + "/cmd/probe", "%s,18" % args.addr)
        time.sleep(4)
        cfgs = [t for t, _, _ in ha.msgs if t.endswith("_on_off/config")]
        check("3 Switch-Discovery-Configs", len(set(cfgs)) == 3,
              ", ".join(sorted(set(cfgs))))
    finally:
        ha.stop()
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        log.close()

    print("\n%d OK, %d FAIL" % (OK, FAIL))
    print("ALLE TESTS BESTANDEN" if FAIL == 0 else "TESTS FEHLGESCHLAGEN")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
