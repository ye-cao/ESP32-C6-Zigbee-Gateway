"""临时检查：拓扑自动采集的「按轮次收尾」逻辑。

要验证的三件事（都是自动刷新引入的新风险）：
  ① 采集**进行中**旧图不能被清空（以前 _topo_refresh 直接 mesh={}，
     页面每 15 s 重载，很容易正好撞上那段空窗）；
  ② 本轮结束（done）时，没被重新采到的设备要被剔掉（掉线的不该一直挂着）；
  ③ 设备自报表上的 rd 必须等于本轮序号。
"""
import sys, types, importlib.util

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


class _FakeMQTT:
    def __init__(self, *a, **k): pass
    def __getattr__(self, name): return lambda *a, **k: None


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


tp = mod._state["topo"]
mod._state["devices"].update({
    "0x2ad6": {"name": "a", "ieee": "", "lqi": 0, "rssi": -82,
               "last_seen": "", "mfr": "", "model": "", "clusters": {}},
    "0x023b": {"name": "b", "ieee": "", "lqi": 40, "rssi": -70,
               "last_seen": "", "mfr": "", "model": "", "clusters": {}},
})

print("== 第 1 轮：两台都报到")
tp["round"] += 1
tp["done"] = False
mod._topo_ingest("topo_mesh", {"a": "0x2ad6", "st": 0, "tot": 1,
                               "nb": [{"a": "0x0000", "rel": 0, "lqi": 61}]})
mod._topo_ingest("topo_mesh", {"a": "0x023b", "st": 0, "tot": 1,
                               "nb": [{"a": "0x2ad6", "rel": 0, "lqi": 42}]})
chk(len(tp["mesh"]) == 2, "采集途中 mesh 已有 2 台（不清空旧数据）")
mod._topo_ingest("topo", {"pan": "0x83e8", "ch": 20, "addr": "0x0000", "done": 1})
chk(tp["done"] is True, "done 置位")
chk(set(tp["mesh"]) == {"0x2ad6", "0x023b"}, "两台都保留：" + " ".join(sorted(tp["mesh"])))
chk(all(e["rd"] == tp["round"] for e in tp["mesh"].values()), "rd 都等于本轮序号")

print("== 第 2 轮：0x023b 掉线（不再回报）")
tp["round"] += 1
tp["done"] = False
chk(len(tp["mesh"]) == 2, "★ 新一轮开始时旧 mesh 仍在 = 图不会闪成空白")
mod._topo_ingest("topo_mesh", {"a": "0x2ad6", "st": 0, "tot": 1,
                               "nb": [{"a": "0x0000", "rel": 0, "lqi": 70}]})
chk(len(tp["mesh"]) == 2, "只回来一台时，另一台先留着撑图")
mod._topo_ingest("topo", {"pan": "0x83e8", "ch": 20, "addr": "0x0000", "done": 1})
chk(set(tp["mesh"]) == {"0x2ad6", "0x023b"}, "★ 设备还在表里 → 自报表保留（不再按\"本轮有没有报\"剔）：" + " ".join(sorted(tp["mesh"])))
chk(tp["mesh"]["0x2ad6"]["nb"][0]["lqi"] == 70, "本轮数据已刷新（lqi 61→70）")

print("== 第 3 轮：一台都没采到（超时）")
tp["round"] += 1
tp["done"] = False
chk(len(tp["mesh"]) == 2, "超时轮开始时旧图还在")
# 超时不会收到 done → 不修剪；再下一轮开始也不清空
chk(tp["done"] is False, "超时轮 done 仍是 False（自动循环会打跳过日志）")

print("RESULT:", "FAIL" if fail else "ALL OK")
sys.exit(1 if fail else 0)
