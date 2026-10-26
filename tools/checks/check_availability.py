"""临时检查：设备级可用性 + 邻居表种设备 + 最后上报时间。

背景：以前所有实体只订阅网关的 status，网关在线 ⇒ 掉线开关也显示"关"，
点了没反应还以为是网络问题。现在每台设备一条 availability 主题，
availability_mode="all"（网关在线 **并且** 设备在线，实体才可用）。
"""
import sys, types, importlib.util, time, json

fake_serial = types.ModuleType("serial")


class _FakeSer:
    def __init__(self, *a, **k): self.port = None
    def open(self): pass
    def close(self): pass
    def write(self, *a, **k): pass
    @property
    def in_waiting(self): raise KeyboardInterrupt("test-stop")
    def read(self, *a, **k): raise KeyboardInterrupt("test-stop")


fake_serial.Serial = _FakeSer
sys.modules["serial"] = fake_serial
sys.modules["serial.serialutil"] = types.ModuleType("serial.serialutil")
sys.modules["pymysql"] = types.ModuleType("pymysql")
paho = types.ModuleType("paho"); mqttm = types.ModuleType("paho.mqtt")
clientm = types.ModuleType("paho.mqtt.client")
SENT = []


class _FakeMQTT:
    def __init__(self, *a, **k): pass
    def __getattr__(self, name): return lambda *a, **k: None
    def publish(self, topic, payload=None, retain=False, qos=0):
        SENT.append((topic, payload, retain))


clientm.Client = _FakeMQTT
sys.modules["paho"] = paho
sys.modules["paho.mqtt"] = mqttm
sys.modules["paho.mqtt.client"] = clientm

sys.argv = ["serial_gateway.py", "--port", "/dev/null", "--baud", "115200",
            "--broker", "127.0.0.1", "--mqport", "1883",
            "--base", "gw/coordinator", "--topo-interval", "0"]
spec = importlib.util.spec_from_file_location("sg", "host/addon/esp32c6_gateway/serial_gateway.py")
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
except KeyboardInterrupt:
    pass

fail = 0


def chk(cond, msg):
    global fail
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        fail += 1


print("== discovery 里的 availability")
av = mod._ha_avail("0x2ad6")
chk(av["availability_mode"] == "all", 'availability_mode = "all"（网关 AND 设备）')
chk(len(av["availability"]) == 2, "两个来源")
chk(av["availability"][0]["topic"] == "gw/coordinator/status",
    "★ [0] 仍是网关主题（_disc_purge_orphans 靠它判归属）")
chk(av["availability"][1]["topic"] == "gw/coordinator/dev/0x2ad6/status",
    "★ [1] 是设备自己的主题")
chk(len(mod._ha_avail()["availability"]) == 1, "不传 addr 时只有一个来源（兼容旧调用）")

print("== 判活记分板")
mod._avail_last_ok.clear(); mod._avail_last_bad.clear()
chk(mod._avail_online("0x9999") is True, "毫无证据 → 不主动判死（保持旧行为）")
mod._avail_touch("0x9999")
chk(mod._avail_online("0x9999") is True, "有证据且新鲜 → 在线")
mod._avail_last_bad["0x9999"] = time.time() + 1
chk(mod._avail_online("0x9999") is False, "★ 探测失败晚于最后成功 → 离线")
mod._avail_last_bad.pop("0x9999")
mod._avail_last_ok["0x9999"] = time.time() - 31 * 60
mod._state.setdefault("node_desc", {})["0x9999"] = {"power": "mains"}
chk(mod._avail_online("0x9999") is False, "市电设备 31 min 无证据 → 离线")
mod._state["node_desc"]["0x9999"] = {"power": "battery"}
chk(mod._avail_online("0x9999") is True, "★ 电池设备 31 min 无证据 → 仍算在线（24 h）")

print("== 邻居表种设备 + 可用性发布")
SENT.clear()
mod._state["devices"].clear()
mod._state["topo"]["addr"] = "0x0000"
mod._state["topo"]["nbr"] = []
mod._topo_ingest("topo_nbr", {"n": [
    {"a": "0x023b", "ty": 1, "lqi": 132, "rssi": -54},
    {"a": "0x8f75", "ty": 1, "lqi": 86, "rssi": -63},
    {"a": "0x0000", "ty": 0, "lqi": 255, "rssi": 0},
]})
devs = mod._state["devices"]
chk(set(devs) == {"0x023b", "0x8f75"}, "★ 两台设备被种进设备表（协调器自己不算）")
chk(devs["0x023b"]["lqi"] == 132 and devs["0x023b"]["lqi_src"] == "nbr",
    "LQI 用邻居表的实测值（不是心跳里那个）")
chk(devs["0x023b"].get("synthetic") is True, "标记 synthetic（占位，还没真上报）")
mod._avail_pub.clear()
SENT.clear()
mod._avail_sync("0x023b")
topics = [t for t, p, r in SENT if t == "gw/coordinator/dev/0x023b/status"]
chk(topics and SENT[-1][1] == "online", "发布 dev/0x023b/status = online")
chk(SENT[-1][2] is True, "retained（HA 重启后仍知道可用性）")

