#!/usr/bin/env python3
"""Test für die HA-MQTT-Auto-Discovery in host/addon/esp32c6_gateway/serial_gateway.py.

Die Datei hat keinen __main__-Guard (sie startet beim Import sofort Serial/MQTT),
deshalb werden hier nur die Discovery-Funktionen per exec aus dem Quelltext
gezogen und mit einem Fake-MQTT-Client getestet.
"""
import json
import os
import threading
import time
import re
import sys

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "host", "addon", "esp32c6_gateway", "serial_gateway.py")
src = open(SRC, encoding="utf-8").read()


def block(start_marker, end_marker):
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


class FakeMQ:
    def __init__(self):
        self.published = {}

    def publish(self, topic, payload, retain=False, qos=0):
        self.published[topic] = json.loads(payload)


DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "host", "addon", "esp32c6_gateway", "device_db.json")
ns = {
    "json": json,
    "os": os,
    "BASE": "gw/coordinator",
    "HA_PREFIX": "homeassistant",
    "HA_ENABLED": True,
    "mq": FakeMQ(),
    "_ha_db": json.load(open(DB_PATH, encoding="utf-8")),
    # Ab 2026-10-17 laufen alle Publishes über mq_put(); Prüfungen und
    # Buchführung sitzen dort. Für den Test genügt der Durchgriff.
    "mq_put": lambda t, p, retain=False, qos=1: None,
    "_ep_key": lambda a, e: "%s/%d" % (a, int(e)),
    "_ctrl": set(),
}
ns["mq_put"] = lambda t, p, retain=False, qos=1: ns["mq"].publish(t, p, retain=retain, qos=qos)
# Der Befehlsblock (Wiederholung/Noread) braucht ein paar Nachbarn; für die
# Discovery-Prüfungen genügen No-Op-Stubs.
ns["threading"] = threading
ns["time"] = time
ns["_nodes_save"] = lambda: None
ns["_classify"] = lambda *a, **k: None
ns["_probe_seen"] = lambda *a, **k: None
ns["_noread"] = set()
ns["_addr_nodes"] = {}
ns["_ieee_nodes"] = {}
exec(block("# ── HA Auto-Discovery", "# ── Dispatcher"), ns)
exec(block("# ── Befehls-Bestätigung und Wiederholung", "def on_message("), ns)
# _ha_handle_set schreibt kompaktes JSON über _js() → echte Implementierung laden
# ("def _j(obj)" als Endmarker, weil "def _j" auch auf "def _js" passt)
exec(block("def _js", "def _j(obj)"), ns)

mq = ns["mq"]

# ser.write wird von _ha_handle_set benutzt → Fake-Serial
class FakeSer:
    def __init__(self):
        self.written = []

    def write(self, d):
        self.written.append(d.decode())


ns["ser"] = FakeSer()
ser = ns["ser"]

fails = []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


print("== 1. 3-fach-Schalter (FB56, ep16/17/18) ==")
for epn in ("ep16", "ep17", "ep18"):
    ns["_ha_publish"]("0x07da", f"{epn}/on_off", {"v": 1})

cfgs = [t for t in mq.published if "/switch/" in t]
check(len(cfgs) == 3, f"3 switch-Entities erzeugt (ist {len(cfgs)})")

c = mq.published["homeassistant/switch/c6_0x07da/ep16_on_off/config"]
check(c["state_topic"] == "gw/coordinator/zigbee/0x07da/ep16/on_off",
      f"state_topic: {c['state_topic']}")
check(c["command_topic"] == "gw/coordinator/set/0x07da/ep16/on_off",
      f"command_topic: {c['command_topic']}")
check(c["value_template"] == "{{ value_json.v }}", f"template: {c['value_template']}")
check(c["payload_on"] == "1" and c["payload_off"] == "0", "payload_on/off = 1/0")
check(c["unique_id"] == "c6_0x07da_ep16_on_off", f"unique_id: {c['unique_id']}")
check(c["device"]["identifiers"] == ["c6_0x07da"], "device identifiers")
check(c["availability"] == [{"topic": "gw/coordinator/status",
                              "payload_available": "online",
                              "payload_not_available": "offline"}]
      and c["availability_mode"] == "latest", "availability (Listenform)")
check(c["origin"]["name"] == "ESP32-C6 Gateway", "origin.name")
check(c["default_entity_id"] == "switch.c6_0x07da_ep16_on_off",
      "default_entity_id: %s" % c.get("default_entity_id"))

