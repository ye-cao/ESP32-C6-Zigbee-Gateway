#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ha_state.py – 控制链的 HA 一侧（retain + 状态回声 + 节点稳定）。

背景（在真机上量的）：HGZB-043 对 On/Off 只回一条 Default-Response
（status 0x00），**不**发 Report-Attribute。
没有状态回声，HA 卡片就停在 "unknown"，每次点击都弹回去
-> "控制不了"。

检查内容：
  1. 进来的 Report 以 retained 落到 state_topic
  2. discovery 配置里的 state_topic 与实际写入的完全一致
  3. HA 命令 -> 线上出现正确的 dev_cmd 行
  4. HA 命令 -> 状态回声（retained），卡片才不会弹回去
  5. toggle 把最近一次已知值翻过来
  6. discovery 里的 command_topic == _ha_handle_set 认得的那个 topic
  7. 之后才出现 IEEE 时，Node-ID 依然稳定
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
exec(block("# ── 命令确认与重发", "def on_message("), ns)
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
print("== 1. Report 以 retained 落到 state_topic ==")
dispatch(json.dumps({"t": "zigbee", "addr": 2010, "sub": "ep16/on_off", "p": {"v": 1}}))
p, r = mq.last(STATE)
check("state_topic beschrieben", p == {"v": 1}, str(p))
check("retain=True", r is True, str(r))

# ── 2. Discovery-Config: state_topic == beschriebenes Topic ───────────────
print("\n== 2. discovery 配置与 state_topic 对得上 ==")
disc_topic = "homeassistant/switch/c6_0x07da/ep16_on_off/config"
cfg, cfg_r = mq.last(disc_topic)
check("Discovery-Config vorhanden", cfg is not None, disc_topic)
check("Config retained", cfg_r is True, str(cfg_r))
if cfg:
    check("state_topic 对", cfg.get("state_topic") == STATE, cfg.get("state_topic"))
    check("command_topic 对", cfg.get("command_topic") == CMD, cfg.get("command_topic"))
    check("unique_id vorhanden", bool(cfg.get("unique_id")), str(cfg.get("unique_id")))
    check("payload_on/off", (cfg.get("payload_on"), cfg.get("payload_off")) == ("1", "0"),
          "%s/%s" % (cfg.get("payload_on"), cfg.get("payload_off")))

# ── 3./4. HA 命令 → dev_cmd + 状态回声 ───────────────────────────────────
print("\n== 3. HA-Befehl '1' -> dev_cmd on + Echo ==")
ser.written.clear()
handle_set(CMD, "1")
check("线上正好一行", len(ser.written) == 1, str(ser.written))
line = json.loads(ser.written[0]) if ser.written else {}
check("cmd=dev_cmd", line.get("cmd") == "dev_cmd", str(line))
check("addr/ep", (line.get("addr"), line.get("ep")) == (ADDR, 16), str(line))
check("sw=on", line.get("sw") == "on", str(line))
p, r = mq.last(STATE)
check("Echo v=1", p == {"v": 1}, str(p))
check("Echo retained", r is True, str(r))

print("\n== 4. HA 命令 '0' -> dev_cmd off + 回声 ==")
ser.written.clear()
handle_set(CMD, "0")
line = json.loads(ser.written[0]) if ser.written else {}
check("sw=off", line.get("sw") == "off", str(line))
p, _ = mq.last(STATE)
check("Echo v=0", p == {"v": 0}, str(p))

# ── 5. toggle dreht richtig ───────────────────────────────────────────────
print("\n== 5. toggle 把上一个值翻过来 ==")
handle_set(CMD, "toggle")                       # 0 -> 1
p, _ = mq.last(STATE)
check("从 0 toggle 得到 1", p == {"v": 1}, str(p))
handle_set(CMD, "toggle")                       # 1 -> 0
p, _ = mq.last(STATE)
check("从 1 toggle 得到 0", p == {"v": 0}, str(p))

# ── 6. 外部格式也接受（HA 按配置不同会发不同格式） ────────────────────────
print("\n== 6. ON/OFF/true/false werden abgebildet ==")
for pl, want in [("ON", "on"), ("OFF", "off"), ("true", "on"), ("false", "off")]:
    ser.written.clear()
    handle_set(CMD, pl)
    got = json.loads(ser.written[0]).get("sw") if ser.written else None
    check("payload %-6s -> %s" % (pl, want), got == want, str(got))

# ── 7. 之后才出现 IEEE 时，Node-ID 保持不变 ───────────────────────────────
print("\n== 7. Node-ID 已被冻结 ==")
node_before = ns["_ha_node"](ADDR)
ns["_ha_devices"].setdefault(ADDR, {})["ieee"] = "00:12:4b:00:1c:2d:3e:4f"
node_after = ns["_ha_node"](ADDR)
check("Nodes gleich", node_before == node_after, "%s / %s" % (node_before, node_after))
check("kein zweiter Discovery-Satz", len([t for t in mq.topics() if t.endswith("ep16_on_off/config")]) == 1,
      str([t for t in mq.topics() if t.endswith("ep16_on_off/config")]))