print("== B：纯 APS 面板也要进设备表（0x747d 之前整个消失）")
mod._state["devices"].pop("0x747d", None)
mod.dispatch('{"t":"aps","src":"0x747d","cl":"0x0006","sep":1,"hex":""}')
d = mod._state["devices"].get("0x747d")
chk(d is not None, "★ 只发裸 APS 帧的面板也被建档")
chk(d and d.get("synthetic") is True, "标 synthetic（还没有真上报）")
chk(mod._avail_last_ok.get("0x747d") is not None, "APS 帧同时算作判活证据")

print("== 广播地址不能被当成设备（0xffff 实测回 0x85 超时）")
mod._state["node_desc"].pop("0xffff", None)
mod._avail_last_bad.pop("0xffff", None)
mod.dispatch('{"t":"node_desc","addr":"0xffff","error":"0x85"}')
chk("0xffff" not in mod._state["node_desc"], "★ 广播地址不进 node_desc 表")
chk("0xffff" not in mod._avail_last_bad, "★ 不计入探测失败（否则假的掉线设备）")
mod.dispatch('{"t":"node_desc","addr":"0x2ad6","role":"router","power":"mains"}')
chk("0x2ad6" in mod._state["node_desc"], "正常设备照旧记录")

print("== Sendeleistung merken（固件不持久化，C6 重启后必须重发）")


class _M:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode()


import tempfile
import os as _os
tf = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
tf.close()
mod._TXP_FILE = tf.name
mod._TXP_DBM = None
mod.on_message(None, None, _M("gw/coordinator/cmd/set_tx_power", "14"))
chk(mod._TXP_DBM == 14, "★ 下发的功率被记住（原来只发不存）")
chk(json.load(open(tf.name, encoding="utf-8"))["dbm"] == 14, "★ 落到 /data 文件（跨进程有效）")
mod._TXP_DBM = None
mod._txp_load()
chk(mod._TXP_DBM == 14, "重新加载后仍是 14 dBm")
mod.on_message(None, None, _M("gw/coordinator/cmd/set_tx_power", "99"))
chk(mod._TXP_DBM == 20, "越界值钳到 20 dBm 后保存")
mod._TXP_DBM = None
chk(mod._txp_reapply() is None, "没记过值 → 不重发（空操作）")
_os.unlink(tf.name)
mod._TXP_FILE = tf.name + ".gone"
mod._TXP_DBM = None
mod._txp_load()
chk(mod._TXP_DBM is None, "文件不存在 → 保持 None，不报错")

print("== C：历史设备真的不在了 → 判 offline，而不是假装「关着」")
mod._addr_nodes.clear()
mod._addr_nodes["0xdead"] = "c6_0xdead"
mod._state["devices"].pop("0xdead", None)
mod._state["topo"]["nbr"] = []
mod._avail_last_ok.clear()
mod._avail_last_bad.clear()
mod._avail_boot = time.time()                      # 刚启动
chk(mod._avail_vanished("0xdead") is False, "启动宽限期内不判死")
mod._avail_boot = time.time() - 600                # 宽限期过了
chk(mod._state["topo"].get("done_at") is None, "（前提：邻居表还没有采集时间）")
mod._state["topo"]["done_at"] = None
chk(mod._avail_vanished("0xdead") is False, "★ 邻居表太旧/没采过 → 不敢判死")
mod._state["topo"]["done_at"] = time.time() - 30   # 新鲜邻居表
chk(mod._avail_vanished("0xdead") is True, "★ 邻居表新鲜 + 不在表里 + 无上报 → 判定消失")
chk(mod._avail_online("0xdead") is False, "→ availability 为 offline")
mod._state["topo"]["nbr"] = [{"a": "0xdead", "ty": 1, "lqi": 40}]
chk(mod._avail_vanished("0xdead") is False, "在协调器邻居表里 → 不算消失")
mod._state["topo"]["nbr"] = []
mod._state["devices"]["0xdead"] = {"clusters": {}}
chk(mod._avail_vanished("0xdead") is False, "设备表里有它 → 不算消失")
mod._addr_nodes.clear()

print("== A：旧 Discovery-Config 补上设备级 availability")
mod._addr_nodes["0x2ad6"] = "c6_0x2ad6"
mod._ieee_nodes.clear(); mod._ieee_addr.clear()
alt = {"topic": "gw/coordinator/status", "payload_available": "online",
       "payload_not_available": "offline"}
fremd = {"topic": "gw/anders/status", "payload_available": "online",
         "payload_not_available": "offline"}
neu2 = {"topic": "gw/coordinator/dev/0x2ad6/status", "payload_available": "online",
        "payload_not_available": "offline"}
mod._disc_remote.clear()
mod._disc_remote["homeassistant/switch/c6_0x2ad6/ep16_on_off/config"] = json.dumps(
    {"name": "alt", "availability": [alt]}, sort_keys=True, ensure_ascii=False)