print("== 2. Sensoren ==")
ns["_ha_publish"]("0x1212", "ep1/temperature", {"raw": 2150})
ns["_ha_publish"]("0x1212", "ep1/humidity", {"raw": 5530})
ns["_ha_publish"]("0x1212", "ep1/illuminance", {"raw": 30001})
ns["_ha_publish"]("0x1212", "ep1/occupancy", {"occ": 1})
ns["_ha_publish"]("0x1212", "ep1/ias_zone", {"zone_status": 1, "motion": 1})
ns["_ha_publish"]("0x1212", "ep1/battery", {"pct": 88, "raw": 176})

t = mq.published["homeassistant/sensor/c6_0x1212/ep1_temperature/config"]
check(t["device_class"] == "temperature" and t["unit_of_measurement"] == "°C",
      "temperature: device_class + °C")
check(t["value_template"] == "{{ (value_json.raw / 100) | round(1) }}",
      f"temp template: {t['value_template']}")
h = mq.published["homeassistant/sensor/c6_0x1212/ep1_humidity/config"]
check(h["device_class"] == "humidity" and h["state_class"] == "measurement", "humidity ok")
il = mq.published["homeassistant/sensor/c6_0x1212/ep1_illuminance/config"]
check("10000" in il["value_template"], f"illuminance log-template: {il['value_template']}")
o = mq.published["homeassistant/binary_sensor/c6_0x1212/ep1_occupancy/config"]
check(o["device_class"] == "occupancy" and o["payload_on"] == "1", "occupancy ok")
check("homeassistant/binary_sensor/c6_0x1212/ep1_ias_zone/config" in mq.published, "ias_zone ok")
b = mq.published["homeassistant/sensor/c6_0x1212/ep1_battery_pct/config"]
check(b["device_class"] == "battery" and b["value_template"] == "{{ value_json.pct }}",
      "battery pct ok")
check("homeassistant/sensor/c6_0x1212/ep1_battery_volt/config" not in mq.published,
      "battery voltage wird ohne voltage_mv nicht angelegt")

print("== 3. Modellname nach Basic-Read ==")
ns["_ha_devices"]["0x07da"] = {"mfr": "Feibit", "model": "FB56+ZSW1IKJ2.5"}
ns["_ha_forget"]("0x07da")
ns["_ha_sent"].clear()
ns["_ha_publish"]("0x07da", "ep16/on_off", {"v": 0})
c = mq.published["homeassistant/switch/c6_0x07da/ep16_on_off/config"]
check(c["device"]["model"] == "FB56+ZSW1IKJ2.5", f"model: {c['device']['model']}")
check(c["device"]["manufacturer"] == "Feibit", f"mfr: {c['device']['manufacturer']}")
check(c["name"] == "EP16 开关", f"entity name: {c['name']}")  # ohne DB keine Endpunkt-Namen

print("== 4. Idempotenz (kein Publish pro Telegramm) ==")
before = len(mq.published)
for _ in range(50):
    ns["_ha_publish"]("0x07da", "ep16/on_off", {"v": 1})
check(len(mq.published) == before, "50 weitere Telegramme → 0 neue Configs")

print("== 5. HA-Befehl → dev_cmd ==")
ser.written.clear()
ns["_ha_handle_set"]("gw/coordinator/set/0x07da/ep16/on_off", "0")
check(len(ser.written) == 1, f"1 UART-Nachricht (ist {len(ser.written)})")
m = json.loads(ser.written[0])
check(m == {"cmd": "dev_cmd", "addr": "0x07da", "ep": 16, "sw": "off"},
      f"payload: {ser.written[0].strip()}")
ns["_ha_handle_set"]("gw/coordinator/set/0x07da/ep17/on_off", "1")
m = json.loads(ser.written[-1])
check(m["ep"] == 17 and m["sw"] == "on", f"ep17/on: {ser.written[-1].strip()}")
ns["_ha_handle_set"]("gw/coordinator/set/0x07da/ep18/on_off", "TOGGLE")
m = json.loads(ser.written[-1])
check(m["sw"] == "toggle", f"toggle: {ser.written[-1].strip()}")

print("== 6. unbekannter sub wird ignoriert ==")
before = len(mq.published)
ns["_ha_publish"]("0x9999", "ep1/raw", {"cluster": "0xfc00"})
check(len(mq.published) == before, "sub=raw → keine Entity")

print("== 7. Modell-Datenbank aus Z2M-Convertern ==")
db = ns["_ha_db"]
check(len(db) > 3000, f"DB geladen: {len(db)} Modelle")
e = db.get("FB56+ZSW1IKJ2.5")
check(e and e["model"] == "HGZB-043", "FB56+ZSW1IKJ2.5 → HGZB-043")
check(e and e["endpoints"] == {"top": 16, "center": 17, "bottom": 18},
      f"Endpunkte: {e and e['endpoints']}")

