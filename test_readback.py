# -*- coding: utf-8 -*-
"""Test des Read-Back-Fallbacks in _ha_handle_set.

把这两个函数从**真实源码**里切出来，对着假串口跑。检查：
  * 先发出去的是哪条命令（dev_cmd）
  * 约 0.7 s 之后是否跟着一条 addr/ep 正确的 read_onoff
  * 乐观回声是否正确（以及之后是否可被覆盖）
"""
import io, json, os, re, sys, time

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "host", "addon", "esp32c6_gateway", "serial_gateway.py")
with io.open(SRC, "r", encoding="utf-8") as f:
    src = f.read()

a = src.index("# ── 命令确认与重发")
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


print("\n1) 对 0x07da/ep16 下 'off'，上一次状态是开")
ns["_ha_last"]["0x07da"] = {"ep16/on_off": {"v": 1}}
ns["_ha_handle_set"](f"{BASE}/set/0x07da/ep16/on_off", "OFF")

check("立刻只写了 1 条命令", len(ser.writes) == 1, str(ser.writes))
t0, first = ser.writes[0]
print("     [→C6] %s" % first)
j = json.loads(first)
check("是 dev_cmd", j.get("cmd") == "dev_cmd", first)
check("sw=off", j.get("sw") == "off", first)
check("addr korrekt", j.get("addr") == "0x07da", first)
check("ep=16 是数字", j.get("ep") == 16, first)
check("Echo {'v':0} publiziert", state_writes and state_writes[-1][2] == {"v": 0},
      str(state_writes))
check("回声 topic 是 ep16/on_off（带前缀）", state_writes[-1][1] == "ep16/on_off",
      str(state_writes))

print("\n2) 等 0.7 s —— 之后必须跟一条 read_onoff")
time.sleep(1.0)
check("2 Befehle insgesamt", len(ser.writes) == 2, str(ser.writes))
t1, second = ser.writes[1]
print("     [→C6] %s" % second)
j2 = json.loads(second)
check("是 read_onoff", j2.get("cmd") == "read_onoff", second)
check("addr korrekt", j2.get("addr") == "0x07da", second)
check("ep=16 是数字", j2.get("ep") == 16, second)
dt = t1 - t0
print("     Abstand: %.2f s" % dt)
check("Abstand ~0.7 s", 0.6 <= dt <= 1.2, "%.2f" % dt)

print("\n3) 行长 < 65（C6 的 UART 缓冲）")
check("dev_cmd  %d Zeichen" % len(first), len(first) < 65, first)
check("read_onoff %d Zeichen" % len(second), len(second) < 65, second)

print("\n4) 事先不知道状态的 'toggle' -> 开")
ser.writes.clear()
state_writes.clear()
ns["_ha_handle_set"](f"{BASE}/set/0x0abc/ep1/on_off", "toggle")
j3 = json.loads(ser.writes[0][1])
check("toggle durchgereicht", j3.get("sw") == "toggle", str(j3))
check("回声 {'v':1}（从未知变成开）", state_writes[-1][2] == {"v": 1},
      str(state_writes))
check("回声 topic 是 ep1，**不带**前缀（= 实体的 state_topic）",
      state_writes[-1][1] == "on_off", str(state_writes))
time.sleep(1.0)
check("这里也补了一条 read_onoff",
      any(json.loads(w).get("cmd") == "read_onoff" for _, w in ser.writes),
      str(ser.writes))

print("\n5) 已知状态为开时的 'toggle' -> 关")
ser.writes.clear()
state_writes.clear()
# 键 = _ha_state 存的样子：ep1 不带前缀。
# （bug 就在这儿：_ha_last 是按 'ep1/on_off' 找的，
#  而 _ha_state 存的是 'on_off' -> toggle 永远翻成"开"。）
ns["_ha_last"]["0x0abc"] = {"on_off": {"v": 1}}
ns["_ha_handle_set"](f"{BASE}/set/0x0abc/ep1/on_off", "toggle")
check("Echo {'v':0}", state_writes[-1][2] == {"v": 0}, str(state_writes))
time.sleep(0.9)

print("\n%d/%d Pruefungen bestanden" % (ok, total))
