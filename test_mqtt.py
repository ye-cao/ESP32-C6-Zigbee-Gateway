# -*- coding: utf-8 -*-
"""test_mqtt.py – 照着 Zigbee2MQTT 做的 MQTT 层。

2026-10-17 从 Z2M 源码（lib/mqtt.ts、
lib/extension/homeassistant.ts）搬过来，并在这里钉死：

  1. mq_put()  —— 通配符校验、retained 消息登记表
  2. _republish_retained() —— 连上之后把 retained 再发一遍
  3. _disc_send() —— 内容变了才发（取代 _DISC_VER）
  4. 启动窗口 —— 先把现有配置读进来，之后只发有差别的
  5. _disc_purge_orphans() —— 删掉孤儿配置
  6. _ha_avail() / _ha_republish_all() —— availability 和 HA 重启
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

mq_block = src[src.index("# ── MQTT 出口：统一的发布路径"):
               src.index("# ── 命令确认与重发")]
cache_block = src[src.index("# ── Discovery-Cache"):
                  src.index("# ── 持久化的节点归属")]
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


print("1) mq_put：通配符校验")
mq.pub.clear()
mq_put("gw/coordinator/c6_#", "x", retain=True)
check("带 # 的 topic 被拒", mq.pub == [], str(mq.pub))
check("而且没进登记表", ns["_retained"] == {}, str(ns["_retained"]))
mq_put("gw/coordinator/a/+/b", "x")
check("带 + 的 topic 被拒", mq.pub == [], str(mq.pub))

print("\n2) mq_put：retained 消息登记表")
ns["_retained"].clear()
mq_put("t/keep", "v1", retain=True)
check("retained gemerkt", ns["_retained"].get("t/keep") == "v1", str(ns["_retained"]))
mq_put("t/keep", "", retain=True)
check("空负载会删掉这一条", "t/keep" not in ns["_retained"],
      str(ns["_retained"]))
mq_put("t/plain", "v2")
check("不带 retain 就不登记", "t/plain" not in ns["_retained"],
      str(ns["_retained"]))

print("\n3) retain：连上之后重新设置")
ns["_retained"].clear()
mq_put("d/1/config", "A", retain=True)
mq_put("d/2/config", "B", retain=True)
mq.pub.clear()
ns["_republish_retained"]()
check("两条 retained 消息都重发了",
      sorted(t for t, p, r in mq.pub) == ["d/1/config", "d/2/config"], str(mq.pub))
check("dabei retain beibehalten", all(r for t, p, r in mq.pub), str(mq.pub))

print("\n4) _disc_send：内容变了才发")
mq.pub.clear(); ns["_ha_sent"].clear()
disc_send("switch", "c6_0x07da", "ep16_on_off", cfg("开关"))
check("第一次调用会发", len(mq.pub) == 1, str(mq.pub))
disc_send("switch", "c6_0x07da", "ep16_on_off", cfg("开关"))
check("内容相同 → 不发（去重）", len(mq.pub) == 1, str(mq.pub))
disc_send("switch", "c6_0x07da", "ep16_on_off", cfg("上 开关"))
check("名字改了 → 照发，不用版本号", len(mq.pub) == 2, str(mq.pub))
check("新负载里带着新名字",
      "上 开关" in mq.pub[-1][1], mq.pub[-1][1][:80])
check("topic 没变",
      mq.pub[-1][0] == "homeassistant/switch/c6_0x07da/ep16_on_off/config", mq.pub[-1][0])

print("\n5) 启动窗口：先读进来，之后只发有差别的")
ns["_ha_sent"].clear()
ns["_disc_defer"].clear()
ns["_disc_remote"].clear()
mq.pub.clear()
mq.subs.clear()
ns["_addr_nodes"]["0x07da"] = "c6_0x07da"       # 设备存量已加载
ns["_ieee_nodes"]["00:12:4b:00:01"] = "c6_0x07da"
ns["_disc_begin_window"]()
check("Fenster ist offen", ns["_disc_ready"] is False, str(ns["_disc_ready"]))
check("Discovery-Topic abonniert",
      "homeassistant/+/+/+/config" in mq.subs, str(mq.subs))
disc_send("switch", "c6_0x07da", "ep16_on_off", cfg("开关"))
disc_send("sensor", "c6_0x07da", "ep1_battery", cfg("电量"))
check("窗口期内什么都不发", mq.pub == [], str(mq.pub))
check("aber vorgemerkt", len(ns["_disc_defer"]) == 2, str(ns["_disc_defer"]))

# broker 已经有 switch 配置了（一模一样），sensor 配置还没有。
gleich = json.dumps(cfg("开关"), sort_keys=True, ensure_ascii=False)
ns["_disc_remote"]["homeassistant/switch/c6_0x07da/ep16_on_off/config"] = gleich
time.sleep(ns["_DISC_WAIT"] + 0.1)          # Timer von _disc_begin_window
check("Fenster ist zu", ns["_disc_ready"] is True, str(ns["_disc_ready"]))
check("Abo wieder abgemeldet", "homeassistant/+/+/+/config" not in mq.subs, str(mq.subs))
check("只发了那份有差别的配置",
      [t for t, p, r in mq.pub] == ["homeassistant/sensor/c6_0x07da/ep1_battery/config"],
      str(mq.pub))
check("没变的那份视为已存在",
      ns["_ha_sent"].get("homeassistant/switch/c6_0x07da/ep16_on_off/config") == gleich,
      str(ns["_ha_sent"].keys()))

print("\n6) 清理孤儿的 discovery 配置")
ns["_addr_nodes"]["0x07da"] = "c6_0x07da"
ns["_ieee_nodes"]["00:12:4b:00:01"] = "c6_0x07da"
ns["_ctrl"].add("0x07da/17")
# 每份配置自带 availability topic —— 归属就是
# 靠它判断的（不看节点名）。
def _c(av_topic):
    return json.dumps({"availability": [{"topic": av_topic}]})
ns["_disc_remote"].clear(); ns["_disc_remote"].update({
    "homeassistant/switch/c6_0x9999/ep1_on_off/config": _c("gw/coordinator/status"),
    "homeassistant/switch/c6_0x07da/ep17_on_off/config": _c("gw/coordinator/status"),
    "homeassistant/switch/c6_0x07da/ep16_on_off/config": _c("gw/coordinator/status"),
    # 同一个 broker 上的第二个网关实例：节点名相同，topic 不同
    "homeassistant/switch/c6_0x07da/ep16_on_off_2/config": _c("gateway2/status"),
    # 旧格式（列表形式之前）—— 也必须能识别出来
    "homeassistant/switch/c6_0x9999/ep2_on_off/config": json.dumps(
        {"availability_topic": "gw/coordinator/status"}),
})
mq.pub.clear()
ns["_disc_purge_orphans"]()
weg = sorted(t for t, p, r in mq.pub)
check("未知设备被删掉",
      "homeassistant/switch/c6_0x9999/ep1_on_off/config" in weg, str(weg))
check("发送方端点上的 switch 被删掉",
      "homeassistant/switch/c6_0x07da/ep17_on_off/config" in weg, str(weg))
check("有效的 switch 留着",
      "homeassistant/switch/c6_0x07da/ep16_on_off/config" not in weg, str(weg))
check("同一 broker 上的第二个实例留着",
      "homeassistant/switch/c6_0x07da/ep16_on_off_2/config" not in weg, str(weg))
check("旧格式（availability_topic）也一起清掉",
      "homeassistant/switch/c6_0x9999/ep2_on_off/config" in weg, str(weg))
check("删除时用空负载 + retain",
      all(p == "" and r for t, p, r in mq.pub), str(mq.pub))

print("\n6b) 没有设备存量时清理：宁可什么都不删")
ns["_disc_remote"].clear(); ns["_disc_remote"].update({
    "homeassistant/switch/c6_0x9999/ep1_on_off/config": "{}"})
alte = dict(ns["_addr_nodes"]), dict(ns["_ieee_nodes"])
ns["_addr_nodes"].clear(); ns["_ieee_nodes"].clear()
mq.pub.clear()
ns["_disc_purge_orphans"]()
check("没有设备存量就一条也不删", mq.pub == [], str(mq.pub))
ns["_addr_nodes"].update(alte[0]); ns["_ieee_nodes"].update(alte[1])

print("\n7) availability 用列表形式")
a = ns["_ha_avail"]()
check("availability 是列表", isinstance(a["availability"], list), str(a))
check("Topic = Gateway-Status",
      a["availability"][0]["topic"] == "gw/coordinator/status", str(a))
check("availability_mode = latest", a["availability_mode"] == "latest", str(a))
check("已经不再有旧字段", "availability_topic" not in a, str(a.keys()))

print("\n8) HA 重启 → 重发状态")
ns["_ha_last"] = {"0x07da": {"ep16/on_off": {"v": 1}, "ep17/on_off": {"v": 0}}}
mq.pub.clear()
ns["_ha_republish_all"]("重新启动")
topics = sorted(t for t, p, r in mq.pub)
check("Bridge-Status zuerst", topics[0] == "gw/coordinator/status", str(topics))
check("两个状态都重发了",
      "gw/coordinator/zigbee/0x07da/ep16/on_off" in topics
      and "gw/coordinator/zigbee/0x07da/ep17/on_off" in topics, str(topics))
check("alles retained", all(r for t, p, r in mq.pub), str(mq.pub))

print("\n%d/%d 项检查通过" % (ok, total))
