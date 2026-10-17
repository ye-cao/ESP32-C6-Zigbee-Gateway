# -*- coding: utf-8 -*-
"""test_mqtt.py – die MQTT-Schicht nach Zigbee2MQTT-Vorbild.

Am 2026-10-17 aus dem Z2M-Quelltext übernommen (lib/mqtt.ts,
lib/extension/homeassistant.ts) und hier festgenagelt:

  1. mq_put()  – Wildcard-Prüfung, Registry der retained Nachrichten
  2. _republish_retained() – retained nach Verbindungsaufbau erneut setzen
  3. _disc_send() – Publish nur bei geändertem Inhalt (ersetzt _DISC_VER)
  4. Startfenster – vorhandene Configs erst einlesen, dann nur Abweichungen
  5. _disc_purge_orphans() – verwaiste Configs löschen
  6. _ha_avail() / _ha_republish_all() – Verfügbarkeit und HA-Neustart
"""
import io
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "host", "addon", "esp32c6_gateway", "serial_gateway.py")
src = io.open(SRC, encoding="utf-8").read()

mq_block = src[src.index("# ── MQTT-Ausgang"):
               src.index("# ── Befehls-Bestätigung und Wiederholung")]
cache_block = src[src.index("# ── Discovery-Cache"):
                  src.index("# ── Persistente Knoten-Zuordnung")]
print("extrahiert: MQTT-Ausgang %d, Discovery-Cache %d Zeichen\n"
      % (len(mq_block), len(cache_block)))


class FakeMq:
    def __init__(self):
        self.pub = []          # (topic, payload, retain)
        self.subs = []

    def publish(self, topic, payload=None, retain=False, qos=0):
        self.pub.append((topic, payload, retain))

    def subscribe(self, topic, qos=0):
        self.subs.append(topic)

    def unsubscribe(self, topic):
        self.subs = [t for t in self.subs if t != topic]

    def sent(self, topic=None):
        return [x for x in self.pub if topic is None or x[0] == topic]


mq = FakeMq()
ns = {
    "json": json, "os": os, "time": time, "threading": threading,
    "mq": mq,
    "BASE": "gw/coordinator",
    "HA_PREFIX": "homeassistant",
    "HA_ENABLED": True,
    "_ha_last": {},
    "_ep_key": lambda a, e: "%s/%d" % (a, int(e)),
    "_ctrl": set(),
    "_addr_nodes": {}, "_ieee_nodes": {},
}
exec(compile(mq_block, "<mq>", "exec"), ns)
exec(compile(cache_block, "<cache>", "exec"), ns)
ns["_DISC_WAIT"] = 0.15
ns["_nodes_save"] = lambda: None
ns["_ha_node"] = lambda addr: "c6_" + str(addr)
ns["_ha_devices"] = {}
ns["_ha_ep_label"] = lambda addr, epn: epn.upper()

mq_put = ns["mq_put"]
disc_send = ns["_disc_send"]

ok = total = 0


def check(label, cond, detail=""):
    global ok, total
    total += 1
    if cond:
        ok += 1
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s  %s" % (label, detail))
        sys.exit(1)


def cfg(name="开关"):
    return {"name": name, "state_topic": "gw/coordinator/zigbee/0x07da/ep16/on_off"}


print("1) mq_put: Wildcard-Prüfung")
mq.pub.clear()
mq_put("gw/coordinator/c6_#", "x", retain=True)
check("Topic mit # wird abgewiesen", mq.pub == [], str(mq.pub))
check("und landet nicht in der Registry", ns["_retained"] == {}, str(ns["_retained"]))
mq_put("gw/coordinator/a/+/b", "x")
check("Topic mit + wird abgewiesen", mq.pub == [], str(mq.pub))

print("\n2) mq_put: Registry der retained Nachrichten")
ns["_retained"].clear()
mq_put("t/keep", "v1", retain=True)
check("retained gemerkt", ns["_retained"].get("t/keep") == "v1", str(ns["_retained"]))
mq_put("t/keep", "", retain=True)
check("leerer Payload löscht den Eintrag", "t/keep" not in ns["_retained"],
      str(ns["_retained"]))
