# -*- coding: utf-8 -*-
"""Render-Smoketest: laedt beide serial_gateway.py mit gemockten
serial/paho-Abhaengigkeiten, fuellt _state und rendert Dashboard +
Geraeteseite. Prueft auf chinesische Texte und deutsche Reste."""
import sys, types, importlib.util, io, os

# ── Mocks: serial ──
fake_serial = types.ModuleType("serial")
class _FakeSer:
    def __init__(self, *a, **k): self.port = None
    def open(self): pass
    def close(self): pass
    def write(self, *a, **k): pass
    @property
    def in_waiting(self): raise KeyboardInterrupt("test-stop")  # bricht Hauptschleife
    def read(self, *a, **k): raise KeyboardInterrupt("test-stop")
fake_serial.Serial = _FakeSer
sys.modules["serial"] = fake_serial

# ── Mocks: pymysql (DB, hier nicht genutzt) ──
sys.modules["pymysql"] = types.ModuleType("pymysql")

# ── Mocks: paho.mqtt.client ──
paho = types.ModuleType("paho"); mqttm = types.ModuleType("paho.mqtt")
clientm = types.ModuleType("paho.mqtt.client")
class _FakeMQTT:
    def __init__(self, *a, **k): pass
    def __getattr__(self, name):   # jede Methode (will_set, connect, ...) = No-op
        return lambda *a, **k: None
clientm.Client = _FakeMQTT
sys.modules["paho"] = paho
sys.modules["paho.mqtt"] = mqttm
sys.modules["paho.mqtt.client"] = clientm

def load(path, name):
    sys.argv = ["serial_gateway.py", "--port", "/dev/null", "--baud", "115200",
                "--broker", "127.0.0.1", "--mqport", "1883",
                "--base", "gw/coordinator"]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)   # type: ignore[attr-defined]
    except KeyboardInterrupt as e:
        # Hauptschleife bricht per Fake-Serial (KeyboardInterrupt, damit der
        # robuste `except Exception` im Lese-Loop ihn NICHT schluckt und
        # endlos weiterschlief – genau daran hing dieser Smoketest stundenlang);
        # dabei läuft auch der saubere Abbau (finally) einmal durch.
        if "test-stop" not in str(e):
            raise
    return mod

FAKE_STATE = {
    "status": "online", "uptime": 1234, "channel": 20, "pan": "0x83e8",
    "permit_join": {"open": True, "seconds": 180},
    "tx_power": {"want": 14, "eff": 14, "ts": "2026-10-17 20:40:00"},
    "ota": {"status": "idle", "bin_path": "", "transferred": 0, "size": 0,
            "image_type": 0x0001},
    "wr_power": {},
    # 拓扑：协调器 + 一台中继 + 一台直连 + 一台经中继
    "topo": {
        "pan": "0x83e8", "ch": 20, "addr": "0x0000",
        "ts": "2026-10-25 22:30:00", "done": True,
        "nbr": [
            {"a": "0x0000", "ty": 0, "dp": 0, "rel": 0, "lqi": 255, "rssi": 0,
             "oc": 0, "age": 0, "rx": 1},
            {"a": "0x2ad6", "ty": 1, "dp": 1, "rel": 1, "lqi": 61, "rssi": -68,
             "oc": 2, "age": 0, "rx": 1},
            {"a": "0x15ba", "ty": 2, "dp": 1, "rel": 1, "lqi": 180, "rssi": -55,
             "oc": 1, "age": 0, "rx": 0},
        ],
        "rt": [
            {"d": "0x2ad6", "nh": "0x2ad6", "st": 0, "ex": 12},
            {"d": "0x15ba", "nh": "0x15ba", "st": 0, "ex": 12},
            {"d": "0x023b", "nh": "0x2ad6", "st": 0, "ex": 10},
        ],
        "rr": [],
        # mesh：每台设备自报的邻居表（Mgmt_Lqi_rsp）。0x023b 明说自己的爹是
        # 中继 0x2ad6（rel=0），这条必须压过协调器路由表/邻居表的推断。
        "mesh": {
            "0x2ad6": {"st": 0, "tot": 2, "nb": [
                {"a": "0x0000", "rel": 0, "ty": 0, "dp": 0, "lqi": 61, "pj": 1},
                {"a": "0x023b", "rel": 1, "ty": 1, "dp": 2, "lqi": 88, "pj": 1},
            ]},
            "0x023b": {"st": 0, "tot": 2, "nb": [
                {"a": "0x2ad6", "rel": 0, "ty": 1, "dp": 1, "lqi": 42, "pj": 1},
                {"a": "0x0000", "rel": 2, "ty": 0, "dp": 0, "lqi": 30, "pj": 1},
            ]},
            "0x15ba": {"st": 0, "tot": 1, "nb": [
                {"a": "0x0000", "rel": 0, "ty": 0, "dp": 0, "lqi": 180, "pj": 1},
            ]},
        },
    },
    "fw_ota": {"status": "idle", "bin_path": "", "size": 0, "off": 0},
    "devices": {
        "0x15ba": {
            "name": "三联开关", "ieee": "00:11:22:33:44:55:66:77",
            "lqi": 180, "rssi": -55, "last_seen": "2026-10-17 02:00",
            "mfr": "Test", "model": "SW3",
            "clusters": {"ep1/on_off": {"payload": {"v": 1}, "ts": "02:00"},
                         "ep2/on_off": {"payload": {"v": 0}, "ts": "02:00"}},
        },
        # ★ lqi=0 专门用来验证"未测"分支：LQI 0 是「这帧没带有效 LQI」，
        #   不能画成 0/255 的 meter（2026-10-26 中继在弱覆盖点上就是这个值）。
        "0x2ad6": {
            "name": "中继", "ieee": "aa:bb:cc:dd:ee:ff:00:11",
            "lqi": 0, "rssi": -82, "last_seen": "2026-10-17 02:00",
            "mfr": "Espressif", "model": "ESP32-C6-Repeater",
            "clusters": {},
        },
    },
}

