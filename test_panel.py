# -*- coding: utf-8 -*-
"""test_panel.py – 没有继电器的按键面板（FTB56+ZSN18ZY1.1、0x747d）。

检查 2026-10-17 新增的三部分：

  1. _noread  —— 端点状态不可回读 → 一次点击只发一条命令，
                不重发，也不再发读取请求。
  2. _ha_aps_action —— 从原始 APS 帧里认出按键
                （并且**不**把设备的 Default-Response 当成按键）。
  3. _ha_mark_controller / _ha_drop_switch – switch-Entity abmelden,
                event-Entity anmelden.

从 serial_gateway.py 里切出真代码块。
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

b1 = src.index("# ── 命令确认与重发")
b2 = src.index("def on_message(")
cmd_block = src[b1:b2]

b3 = src.index("# ── 按键事件 ──")
b4 = src.index("# ── Dispatcher ──")
act_block = src[b3:b4]

# discovery 缓存（负载比对、启动窗口、_disc_send）在映射那一段
# 之前，一并加载 —— 否则 _ha_publish 跑不起来，
# 去重检查也就只是在测一个桩。
b45 = src.index("# ── Discovery-Cache")
b5 = src.index("# sub-Basisname → (HA-Komponente")
cache_block = src[b45:b5]
b6 = src.index("def _ha_prepublish(")
pub_block = src[b5:b6]

b7 = src.index("def _ha_db_hint(")
hint_block = src[b7:b6]
print("已切出：命令 %d、按键 %d、discovery %d、DB 提示 %d、缓存 %d 字符\n"
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
# mq_put：直接穿透到假客户端（真函数在
# MQTT 那段里，这里没加载）。
ns["mq_put"] = lambda t, p, retain=False, qos=1: ns["mq"].publish(t, p, retain=retain, qos=qos)
exec(compile(cmd_block, "<cmd>", "exec"), ns)
exec(compile(cache_block, "<cache>", "exec"), ns)
# cache_block 里是真正的 _nodes_save（会写文件）—— 测试里
# 把它改回计数器。
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
    """构造 C6 在 aps_data_ind_cb() 里输出的那一行。"""
    return {"t": "aps", "src": src_addr, "sep": sep, "dep": dep,
            "cl": cl, "sec": 0, "len": len(hexstr) // 2, "hex": hexstr}


print("1) 设备的 Default-Response **不是**按键")
mq.pub.clear()
ns["_ha_aps_action"](aps("0x747d", 1, "0x0006", "184f0b0100"))
check("没有发布 action", not mq.pub, str(mq.pub))
check("kein Controller-Merker", not ns["_ctrl"], str(ns["_ctrl"]))

print("\n2) Tastendruck (On, cluster-spezifisch, Client→Server)")
mq.pub.clear()
ns["_ha_aps_action"](aps("0x747d", 1, "0x0006", "014f0100"))
topics = [t for t, _p, _r in mq.pub]
check("action topic 不带 ep1 前缀",
      "gw/coordinator/zigbee/0x747d/action" in topics, str(topics))
pay = [p for t, p, _r in mq.pub if t.endswith("/action")]
check("Payload = {'event_type': 'on'}", pay == ['{"event_type": "on"}'], str(pay))
check("action 不带 retain",
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
# 场景 ID 必须进到值里：否则所有按键都分不出来。
# 这个设备族的上游转换器给出的是 "recall_<sceneid>"。
check("Payload 'recall_1'", pay == ['{"event_type": "recall_1"}'], str(mq.pub))
check("Topic ep16/action",
      "gw/coordinator/zigbee/0x07da/ep16/action" in [t for t, _p, _r in mq.pub])

print("\n4b) Herstellerspezifischer Frame (Frame-Control-Bit 0x04)")
mq.pub.clear()
# fc=05 = clusterspezifisch(0x01) | herstellerspezifisch(0x04)
# mfr=0x3412 (LSB first) tsn=12 cmd=05 grp=0001 sid=02
ns["_ha_aps_action"](aps("0x07da", 17, "0x0005", "0512341205010002"))
pay = [p for t, p, _r in mq.pub if t.endswith("/action")]
check("有厂商码时 cmd 照样读对", pay == ['{"event_type": "recall_2"}'], str(mq.pub))

print("\n4c) Tastendruck als on/off-REPORT (so meldet 0x747d wirklich)")
mq.pub.clear()
ns["_ctrl"].add("0x747d/2")
ns["_ha_button_from_report"]("0x747d", "ep2/on_off", {"v": 1})
topics = [t for t, _p, _r in mq.pub]
check("从 Report 得出 ep2/action",
      "gw/coordinator/zigbee/0x747d/ep2/action" in topics, str(topics))
check("Payload 'on'", [p for t, p, _r in mq.pub if t.endswith("/action")] == ['{"event_type": "on"}'],
      str(mq.pub))
mq.pub.clear()
ns["_ha_button_from_report"]("0x747d", "ep2/on_off", {"v": 0})
check("v=0 → 'off'", [p for t, p, _r in mq.pub if t.endswith("/action")] == ['{"event_type": "off"}'],
      str(mq.pub))

print("\n4d) Report 是在回答我们自己的读取 / 来自真开关")
mq.pub.clear()
ns["_ha_button_from_report"]("0x747d", "ep2/on_off", {"v": 1}, was_expected=True)
check("这是我们等的值 → 不算按键", not mq.pub, str(mq.pub))
ns["_ha_button_from_report"]("0x07da", "ep18/on_off", {"v": 1})
check("真开关（不在 _ctrl 里）→ 不算按键", not mq.pub, str(mq.pub))
ns["_ctrl"].discard("0x747d/2")

print("\n4e) 另一个翘板没有自己的 _ctrl 条目（但这一族是面板）")
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
ns["_ctrl"].add("0x747d/1")            # 只有 ep1 已知
ns["_ha_button_from_report"]("0x747d", "ep2/on_off", {"v": 1})
check("ep2 照样报出按键",
      [p for t, p, _r in mq.pub if t.endswith("/action")] == ['{"event_type": "on"}'], str(mq.pub))
mq.pub.clear()
ns["_ha_button_from_report"]("0x9999", "ep2/on_off", {"v": 1})
check("不认识的设备（无 _ctrl）→ 什么都不发", not mq.pub, str(mq.pub))
ns["_ha_db"] = {}
ns["_ctrl"].clear()

print("\n5) 陌生簇 / Read-Attributes 应答一律忽略")
mq.pub.clear()
ns["_ha_aps_action"](aps("0x07da", 16, "0x0006", "186c010000002001"))  # 这是应答
ns["_ha_aps_action"](aps("0x07da", 16, "0x0001", "08000a35002000"))    # Power
check("nichts publiziert", not mq.pub, str(mq.pub))

print("\n6) 静默回读：3 次失败之后就记住了")
ser.writes.clear(); mq.pub.clear()
ns["_pending"].clear(); ns["_noread"].clear(); ns["_ctrl"].clear()
ns["_ha_handle_set"]("gw/coordinator/set/0x747d/ep1/on_off", "1")
time.sleep(0.75)                       # 把 3 次 × 0.2 s 的重试等完
check("genau 3 Befehle (2 Wiederholungen)", len(devcmds()) == 3, str(devcmds()))
check("0x747d/1 被记为不可回读", "0x747d/1" in ns["_noread"],
      str(ns["_noread"]))
check("in ha_nodes.json gelandet", bool(saved), str(len(saved)))

print("\n7) 下一次点击：一条命令、一次读取、不重发")
ser.writes.clear(); ns["_pending"].clear()
ns["_ha_handle_set"]("gw/coordinator/set/0x747d/ep1/on_off", "0")
time.sleep(0.4)
check("正好 1 条命令", len(devcmds()) == 1, str(devcmds()))
# 保留了一次读取：它只花一个帧，却是
# 万一设备忽然应答时自动撤销分级的唯一机会。
# 以前是两次 —— 外加三条命令。
check("genau 1 read_onoff (statt 2)", len(reads()) == 1, str(reads()))
# pending 故意留着（tries = MAX、noread = True）：万一之后
# 真来了值，_pending_ack 会把显示纠正回来。重发它已经
# 做不到了 —— "noread" 这个特征管着。
pend = ns["_pending"].get(("0x747d", 1)) or {}
check("pending als noread markiert", pend.get("noread") is True, str(pend))
check("已经不可能重发", pend.get("tries") == ns["_MAX_TRIES"], str(pend))
time.sleep(0.3)
check("超时之后也没有第 2 条命令", len(devcmds()) == 1, str(devcmds()))

print("\n8) Controller + noread → switch abgemeldet")
mq.pub.clear()
ns["_ha_sent"].clear()          # 否则第 2 项测试的去重会生效
ns["_ha_mark_controller"]("0x747d", 1)
drops = [t for t, p, r in mq.pub
         if t.startswith("homeassistant/switch/") and p == "" and r is True]
check("switch 配置用空负载 + retain 删掉了",
      any(t.endswith("/c6_0x747d/ep1_on_off/config") for t in drops), str(mq.pub))
check("event-Entity existiert",
      any(t.startswith("homeassistant/event/") for t, _p, _r in mq.pub), str(mq.pub))

print("\n9) _ha_publish **不会**给发送方重新建开关")
mq.pub.clear()
ns["_ha_sent"].clear()
ns["_ha_publish"]("0x747d", "on_off", {"v": 1})
check("0x747d ep1 没有 switch/config",
      not [t for t, _p, _r in mq.pub if "/switch/" in t], str(mq.pub))

print("\n10) 普通开关照样拿到自己的卡片")
mq.pub.clear()
ns["_ha_publish"]("0x07da", "ep16/on_off", {"v": 1})
sw = [(t, p) for t, p, r in mq.pub if "/switch/" in t]
check("已发布 0x07da ep16 的 switch/config", len(sw) == 1, str(mq.pub))
check("command_topic 指向 set/0x07da/ep16/on_off",
      '"command_topic": "gw/coordinator/set/0x07da/ep16/on_off"' in sw[0][1],
      sw[0][1][:200] if sw else "")

print("\n11) Simple-Descriptor：On/Off 是 SERVER 簇 → 真开关")
ns["_classify_from_desc"]("0x747d", 1, [0x0006], [])
check("不再是发送方", "0x747d/1" not in ns["_ctrl"], str(ns["_ctrl"]))
check("noread widerrufen", "0x747d/1" not in ns["_noread"], str(ns["_noread"]))

print("\n12) Simple-Descriptor：On/Off 只是 CLIENT 簇 → 按键面板")
mq.pub.clear(); ns["_ha_sent"].clear()
ns["_classify_from_desc"]("0x747d", 2, [], [0x0006, 0x0005])
check("ep2 als Sender eingestuft", "0x747d/2" in ns["_ctrl"], str(ns["_ctrl"]))
check("event-Entity publiziert",
      any(t.startswith("homeassistant/event/") for t, _p, _r in mq.pub), str(mq.pub))
check("switch **没有**被撤（还没判成 noread）",
      not [t for t, p, r in mq.pub if "/switch/" in t and p == ""], str(mq.pub))

print("\n13) 入网探测：没回应 → noread；有回应 → 撤销")
ns["_noread"].clear()
ns["_probe"][("0x0abc", 1)] = time.time() + 99
ns["_probe_done"]("0x0abc", 1)
check("noread gesetzt", "0x0abc/1" in ns["_noread"], str(ns["_noread"]))
ns["_probe_seen"]("0x0abc", 1)
check("被真实值撤销了", "0x0abc/1" not in ns["_noread"],
      str(ns["_noread"]))

print("\n14) 面板 noread + ctrl → 撤掉 switch（走 _classify 那条路也一样）")
ns["_noread"].add("0x747d/2")
mq.pub.clear(); ns["_ha_sent"].clear(); ns["_dropped"].clear()
ns["_classify"]("0x747d", 2)
check("switch/config geleert",
      [t for t, p, r in mq.pub if "/switch/" in t and p == "" and r], str(mq.pub))

print("\n15) 入网探测只问带 On/Off 簇的端点")
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
ns["_classify_from_desc"]("0x0def", 2, [0x0006], [])           # 开关
ns["_probe_start"]("0x0def", [1, 2])
time.sleep(0.5)
check("只查了 ep2", [c[1] for c in calls] == [2], str(calls))
check("Temperatur-ep NICHT als noread", "0x0def/1" not in ns["_noread"],
      str(ns["_noread"]))
check("开关端点被判 noread（测试里没给回应）", "0x0def/2" in ns["_noread"],
      str(ns["_noread"]))

print("\n16) DB 提示：只是提示，从不下结论")
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
check("带 action 的插座 → 只给 'maybe'",
      ns["_ha_db_hint"]("0xaaa2")[0] == "maybe", str(ns["_ha_db_hint"]("0xaaa2")))
check("真开关 → 'switch'（不提示面板）",
      ns["_ha_db_hint"]("0xaaa3")[0] == "switch", str(ns["_ha_db_hint"]("0xaaa3")))
check("未知设备 → 不给提示", ns["_ha_db_hint"]("0xffff")[0] is None)

print("\n16b) DB 里**没有**的型号走系列识别")
ns["_ha_db"] = {
    "FTB56+SKT1BCW1.0": {"caps": ["switch"], "model": "SFS01ZB",
                         "description": "Power plug", "vendor": "Nue / 3A"},
    "FTB56+ZSN15HG1.0": {"caps": ["switch", "action"], "model": "HGZB-1S",
                         "description": "Smart 1 key scene wall switch"},
    "FTB56+ZSN16HG1.0": {"caps": ["switch", "action"], "model": "HGZB-02S",
                         "description": "Smart 2 key scene wall switch"},
}
D["0xf001"] = {"model": "FTB56+ZSN18ZY1.1"}     # 正是我们这台
k, why = ns["_ha_db_hint"]("0xf001")
check("缺失型号从系列继承 → 'panel'", k == "panel", str((k, why)))
check("系列前缀落在 FTB56+ZSN…（不是 FTB56+，那是插座）",
      why.find("FTB56+ZSN") > 0 and "SKT1" not in why, why)
pre, hits = ns["_ha_db_family"]("FTB56+ZSN18ZY1.1")
check("最长前缀 + 只有那 2 个面板",
      pre.startswith("FTB56+ZSN") and len(pre) > 9 and len(hits) == 2, str((pre, hits)))
# 反向验证：混了面板和插座的一族，不许下任何结论
ns["_ha_db"]["ABC123X1"] = {"caps": ["switch"], "model": "X", "description": "Power plug"}
ns["_ha_db"]["ABC123Y1"] = {"caps": ["switch", "action"], "model": "Y",
                            "description": "Smart 1 key scene wall switch"}
D["0xf002"] = {"model": "ABC123Z9"}
check("gemischte Familie → kein Hinweis", ns["_ha_db_hint"]("0xf002")[0] is None,
      str(ns["_ha_db_hint"]("0xf002")))
check("型号短时前缀不生效",
      ns["_ha_db_family"]("FTB56")[0] is None, str(ns["_ha_db_family"]("FTB56")))

print("\n17) 用 Simple-Descriptor 的 Device-ID 作第二特征")
ns["_classify_from_desc"]("0x0b0b", 1, [0x0000, 0x0003], [], 0x0008)   # On/Off Light Switch
check("Switch-Device-ID → Sender", "0x0b0b/1" in ns["_ctrl"], str(ns["_ctrl"]))
ns["_classify_from_desc"]("0x0b0c", 1, [0x0000, 0x0006], [], 0x0100)   # On/Off Light
check("执行器 Device-ID + Server 簇 → 不是发送方",
      "0x0b0c/1" not in ns["_ctrl"], str(ns["_ctrl"]))

print("\n%d/%d 项检查通过" % (ok, total))