print("== 8. Erkanntes Modell → sprechende Namen (上/中/下) ==")
ns["_ha_devices"]["0x07da"] = {"model": "FB56+ZSW1IKJ2.5",
                               "db": db["FB56+ZSW1IKJ2.5"]}
ns["_ha_sent"].clear()
mq.published.clear()
ns["_ha_publish"]("0x07da", "ep16/on_off", {"v": 1})
ns["_ha_publish"]("0x07da", "ep17/on_off", {"v": 0})
ns["_ha_publish"]("0x07da", "ep18/on_off", {"v": 1})
names = sorted(c["name"] for c in mq.published.values())
# sortiert nach Codepoint: 上(U+4E0A) < 下(U+4E0B) < 中(U+4E2D)
# has_entity_name: HA stellt den Gerätenamen selbst voran – im Feld name
# steht nur noch der unterscheidende Teil (Endpunkt + Rolle).
check(names == ["上 开关", "下 开关", "中 开关"], f"Namen: {names}")
c = mq.published["homeassistant/switch/c6_0x07da/ep17_on_off/config"]
check(c["device"]["manufacturer"] == "Nue / 3A", f"Hersteller: {c['device']['manufacturer']}")

print("== 9. Entities direkt beim Join (vor dem ersten Tastendruck) ==")
ns["_ha_sent"].clear()
mq.published.clear()
ns["_ha_prepublish"]("0x07da")
check(len(mq.published) == 3, f"3 Entities sofort da (ist {len(mq.published)})")
check(sorted(mq.published) == [
    "homeassistant/switch/c6_0x07da/ep16_on_off/config",
    "homeassistant/switch/c6_0x07da/ep17_on_off/config",
    "homeassistant/switch/c6_0x07da/ep18_on_off/config"], "Topics der 3 Entities")

print("== 10. Unbekanntes Modell → keine Phantom-Entities ==")
ns["_ha_devices"]["0xabcd"] = {"model": "IRGENDEIN-NEUS-GERAET"}
ns["_ha_sent"].clear()
mq.published.clear()
ns["_ha_prepublish"]("0xabcd")
check(len(mq.published) == 0, "unbekannt → 0 Entities (kein Raten)")

print("== 11. End-to-End mit echter Leitungsaufzeichnung ==")
# So kam es real vom Koordinator (COM9, 2026-10-17 05:47)
real_lines = [
    '{"t":"aps","src":"0x07da","sep":16,"dep":1,"cl":"0x0000","sec":0,"len":44,'
    '"hex":"180101040000421046656962697420496e6320636f2e2020"}',
    '{"t":"zigbee","addr":2010,"sub":"basic",'
    '"p":{"mfr":"Feibit Inc co.  ","model":"FB56+ZSW1IKJ2.5"}}',
    '{"t":"zigbee","addr":2010,"sub":"ep16/on_off","p":{"v":1}}',
]
ns["_ha_devices"].clear()
ns["_ha_sent"].clear()
mq.published.clear()
for ln in real_lines:
    m = json.loads(ln)
    if m.get("t") != "zigbee":
        continue
    a = "0x{:04x}".format(m.get("addr", 0))
    sub, p = m.get("sub"), m.get("p")
    if sub == "basic":
        d = ns["_ha_devices"].setdefault(a, {})
        d["mfr"], d["model"] = p.get("mfr", ""), p.get("model", "")
        hit = ns["_ha_db"].get((p.get("model") or "").strip())
        if hit:
            d["db"] = hit
        ns["_ha_forget"](a)
        ns["_ha_prepublish"](a)
    else:
        ns["_ha_publish"](a, sub, p)

sw = sorted(t for t in mq.published if "/switch/" in t)
check(len(sw) == 3, f"3 Schalter aus echtem Modell erzeugt (ist {len(sw)})")
for t in sw:
    c = mq.published[t]
    print(f"    {t.split('/')[-2]:14s} {c['name']:22s} state={c['state_topic']}")
    print(f"    {'':14s} cmd={c['command_topic']}")
check(all(mq.published[t]["device"]["model"] == "HGZB-043" for t in sw),
      "alle 3 als HGZB-043 erkannt")
check(mq.published[sw[0]]["name"] == "上 开关", "erster Schalter heißt 上")

print()
if fails:
    print(f"FEHLGESCHLAGEN: {len(fails)}")
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("ALLE TESTS BESTANDEN")
