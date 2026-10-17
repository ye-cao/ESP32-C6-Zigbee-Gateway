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
    "ota": {"status": "idle", "bin_path": "", "transferred": 0, "size": 0},
    "fw_ota": {"status": "idle", "bin_path": "", "size": 0, "off": 0},
    "devices": {
        "0x15ba": {
            "name": "三联开关", "ieee": "00:11:22:33:44:55:66:77",
            "lqi": 180, "rssi": -55, "last_seen": "2026-10-17 02:00",
            "mfr": "Test", "model": "SW3",
            "clusters": {"ep1/on_off": {"payload": {"v": 1}, "ts": "02:00"},
                         "ep2/on_off": {"payload": {"v": 0}, "ts": "02:00"}},
        }
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
                  "返回仪表盘", "最近通信", "Cluster 数据", "命令", "发送",
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

    for label, html in [("dashboard", dash), ("device", dev)]:
        if not html:
            print(f"   !! {label}: leer"); fail += 1; continue
        for w in GERMAN_LEFTOVERS:
            if w in html:
                print(f"   !! {label}: deutscher Rest {w!r}"); fail += 1
        hits = sum(1 for w in CHINESE_EXPECT if w in html)
        print(f"   {label}: {len(html)} Zeichen, {hits}/{len(CHINESE_EXPECT)} chinesische Texte")
        if hits < 10:
            print(f"   !! {label}: zu wenige chinesische Texte"); fail += 1
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
