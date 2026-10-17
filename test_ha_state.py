#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ha_state.py – HA-Seite der Steuerkette (Retain + State-Echo + Node-Stabilität).

Hintergrund (am echten Gerät gemessen): Das HGZB-043 antwortet auf On/Off nur
mit einem Default-Response (status 0x00) und schickt KEIN Report-Attribute.
Ohne Zustands-Echo bleibt die HA-Kachel auf "unknown" und springt nach jedem
Klick zurück -> "lässt sich nicht steuern".

Geprüft wird:
  1. eingehender Report landet retained auf dem state_topic
  2. Discovery-Config nennt genau das state_topic, das auch beschrieben wird
  3. HA-Befehl -> korrekte dev_cmd-Zeile auf der Leitung
  4. HA-Befehl -> Zustands-Echo (retained), damit die Kachel nicht zurückspringt
  5. toggle dreht den zuletzt bekannten Wert um
  6. command_topic aus der Discovery == Topic, das _ha_handle_set versteht
  7. Node-ID bleibt stabil, auch wenn später eine IEEE auftaucht
"""
import json
import os
import threading
import time

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "host", "addon", "esp32c6_gateway", "serial_gateway.py")
src = open(SRC, encoding="utf-8").read()

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "host", "addon", "esp32c6_gateway", "device_db.json")


def block(a, b):
    i = src.index(a)
    j = src.index(b, i)
    return src[i:j]


class FakeMQ:
    def __init__(self):
        self.log = []          # (topic, payload, retain)

    def publish(self, topic, payload, retain=False, qos=0):
        try:
            pl = json.loads(payload)
        except Exception:
            pl = payload
        self.log.append((topic, pl, retain))

    def last(self, topic):
        for t, p, r in reversed(self.log):
            if t == topic:
                return p, r
        return None, None

    def topics(self):
        return [t for t, _, _ in self.log]


class FakeSer:
    def __init__(self):
        self.written = []

    def write(self, d):
        self.written.append(d.decode())


ns = {
    "json": json,
    "os": os,
    "threading": threading,
    "BASE": "gw/coordinator",
    "HA_PREFIX": "homeassistant",
    "HA_ENABLED": True,
    "mq": FakeMQ(),
    "ser": FakeSer(),
    "_ha_db": json.load(open(DB, encoding="utf-8")),
    "_state": {"status": "online", "devices": {}},
    "_state_lock": threading.Lock(),
    "_now": lambda: "2026-10-17 11:00:00",
    "db_upsert_gateway": lambda *a, **k: None,
    "db_upsert_zigbee_device": lambda *a, **k: None,
    "db_insert_zigbee_data": lambda *a, **k: None,
    "mq_put": lambda t, p, retain=False, qos=1: None,
    "_ep_key": lambda a, e: "%s/%d" % (a, int(e)),
    "_ctrl": set(),
}
ns["mq_put"] = lambda t, p, retain=False, qos=1: ns["mq"].publish(t, p, retain=retain, qos=qos)
ns["threading"] = threading
ns["time"] = time
ns["_nodes_save"] = lambda: None
ns["_classify"] = lambda *a, **k: None
ns["_probe_seen"] = lambda *a, **k: None
ns["_noread"] = set()
ns["_addr_nodes"] = {}
ns["_ieee_nodes"] = {}

exec(block("def _js", "def _j(obj)"), ns)
exec(block("# ── HA Auto-Discovery", "# ── Dispatcher"), ns)
exec(block("# ── Befehls-Bestätigung und Wiederholung", "def on_message("), ns)
exec(block("# ── Dispatcher", "def _ota_start"), ns)

mq, ser = ns["mq"], ns["ser"]
dispatch = ns["dispatch"]
handle_set = ns["_ha_handle_set"]

ADDR = "0x07da"
STATE = "gw/coordinator/zigbee/%s/ep16/on_off" % ADDR
CMD = "gw/coordinator/set/%s/ep16/on_off" % ADDR

ok_n = fail_n = 0


def check(name, cond, extra=""):
    global ok_n, fail_n
    if cond:
        ok_n += 1
        print("  OK   " + name + ((": " + extra) if extra else ""))
    else:
        fail_n += 1
        print("  FAIL " + name + ((": " + extra) if extra else ""))


# ── 1. Report → state_topic, retained ─────────────────────────────────────
print("== 1. Report landet retained auf dem state_topic ==")
dispatch(json.dumps({"t": "zigbee", "addr": 2010, "sub": "ep16/on_off", "p": {"v": 1}}))
p, r = mq.last(STATE)
check("state_topic beschrieben", p == {"v": 1}, str(p))
check("retain=True", r is True, str(r))

# ── 2. Discovery-Config: state_topic == beschriebenes Topic ───────────────
print("\n== 2. Discovery-Config passt zum state_topic ==")
disc_topic = "homeassistant/switch/c6_0x07da/ep16_on_off/config"
cfg, cfg_r = mq.last(disc_topic)
check("Discovery-Config vorhanden", cfg is not None, disc_topic)
check("Config retained", cfg_r is True, str(cfg_r))
if cfg:
    check("state_topic stimmt", cfg.get("state_topic") == STATE, cfg.get("state_topic"))
    check("command_topic stimmt", cfg.get("command_topic") == CMD, cfg.get("command_topic"))
    check("unique_id vorhanden", bool(cfg.get("unique_id")), str(cfg.get("unique_id")))
    check("payload_on/off", (cfg.get("payload_on"), cfg.get("payload_off")) == ("1", "0"),
          "%s/%s" % (cfg.get("payload_on"), cfg.get("payload_off")))

# ── 3./4. HA-Befehl → dev_cmd + Zustands-Echo ─────────────────────────────
print("\n== 3. HA-Befehl '1' -> dev_cmd on + Echo ==")
ser.written.clear()
handle_set(CMD, "1")
check("genau eine Zeile auf der Leitung", len(ser.written) == 1, str(ser.written))
line = json.loads(ser.written[0]) if ser.written else {}
check("cmd=dev_cmd", line.get("cmd") == "dev_cmd", str(line))
check("addr/ep", (line.get("addr"), line.get("ep")) == (ADDR, 16), str(line))
check("sw=on", line.get("sw") == "on", str(line))
p, r = mq.last(STATE)
check("Echo v=1", p == {"v": 1}, str(p))
check("Echo retained", r is True, str(r))

print("\n== 4. HA-Befehl '0' -> dev_cmd off + Echo ==")
ser.written.clear()
handle_set(CMD, "0")
line = json.loads(ser.written[0]) if ser.written else {}
check("sw=off", line.get("sw") == "off", str(line))
p, _ = mq.last(STATE)
check("Echo v=0", p == {"v": 0}, str(p))

# ── 5. toggle dreht richtig ───────────────────────────────────────────────
print("\n== 5. toggle dreht den letzten Wert um ==")
handle_set(CMD, "toggle")                       # 0 -> 1
p, _ = mq.last(STATE)
check("toggle aus 0 ergibt 1", p == {"v": 1}, str(p))
handle_set(CMD, "toggle")                       # 1 -> 0
p, _ = mq.last(STATE)
check("toggle aus 1 ergibt 0", p == {"v": 0}, str(p))

# ── 6. Fremdformate werden akzeptiert (HA schickt je nach Config anders) ──
print("\n== 6. ON/OFF/true/false werden abgebildet ==")
for pl, want in [("ON", "on"), ("OFF", "off"), ("true", "on"), ("false", "off")]:
    ser.written.clear()
    handle_set(CMD, pl)
    got = json.loads(ser.written[0]).get("sw") if ser.written else None
    check("payload %-6s -> %s" % (pl, want), got == want, str(got))

# ── 7. Node-ID bleibt stabil, wenn später IEEE auftaucht ──────────────────
print("\n== 7. Node-ID ist eingefroren ==")
node_before = ns["_ha_node"](ADDR)
ns["_ha_devices"].setdefault(ADDR, {})["ieee"] = "00:12:4b:00:1c:2d:3e:4f"
node_after = ns["_ha_node"](ADDR)
check("Nodes gleich", node_before == node_after, "%s / %s" % (node_before, node_after))
check("kein zweiter Discovery-Satz", len([t for t in mq.topics() if t.endswith("ep16_on_off/config")]) == 1,
      str([t for t in mq.topics() if t.endswith("ep16_on_off/config")]))

# ── 8. Alle drei Endpunkte bekommen Entities (auch ohne Tastendruck) ──────
print("\n== 8. Nach Basic-Read: 3 Schalter sofort vorhanden ==")
dispatch(json.dumps({"t": "zigbee", "addr": 2010, "sub": "basic",
                     "p": {"mfr": "Feibit Inc co.  ", "model": "FB56+ZSW1IKJ2.5"}}))
eps = [t for t in mq.topics() if t.endswith("/config") and "/switch/" in t]
check("3 Switch-Configs", len(set(eps)) == 3, "; ".join(sorted(set(eps))))
for ep in (16, 17, 18):
    t = "homeassistant/switch/c6_0x07da/ep%d_on_off/config" % ep
    c, _ = mq.last(t)
    check("ep%d Config vorhanden" % ep, c is not None, t)

# ── 9. Sendeleistung: Kommando runter, Rücklesewert rauf ──────────────────
# Der C6 kann esp_zb_set_tx_power() zur Laufzeit umstellen. Ob die PA den
# Wunschwert wirklich fährt, sagt nur der Rücklesewert – und nur der darf in
# der Anzeige stehen. Beides wird hier festgenagelt.
print("\n== 9. Sendeleistung ==")
exec(block("def on_message(", "mq.on_connect = on_connect"), ns)
on_message = ns["on_message"]
ns["_self_pub"] = {}
ns["_disc_ready"] = True
ns["_HA_STATUS_TOPIC"] = "homeassistant/status"


class Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode()


ser.written.clear()
on_message(None, None, Msg("gw/coordinator/cmd/set_tx_power", "14"))
line = json.loads(ser.written[0]) if ser.written else {}
check("Web/MQTT-Kommando geht auf die Leitung", line.get("cmd") == "set_tx_power", str(line))
check("dBm als Zahl, nicht als String", line.get("dbm") == 14, str(line))

# Eigenes Kommando über MQTT: mq_put markiert das Topic als Selbst-Publish,
# der Echo-Schutz darf {BASE}/cmd/* trotzdem nicht schlucken.
ser.written.clear()
ns["_self_pub"]["gw/coordinator/cmd/set_tx_power"] = time.time() + 30
on_message(None, None, Msg("gw/coordinator/cmd/set_tx_power", "8"))
check("eigenes cmd/ wird nicht als Echo verworfen",
      len(ser.written) == 1 and json.loads(ser.written[0]).get("dbm") == 8,
      str(ser.written))

ser.written.clear()
on_message(None, None, Msg("gw/coordinator/cmd/set_tx_power", "999"))
check("Wert wird auf 20 dBm begrenzt",
      json.loads(ser.written[0]).get("dbm") == 20 if ser.written else None, str(ser.written))

ser.written.clear()
on_message(None, None, Msg("gw/coordinator/cmd/set_tx_power", "abc"))
check("Unsinn wird nicht weitergeleitet", ser.written == [], str(ser.written))

ser.written.clear()
on_message(None, None, Msg("gw/coordinator/cmd/set_tx_power", '{"dbm": 2}'))
check("JSON-Payload wird auch verstanden",
      json.loads(ser.written[0]).get("dbm") == 2 if ser.written else None, str(ser.written))

mq.log.clear()
dispatch(json.dumps({"t": "txpower", "want": 14, "eff": 14}))
check("Rücklesewert im Zustand", ns["_state"]["tx_power"]["eff"] == 14,
      str(ns["_state"].get("tx_power")))
p, r = mq.last("gw/coordinator/tx_power")
check("als retained Diagnose-Topic veröffentlicht", p == {"t": "txpower", "want": 14, "eff": 14},
      str(p))
check("retain=True, damit HA ihn nach Neustart sieht", r is True, str(r))

# Wunsch ≠ Wunsch: dann hat die Umstellung nicht gegriffen → muss auffallen.
mq.log.clear()
dispatch(json.dumps({"t": "txpower", "want": -4, "eff": 20}))
check("Abweichung bleibt sichtbar (want -4, eff 20)",
      ns["_state"]["tx_power"] == {"want": -4, "eff": 20, "ts": "2026-10-17 11:00:00"},
      str(ns["_state"]["tx_power"]))

print("\n== 10. LQI-Momentaufnahme auf Anfrage ==")
ser.written.clear()
on_message(None, None, Msg("gw/coordinator/cmd/lqi", ""))
check("lqi-Kommando geht auf die Leitung",
      json.loads(ser.written[0]).get("cmd") == "lqi" if ser.written else None,
      str(ser.written))

ns["_state"]["devices"].pop("0x747d", None)
mq.log.clear()
dispatch(json.dumps({"t": "lqi", "ms": 123456, "dev": [
    {"addr": "0x747d", "lqi": 61, "rssi": -68},
    {"addr": "0x07da", "lqi": 153, "rssi": -50}]}))
d = ns["_state"]["devices"].get("0x747d", {})
check("LQI/RSSI im Gerätezustand", (d.get("lqi"), d.get("rssi")) == (61, -68), str(d))
check("zweites Gerät ebenfalls übernommen",
      ns["_state"]["devices"].get("0x07da", {}).get("lqi") == 153,
      str(ns["_state"]["devices"].get("0x07da")))
p, r = mq.last("gw/coordinator/lqi")
check("als retained Topic veröffentlicht", isinstance(p, dict) and p.get("t") == "lqi", str(p))
check("retain=True", r is True, str(r))

print("\n%d OK, %d FAIL" % (ok_n, fail_n))
print("ALLE TESTS BESTANDEN" if fail_n == 0 else "TESTS FEHLGESCHLAGEN")
raise SystemExit(1 if fail_n else 0)