mq_put("t/plain", "v2")
check("ohne retain kein Eintrag", "t/plain" not in ns["_retained"],
      str(ns["_retained"]))

print("\n3) retain: erneutes Setzen nach Verbindungsaufbau")
ns["_retained"].clear()
mq_put("d/1/config", "A", retain=True)
mq_put("d/2/config", "B", retain=True)
mq.pub.clear()
ns["_republish_retained"]()
check("beide retained Nachrichten erneut gesendet",
      sorted(t for t, p, r in mq.pub) == ["d/1/config", "d/2/config"], str(mq.pub))
check("dabei retain beibehalten", all(r for t, p, r in mq.pub), str(mq.pub))

print("\n4) _disc_send: Publish nur bei geändertem Inhalt")
mq.pub.clear(); ns["_ha_sent"].clear()
disc_send("switch", "c6_0x07da", "ep16_on_off", cfg("开关"))
check("erster Aufruf publiziert", len(mq.pub) == 1, str(mq.pub))
disc_send("switch", "c6_0x07da", "ep16_on_off", cfg("开关"))
check("gleicher Inhalt → kein Publish (Dedupe)", len(mq.pub) == 1, str(mq.pub))
disc_send("switch", "c6_0x07da", "ep16_on_off", cfg("上 开关"))
check("geänderter Name → Publish ohne Versionszähler", len(mq.pub) == 2, str(mq.pub))
check("neuer Payload enthält den neuen Namen",
      "上 开关" in mq.pub[-1][1], mq.pub[-1][1][:80])
check("Topic unverändert",
      mq.pub[-1][0] == "homeassistant/switch/c6_0x07da/ep16_on_off/config", mq.pub[-1][0])

print("\n5) Startfenster: erst einlesen, dann nur Abweichungen senden")
ns["_ha_sent"].clear()
ns["_disc_defer"].clear()
ns["_disc_remote"].clear()
mq.pub.clear()
mq.subs.clear()
ns["_addr_nodes"]["0x07da"] = "c6_0x07da"       # Gerätebestand ist geladen
ns["_ieee_nodes"]["00:12:4b:00:01"] = "c6_0x07da"
ns["_disc_begin_window"]()
check("Fenster ist offen", ns["_disc_ready"] is False, str(ns["_disc_ready"]))
check("Discovery-Topic abonniert",
      "homeassistant/+/+/+/config" in mq.subs, str(mq.subs))
disc_send("switch", "c6_0x07da", "ep16_on_off", cfg("开关"))
disc_send("sensor", "c6_0x07da", "ep1_battery", cfg("电量"))
check("im Fenster wird nichts publiziert", mq.pub == [], str(mq.pub))
check("aber vorgemerkt", len(ns["_disc_defer"]) == 2, str(ns["_disc_defer"]))

# Der Broker hat die switch-Config schon (identisch), die sensor-Config nicht.
gleich = json.dumps(cfg("开关"), sort_keys=True, ensure_ascii=False)
ns["_disc_remote"]["homeassistant/switch/c6_0x07da/ep16_on_off/config"] = gleich
time.sleep(ns["_DISC_WAIT"] + 0.1)          # Timer von _disc_begin_window
check("Fenster ist zu", ns["_disc_ready"] is True, str(ns["_disc_ready"]))
check("Abo wieder abgemeldet", "homeassistant/+/+/+/config" not in mq.subs, str(mq.subs))
check("nur die abweichende Config wurde gesendet",
      [t for t, p, r in mq.pub] == ["homeassistant/sensor/c6_0x07da/ep1_battery/config"],
      str(mq.pub))
check("die unveränderte gilt als bereits vorhanden",
      ns["_ha_sent"].get("homeassistant/switch/c6_0x07da/ep16_on_off/config") == gleich,
      str(ns["_ha_sent"].keys()))

