# -*- coding: utf-8 -*-
"""test_panel.py – Tastenpanel ohne Relais (FTB56+ZSN18ZY1.1, 0x747d).

Prüft die drei Teile, die am 2026-10-17 dazugekommen sind:

  1. _noread  – Endpunkt kann seinen Zustand nicht rücklesen → ein Befehl
                pro Klick, keine Wiederholung, keine Leseanfragen mehr.
  2. _ha_aps_action – Tastendruck aus dem rohen APS-Frame erkennen
                (und die Default-Response des Geräts NICHT als Tastendruck).
  3. _ha_mark_controller / _ha_drop_switch – switch-Entity abmelden,
                event-Entity anmelden.

Schneidet die echten Codeblöcke aus serial_gateway.py heraus.
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

b1 = src.index("# ── Befehls-Bestätigung und Wiederholung")
b2 = src.index("def on_message(")
cmd_block = src[b1:b2]

b3 = src.index("# ── Tastenereignisse ──")
b4 = src.index("# ── Dispatcher ──")
act_block = src[b3:b4]

# Der Discovery-Cache (Payload-Vergleich, Startfenster, _disc_send) liegt vor
# dem Mapping-Abschnitt und wird mitgeladen – sonst wäre _ha_publish nicht
# lauffähig und die Dedupe-Prüfungen würden nur einen Stub testen.
b45 = src.index("# ── Discovery-Cache")
b5 = src.index("# sub-Basisname → (HA-Komponente")
cache_block = src[b45:b5]
b6 = src.index("def _ha_prepublish(")
pub_block = src[b5:b6]

b7 = src.index("def _ha_db_hint(")
hint_block = src[b7:b6]
print("extrahiert: Befehl %d, Taste %d, Discovery %d, DB-Hinweis %d, Cache %d Zeichen\n"
      % (len(cmd_block), len(act_block), len(pub_block), len(hint_block), len(cache_block)))


class FakeSer:
    def __init__(self):
        self.writes = []

    def write(self, b):
        self.writes.append(b.decode().strip())


class FakeMq:
    def __init__(self):
        self.pub = []       # (topic, payload, retain)

    def publish(self, topic, payload=None, retain=False, qos=0):
        self.pub.append((topic, payload, retain))


ser, mq = FakeSer(), FakeMq()
states, saved = [], []

ns = {
    "json": json, "time": time, "threading": threading, "os": os,
    "__builtins__": __builtins__,
    "ser": ser, "mq": mq,
    "BASE": "gw/coordinator",
    "HA_PREFIX": "homeassistant",
    "HA_ENABLED": True,
    "_DISC_VER": 3,
    "_js": lambda d: json.dumps(d, separators=(",", ":")) + "\n",
    "_ha_last": {},
    "_ha_state": lambda addr, sub, p, retain=True: states.append((addr, sub, dict(p))),
    "_nodes_save": lambda: saved.append(1),
    "_ha_node": lambda addr: "c6_" + addr.lstrip("0x").lower(),
    "_ha_device": lambda addr: {"identifiers": ["c6_" + addr], "name": "Panel",
                                "manufacturer": "Feibit", "model": "FTB56+ZSN18ZY1.1"},
    "_ha_ep_label": lambda addr, epn: {"ep1": "EP1", "ep2": "EP2"}.get(epn, epn.upper()),
    "_ctrl": set(),
    "_addr_nodes": {}, "_ieee_nodes": {}, "_addr_ieee": {},
    "_ieee_addr": {}, "_addr_subs": {}, "_ha_nodes": {},
    "_ha_devices": {},
    "_ha_db": {},
    "_desc": {},
    "_probe": {},
    "_dropped": set(),
}
# mq_put: Durchgriff auf den Fake-Client (die echte Funktion sitzt im
# MQTT-Abschnitt, der hier nicht mitgeladen wird).
ns["mq_put"] = lambda t, p, retain=False, qos=1: ns["mq"].publish(t, p, retain=retain, qos=qos)
exec(compile(cmd_block, "<cmd>", "exec"), ns)
exec(compile(cache_block, "<cache>", "exec"), ns)
# cache_block enthält die echte _nodes_save (schreibt Datei) – für den Test
# wieder auf den Zähler umbiegen.
ns["_nodes_save"] = lambda: saved.append(1)
exec(compile(act_block, "<act>", "exec"), ns)
exec(compile(pub_block, "<pub>", "exec"), ns)
exec(compile(hint_block, "<hint>", "exec"), ns)
ns["_READ_DELAY"] = 0.05
ns["_READ_DELAY2"] = 0.10
ns["_RETRY_AFTER"] = 0.20

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


def devcmds():
    return [w for w in ser.writes if '"dev_cmd"' in w]


def reads():
    return [w for w in ser.writes if '"read_onoff"' in w]


def aps(src_addr, sep, cl, hexstr, dep=1):
    """Baut die Zeile, die der C6 aus aps_data_ind_cb() ausgibt."""
    return {"t": "aps", "src": src_addr, "sep": sep, "dep": dep,
            "cl": cl, "sec": 0, "len": len(hexstr) // 2, "hex": hexstr}


print("1) Default-Response des Geräts ist KEIN Tastendruck")
mq.pub.clear()
ns["_ha_aps_action"](aps("0x747d", 1, "0x0006", "184f0b0100"))
check("keine action veröffentlicht", not mq.pub, str(mq.pub))
check("kein Controller-Merker", not ns["_ctrl"], str(ns["_ctrl"]))

print("\n2) Tastendruck (On, cluster-spezifisch, Client→Server)")
mq.pub.clear()
ns["_ha_aps_action"](aps("0x747d", 1, "0x0006", "014f0100"))
topics = [t for t, _p, _r in mq.pub]
check("action-Topic ohne ep1-Präfix",
      "gw/coordinator/zigbee/0x747d/action" in topics, str(topics))
pay = [p for t, p, _r in mq.pub if t.endswith("/action")]
check("Payload = {'event_type': 'on'}", pay == ['{"event_type": "on"}'], str(pay))
check("action ist nicht retained",
      all(r is False for t, _p, r in mq.pub if t.endswith("/action")), str(mq.pub))
evc = [t for t, _p, _r in mq.pub if t.startswith("homeassistant/event/")]
check("event-Discovery publiziert", len(evc) == 1, str(topics))
check("Endpunkt als Sender gemerkt", "0x747d/1" in ns["_ctrl"], str(ns["_ctrl"]))

print("\n3) Ep2 landet auf ep2/action")
mq.pub.clear()
ns["_ha_aps_action"](aps("0x747d", 2, "0x0006", "01500200"))
topics = [t for t, _p, _r in mq.pub]
check("ep2/action", "gw/coordinator/zigbee/0x747d/ep2/action" in topics, str(topics))
check("Payload toggle", [p for t, p, _r in mq.pub if t.endswith("/action")] == ['{"event_type": "toggle"}'])

print("\n4) Scenes-Recall (Cluster 0x0005, cmd 0x05)")
mq.pub.clear()
ns["_ha_aps_action"](aps("0x07da", 16, "0x0005", "011105010001"))
pay = [p for t, p, _r in mq.pub if t.endswith("/action")]
# Szenen-ID gehoert in den Wert: sonst sind alle Tasten ununterscheidbar.
# Upstream-Converter dieser Geraetefamilie liefert "recall_<sceneid>".
check("Payload 'recall_1'", pay == ['{"event_type": "recall_1"}'], str(mq.pub))
check("Topic ep16/action",
      "gw/coordinator/zigbee/0x07da/ep16/action" in [t for t, _p, _r in mq.pub])

print("\n4b) Herstellerspezifischer Frame (Frame-Control-Bit 0x04)")
mq.pub.clear()
# fc=05 = clusterspezifisch(0x01) | herstellerspezifisch(0x04)
# mfr=0x3412 (LSB first) tsn=12 cmd=05 grp=0001 sid=02
ns["_ha_aps_action"](aps("0x07da", 17, "0x0005", "0512341205010002"))
pay = [p for t, p, _r in mq.pub if t.endswith("/action")]
check("cmd wird trotz Herstellercode richtig gelesen", pay == ['{"event_type": "recall_2"}'], str(mq.pub))

print("\n4c) Tastendruck als on/off-REPORT (so meldet 0x747d wirklich)")
mq.pub.clear()
ns["_ctrl"].add("0x747d/2")
ns["_ha_button_from_report"]("0x747d", "ep2/on_off", {"v": 1})
topics = [t for t, _p, _r in mq.pub]
check("ep2/action aus Report",
      "gw/coordinator/zigbee/0x747d/ep2/action" in topics, str(topics))
check("Payload 'on'", [p for t, p, _r in mq.pub if t.endswith("/action")] == ['{"event_type": "on"}'],
      str(mq.pub))
mq.pub.clear()
ns["_ha_button_from_report"]("0x747d", "ep2/on_off", {"v": 0})
check("v=0 → 'off'", [p for t, p, _r in mq.pub if t.endswith("/action")] == ['{"event_type": "off"}'],
      str(mq.pub))

print("\n4d) Report auf unser eigenes Read / auf einen echten Schalter")
mq.pub.clear()
ns["_ha_button_from_report"]("0x747d", "ep2/on_off", {"v": 1}, was_expected=True)
check("erwarteter Wert → kein Tastendruck", not mq.pub, str(mq.pub))
ns["_ha_button_from_report"]("0x07da", "ep18/on_off", {"v": 1})
check("echter Schalter (nicht in _ctrl) → kein Tastendruck", not mq.pub, str(mq.pub))
ns["_ctrl"].discard("0x747d/2")

print("\n4e) Zweitwippe ohne eigenen _ctrl-Eintrag (Familie ist Panel)")
mq.pub.clear()
ns["_ha_db"] = {"FTB56+ZSN15HG1.0": {"model": "HGZB-1S", "vendor": "Nue / 3A",
                                    "caps": ["switch", "action"],
                                    "description": "Smart 1 key scene wall switch",
                                    "endpoints": {}},
                "FTB56+ZSN16HG1.0": {"model": "HGZB-02S", "vendor": "Nue / 3A",
                                    "caps": ["switch", "action"],
                                    "description": "Smart 2 key scene wall switch",
                                    "endpoints": {}}}
ns["_ha_devices"]["0x747d"] = {"model": "FTB56+ZSN18ZY1.1"}
ns["_ctrl"].add("0x747d/1")            # nur ep1 bekannt
ns["_ha_button_from_report"]("0x747d", "ep2/on_off", {"v": 1})
check("ep2 meldet trotzdem eine Taste",
      [p for t, p, _r in mq.pub if t.endswith("/action")] == ['{"event_type": "on"}'], str(mq.pub))
mq.pub.clear()
ns["_ha_button_from_report"]("0x9999", "ep2/on_off", {"v": 1})
check("fremdes Gerät ohne _ctrl → nichts", not mq.pub, str(mq.pub))
ns["_ha_db"] = {}
ns["_ctrl"].clear()

print("\n5) Fremder Cluster / Read-Attribute-Response wird ignoriert")
mq.pub.clear()
ns["_ha_aps_action"](aps("0x07da", 16, "0x0006", "186c010000002001"))  # Antwort
ns["_ha_aps_action"](aps("0x07da", 16, "0x0001", "08000a35002000"))    # Power
check("nichts publiziert", not mq.pub, str(mq.pub))

print("\n6) Silent-Readback: nach 3 erfolglosen Versuchen wird gelernt")
ser.writes.clear(); mq.pub.clear()
ns["_pending"].clear(); ns["_noread"].clear(); ns["_ctrl"].clear()
ns["_ha_handle_set"]("gw/coordinator/set/0x747d/ep1/on_off", "1")
time.sleep(0.75)                       # 3 Versuche à 0.2 s auslöffeln
check("genau 3 Befehle (2 Wiederholungen)", len(devcmds()) == 3, str(devcmds()))
check("0x747d/1 als nicht rücklesbar gemerkt", "0x747d/1" in ns["_noread"],
      str(ns["_noread"]))
check("in ha_nodes.json gelandet", bool(saved), str(len(saved)))

print("\n7) Nächster Klick: EIN Befehl, EINE Leseanfrage, keine Wiederholung")
ser.writes.clear(); ns["_pending"].clear()
ns["_ha_handle_set"]("gw/coordinator/set/0x747d/ep1/on_off", "0")
time.sleep(0.4)
check("genau 1 Befehl", len(devcmds()) == 1, str(devcmds()))
# Eine einzelne Leseanfrage bleibt: sie kostet einen Frame und ist die
# einzige Chance, die Einstufung automatisch zu widerrufen, falls das Gerät
# doch einmal antwortet. Vorher waren es zwei – und drei Befehle.
check("genau 1 read_onoff (statt 2)", len(reads()) == 1, str(reads()))
# pending bleibt absichtlich stehen (tries = MAX, noread = True): kommt später
# doch ein Wert, korrigiert _pending_ack die Anzeige. Wiederholen kann es
# nicht mehr – dafür sorgt das Merkmal "noread".
pend = ns["_pending"].get(("0x747d", 1)) or {}
check("pending als noread markiert", pend.get("noread") is True, str(pend))
check("keine Wiederholung mehr möglich", pend.get("tries") == ns["_MAX_TRIES"], str(pend))
time.sleep(0.3)
check("auch nach der Frist kein 2. Befehl", len(devcmds()) == 1, str(devcmds()))

print("\n8) Controller + noread → switch abgemeldet")
mq.pub.clear()
ns["_ha_sent"].clear()          # sonst greift die Dedupe aus Test 2
ns["_ha_mark_controller"]("0x747d", 1)
drops = [t for t, p, r in mq.pub
         if t.startswith("homeassistant/switch/") and p == "" and r is True]
check("switch-config mit leerem Payload retained gelöscht",
      any(t.endswith("/c6_0x747d/ep1_on_off/config") for t in drops), str(mq.pub))
check("event-Entity existiert",
      any(t.startswith("homeassistant/event/") for t, _p, _r in mq.pub), str(mq.pub))

print("\n9) _ha_publish legt den Schalter für einen Sender NICHT wieder an")
mq.pub.clear()
ns["_ha_sent"].clear()
ns["_ha_publish"]("0x747d", "on_off", {"v": 1})
check("kein switch/config für 0x747d ep1",
      not [t for t, _p, _r in mq.pub if "/switch/" in t], str(mq.pub))

print("\n10) Ein normaler Schalter bekommt seine Kachel weiterhin")
mq.pub.clear()
ns["_ha_publish"]("0x07da", "ep16/on_off", {"v": 1})
sw = [(t, p) for t, p, r in mq.pub if "/switch/" in t]
check("switch/config für 0x07da ep16 publiziert", len(sw) == 1, str(mq.pub))
check("command_topic zeigt auf set/0x07da/ep16/on_off",
      '"command_topic": "gw/coordinator/set/0x07da/ep16/on_off"' in sw[0][1],
      sw[0][1][:200] if sw else "")

print("\n11) Simple-Descriptor: On/Off als SERVER-Cluster → echter Schalter")
ns["_classify_from_desc"]("0x747d", 1, [0x0006], [])
check("kein Sender mehr", "0x747d/1" not in ns["_ctrl"], str(ns["_ctrl"]))
check("noread widerrufen", "0x747d/1" not in ns["_noread"], str(ns["_noread"]))

print("\n12) Simple-Descriptor: On/Off nur als CLIENT-Cluster → Tastenpanel")
mq.pub.clear(); ns["_ha_sent"].clear()
ns["_classify_from_desc"]("0x747d", 2, [], [0x0006, 0x0005])
check("ep2 als Sender eingestuft", "0x747d/2" in ns["_ctrl"], str(ns["_ctrl"]))
check("event-Entity publiziert",
      any(t.startswith("homeassistant/event/") for t, _p, _r in mq.pub), str(mq.pub))
check("switch NICHT abgemeldet (noch kein noread)",
      not [t for t, p, r in mq.pub if "/switch/" in t and p == ""], str(mq.pub))

print("\n13) Join-Probe: keine Antwort → noread; Antwort → widerrufen")
ns["_noread"].clear()
ns["_probe"][("0x0abc", 1)] = time.time() + 99
ns["_probe_done"]("0x0abc", 1)
check("noread gesetzt", "0x0abc/1" in ns["_noread"], str(ns["_noread"]))
ns["_probe_seen"]("0x0abc", 1)
check("durch echten Wert widerrufen", "0x0abc/1" not in ns["_noread"],
      str(ns["_noread"]))

print("\n14) Panel mit noread + ctrl → switch wird abgemeldet (auch via _classify)")
ns["_noread"].add("0x747d/2")
mq.pub.clear(); ns["_ha_sent"].clear(); ns["_dropped"].clear()
ns["_classify"]("0x747d", 2)
check("switch/config geleert",
      [t for t, p, r in mq.pub if "/switch/" in t and p == "" and r], str(mq.pub))

print("\n15) Join-Probe fragt nur Endpunkte mit On/Off-Cluster")
_real_send = ns["_send_read_onoff"]
calls = []


def _rec(addr, ep):
    calls.append((addr, ep))
    _real_send(addr, ep)


ns["_send_read_onoff"] = _rec
ns["_PROBE_AFTER"] = 0.05
ns["_PROBE_WAIT"] = 0.15
ns["_probe"].clear(); ns["_noread"].clear(); ns["_desc"].clear()
ns["_classify_from_desc"]("0x0def", 1, [0x0000, 0x0402], [])   # Temperatur
ns["_classify_from_desc"]("0x0def", 2, [0x0006], [])           # Schalter
ns["_probe_start"]("0x0def", [1, 2])
time.sleep(0.5)
check("nur ep2 geprüft", [c[1] for c in calls] == [2], str(calls))
check("Temperatur-ep NICHT als noread", "0x0def/1" not in ns["_noread"],
      str(ns["_noread"]))
check("Schalter-ep als noread (keine Antwort im Test)", "0x0def/2" in ns["_noread"],
      str(ns["_noread"]))

print("\n16) DB-Hinweis: nur Hinweis, nie Urteil")
D = ns["_ha_devices"]
D["0xaaa1"] = {"db": {"caps": ["switch", "action"], "model": "HGZB-02S",
                      "description": "Smart 2 key scene wall switch",
                      "endpoints": {}}}
D["0xaaa2"] = {"db": {"caps": ["switch", "action"], "model": "MEAZON_BIZY_PLUG",
                      "description": "Bizy plug meter", "endpoints": {}}}
D["0xaaa3"] = {"db": {"caps": ["switch"], "model": "HGZB-043",
                      "description": "Smart light switch - 3 gang",
                      "endpoints": {"top": 16}}}
check("scene wall switch → 'panel'", ns["_ha_db_hint"]("0xaaa1")[0] == "panel",
      str(ns["_ha_db_hint"]("0xaaa1")))
check("Steckdose mit action → nur 'maybe'",
      ns["_ha_db_hint"]("0xaaa2")[0] == "maybe", str(ns["_ha_db_hint"]("0xaaa2")))
check("echter Schalter → 'switch' (kein Panel-Hinweis)",
      ns["_ha_db_hint"]("0xaaa3")[0] == "switch", str(ns["_ha_db_hint"]("0xaaa3")))
check("unbekanntes Gerät → kein Hinweis", ns["_ha_db_hint"]("0xffff")[0] is None)

print("\n16b) Familien-Erkennung für Modelle, die NICHT in der DB stehen")
ns["_ha_db"] = {
    "FTB56+SKT1BCW1.0": {"caps": ["switch"], "model": "SFS01ZB",
                         "description": "Power plug", "vendor": "Nue / 3A"},
    "FTB56+ZSN15HG1.0": {"caps": ["switch", "action"], "model": "HGZB-1S",
                         "description": "Smart 1 key scene wall switch"},
    "FTB56+ZSN16HG1.0": {"caps": ["switch", "action"], "model": "HGZB-02S",
                         "description": "Smart 2 key scene wall switch"},
}
D["0xf001"] = {"model": "FTB56+ZSN18ZY1.1"}     # genau unser Gerät
k, why = ns["_ha_db_hint"]("0xf001")
check("fehlendes Modell erbt von der Familie → 'panel'", k == "panel", str((k, why)))
check("Familien-Präfix liegt bei FTB56+ZSN… (nicht FTB56+, das wäre die Steckdose)",
      why.find("FTB56+ZSN") > 0 and "SKT1" not in why, why)
pre, hits = ns["_ha_db_family"]("FTB56+ZSN18ZY1.1")
check("längstes Präfix + nur die 2 Panels",
      pre.startswith("FTB56+ZSN") and len(pre) > 9 and len(hits) == 2, str((pre, hits)))
# Gegenprobe: eine Familie, die Panel und Steckdose mischt, darf nichts behaupten
ns["_ha_db"]["ABC123X1"] = {"caps": ["switch"], "model": "X", "description": "Power plug"}
ns["_ha_db"]["ABC123Y1"] = {"caps": ["switch", "action"], "model": "Y",
                            "description": "Smart 1 key scene wall switch"}
D["0xf002"] = {"model": "ABC123Z9"}
check("gemischte Familie → kein Hinweis", ns["_ha_db_hint"]("0xf002")[0] is None,
      str(ns["_ha_db_hint"]("0xf002")))
check("Präfix bei kurzen Modellen greift nicht",
      ns["_ha_db_family"]("FTB56")[0] is None, str(ns["_ha_db_family"]("FTB56")))

print("\n17) Device-ID aus dem Simple-Descriptor als Zweitmerkmal")
ns["_classify_from_desc"]("0x0b0b", 1, [0x0000, 0x0003], [], 0x0008)   # On/Off Light Switch
check("Switch-Device-ID → Sender", "0x0b0b/1" in ns["_ctrl"], str(ns["_ctrl"]))
ns["_classify_from_desc"]("0x0b0c", 1, [0x0000, 0x0006], [], 0x0100)   # On/Off Light
check("Aktor-Device-ID + Server-Cluster → kein Sender",
      "0x0b0c/1" not in ns["_ctrl"], str(ns["_ctrl"]))

print("\n%d/%d Prüfungen bestanden" % (ok, total))
