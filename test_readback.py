# -*- coding: utf-8 -*-
"""Test des Read-Back-Fallbacks in _ha_handle_set.

Schneidet die beiden Funktionen aus dem ECHTEN Quelltext und laesst sie
gegen ein Fake-Serial laufen. Geprueft wird:
  * welcher Befehl zuerst rausgeht (dev_cmd)
  * dass ~0.7 s spaeter read_onoff mit richtiger addr/ep folgt
  * dass der optimistische Echo stimmt (und spaeter ueberschreibbar ist)
"""
import io, json, os, re, sys, time

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "host", "addon", "esp32c6_gateway", "serial_gateway.py")
with io.open(SRC, "r", encoding="utf-8") as f:
    src = f.read()

a = src.index("# ── Befehls-Bestätigung und Wiederholung")
b = src.index("def on_message(")
block = src[a:b]
print("extrahiert: %d Zeichen" % len(block))


class FakeSer:
    def __init__(self):
        self.writes = []

    def write(self, b):
        self.writes.append((time.time(), b.decode().strip()))


ser = FakeSer()
state_writes = []
BASE = "esp32c6"

ns = {"json": json, "time": time, "threading": __import__("threading"),
      "os": os, "__builtins__": __builtins__,
      "ser": ser, "BASE": BASE,
      "_js": lambda d: json.dumps(d, separators=(",", ":")) + "\n",
      "_ha_last": {},
      "_ha_state": lambda addr, sub, p, retain=True: state_writes.append((addr, sub, p))}
exec(compile(block, "<extract>", "exec"), ns)

ok = 0
total = 0


def check(label, cond, detail=""):
    global ok, total
    total += 1
    if cond:
        ok += 1
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s %s" % (label, detail))
        sys.exit(1)


print("\n1) Befehl 'off' auf 0x07da/ep16, letzter Zustand war an")
ns["_ha_last"]["0x07da"] = {"ep16/on_off": {"v": 1}}
ns["_ha_handle_set"](f"{BASE}/set/0x07da/ep16/on_off", "OFF")

check("genau 1 Befehl sofort geschrieben", len(ser.writes) == 1, str(ser.writes))
t0, first = ser.writes[0]
print("     [→C6] %s" % first)
j = json.loads(first)
check("ist dev_cmd", j.get("cmd") == "dev_cmd", first)
check("sw=off", j.get("sw") == "off", first)
check("addr korrekt", j.get("addr") == "0x07da", first)
check("ep=16 als Zahl", j.get("ep") == 16, first)
check("Echo {'v':0} publiziert", state_writes and state_writes[-1][2] == {"v": 0},
      str(state_writes))
check("Echo-Topic ep16/on_off (mit Praefix)", state_writes[-1][1] == "ep16/on_off",
      str(state_writes))

print("\n2) 0.7 s warten – dann muss read_onoff folgen")
time.sleep(1.0)
check("2 Befehle insgesamt", len(ser.writes) == 2, str(ser.writes))
t1, second = ser.writes[1]
print("     [→C6] %s" % second)
j2 = json.loads(second)
check("ist read_onoff", j2.get("cmd") == "read_onoff", second)
check("addr korrekt", j2.get("addr") == "0x07da", second)
check("ep=16 als Zahl", j2.get("ep") == 16, second)
dt = t1 - t0
print("     Abstand: %.2f s" % dt)
check("Abstand ~0.7 s", 0.6 <= dt <= 1.2, "%.2f" % dt)

print("\n3) Zeilenlaenge < 65 (UART-Puffer des C6)")
check("dev_cmd  %d Zeichen" % len(first), len(first) < 65, first)
check("read_onoff %d Zeichen" % len(second), len(second) < 65, second)

print("\n4) 'toggle' ohne Vorwissen -> an")
ser.writes.clear()
state_writes.clear()
ns["_ha_handle_set"](f"{BASE}/set/0x0abc/ep1/on_off", "toggle")
j3 = json.loads(ser.writes[0][1])
check("toggle durchgereicht", j3.get("sw") == "toggle", str(j3))
check("Echo {'v':1} (aus unbekannt wird an)", state_writes[-1][2] == {"v": 1},
      str(state_writes))
check("Echo-Topic ep1 OHNE Praefix (= state_topic der Entity)",
      state_writes[-1][1] == "on_off", str(state_writes))
time.sleep(1.0)
check("auch hier read_onoff nachgeschoben",
      any(json.loads(w).get("cmd") == "read_onoff" for _, w in ser.writes),
      str(ser.writes))

print("\n5) 'toggle' mit bekanntem Zustand an -> aus")
ser.writes.clear()
state_writes.clear()
# Schluessel = wie _ha_state ihn ablegt: bei ep1 ohne Praefix.
# (Genau hier lag der Bug: _ha_last wurde unter 'ep1/on_off' gesucht,
#  _ha_state legte aber 'on_off' ab -> toggle ging immer auf "an".)
ns["_ha_last"]["0x0abc"] = {"on_off": {"v": 1}}
ns["_ha_handle_set"](f"{BASE}/set/0x0abc/ep1/on_off", "toggle")
check("Echo {'v':0}", state_writes[-1][2] == {"v": 0}, str(state_writes))
time.sleep(0.9)

print("\n%d/%d Pruefungen bestanden" % (ok, total))