print("\n6) verwaiste Discovery-Configs aufräumen")
ns["_addr_nodes"]["0x07da"] = "c6_0x07da"
ns["_ieee_nodes"]["00:12:4b:00:01"] = "c6_0x07da"
ns["_ctrl"].add("0x07da/17")
# Jede Config trägt ihr eigenes availability-Topic – daran wird die
# Zugehörigkeit erkannt (nicht am Knotennamen).
def _c(av_topic):
    return json.dumps({"availability": [{"topic": av_topic}]})
ns["_disc_remote"].clear(); ns["_disc_remote"].update({
    "homeassistant/switch/c6_0x9999/ep1_on_off/config": _c("gw/coordinator/status"),
    "homeassistant/switch/c6_0x07da/ep17_on_off/config": _c("gw/coordinator/status"),
    "homeassistant/switch/c6_0x07da/ep16_on_off/config": _c("gw/coordinator/status"),
    # Zweite Gateway-Instanz am selben Broker: gleicher Knotenname, anderes Topic
    "homeassistant/switch/c6_0x07da/ep16_on_off_2/config": _c("gateway2/status"),
    # Alte Fassung (vor der Listenform) – muss ebenfalls erkannt werden
    "homeassistant/switch/c6_0x9999/ep2_on_off/config": json.dumps(
        {"availability_topic": "gw/coordinator/status"}),
})
mq.pub.clear()
ns["_disc_purge_orphans"]()
weg = sorted(t for t, p, r in mq.pub)
check("unbekanntes Gerät entfernt",
      "homeassistant/switch/c6_0x9999/ep1_on_off/config" in weg, str(weg))
check("switch auf Sender-Endpunkt entfernt",
      "homeassistant/switch/c6_0x07da/ep17_on_off/config" in weg, str(weg))
check("gültiger switch bleibt",
      "homeassistant/switch/c6_0x07da/ep16_on_off/config" not in weg, str(weg))
check("zweite Instanz am selben Broker bleibt",
      "homeassistant/switch/c6_0x07da/ep16_on_off_2/config" not in weg, str(weg))
check("alte Config-Fassung (availability_topic) wird auch aufgeräumt",
      "homeassistant/switch/c6_0x9999/ep2_on_off/config" in weg, str(weg))
check("gelöscht wird mit leerem Payload + retain",
      all(p == "" and r for t, p, r in mq.pub), str(mq.pub))

print("\n6b) Aufräumen ohne Gerätebestand: lieber nichts löschen")
ns["_disc_remote"].clear(); ns["_disc_remote"].update({
    "homeassistant/switch/c6_0x9999/ep1_on_off/config": "{}"})
alte = dict(ns["_addr_nodes"]), dict(ns["_ieee_nodes"])
ns["_addr_nodes"].clear(); ns["_ieee_nodes"].clear()
mq.pub.clear()
ns["_disc_purge_orphans"]()
check("ohne Gerätebestand wird nichts gelöscht", mq.pub == [], str(mq.pub))
ns["_addr_nodes"].update(alte[0]); ns["_ieee_nodes"].update(alte[1])

print("\n7) Verfügbarkeit im Listenformat")
a = ns["_ha_avail"]()
check("availability ist eine Liste", isinstance(a["availability"], list), str(a))
check("Topic = Gateway-Status",
      a["availability"][0]["topic"] == "gw/coordinator/status", str(a))
check("availability_mode = latest", a["availability_mode"] == "latest", str(a))
check("kein altes Feld mehr", "availability_topic" not in a, str(a.keys()))

print("\n8) HA-Neustart → Zustände erneut senden")
ns["_ha_last"] = {"0x07da": {"ep16/on_off": {"v": 1}, "ep17/on_off": {"v": 0}}}
mq.pub.clear()
ns["_ha_republish_all"]("neu gestartet")
topics = sorted(t for t, p, r in mq.pub)
check("Bridge-Status zuerst", topics[0] == "gw/coordinator/status", str(topics))
check("beide Zustände erneut gesendet",
      "gw/coordinator/zigbee/0x07da/ep16/on_off" in topics
      and "gw/coordinator/zigbee/0x07da/ep17/on_off" in topics, str(topics))
check("alles retained", all(r for t, p, r in mq.pub), str(mq.pub))

print("\n%d/%d Prüfungen bestanden" % (ok, total))
