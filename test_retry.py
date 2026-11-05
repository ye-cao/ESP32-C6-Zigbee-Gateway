# -*- coding: utf-8 -*-
"""test_retry.py – _ha_handle_set 里的确认与重发。

从 serial_gateway.py 里切出真代码块，对着假串口跑。
等待时间压到毫秒级。
"""
import io, json, os, sys, threading, time

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "host", "addon", "esp32c6_gateway", "serial_gateway.py")
src = io.open(SRC, encoding="utf-8").read()
a = src.index("# ── 命令确认与重发")
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
    # 别的代码块里的函数：这里只放个假的，好让这个测试
    # 继续只测确认/重发。
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


print("1) 设备立刻确认 -> 不重发")
ser.writes.clear(); states.clear(); ns["_pending"].clear()
ns["_ha_handle_set"]("gw/coordinator/set/0x07da/ep16/on_off", "1")
check("只发了 1 条命令", len(devcmds()) == 1, str(ser.writes))
ns["_pending_ack"]("0x07da", "ep16/on_off", {"v": 1})
check("Bestätigung räumt pending ab", ("0x07da", 16) not in ns["_pending"],
      str(ns["_pending"]))
time.sleep(0.55)
check("没有第二条命令", len(devcmds()) == 1, str(ser.writes))
check("Echo war v=1", states and states[-1][2] == {"v": 1}, str(states))

print("\n2) 设备报的是相反值 -> 重发，3 次之后放弃")
ser.writes.clear(); states.clear(); ns["_pending"].clear()
ns["_ha_handle_set"]("gw/coordinator/set/0x07da/ep17/on_off", "0")
check("1. Versuch", len(devcmds()) == 1, str(devcmds()))
ns["_pending_ack"]("0x07da", "ep17/on_off", {"v": 1})     # 设备还开着
check("2. Versuch", len(devcmds()) == 2, str(devcmds()))
check("2. Versuch wiederholt 'off'", '"sw":"off"' in devcmds()[-1], devcmds()[-1])
ns["_pending_ack"]("0x07da", "ep17/on_off", {"v": 1})
check("3. Versuch", len(devcmds()) == 3, str(devcmds()))
ns["_pending_ack"]("0x07da", "ep17/on_off", {"v": 1})     # 现在放弃
check("没有第 4 次尝试", len(devcmds()) == 3, str(devcmds()))
check("pending abgeräumt", ("0x07da", 17) not in ns["_pending"], str(ns["_pending"]))
check("显示已纠正为设备值", states[-1][2] == {"v": 1}, str(states[-3:]))

print("\n3) 设备完全不答 -> 超时重发")
ser.writes.clear(); states.clear(); ns["_pending"].clear()
ns["_ha_handle_set"]("gw/coordinator/set/0x07da/ep18/on_off", "1")
check("1. Versuch", len(devcmds()) == 1, str(devcmds()))
time.sleep(0.5)
check("超时后重发了", len(devcmds()) == 2, str(devcmds()))
check("Wiederholung ist 'on'", '"sw":"on"' in devcmds()[-1], devcmds()[-1])
check("标成第 2 次尝试", ns["_pending"][("0x07da", 18)]["tries"] == 2,
      str(ns["_pending"]))
ns["_pending"].clear()
time.sleep(0.4)

print("\n4) 端点 1：topic 里不带前缀")
ser.writes.clear(); states.clear(); ns["_pending"].clear()
ns["_ha_handle_set"]("gw/coordinator/set/0x747d/ep1/on_off", "1")
check("回声发到 'on_off'（不是 ep1/on_off）", states[-1][1] == "on_off", str(states))
ns["_pending_ack"]("0x747d", "on_off", {"v": 1})
check("通过 'on_off' 认出了确认", ("0x747d", 1) not in ns["_pending"],
      str(ns["_pending"]))
ns["_pending"].clear()
time.sleep(0.4)

print("\n5) 双击：第二条命令覆盖了确认")
ser.writes.clear(); states.clear(); ns["_pending"].clear()
ns["_ha_handle_set"]("gw/coordinator/set/0x07da/ep16/on_off", "1")
ns["_ha_handle_set"]("gw/coordinator/set/0x07da/ep16/on_off", "0")
check("2 Befehle", len(devcmds()) == 2, str(devcmds()))
check("pending will jetzt 0", ns["_pending"][("0x07da", 16)]["want"] == 0,
      str(ns["_pending"]))
ns["_pending"].clear()
time.sleep(0.4)

print("\n%d/%d Pruefungen bestanden" % (ok, total))