mod._disc_remote["homeassistant/switch/c6_0x2ad6/ep17_on_off/config"] = json.dumps(
    {"name": "schon neu", "availability": [alt, neu2],
     "availability_mode": "all"}, sort_keys=True, ensure_ascii=False)
mod._disc_remote["homeassistant/switch/fremd_x/y/config"] = json.dumps(
    {"name": "fremd", "availability": [fremd]}, sort_keys=True, ensure_ascii=False)
mod._disc_remote["homeassistant/switch/c6_0x2ad6/ep18_on_off/config"] = json.dumps(
    {"name": "uralt", "availability_topic": "gw/coordinator/status"},
    sort_keys=True, ensure_ascii=False)
SENT.clear()
mod._disc_upgrade_avail()
pub = {t: json.loads(p) for t, p, r in SENT if r}
t16 = "homeassistant/switch/c6_0x2ad6/ep16_on_off/config"
chk(t16 in pub, "★ 旧 Config 被重写")
chk(t16 in pub and len(pub[t16]["availability"]) == 2, "补成两个来源")
chk(t16 in pub and pub[t16]["availability"][0]["topic"] == "gw/coordinator/status",
    "★ 网关来源仍在 [0]（_disc_purge_orphans 靠它判归属）")
chk(t16 in pub and pub[t16]["availability"][1]["topic"] ==
    "gw/coordinator/dev/0x2ad6/status", "★ [1] 是设备自己的主题")
chk(t16 in pub and pub[t16]["availability_mode"] == "all", '模式 "all"')
chk(t16 in pub and pub[t16]["name"] == "alt", "其余字段原样保留")
t18 = "homeassistant/switch/c6_0x2ad6/ep18_on_off/config"
chk(t18 in pub and pub[t18].get("availability_topic") is None,
    "★ 更老的 availability_topic 写法也迁到列表形式")
chk("homeassistant/switch/c6_0x2ad6/ep17_on_off/config" not in pub,
    "已经是两个来源的不重复发布")
chk("homeassistant/switch/fremd_x/y/config" not in pub,
    "★ 别的网关实例的 Config 一律不碰")
chk(mod._ha_sent.get(t16), "_ha_sent 同步（之后 _disc_send 不会重复发）")
mod._addr_nodes.clear()
mod._disc_remote.clear()

print("== 真上报后摘掉占位标记")
mod._state["devices"]["0x023b"]["seen"] = None
mod.dispatch('{"t":"zigbee","addr":571,"sub":"ep16/on_off","p":{"v":1}}')
d = mod._state["devices"].get("0x023b")
chk(d is not None and d.get("synthetic") is None, "收到真帧 → synthetic 被摘掉")
chk(d and d.get("seen"), "记录了这台设备自己的最后上报时刻")

print("== 设备页显示")
html = mod._device_html("0x023b")
chk("最后上报" in html, "有「最后上报」行（不再是整批的批次时间）")
chk("可用性" in html, "有「可用性」行")
chk("在线" in html, "显示「在线」")

print("== _ago 措辞")
chk(mod._ago(time.time() - 5) == "5 秒前", "5 秒前")
chk(mod._ago(time.time() - 130) == "2 分钟前", "2 分钟前")
chk(mod._ago(time.time() - 7200) == "2 小时前", "2 小时前")
chk(mod._ago(None) == "—", "空值 → —")

print("== 拓扑页自动刷新（用户 16:51 报的 bug：必须手动点刷新）")
mod._state["topo"]["ts"] = "26.10. 16:51:26"
mod._state["topo"]["done_at"] = time.time() - 8
mod._state["topo"]["round"] = 7
topo = mod._topo_html()
chk("setInterval" in topo, "★ 拓扑页有 setInterval 定时重载脚本")
chk("location.reload()" in topo, "调用 location.reload()")
chk("本轮采集完成于" in topo, "★ 显示「本轮采集完成于…」时间戳（不然看不出在自动更新）")
chk("8 秒前" in topo, "时间戳带相对时间")
chk("第 7 轮" in topo, "带轮次")
chk('id="rl"' in topo, "有 #rl 状态标签（自动刷新会被暂停时能显示原因）")
chk("自动刷新 15 秒" in topo, "页脚写明 15 秒")


print("== lqia：LQI 的年龄")
mod._state["devices"]["0x023b"]["lqi"] = 132
mod._state["devices"]["0x023b"]["lqi_ts"] = time.time() - 600
h1 = mod._lqi_cell(132, 50, mod._state["devices"]["0x023b"]["lqi_ts"])
chk("10 分钟前测的" in h1, "★ 陈旧 LQI 标出年龄：" + h1[-60:])
h2 = mod._lqi_cell(132, 50, time.time() - 5)
chk("测的" not in h2, "新鲜 LQI（5 秒）不加年龄")
h3 = mod._lqi_cell(0, 50, time.time() - 600)
chk("未测" in h3, "lqi=0 仍然显示「未测」")

print("RESULT:", "FAIL" if fail else "ALL OK")
sys.exit(1 if fail else 0)