# ── 8. 三个端点都拿到实体（哪怕没按过键） ────────────────────────────────
print("\n== 8. Basic 读完之后：3 个开关立刻就在 ==")
dispatch(json.dumps({"t": "zigbee", "addr": 2010, "sub": "basic",
                     "p": {"mfr": "Feibit Inc co.  ", "model": "FB56+ZSW1IKJ2.5"}}))
eps = [t for t in mq.topics() if t.endswith("/config") and "/switch/" in t]
check("3 Switch-Configs", len(set(eps)) == 3, "; ".join(sorted(set(eps))))
for ep in (16, 17, 18):
    t = "homeassistant/switch/c6_0x07da/ep%d_on_off/config" % ep
    c, _ = mq.last(t)
    check("ep%d Config vorhanden" % ep, c is not None, t)

# ── 9. 发射功率：命令下去，回读值上来 ─────────────────────────────────────
# C6 可以在运行期调 esp_zb_set_tx_power()。PA 是否真的照着
# 期望值跑，只有回读值说了算 —— 而且也只有它能
# 显示出来。这两件事都钉在这里。
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
check("网页/MQTT 命令上了线", line.get("cmd") == "set_tx_power", str(line))
check("dBm 是数字，不是字符串", line.get("dbm") == 14, str(line))

# 经由 MQTT 发自己的命令：mq_put 会把这个 topic 标成自己发的，
# 但回声保护仍然不能把 {BASE}/cmd/* 吞掉。
ser.written.clear()
ns["_self_pub"]["gw/coordinator/cmd/set_tx_power"] = time.time() + 30
on_message(None, None, Msg("gw/coordinator/cmd/set_tx_power", "8"))
check("自己发的 cmd/ 不会被当回声丢掉",
      len(ser.written) == 1 and json.loads(ser.written[0]).get("dbm") == 8,
      str(ser.written))

ser.written.clear()
on_message(None, None, Msg("gw/coordinator/cmd/set_tx_power", "999"))
check("数值被限制在 20 dBm",
      json.loads(ser.written[0]).get("dbm") == 20 if ser.written else None, str(ser.written))

ser.written.clear()
on_message(None, None, Msg("gw/coordinator/cmd/set_tx_power", "abc"))
check("胡来的值不往下转", ser.written == [], str(ser.written))

ser.written.clear()
on_message(None, None, Msg("gw/coordinator/cmd/set_tx_power", '{"dbm": 2}'))
check("JSON 负载也认得",
      json.loads(ser.written[0]).get("dbm") == 2 if ser.written else None, str(ser.written))

mq.log.clear()
dispatch(json.dumps({"t": "txpower", "want": 14, "eff": 14}))
check("状态里有回读值", ns["_state"]["tx_power"]["eff"] == 14,
      str(ns["_state"].get("tx_power")))
p, r = mq.last("gw/coordinator/tx_power")
check("以 retained 诊断 topic 发布", p == {"t": "txpower", "want": 14, "eff": 14},
      str(p))
check("retain=True，HA 重启后还看得到", r is True, str(r))

# 期望值 ≠ 期望值：那说明切换没生效 → 必须能看出来。
mq.log.clear()
dispatch(json.dumps({"t": "txpower", "want": -4, "eff": 20}))
check("偏差要看得见（want -4、eff 20）",
      ns["_state"]["tx_power"] == {"want": -4, "eff": 20, "ts": "2026-10-17 11:00:00"},
      str(ns["_state"]["tx_power"]))

print("\n== 10. LQI-Momentaufnahme auf Anfrage ==")
ser.written.clear()
on_message(None, None, Msg("gw/coordinator/cmd/lqi", ""))
check("lqi 命令上了线",
      json.loads(ser.written[0]).get("cmd") == "lqi" if ser.written else None,
      str(ser.written))

ns["_state"]["devices"].pop("0x747d", None)
mq.log.clear()
dispatch(json.dumps({"t": "lqi", "ms": 123456, "dev": [
    {"addr": "0x747d", "lqi": 61, "rssi": -68},
    {"addr": "0x07da", "lqi": 153, "rssi": -50}]}))
d = ns["_state"]["devices"].get("0x747d", {})
check("设备状态里有 LQI/RSSI", (d.get("lqi"), d.get("rssi")) == (61, -68), str(d))
check("第二台设备也收进来了",
      ns["_state"]["devices"].get("0x07da", {}).get("lqi") == 153,
      str(ns["_state"]["devices"].get("0x07da")))
p, r = mq.last("gw/coordinator/lqi")
check("以 retained topic 发布", isinstance(p, dict) and p.get("t") == "lqi", str(p))
check("retain=True", r is True, str(r))

print("\n%d OK, %d FAIL" % (ok_n, fail_n))
print("ALLE TESTS BESTANDEN" if fail_n == 0 else "TESTS FEHLGESCHLAGEN")
raise SystemExit(1 if fail_n else 0)
