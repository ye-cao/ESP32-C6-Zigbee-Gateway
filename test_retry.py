# -*- coding: utf-8 -*-
"""test_retry.py – Bestätigung und Wiederholung in _ha_handle_set.

Schneidet den echten Block aus serial_gateway.py und laesst ihn gegen ein
Fake-Serial laufen. Die Wartezeiten werden auf Millisekunden verkuerzt.
"""
import io, json, os, sys, threading, time

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "host", "addon", "esp32c6_gateway", "serial_gateway.py")
src = io.open(SRC, encoding="utf-8").read()
a = src.index("# ── Befehls-Bestätigung und Wiederholung")
b = src.index("def on_message(")
block = src[a:b]
print("extrahiert: %d Zeichen\n" % len(block))


class FakeSer:
    def __init__(self):
        self.writes = []

    def write(self, b):
        self.writes.append(b.decode().strip())


ser = FakeSer()
states = []

ns = {
    "json": json, "time": time, "threading": threading, "os": os,
    "__builtins__": __builtins__,
    "ser": ser,
    "BASE": "gw/coordinator",
    "HA_PREFIX": "homeassistant",
    "mq": type("M", (), {"publish": staticmethod(lambda *a, **k: None)})(),
    "_js": lambda d: json.dumps(d, separators=(",", ":")) + "\n",
    "_ha_last": {},
    "_ha_state": lambda addr, sub, p, retain=True: states.append((addr, sub, dict(p))),
    # Funktionen aus anderen Blöcken: hier nur als Attrappe, damit dieser
    # Test weiterhin ausschließlich Bestätigung/Wiederholung prüft.
    "_nodes_save": lambda: None,
    "_probe_seen": lambda addr, ep: None,
    "_classify": lambda *a, **k: None,
}
exec(compile(block, "<extract>", "exec"), ns)

# Wartezeiten verkuerzen
ns["_READ_DELAY"] = 0.05
ns["_READ_DELAY2"] = 0.10
ns["_RETRY_AFTER"] = 0.30

ok = 0
total = 0


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


print("1) Gerät bestätigt sofort -> keine Wiederholung")
ser.writes.clear(); states.clear(); ns["_pending"].clear()
ns["_ha_handle_set"]("gw/coordinator/set/0x07da/ep16/on_off", "1")
check("1 Befehl raus", len(devcmds()) == 1, str(ser.writes))
ns["_pending_ack"]("0x07da", "ep16/on_off", {"v": 1})
check("Bestätigung räumt pending ab", ("0x07da", 16) not in ns["_pending"],
      str(ns["_pending"]))
time.sleep(0.55)
check("kein zweiter Befehl", len(devcmds()) == 1, str(ser.writes))
check("Echo war v=1", states and states[-1][2] == {"v": 1}, str(states))

print("\n2) Gerät meldet Gegenteil -> Wiederholung, nach 3 Versuchen aufgeben")
ser.writes.clear(); states.clear(); ns["_pending"].clear()
ns["_ha_handle_set"]("gw/coordinator/set/0x07da/ep17/on_off", "0")
check("1. Versuch", len(devcmds()) == 1, str(devcmds()))
ns["_pending_ack"]("0x07da", "ep17/on_off", {"v": 1})     # Gerät ist noch an
check("2. Versuch", len(devcmds()) == 2, str(devcmds()))
check("2. Versuch wiederholt 'off'", '"sw":"off"' in devcmds()[-1], devcmds()[-1])
ns["_pending_ack"]("0x07da", "ep17/on_off", {"v": 1})
check("3. Versuch", len(devcmds()) == 3, str(devcmds()))
ns["_pending_ack"]("0x07da", "ep17/on_off", {"v": 1})     # jetzt aufgeben
check("kein 4. Versuch", len(devcmds()) == 3, str(devcmds()))
check("pending abgeräumt", ("0x07da", 17) not in ns["_pending"], str(ns["_pending"]))
check("Anzeige auf Gerätewert korrigiert", states[-1][2] == {"v": 1}, str(states[-3:]))

print("\n3) Gerät antwortet gar nicht -> Timeout-Wiederholung")
ser.writes.clear(); states.clear(); ns["_pending"].clear()
ns["_ha_handle_set"]("gw/coordinator/set/0x07da/ep18/on_off", "1")
check("1. Versuch", len(devcmds()) == 1, str(devcmds()))
time.sleep(0.5)
check("nach Timeout wiederholt", len(devcmds()) == 2, str(devcmds()))
check("Wiederholung ist 'on'", '"sw":"on"' in devcmds()[-1], devcmds()[-1])
check("als Versuch 2 markiert", ns["_pending"][("0x07da", 18)]["tries"] == 2,
      str(ns["_pending"]))
ns["_pending"].clear()
time.sleep(0.4)

print("\n4) Endpunkt 1: kein Präfix im Topic")
ser.writes.clear(); states.clear(); ns["_pending"].clear()
ns["_ha_handle_set"]("gw/coordinator/set/0x747d/ep1/on_off", "1")
check("Echo auf 'on_off' (nicht ep1/on_off)", states[-1][1] == "on_off", str(states))
ns["_pending_ack"]("0x747d", "on_off", {"v": 1})
check("Bestätigung über 'on_off' erkannt", ("0x747d", 1) not in ns["_pending"],
      str(ns["_pending"]))
ns["_pending"].clear()
time.sleep(0.4)

print("\n5) Doppelklick: zweiter Befehl überschreibt die Bestätigung")
ser.writes.clear(); states.clear(); ns["_pending"].clear()
ns["_ha_handle_set"]("gw/coordinator/set/0x07da/ep16/on_off", "1")
ns["_ha_handle_set"]("gw/coordinator/set/0x07da/ep16/on_off", "0")
check("2 Befehle", len(devcmds()) == 2, str(devcmds()))
check("pending will jetzt 0", ns["_pending"][("0x07da", 16)]["want"] == 0,
      str(ns["_pending"]))
ns["_pending"].clear()
time.sleep(0.4)

print("\n%d/%d Pruefungen bestanden" % (ok, total))