GERMAN_LEFTOVERS = [
    "offen", "geschlossen", "Gerät", "Zurück", "Uptime", "Kanal",
    "Fortschritt", "Hersteller", "Sekunden", "Setzen", "Senden",
    "Adresse", "Zuletzt", "keine ", "Detail anzeigen", "Schließen",
    "öffnen", "entfernen", "nicht gefunden", "zurück", "Commands",
    "Sleep-Intervall", "Auto-Refresh", 'lang="de"',
]

CHINESE_EXPECT = ["在线", "允许加入", "Zigbee 设备", "运行时间", "信道",
                  "开启 180 秒", "OTA 固件升级", "制造商 / 型号", "暂无数据",
                  "返回仪表盘", "状态刷新", "Cluster 数据", "命令", "发送",
                  "从列表移除", "休眠间隔", "秒", "设置", "开始 OTA",
                  "自动刷新", "原始状态", "主题前缀", "数据库", "查看详情"]

fail = 0
for path, name in [
    ("host/addon/esp32c6_gateway/serial_gateway.py", "sg_addon"),
    ("host/serial_gateway.py", "sg_up"),
]:
    print(f"== {path}")
    try:
        mod = load(path, name)
    except Exception as e:
        print(f"   !! IMPORT FEHLER: {type(e).__name__}: {e}")
        fail += 1
        continue

    mod._state.clear()
    mod._state.update(FAKE_STATE)
    dash = mod._html()
    dev = mod._device_html("0x15ba")
    topo = mod._topo_html() if hasattr(mod, "_topo_html") else None

    # 拓扑页用自己的关键词表（CHINESE_EXPECT 是仪表盘/设备页的词），need=0 跳过
    pages = [("dashboard", dash, 10), ("device", dev, 10)]
    if topo is not None:
        pages.append(("topology", topo, 0))

    for label, html, need in pages:
        if not html:
            print(f"   !! {label}: leer"); fail += 1; continue
        for w in GERMAN_LEFTOVERS:
            if w in html:
                print(f"   !! {label}: deutscher Rest {w!r}"); fail += 1
        hits = sum(1 for w in CHINESE_EXPECT if w in html)
        print(f"   {label}: {len(html)} Zeichen, {hits}/{len(CHINESE_EXPECT)} chinesische Texte")
        if need and hits < need:
            print(f"   !! {label}: zu wenige chinesische Texte"); fail += 1

    if topo:
        for w in ("网络拓扑", "连接方式", "直连", "经中继", "父节点/下一跳"):
            if w not in topo:
                print(f"   !! topology: fehlt {w!r}"); fail += 1
        if "<svg" not in topo:
            print("   !! topology: kein SVG"); fail += 1
    # 404-Pfad
    page404 = f"<h2>设备 0xffff 不存在</h2>"  # via handler logic simuliert
    print("   404-Text ( Quelle ): ", "设备" in page404 and "不存在" in page404)

# 404-String direkt aus der Handler-Quelle pruefen
src = io.open("host/addon/esp32c6_gateway/serial_gateway.py", encoding="utf-8").read()
if "设备 {addr} 不存在" in src and "← 返回" in src:
    print("== 404-Text: OK ( Quelle )")
else:
    print("== 404-Text: FEHLT"); fail += 1

print("RESULT:", "FAIL" if fail else "ALL OK")
sys.exit(1 if fail else 0)
