#!/usr/bin/env python3
"""
serial_gateway.py – liest JSON-Lines vom ESP32-C6 (COM-Port)
und published sie als MQTT an den Broker + schreibt in MariaDB.
Web-Dashboard auf Port 8080.

Verwendung:
    python3 serial_gateway.py
    python3 serial_gateway.py --port /dev/ttyACM1 --broker 192.168.178.218
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import serial
import paho.mqtt.client as mqtt
import pymysql

# ── Log-Zeitstempel: lokale Zeitzone, nicht Container-UTC ────────────────
# Der HA-Add-on-Container läuft ohne TZ in UTC – dann steht im Log 02:33,
# während an der Uhr 10:33 steht. Quelle ist deshalb (in dieser Reihenfolge):
#   1. LOG_TZ  (vom Add-on aus der Option "timezone" gesetzt)
#   2. TZ      (nur wenn nicht UTC/GMT)
#   3. /etc/timezone bzw. Symlink /etc/localtime
#   4. Systemzeit (unverändert)
def _resolve_local_tz():
    """Gibt (tzinfo|None, Anzeigename) zurück. None = Systemzeit verwenden."""
    name = (os.getenv("LOG_TZ") or "").strip()
    if not name:
        env_tz = (os.getenv("TZ") or "").strip()
        if env_tz and env_tz.upper() not in ("UTC", "ETC/UTC", "GMT", "UCT"):
            name = env_tz
    if not name:
        try:
            with open("/etc/timezone", encoding="utf-8") as f:
                cand = f.read().strip()
            if cand and cand.upper() not in ("UTC", "ETC/UTC"):
                name = cand
        except OSError:
            pass
    if not name:
        try:
            link = os.readlink("/etc/localtime")
            for marker in ("zoneinfo/", "posix/"):
                if marker in link:
                    name = link.split(marker, 1)[1]
                    break
        except OSError:
            pass
    if not name:
        return None, "Systemzeit"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name), name
    except Exception:
        pass
    # Kein tzdata im Image: TZ + tzset() als Notnagel
    try:
        if hasattr(time, "tzset"):
            os.environ["TZ"] = name
            time.tzset()
            return None, name + " (via TZ)"
    except Exception:
        pass
    return None, "Systemzeit"


_LOG_TZ, _LOG_TZ_NAME = _resolve_local_tz()


def _ts() -> str:
    """Log-Zeitstempel [HH:MM:SS] in Ortszeit."""
    if _LOG_TZ is not None:
        return datetime.now(_LOG_TZ).strftime("[%H:%M:%S]")
    return time.strftime("[%H:%M:%S]")


_print_raw = print
def print(*args, **kwargs):   # noqa: A001 – absichtlich global überschrieben
    kwargs.setdefault("flush", True)
    _print_raw(_ts(), *args, **kwargs)

_dt0 = datetime.now(_LOG_TZ) if _LOG_TZ else datetime.now().astimezone()
print(f"[Log] Zeitzone {_LOG_TZ_NAME} · UTC{_dt0.strftime('%z')} · "
      f"jetzt {_dt0.strftime('%Y-%m-%d %H:%M:%S')}")

# ── Konfiguration ─────────────────────────────────────────────────────────
DEFAULT_PORT   = "/dev/ttyACM0"
DEFAULT_BAUD   = 115200
DEFAULT_BROKER = "192.168.178.218"
DEFAULT_MQPORT = 1883
WEB_PORT       = int(os.getenv("WEB_PORT", "8080"))

DB_HOST = os.getenv("DB_HOST", "")   # empty = no database (e.g. HAOS add-on; HA records history itself)
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "gh")
DB_PASS = os.getenv("DB_PASS", "a12345")
DB_NAME = os.getenv("DB_NAME", "wagodb")

# ── Argumente ──────────────────────────────────────────────────────────────
ap = argparse.ArgumentParser()
ap.add_argument("--port",   default=DEFAULT_PORT)
ap.add_argument("--baud",   default=DEFAULT_BAUD,   type=int)
ap.add_argument("--broker", default=DEFAULT_BROKER)
ap.add_argument("--mqport", default=DEFAULT_MQPORT, type=int)
ap.add_argument("--user",   default=None)
ap.add_argument("--pass",   default=None, dest="passwd")
ap.add_argument("--base",   default="gw/c6serial",
                help="MQTT base topic (z.B. gw/coordinator)")
ap.add_argument("--topo-interval", default=60, type=int, dest="topo_interval",
                help="自动采集网络拓扑的间隔秒数，0 = 关闭（拓扑页从此自动更新）")
args = ap.parse_args()

BASE   = args.base
DB_MAC = BASE.split("/")[-1][:8]

# Muss zu config.json passen. Steht im Startbanner und im Web-UI – damit ist
# sofort erkennbar, ob der Container wirklich den neuen Code fährt.
GW_VERSION = "1.5.3"

# ── Home Assistant MQTT Auto-Discovery ────────────────────────────────────
# Jedes Zigbee-Gerät/Endpoint wird beim ersten Daten-Telegramm automatisch als
# HA-Entity angemeldet – es muss NICHTS in configuration.yaml eingetragen
# werden (vorausgesetzt die HA-MQTT-Integration ist aktiv).
HA_PREFIX  = os.getenv("HA_PREFIX", "homeassistant")
HA_ENABLED = os.getenv("HA_DISCOVERY", "1").lower() not in ("0", "no", "off", "false")
# 每台设备独立的 availability（见 _ha_avail / _avail_sync）。
# 关掉就退回"只看网关在线"，出问题时可以一条环境变量回滚。
DEVICE_AVAIL = os.getenv("DEVICE_AVAIL", "1").lower() not in ("0", "no", "off", "false")

# Modell-Datenbank aus den Zigbee2MQTT-Convertern (von gen_device_db.py erzeugt).
# Schlüssel = ZCL Basic 0x0005 (ModelIdentifier). Damit werden Endpunkte und
# Fähigkeiten eines frisch beigetretenen Geräts sofort richtig erkannt.
_ha_db = {}
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "device_db.json"), encoding="utf-8") as _f:
        _ha_db = json.load(_f)
    print(f"[HA] Gerätedatenbank: {len(_ha_db)} Modelle geladen")
except Exception as _e:
    print(f"[HA] WARNUNG: device_db.json nicht ladbar ({_e}) – nur generische Discovery")

# ── In-Memory State ────────────────────────────────────────────────────────
_state_lock = threading.Lock()
_state = {
    "status":       "offline",
    "uptime":       0,
    "channel":      0,
    "pan":          "–",
    "permit_join":  {"open": False, "seconds": 0},
    # Sendepfad des Funks: "want" ist der Wunschwert, "eff" der von der PA
    # zurückgelesene Istwert. Nur "eff" ist eine Messung (siehe §21).
    "tx_power":     {"want": None, "eff": None, "ts": ""},
    "devices":      {},   # addr → {ieee, name, last_seen, clusters: {name: value}}
    # 中继自报的父节点：addr → 父节点地址。来自中继固件把
    # zb_nwk_get_parent() 写进协调器 0xFC00/0x0002（设备自己说的，最权威）。
    "parent":       {},
    # 中继功率下发记录：addr → {dbm, ts, tsn}
    "wr_power":     {},
    # 网络拓扑（直连 / 经中继）。coordinator 的三张表直接镜像过来：
    #   nbr = 邻居表（我直接听得到谁）  rt = 路由表（到某地址的下一跳）
    #   rr  = 源路由（完整中继链路）    done = 本轮是否到齐
    "topo": {
        "pan": "", "ch": 0, "addr": "0x0000",
        "nbr": [], "rt": [], "rr": [],
        # mesh: addr → {"st":…, "tot":…, "nb":[…]}，每台设备**自己上报**的
        # 邻居表（Mgmt_Lqi_rsp）。这是判父节点的权威数据。
        "mesh": {},
        # round: 本轮采集的序号。每轮 +1，设备自报表里记下自己是第几轮采到的，
        # 收尾时把「不是本轮」的旧条目剔掉 —— 这样采集过程中图不会被清空
        # （旧图一直用到新图到齐），又不会留下已经掉线的设备。
        "round": 0,
        "ts": "", "done": False,
    },
    "ota": {
        "status":      "idle",   # idle | pending | transferring | done | error
        "bin_path":    "",
        "size":        0,
        "version":     1,
        "transferred": 0,
        # image type: welche Geräteklasse bekommt dieses Image
        #   0x0001 = Endgerät (client/, switch2wifi)
        #   0x0002 = Repeater (router/)
        "image_type":  0x0001,
        # 网关侧 OTA server 的内部计数（窗口命中/未命中/超时）
        "dbg":         {},
        # 交付对账（见 _ota_handle_req 注释）
        "sent":        0,
        "sent_hash":   "0x%08x" % 2166136261,
    },
    "fw_ota": {
        "status":   "idle",   # idle | pending | transferring | done | error
        "bin_path": "",
        "size":     0,
        "off":      0,
    },
}

# ── OTA state machine ───────────────────────────────────────────────────────
_ota_file = None   # open file handle during transfer
# ★ 交付对账：对「按顺序真正发出去的字节」算 FNV-1a，网关侧对「真正交给
#   协议栈的字节」算同一个哈希。两边一致 ⇒ 中继收到的数据逐字节正确。
#   只看 offset == _ota_next 的块，重复请求不会污染哈希。
_OTA_FNV0   = 2166136261
_ota_hash   = _OTA_FNV0
_ota_sent   = 0
_ota_next   = 0

# ── Coordinator-Firmware-OTA über Serial ────────────────────────────────────
# Bin liegt auf dem Pi; Blöcke gehen per UART an den C6, der sie via esp_ota
# in die nächste OTA-Partition schreibt. Flow-Control: FW_BATCH Blöcke pro
# Stoß, C6 quittiert alle FW_ACK_EVERY (=16) Blöcke mit st="write".
_fw_ota_file = None
_fw_ota_size = 0
_fw_ota_off  = 0

# ★ 2026-10-25: Zeitstempel des letzten {"t":"boot"}.
# Der C6 startet den uart_rx-Task VOR esp_zb_start(); Befehle, die in diesem
# Fenster ankommen, haben ihn in eine Reboot-Schleife geschickt
# (Load access fault in zb_schedule_alarm). Firmware ist gefixt – dieses
# Cooldown schützt zusätzlich alte Images und verhindert, dass der Transfer
# durch einen Reboot mitten im OTA endgültig hängen bleibt.
_gw_boot_ts = 0.0
_GW_BOOT_COOLDOWN = 8.0
_fw_ota_restarts = 0


def _env_int(name, default, lo, hi):
    try:
        v = int(os.getenv(name, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


# Frame-Größe: eine Zeile ist 43 + len(str(off)) + 2*chunk Zeichen lang.
# Der C6 liest Kommandos mit fgets(buf, BUF_SIZE). Ältere Builds haben
# BUF_SIZE=256 – eine ~950-Zeichen-Zeile wird dann in Fragmente zerhackt und
# STILLSCHWEIGEND verworfen (genau das Symptom: begin kommt, dann nie ein
# "write"-ACK). Mit chunk=96 bleibt die Zeile bei ~241 Zeichen und passt
# auch in den alten Puffer – dafür dauert der Transfer ein paar Minuten.
FW_CHUNK = _env_int("FW_CHUNK", 80, 32, 512)
FW_BATCH = _env_int("FW_BATCH", 16, 1, 64)

# Startbanner: ohne diese Zeile läuft noch ein alter Build im Container.
print(f"[GATEWAY] v{GW_VERSION} · OTA-Frame {FW_CHUNK} B × {FW_BATCH} = "
      f"{FW_CHUNK * FW_BATCH} B/Stoß · kompaktes JSON")

def _js(obj) -> str:
    """Kompaktes JSON + Zeilenende (für Log-Ausgabe und .encode())."""
    return json.dumps(obj, separators=(",", ":")) + "\n"


def _j(obj) -> bytes:
    """Kompaktes JSON für die UART – ohne Leerzeichen nach ':' und ','.

    Wichtig: alte Firmware-Builds parsen mit `strstr(buf, "\\"data\\":\\"")`.
    `json.dumps()` schreibt standardmäßig `"data": "…"` (Leerzeichen nach dem
    Doppelpunkt) – das wird von so einem starren strstr nie gefunden, der Frame
    verschwindet still. Kompakt geschrieben (`"data":"…"`) erfüllen wir beide
    Parser-Varianten, die alte strenge und die neue tolerante (json_val).
    """
    return _js(obj).encode()


def _fw_ota_close():
    global _fw_ota_file, _fw_ota_off
    if _fw_ota_file:
        _fw_ota_file.close()
        _fw_ota_file = None
    _fw_ota_off = 0

def _save_upload(ctype: str, body: bytes, dst: str = None):
    """Speichert die erste Datei aus einem multipart/form-data-Body.

    Nötig, weil /data im Add-on-Container liegt und auf dem HA-Host nicht
    sichtbar ist – Hochladen im Browser ist der einzige Weg ohne Samba.
    Gibt den geschriebenen Pfad zurück (oder None).

    dst: optionaler Zielpfad. Wichtig für OTA – jede Geräteklasse braucht ihr
    eigenes Image, sonst überschreibt der Repeater-Upload das Endgerät-Image
    (und umgekehrt), bevor irgendwer es geflasht hat.
    """
    import re as _re
    m = _re.search(r"boundary=([^;]+)", ctype)
    if not m:
        return None
    boundary = b"--" + m.group(1).strip().strip('"').encode()
    for part in body.split(boundary):
        if b"filename=" not in part:
            continue
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep or not data:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]                       # abschließendes CRLF der Part
        if not data:
            continue
        if not dst:
            dst = "/data/fw_upload.bin" if os.path.isdir("/data") else "/tmp/fw_upload.bin"
        with open(dst, "wb") as f:
            f.write(data)
        print(f"[OTA] Upload: {len(data)} Bytes → {dst}")
        return dst
    return None

def _mp_field(ctype: str, body: bytes, name: str):
    """Liest ein gewöhnliches Textfeld aus einem multipart/form-data-Body.

    Nötig, weil do_POST bei multipart `params = {}` setzt (parse_qs würde die
    Binärdaten verstümmeln) – damit sind aber auch die <select>-Felder im
    selben Formular unsichtbar. Für /api/ota brauchen wir genau die (image type).
    """
    import re as _re
    m = _re.search(r"boundary=([^;]+)", ctype)
    if not m:
        return None
    boundary = b"--" + m.group(1).strip().strip('"').encode()
    needle = ('name="%s"' % name).encode()
    for part in body.split(boundary):
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep or needle not in head or b"filename=" in head:
            continue
        return data.rstrip(b"\r\n").decode("utf-8", "replace")
    return None


def _ota_image_type(raw_val, default=0x0001) -> int:
    """Parst '0x0001' / '1' / None robust zu int."""
    if raw_val is None:
        return default
    s = str(raw_val).strip()
    if not s:
        return default
    try:
        return int(s, 0)
    except ValueError:
        try:
            return int(s, 16)
        except ValueError:
            return default


def _fw_ota_watchdog():
    """Meldet, wenn der C6 die Datenframes nicht mehr quittiert.

    Ohne das bleibt der Transfer stumm stehen und man sieht nur "7168/789804"
    – ohne jede Erklärung, warum es nicht weitergeht.
    """
    last, still = -1, 0
    while True:
        time.sleep(5)
        if _fw_ota_file is None:
            return
        if _fw_ota_off == last:
            still += 5
            if still == 20:
                print(f"[OTA] WARNUNG: seit 20 s keine Quittung vom C6 "
                      f"(steht bei {_fw_ota_off}/{_fw_ota_size}).")
                if FW_CHUNK <= 128:
                    print("[OTA] Die Frames sind mit "
                          f"{FW_CHUNK} B (~{2*FW_CHUNK+50} Zeichen) bereits klein "
                          "genug für einen 256-Byte-Puffer → der laufende Build "
                          "kann fw_ota_data nicht parsen. Dann hilft nur noch "
                          "esptool über USB.")
                else:
                    print("[OTA] Vermutlich BUF_SIZE=256 im laufenden Build: "
                          f"ein Frame ist {FW_CHUNK} B ≈ {2*FW_CHUNK+50} Zeichen. "
                          "Add-on-Option 'fw_chunk' auf 80 setzen und neu starten.")
        else:
            last, still = _fw_ota_off, 0


def _fw_ota_start(bin_path: str):
    """Coordinator-Firmware über USB-Serial flashen (esp_ota)."""
    global _fw_ota_file, _fw_ota_size, _fw_ota_off
    import os as _os
    if not bin_path:
        bin_path = "/data/zigbee-vital-sensor.bin"
    if not _os.path.isfile(bin_path):
        print(f"[OTA] Coordinator-Datei nicht gefunden: {bin_path}")
        with _state_lock:
            _state["fw_ota"]["status"] = "error"
        return
    _fw_ota_close()
    _fw_ota_size = _os.path.getsize(bin_path)
    _fw_ota_off  = 0
    _fw_ota_file = open(bin_path, "rb")
    with _state_lock:
        _state["fw_ota"].update({
            "status": "pending", "bin_path": bin_path,
            "size": _fw_ota_size, "off": 0,
        })
    ser.write(_j({"cmd": "fw_ota_start", "size": _fw_ota_size}))
    frames  = -(-_fw_ota_size // FW_CHUNK)          # aufrunden
    batches = -(-frames // FW_BATCH)
    est_s   = batches * 0.55 + frames * 0.004
    print(f"[OTA] Coordinator-Update: {bin_path} size={_fw_ota_size}")
    print(f"[OTA] Plan: {frames} Frames à {FW_CHUNK} B, {FW_BATCH} pro Stoß → "
          f"{batches} Stöße, geschätzt {est_s/60:.1f} min")
    threading.Thread(target=_fw_ota_watchdog, daemon=True).start()

def _fw_ota_send_batch():
    """FW_BATCH Blöcke ab aktuellem Offset senden; letzter Stoß löst Ende aus."""
    global _fw_ota_off
    if _fw_ota_file is None:
        return
    sent = 0
    while sent < FW_BATCH and _fw_ota_off < _fw_ota_size:
        _fw_ota_file.seek(_fw_ota_off)
        data = _fw_ota_file.read(FW_CHUNK)
        if not data:
            break
        ser.write(_j({"cmd": "fw_ota_data", "off": _fw_ota_off,
                      "data": data.hex()}))
        _fw_ota_off += len(data)
        sent += 1
    with _state_lock:
        _state["fw_ota"]["off"] = _fw_ota_off
        _state["fw_ota"]["status"] = "transferring"
    pct = (_fw_ota_off * 100 // _fw_ota_size) if _fw_ota_size else 0
    print(f"[OTA] coordinator {_fw_ota_off}/{_fw_ota_size} Bytes gesendet ({pct} %)")
    if _fw_ota_off >= _fw_ota_size:
        ser.write(b'{"cmd":"fw_ota_end"}\n')
        print("[OTA] coordinator: alle Bytes übertragen – Ende …")

def _now():
    """Datum/Uhrzeit in Ortszeit (identisch zum Log-Zeitstempel)."""
    if _LOG_TZ is not None:
        return datetime.now(_LOG_TZ).strftime("%d.%m. %H:%M:%S")
    return datetime.now().strftime("%d.%m. %H:%M:%S")

# ── MariaDB ────────────────────────────────────────────────────────────────
_db_conn = None
_db_lock = threading.Lock()

def _db_connect():
    if not DB_HOST:
        raise RuntimeError("DB_HOST not set - database disabled")
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASS,
        database=DB_NAME,
        autocommit=True,
        connect_timeout=5,
    )

def _db_cursor():
    global _db_conn
    try:
        _db_conn.ping(reconnect=True)
    except Exception:
        _db_conn = _db_connect()
    return _db_conn.cursor()

def db_init():
    global _db_conn
    if not DB_HOST:
        print("[DB] disabled (DB_HOST empty)")
        return
    try:
        _db_conn = _db_connect()
        cur = _db_conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS esp32_gateways (
                mac        CHAR(8)      NOT NULL,
                status     VARCHAR(16)  NOT NULL DEFAULT 'offline',
                last_seen  DATETIME,
                PRIMARY KEY (mac)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS esp32_zigbee_devices (
                mac        CHAR(8)      NOT NULL,
                addr       VARCHAR(8)   NOT NULL,
                ieee       VARCHAR(24)  NOT NULL DEFAULT '',
                name       VARCHAR(64)  NOT NULL DEFAULT '',
                last_seen  DATETIME,
                PRIMARY KEY (mac, addr)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS esp32_chan_scan (
                id      INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                mac     CHAR(8)      NOT NULL,
                ts      DATETIME(3)  NOT NULL,
                ch      TINYINT UNSIGNED NOT NULL,
                count   TINYINT UNSIGNED NOT NULL DEFAULT 0,
                nets    TEXT,
                INDEX (mac, ts)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS esp32_zigbee_data (
                id         INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                mac        CHAR(8)      NOT NULL,
                addr       VARCHAR(8)   NOT NULL,
                cluster    VARCHAR(32)  NOT NULL,
                ts         DATETIME(3)  NOT NULL,
                value      DOUBLE,
                raw_json   TEXT,
                INDEX (mac, addr, cluster, ts)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        # Bekannte Devices aus DB in State laden
        cur.execute("""
            SELECT addr, ieee, name, last_seen FROM esp32_zigbee_devices WHERE mac=%s
        """, (DB_MAC,))
        with _state_lock:
            for row in cur.fetchall():
                addr, ieee, name, last_seen = row
                ts = last_seen.strftime("%d.%m. %H:%M:%S") if last_seen else "–"
                _state["devices"].setdefault(addr, {
                    "ieee": ieee or "", "name": name or addr,
                    "last_seen": ts, "clusters": {}
                })
        cur.close()
        print(f"[DB] verbunden {DB_HOST}/{DB_NAME} mac={DB_MAC} devices={len(_state['devices'])}")
    except Exception as e:
        print(f"[DB] Fehler bei Init: {e}")
        _db_conn = None

def db_insert_chan_scan(ch, count, nets_json):
    if not DB_HOST:
        return
    with _db_lock:
        try:
            cur = _db_cursor()
            cur.execute("""
                INSERT INTO esp32_chan_scan (mac, ts, ch, count, nets)
                VALUES (%s, UTC_TIMESTAMP(3), %s, %s, %s)
            """, (DB_MAC, ch, count, nets_json))
            cur.close()
        except Exception as e:
            print(f"[DB] chan_scan insert: {e}")

def db_upsert_gateway(status):
    if not DB_HOST:
        return
    with _db_lock:
        try:
            cur = _db_cursor()
            cur.execute("""
                INSERT INTO esp32_gateways (mac, status, last_seen)
                VALUES (%s, %s, UTC_TIMESTAMP())
                ON DUPLICATE KEY UPDATE status=VALUES(status), last_seen=UTC_TIMESTAMP()
            """, (DB_MAC, status[:16]))
            cur.close()
        except Exception as e:
            print(f"[DB] gateway upsert: {e}")

def db_upsert_zigbee_device(addr, ieee=""):
    if not DB_HOST:
        return
    with _db_lock:
        try:
            cur = _db_cursor()
            cur.execute("""
                INSERT INTO esp32_zigbee_devices (mac, addr, ieee, last_seen)
                VALUES (%s, %s, %s, UTC_TIMESTAMP())
                ON DUPLICATE KEY UPDATE ieee=VALUES(ieee), last_seen=UTC_TIMESTAMP()
            """, (DB_MAC, addr, ieee))
            cur.close()
        except Exception as e:
            print(f"[DB] zigbee device upsert: {e}")

def _extract_value(cluster, raw):
    # Multi-Gang-Geräte: Sub-Topic kann "ep2/on_off" etc. sein → Basisnamen vergleichen
    base = cluster.rsplit("/", 1)[-1] if "/" in cluster else cluster
    if base == "temperature":
        r = raw.get("raw")
        return None if (r is None or r == -32768) else r / 100.0
    if base == "humidity":     return raw.get("raw", 0) / 100.0
    if base == "illuminance":  return raw.get("raw")
    if base == "on_off":       return raw.get("v")
    if base == "occupancy":    return raw.get("occ")
    return None

def db_insert_zigbee_data(addr, cluster, raw):
    if not DB_HOST:
        return
    value = _extract_value(cluster, raw)
    with _db_lock:
        try:
            cur = _db_cursor()
            cur.execute("""
                INSERT INTO esp32_zigbee_data (mac, addr, cluster, ts, value, raw_json)
                VALUES (%s, %s, %s, UTC_TIMESTAMP(3), %s, %s)
            """, (DB_MAC, addr, cluster, value, json.dumps(raw)))
            cur.execute("""
                UPDATE esp32_zigbee_devices SET last_seen=UTC_TIMESTAMP()
                WHERE mac=%s AND addr=%s
            """, (DB_MAC, addr))
            cur.close()
        except Exception as e:
            print(f"[DB] zigbee data insert: {e}")

# ── MQTT ───────────────────────────────────────────────────────────────────
mq = mqtt.Client(client_id="serial-gateway")
if args.user:
    mq.username_pw_set(args.user, args.passwd)
mq.will_set(f"{BASE}/status", "offline", retain=True, qos=1)

# ── MQTT-Ausgang: einheitlicher Publish-Pfad ──────────────────────────────
# Nachbau von vier Mechanismen aus Zigbee2MQTT (lib/mqtt.ts), die hier fehlten:
#
#  1. Topic-Prüfung. Ein Publish-Topic mit '+' oder '#' ist kein Topic, sondern
#     ein Filter – der Broker antwortet mit einem Protokollfehler und trennt
#     die Verbindung. Genau das ist beim Debuggen passiert (mqtt_probe.py mit
#     'c6_#': sofortiger Disconnect). Ein Gerätename mit '#' würde die
#     Gateway-Verbindung also hart abschießen → vorher abfangen.
#  2. Registry der retained Nachrichten. Läuft der Broker ohne Persistenz,
#     sind nach seinem Neustart alle Discovery-Configs weg und HA kennt die
#     Entities nicht mehr. Z2M setzt seine retained Nachrichten deshalb 2 s
#     nach jedem Verbindungsaufbau erneut (lib/mqtt.ts, onConnect).
#  3. Echo-Schutz. Wir abonnieren {BASE}/cmd/+, {BASE}/set/# und – während des
#     Startfensters – {HA_PREFIX}/+/+/+/config. Auf {BASE}/cmd/* publizieren
#     wir selbst (Web-Formulare), deshalb ist dieser Zweig ausdrücklich von der
#     Echo-Unterdrückung ausgenommen (siehe on_message).
#  4. Ein Ausgangspunkt. Jeder Publish läuft über mq_put(); damit lassen sich
#     Prüfungen und Buchführung an genau einer Stelle halten.
_retained = {}       # topic → payload  (was wir mit retain=true gesetzt haben)
_self_pub = {}       # topic → Zeitpunkt, bis zu dem ein Echo ignoriert wird

# HA publiziert hier 'online', sobald es fertig gestartet ist. Z2M hängt
# daran das erneute Senden aller Zustände (lib/extension/homeassistant.ts).
_HA_STATUS_TOPIC = "homeassistant/status"


def mq_put(topic, payload, retain=False, qos=1):
    """Einziger Ausgangspunkt für alle Publishes."""
    if "+" in topic or "#" in topic:
        print(f"[MQTT] Topic '{topic}' enthält ein Wildcard-Zeichen – nicht gesendet")
        return
    if retain:
        if payload:
            _retained[topic] = payload
        else:
            _retained.pop(topic, None)
    if len(_self_pub) > 400:
        _self_pub.clear()          # reiner Echo-Schutz, kein Datenverlust
    _self_pub[topic] = time.time() + 1.0
    try:
        mq.publish(topic, payload, retain=retain, qos=qos)
    except Exception as e:
        print(f"[MQTT] publish '{topic}' fehlgeschlagen: {e}")


def _republish_retained():
    """Nach einem Verbindungsaufbau alle retained Nachrichten erneut setzen.

    Beim ersten Verbindungsaufbau ist die Registry leer – dann passiert nichts.
    Nötig wird das erst nach einem Broker-Neustart ohne Persistenz.
    """
    n = 0
    for topic, payload in list(_retained.items()):
        mq_put(topic, payload, retain=True, qos=1)
        n += 1
    if n:
        print(f"[MQTT] {n} retained Nachricht(en) nach Verbindungsaufbau erneut gesetzt")


def _ha_republish_all(why=""):
    """Alle bekannten Zustände erneut senden (nach einem HA-Neustart).

    Ohne das stehen die Entities nach einem HA-Neustart auf 'unknown', wenn
    der Broker ohne Persistenz läuft: HA hat die Zustände nicht mehr, und das
    Gateway erfährt davon nichts, weil sich am Gerät nichts geändert hat.
    Z2M macht genau das Gegenteil von 'nichts tun': es publiziert den
    Bridge-Status und danach jeden zwischengespeicherten Gerätezustand neu.
    """
    mq_put(f"{BASE}/status", "online", retain=True, qos=1)
    n = 0
    for addr, subs in list(_ha_last.items()):
        for sub, p in list(subs.items()):
            mq_put(f"{BASE}/zigbee/{addr}/{sub}", json.dumps(p), retain=True, qos=1)
            n += 1
    print(f"[MQTT] HA {why or 'neu gestartet'}: {n} Zustand/Zustände erneut gesendet")


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"[MQTT] verbunden → {BASE}")
        mq_put(f"{BASE}/status", "online", retain=True, qos=1)
        client.subscribe(f"{BASE}/cmd/+", qos=1)
        client.subscribe(f"{BASE}/set/#", qos=1)   # HA-Entity-Befehle
        client.subscribe(_HA_STATUS_TOPIC, qos=0)  # HA-Neustart erkennen
        db_upsert_gateway("online")
        with _state_lock:
            _state["status"] = "online"
        # Z2M setzt retained Nachrichten 2 s nach dem Verbindungsaufbau erneut;
        # die Discovery-Configs kommen zusätzlich aus dem Startfenster zurück.
        threading.Timer(2.0, _republish_retained).start()
        _disc_begin_window()
    else:
        print(f"[MQTT] Fehler rc={rc}")

# ── Befehls-Bestätigung und Wiederholung ─────────────────────────────────
# Das Gerät antwortet auf einen Fernbefehl nur mit Default-Response und
# schickt KEINEN Report-Attribut. Ob der Befehl wirklich angekommen ist,
# erfährt man erst durch ein aktives Read-Attributes (read_onoff).
#
# Bei LQI 15–30 – Gerät sitzt in der Wand, Koordinator im Schaltschrank –
# gehen BEFEHL und LESEANFRAGE gelegentlich verloren. Dann zeigte HA den
# angenommenen Wert, obwohl das Gerät nie geschaltet hat. Genau das war am
# 2026-10-17 messbar: ep17 meldete 0.78 s nach "off" wieder v=1.
#
# Deshalb: nach dem Befehl lesen, zweimal nachfassen, und bei Abweichung oder
# ausbleibender Antwort den Befehl bis zu _MAX_TRIES mal wiederholen.
_MAX_TRIES   = 3
_READ_DELAY  = 0.7     # s bis zum ersten Read-Attributes
_READ_DELAY2 = 1.5     # s bis zum zweiten (falls der erste verloren geht)
_RETRY_AFTER = 2.6     # s ohne jede Rückmeldung → Befehl wiederholen

_pending      = {}     # (addr, ep) → {"want": 0|1, "tries": n, "ts": t}
_pending_lock = threading.Lock()

# ── Geräte ohne rücklesbaren Zustand ───────────────────────────────────────
# Nicht jedes Gerät kann seinen On/Off-Zustand beantworten. Das Szenen-Panel
# FTB56+ZSN18ZY1.1 (0x747d) ist derselbe Feibit-Bau wie der HGZB-043, aber
# ohne Relais: es nimmt On/Off-Befehle an und quittiert sie mit
# Default-Response SUCCESS, beantwortet ein Read-Attributes auf 0x0006/0x0000
# jedoch mit 0x86 (UNSUPPORTED_ATTRIBUTE) – es gibt schlicht kein Attribut,
# weil es nichts zu schalten gibt.
#   gemessen 2026-10-17:  0x07da → "186c010000002001"  (status 00, type 20, v=1)
#                         0x747d → "185001000086"      (status 86)
# Für solche Endpunkte ist eine Bestätigung prinzipiell unmöglich. Ohne diese
# Liste würde JEDER Klick drei Befehle und sechs Leseanfragen auslösen und am
# Ende "nicht durchgesetzt" melden – für immer.
_noread = set()        # "addr/ep" – Zustand nicht rücklesbar
_ctrl   = set()        # "addr/ep" – reiner Sender (Tastenpanel, kein Relais)


def _ep_key(addr, ep):
    return f"{addr}/{int(ep)}"


def _noread_add(addr, ep, why):
    k = _ep_key(addr, ep)
    if k in _noread:
        return
    _noread.add(k)
    _nodes_save()
    print(f"[HA] {addr} ep{ep}: Zustand nicht rücklesbar ({why}) – "
          f"Befehle werden ab jetzt NICHT mehr wiederholt")
    # Ist der Endpunkt schon als Sender bekannt, ist die switch-Entity damit
    # endgültig widerlegt → jetzt abmelden (siehe _classify).
    _classify(addr, ep, f"kein OnOff-Attribut ({why})")


def _noread_del(addr, ep):
    k = _ep_key(addr, ep)
    if k in _noread:
        _noread.discard(k)
        _nodes_save()
        print(f"[HA] {addr} ep{ep}: meldet doch einen Zustand – "
              f"Rücklesen wieder aktiv")


def _send_read_onoff(addr, ep):
    """Fragt den echten On/Off-Zustand beim Gerät ab (Read Attributes)."""
    if time.monotonic() - _gw_boot_ts < _GW_BOOT_COOLDOWN:
        # Gateway hat gerade gebootet, Zigbee-Stack ist evtl. noch nicht oben.
        return
    try:
        msg = _js({"cmd": "read_onoff", "addr": addr, "ep": ep})
        ser.write(msg.encode())
        print(f"[→C6] {msg.strip()}")
    except Exception as e:
        print(f"[HA] Rücklesen {addr} ep{ep} fehlgeschlagen: {e}")


def _sub_ep(sub):
    """'ep17/on_off' → 17, 'on_off' → 1 (der C6 lässt bei EP1 das Präfix weg)."""
    if sub.startswith("ep") and "/" in sub:
        try:
            return int(sub.split("/", 1)[0][2:])
        except ValueError:
            return None
    return 1


def _pending_ack(addr, sub, p):
    """Ein echter Gerätewert ist eingetroffen – offene Bestätigung prüfen."""
    if not sub.endswith("on_off"):
        return
    v = (p or {}).get("v")
    if v is None:
        return
    ep = _sub_ep(sub)
    if ep is None:
        return
    # Ein echter Zustand ist der Beweis, dass der Endpunkt lesbar ist.
    _probe_seen(addr, ep)
    key = (addr, ep)
    with _pending_lock:
        pend = _pending.get(key)
        if not pend:
            return
        nr = bool(pend.get("noread"))
        if int(v) == int(pend["want"]):
            _pending.pop(key, None)
            print(f"[HA] {addr} ep{ep}: Gerät bestätigt v={int(v)}")
            _noread_del(addr, ep)
            return
        if nr or pend["tries"] >= _MAX_TRIES:
            _pending.pop(key, None)
            if nr:
                print(f"[HA] {addr} ep{ep}: Gerät meldet v={int(v)} "
                      f"(kein Rücklesen möglich) – Anzeige korrigiert")
            else:
                print(f"[HA] {addr} ep{ep}: nach {_MAX_TRIES} Versuchen nicht "
                      f"durchgesetzt (Gerät meldet v={int(v)})")
            # Anzeige auf die Wahrheit korrigieren – nicht auf der Annahme
            # stehen lassen, sonst zeigt HA dauerhaft einen falschen Zustand.
            _ha_state(addr, f"ep{ep}/on_off" if ep > 1 else "on_off",
                      {"v": int(v)})
            return
        pend["tries"] += 1
        want = pend["want"]
        n = pend["tries"]
        pend["ts"] = time.time()
    print(f"[HA] {addr} ep{ep}: Gerät meldet v={int(v)}, gewollt {want} "
          f"→ Befehl wird wiederholt ({n}/{_MAX_TRIES})")
    _issue_cmd(addr, ep, want, "on" if want else "off", n)


def _issue_cmd(addr, ep, want, act, attempt=1):
    """Schickt dev_cmd und legt die Bestätigung an (mit Read-Back-Timern)."""
    noread = _ep_key(addr, ep) in _noread
    uart_msg = _js({"cmd": "dev_cmd", "addr": addr, "ep": ep, "sw": act})
    ser.write(uart_msg.encode())
    print(f"[→C6] {uart_msg.strip()}"
          + (f"   (Versuch {attempt}/{_MAX_TRIES})" if attempt > 1 else ""))
    with _pending_lock:
        # Bei "noread" wird tries auf _MAX_TRIES gesetzt: dann kann weder
        # _retry_if_silent noch ein abweichender Report eine Wiederholung
        # auslösen – ein Befehl pro Klick, mehr nicht.
        _pending[(addr, ep)] = {"want": want,
                                "tries": _MAX_TRIES if noread else attempt,
                                "noread": noread,
                                "ts": time.time()}
    try:
        # EIN Rückleseversuch bleibt auch bei "noread" stehen: er kostet
        # einen Frame und ist die einzige Chance, die Einstufung später
        # automatisch zu revidieren (meldet das Gerät doch einen Wert,
        # löscht _pending_ack das Merkmal wieder).
        threading.Timer(_READ_DELAY, _send_read_onoff, (addr, ep)).start()
        if not noread:
            if attempt == 1:
                threading.Timer(_READ_DELAY2, _send_read_onoff, (addr, ep)).start()
            # Der Timeout-Wächter gehört an JEDEN Versuch, nicht nur an den
            # ersten: stand er nur dort, endete eine stumme Kette nach
            # Versuch 2 und der Eintrag in _pending blieb für immer liegen
            # (am 2026-10-17 im Test aufgefallen: 2 statt 3 Befehle, pending
            # nie abgeräumt). _retry_if_silent prüft selbst, ob es zu früh ist.
            threading.Timer(_RETRY_AFTER, _retry_if_silent, (addr, ep)).start()
    except Exception as e:
        print(f"[HA] Bestätigungs-Timer nicht gestartet: {e}")


def _retry_if_silent(addr, ep):
    """Gar keine Rückmeldung? Dann ist vermutlich schon der Befehl verloren."""
    key = (addr, ep)
    with _pending_lock:
        pend = _pending.get(key)
        if not pend:
            return                        # wurde bestätigt
        if pend.get("noread"):
            return                        # hier ist Wiederholen sinnlos
        if time.time() - pend["ts"] < _RETRY_AFTER * 0.5:
            return                        # gerade erst wiederholt
        if pend["tries"] >= _MAX_TRIES:
            _pending.pop(key, None)
            print(f"[HA] {addr} ep{ep}: keine Antwort vom Gerät, "
                  f"{_MAX_TRIES} Versuche ohne Erfolg")
            # Nach drei erfolglosen Runden nie wieder: entweder kann das Gerät
            # kein Read-Attributes (siehe _noread) oder der Funkweg ist so
            # schlecht, dass Wiederholen nichts bringt. Beides heißt "nicht
            # bestätigbar" – und dann soll ein Klick EINEN Befehl auslösen,
            # nicht drei.
            _noread_add(addr, ep, "keine Antwort auf Read-Attributes")
            return
        pend["tries"] += 1
        want = pend["want"]
        n = pend["tries"]
        pend["ts"] = time.time()
    print(f"[HA] {addr} ep{ep}: keine Bestätigung → Befehl wiederholt "
          f"({n}/{_MAX_TRIES})")
    _issue_cmd(addr, ep, want, "on" if want else "off", n)


def _ha_handle_set(topic, payload):
    """HA-Schaltbefehl → dev_cmd an den Koordinator.

    Topic:   {BASE}/set/{addr}/{epN}/on_off
    Payload: "1" | "0" | "toggle"
    """
    parts = topic[len(BASE) + 1:].split("/")   # ["set", addr, epN, on_off]
    if len(parts) < 4 or parts[0] != "set":
        return
    addr = parts[1]
    ep   = int("".join(c for c in parts[2] if c.isdigit()) or 1)
    # HA schickt je nach Config 1/0, ON/OFF oder true/false – alles abbilden.
    # Alles Unbekannte bleibt toggle.
    pl   = payload.strip().upper()
    if   pl in ("1", "ON", "TRUE"):  act = "on"
    elif pl in ("0", "OFF", "FALSE"): act = "off"
    else:                              act = "toggle"

    # Zustand sofort zurückspiegeln. Das Gerät (z. B. HGZB-043) antwortet auf
    # On/Off nur mit einem Default-Response und schickt KEIN Report-Attribute –
    # ohne diesen Echo bliebe die HA-Kachel auf "unknown" und der Schalter
    # würde nach jedem Klick zurückspringen ("lässt sich nicht steuern").
    # Kommt später doch ein echter Report, überschreibt der den Wert.
    #
    # WICHTIG: gleiche Namensregel wie ha_mqtt_publish_zigbee_ep() im C6 –
    # Endpunkt 1 bekommt KEIN "ep1/"-Präfix. Sonst landet der Echo auf einem
    # anderen Topic als dem state_topic der Entity, und bei Einzel-Endpunkt-
    # Geräten springt der Schalter zurück, obwohl der Befehl ankam.
    sub = f"ep{ep}/on_off" if ep > 1 else "on_off"
    old = (_ha_last.get(addr, {}).get(sub) or {}).get("v")
    if act == "on":
        new = 1
    elif act == "off":
        new = 0
    else:
        new = 0 if old == 1 else 1
    _ha_state(addr, sub, {"v": new})

    # Befehl raus + Bestätigung anfordern (siehe Block oben).
    _issue_cmd(addr, ep, new, act)

def on_message(client, userdata, msg):
    """Kommandos vom Host → an C6 über Serial weiterleiten."""
    # Echo-Schutz: was wir gerade selbst gesendet haben, ist kein Kommando.
    #
    # Ausnahme {BASE}/cmd/*: dorthin publiziert das Web-Formular seine Befehle
    # (permit_join, tx_power …), damit Web und MQTT denselben Weg nehmen.
    # Ohne die Ausnahme frisst der Schutz genau diese Kommandos – der Knopf
    # im Web-Frontend tut dann nichts, ohne dass irgendwo etwas zu sehen ist.
    if not msg.topic.startswith(f"{BASE}/cmd/") and _self_pub.get(msg.topic, 0) > time.time():
        return
    payload = msg.payload.decode(errors="replace")

    # HA ist fertig gestartet → Zustände erneut senden.
    if msg.topic == _HA_STATUS_TOPIC:
        if payload.strip().lower() == "online":
            print("[HA] meldet Neustart (homeassistant/status) → sende Zustände neu")
            threading.Timer(1.0, _ha_republish_all, ("neu gestartet",)).start()
        return

    # Startfenster: die vorhandenen Discovery-Configs nur einlesen, nicht
    # als Kommando behandeln.
    if not _disc_ready and msg.topic.startswith(f"{HA_PREFIX}/") and msg.topic.endswith("/config"):
        _disc_remote[msg.topic] = payload
        return

    # HA-Discovery-Schaltbefehle: {BASE}/set/0x07da/ep16/on_off
    if msg.topic.startswith(f"{BASE}/set/"):
        _ha_handle_set(msg.topic, payload)
        return
    cmd = msg.topic.split("/")[-1]
    if cmd == "permit_join":
        uart_msg = _js({"cmd": "permit_join", "sec": int(payload)})
        ser.write(uart_msg.encode())
        print(f"[→C6] {uart_msg.strip()}")
    elif cmd == "set_channel":
        ch = int(payload)
        if 11 <= ch <= 26:
            uart_msg = _js({"cmd": "set_channel", "ch": ch})
            ser.write(uart_msg.encode())
            print(f"[→C6] {uart_msg.strip()}")
        else:
            print(f"[WARN] Ungültiger Zigbee-Kanal: {ch} (11-26)")
    elif cmd == "scan_chan":
        uart_msg = _js({"cmd": "scan_chan"})
        ser.write(uart_msg.encode())
        print(f"[→C6] {uart_msg.strip()}")
    elif cmd == "switch2wifi":
        uart_msg = _js({"cmd": "switch2wifi"})
        ser.write(uart_msg.encode())
        print(f"[→C6] {uart_msg.strip()}")
    elif cmd == "probe":
        # Modellabfrage für ein Gerät, das schon im Netz ist, aber nichts von
        # sich hören lässt (kein Announce, keine Reports) → Basic-Cluster lesen.
        # Payload: "0x07da,16"  (EP optional, Default 1)
        try:
            parts = payload.split(",")
            addr = parts[0].strip()
            ep = int(parts[1]) if len(parts) > 1 else 1
            uart_msg = _js({"cmd": "probe", "addr": addr, "ep": ep})
            ser.write(uart_msg.encode())
            print(f"[→C6] {uart_msg.strip()}")
        except (ValueError, IndexError):
            print(f"[WARN] probe: ungültiger Payload {payload}")
    elif cmd == "set_sleep":
        try:
            secs = max(10, int(payload))
            uart_msg = _js({"cmd": "set_sleep", "s": secs})
            ser.write(uart_msg.encode())
            print(f"[→C6] {uart_msg.strip()}")
        except ValueError:
            print(f"[WARN] set_sleep: ungültiger Wert {payload}")
    elif cmd == "set_dev_power":
        # 中继发射功率：payload 是 JSON {"addr":"0x1234","ep":1,"dbm":14}
        # 网关会翻成 ZCL Write Attribute 0xFC00/0x0001 发给那台中继；
        # 中继收到即改 PA 并落自己的 NVS，所以以后不用重新刷固件。
        try:
            o = json.loads(payload)
            addr = str(o.get("addr", "")).strip()
            ep   = int(o.get("ep", 1) or 1)
            dbm  = max(-24, min(20, int(o.get("dbm", 8))))
            if not addr:
                raise ValueError("addr fehlt")
        except (ValueError, TypeError, json.JSONDecodeError):
            print(f"[WARN] set_dev_power: ungültiger Payload {payload!r}")
            return
        uart_msg = _js({"cmd": "set_dev_power", "addr": addr, "ep": ep, "dbm": dbm})
        ser.write(uart_msg.encode())
        print(f"[→C6] {uart_msg.strip()}")
    elif cmd == "set_tx_power":
        # Sendepfad des C6 in dBm. Payload: "-4" oder "{\"dbm\":-4}"
        try:
            raw = payload.strip()
            if raw.startswith("{"):
                raw = str(json.loads(raw).get("dbm", ""))
            dbm = int(raw)
        except (ValueError, TypeError, json.JSONDecodeError):
            print(f"[WARN] set_tx_power: ungültiger Wert {payload!r}")
            return
        dbm = max(-24, min(20, dbm))
        uart_msg = _js({"cmd": "set_tx_power", "dbm": dbm})
        ser.write(uart_msg.encode())
        _txp_save(dbm)          # überlebt C6-Neustart und Add-on-Neustart
        print(f"[→C6] {uart_msg.strip()}   (Rücklesewert folgt als {{\"t\":\"txpower\"}})")
    elif cmd == "lqi":
        # Momentaufnahme: sonst liefert nur der 60-s-Heartbeat LQI/RSSI.
        uart_msg = _js({"cmd": "lqi"})
        ser.write(uart_msg.encode())
        print(f"[→C6] {uart_msg.strip()}")
    elif cmd == "read_onoff":
        # Payload "0x747d,2" → Read Attributes auf On/Off.
        # Der einzige harte Beweis, dass ein Downlink-Frame das Gerät erreicht
        # hat, ist dessen Antwort. Genau das braucht der Sendeleistungs-Vergleich
        # (txpower_ab.py): LQI/RSSI beschreiben die Gegenrichtung und bleiben
        # beim Ändern der Gateway-Leistung praktisch unverändert.
        try:
            parts = payload.split(",")
            addr = parts[0].strip()
            ep   = int(parts[1]) if len(parts) > 1 else 1
        except (ValueError, IndexError):
            print(f"[WARN] read_onoff: ungültiger Payload {payload!r}")
            return
        uart_msg = _js({"cmd": "read_onoff", "addr": addr, "ep": ep})
        ser.write(uart_msg.encode())
        print(f"[→C6] {uart_msg.strip()}")

mq.on_connect = on_connect
mq.on_message = on_message

# ── Serial ─────────────────────────────────────────────────────────────────
_write_err = 0        # 累计写失败次数（只用于限速日志，见 CoordinatorSerial.write）


class CoordinatorSerial:
    """Persistente Serial-Verbindung ohne ESP32-C6 Reset-Trigger."""
    def __init__(self, port, baudrate):
        # HUPCL deaktivieren bevor pyserial öffnet → WCH-Chip löst kein DTR-Reset aus
        import subprocess
        subprocess.run(["stty", "-F", port, "-hupcl"], check=False)
        self.ser = serial.Serial()
        self.ser.port     = port
        self.ser.baudrate = baudrate
        self.ser.timeout  = 1
        self.ser.dtr      = False
        self.ser.rts      = False
        self.ser.open()
        time.sleep(0.5)
        try:
            self.ser.dtr = False
        except Exception:
            pass
        print(f"[Serial] {port} @ {baudrate} offen (DTR/RTS=LOW, HUPCL=off)")

    def read(self, n):   return self.ser.read(n)

    def write(self, d):
        """写串口。★ 异常必须在这里吞掉。

        串口 EIO 时如果把 SerialException 抛出去，会顺着 paho 的 on_message
        一路炸穿 MQTT 回调线程 —— 2026-10-26 实测日志：
            _issue_cmd → ser.write → SerialException: write failed: [Errno 5]
        之后 HA 发来的开关命令全部失灵，日志只刷 traceback，而真正的病根
        （端口已死）没人处理。这里只记数，端口重开统一交给读循环的看门狗。
        """
        global _write_err
        try:
            return self.ser.write(d)
        except Exception as e:                          # noqa: BLE001
            _write_err += 1
            if _write_err in (1, 10) or _write_err % 200 == 0:
                print(f"[Serial] 写失败 {_write_err} 次: {e}"
                      + ("  → 端口已死，等读循环重开" if _write_err == 1 else ""))
            return 0

    def close(self):     self.ser.close()

    @property
    def in_waiting(self): return self.ser.in_waiting

# ── Notfall-Flash über esptool (Bootloader-Weg) ───────────────────────────
# Der App-Level-OTA (fw_ota_*) setzt voraus, dass die LAUFENDE Firmware
# fw_ota_data versteht. Ältere Builds tun das nicht – sie lesen Kommandos mit
# fgets(buf, 256) und/oder matchen starr mit strstr("\"data\":\"") –, dann
# kommt nie ein ACK und der Transfer hängt nach dem ersten Stoß.
# In dem Fall bleibt nur der Bootloader: esptool resettet den C6 über
# USB-Serial-JTAG in den Download-Modus und schreibt das Image direkt.
APP_OFFSET = "0x20000"   # ota_0 – boot-Log: "Loaded app from partition at offset 0x20000"
FLASH_BAUD = 460800

_serial_pause      = threading.Event()   # hält die Leseschleife an
_flash_mutex       = threading.Lock()    # nur ein Flash gleichzeitig
_flash_state_lock  = threading.Lock()
_flash_state       = {"status": "idle", "bin": "", "tail": []}


class _NullSerial:
    """占位用的"死"串口：开机打不开端口时顶上，让 Web UI / MQTT 照常起来。

    读一律抛 OSError，所以主循环的看门狗会自动走「重开」分支，等设备回来了
    自己接上 —— 总比开机就 traceback 退出、被 Supervisor 反复重启好。
    """
    def read(self, n):   raise OSError("serial not open")
    def write(self, d):  return 0
    def close(self):     pass

    @property
    def in_waiting(self):
        raise OSError("serial not open")


def _serial_open(port, baud, attempts=3, first_delay=0.0):
    """打开串口，失败就重试几次。全失败时返回 _NullSerial（不抛异常）。"""
    delay = first_delay
    for i in range(1, attempts + 1):
        if delay:
            time.sleep(delay)
        try:
            s = CoordinatorSerial(port, baud)
            return s
        except Exception as e:                          # noqa: BLE001
            print(f"[Serial] 打开 {port} 失败（第 {i}/{attempts} 次）: {e}")
            delay = min(max(delay * 2, 1.0), 10.0)
    print(f"[Serial] !! {port} 打不开，先用占位串口顶着，稍后自动重试")
    return _NullSerial()


def _serial_reopen(reason: str = "") -> bool:
    """掉线自愈：关掉旧 fd 重新打开端口。

    ★ 为什么必须"重开"而不是"重试"：USB-CDC 端点在设备重新枚举后会失效，
      **旧 fd 会永远返回 `[Errno 5] I/O error`**。2026-10-26 重建 add-on 后
      实测：连续 100+ 次读全是 EIO，网关一台设备都收不到（页面空列表），
      而 read() 里原来那句「sleep 0.5 继续」永远不会自己好 —— 只能换 fd。

    返回 True 表示端口已恢复。与 esptool 刷机互斥：正在刷就让开。
    """
    global ser
    if _serial_pause.is_set():
        return False                       # esptool 正在用端口
    if not _flash_mutex.acquire(blocking=False):
        print("[Serial] 端口被刷机占用，本次不重开")
        return False
    try:
        try:
            ser.close()
        except Exception:
            pass
        delay = 1.0
        for i in range(1, 7):
            time.sleep(delay)
            try:
                ser = CoordinatorSerial(args.port, args.baud)
                global _write_err
                _write_err = 0
                print(f"[Serial] 端口已重开（{reason}）· 第 {i} 次尝试成功")
                return True
            except Exception as e:                      # noqa: BLE001
                print(f"[Serial] 重开失败 第 {i} 次: {e}")
                delay = min(delay * 2, 15.0)
        print("[Serial] !! 端口连续 6 次打不开，稍后再试")
        return False
    finally:
        _flash_mutex.release()


def _serial_kick_reset(reason: str = "") -> bool:
    """硬复位 C6：拉一下 RTS（=EN），让它重新启动。

    什么时候用：端口能打开、但**一个字节都收不到**。这时问题在芯片侧
    （固件挂了 / USB-JTAG 端点停了），重开 fd 没用，只能复位。

    ★ RTS=True 拉低 EN、False 放开 —— 和 esptool 的默认复位序列一致。
      DTR 保持 False，否则会被 strapping 带进下载模式（那样就再也收不到
      JSON 上报了，只剩 bootloader）。
    """
    try:
        s = getattr(ser, "ser", None)
        if s is None:
            return False
        print(f"[Serial] 强制复位 C6（{reason}）")
        s.rts = True
        time.sleep(0.15)
        s.rts = False
        return True
    except Exception as e:                              # noqa: BLE001
        print(f"[Serial] 复位失败: {e}")
        return False


def _esptool_cmd(bin_path: str) -> list:
    return [sys.executable, "-m", "esptool",
            "--chip", "esp32c6",
            "--port", args.port,
            "--baud", str(FLASH_BAUD),
            "--before", "default_reset",
            "--after", "hard_reset",
            "write_flash", APP_OFFSET, bin_path]


def flash_status() -> dict:
    with _flash_state_lock:
        return {"status": _flash_state["status"],
                "bin": _flash_state["bin"],
                "tail": list(_flash_state["tail"])}


def flash_is_running() -> bool:
    return _flash_state["status"] == "running"


def flash_apply(bin_path: str) -> bool:
    """Schreibt bin_path über den Bootloader. Blockiert bis esptool fertig ist."""
    global ser
    if not _flash_mutex.acquire(blocking=False):
        print("[FLASH] läuft bereits – Anfrage ignoriert")
        return False
    _serial_pause.set()                 # Hauptschleife anhalten
    with _flash_state_lock:
        _flash_state.update({"status": "running", "bin": bin_path, "tail": []})
    ok = False
    try:
        time.sleep(0.3)
        try:
            ser.close()
        except Exception:
            pass
        cmd = _esptool_cmd(bin_path)
        print("[FLASH] starte: " + " ".join(cmd[1:]))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                errors="replace", bufsize=1)
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            print(f"[FLASH] {line}")
            with _flash_state_lock:
                _flash_state["tail"] = (_flash_state["tail"] + [line])[-40:]
        rc = proc.wait()
        ok = rc == 0
        print(f"[FLASH] esptool beendet, rc={rc}"
              + ("" if ok else "  (Details oben; fehlt esptool im Image?)"))
    except FileNotFoundError:
        print("[FLASH] esptool/python nicht gefunden")
    except Exception as e:
        print(f"[FLASH] Fehler: {e}")
    finally:
        # 刷完恢复串口。用 _serial_open：刷机后设备正在重新枚举，一次打不开
        # 很正常，别让整个 finally 抛异常（会把 _flash_state 卡在 running）。
        ser = _serial_open(args.port, args.baud, attempts=5, first_delay=1.0)
        print("[FLASH] Serial 恢复流程结束 – 桥接继续")
        with _flash_state_lock:
            _flash_state["status"] = "done" if ok else "error"
        _serial_pause.clear()
        _flash_mutex.release()
    return ok


# ── HA Auto-Discovery ─────────────────────────────────────────────────────
_ha_devices = {}     # addr → {"ieee", "mfr", "model"}
_ha_nodes   = {}     # addr → node  (Sitzungs-Cache, siehe _ha_node)
_ha_last    = {}     # addr → {sub: payload}  letzter bekannter Zustand

# ── Discovery-Cache ───────────────────────────────────────────────────────
# Früher war _ha_sent eine Menge (node, obj_id, addr, _DISC_VER). Zwei
# Nachteile, die sich beide in der Praxis gezeigt haben:
#
#   * Der Versionszähler _DISC_VER musste bei JEDER Inhaltsänderung von Hand
#     erhöht werden. Wurde er vergessen, ging die neue Config nie raus und HA
#     behielt stillschweigend die alte Entity – der Fehler ist nicht sichtbar.
#   * Nach jedem Add-on-Neustart war die Menge leer, also wurden ALLE
#     Configs neu publiziert und HA hat jedes Mal sämtliche Entities neu
#     verarbeitet.
#
# Nach Z2M-Vorbild (lib/extension/homeassistant.ts) wird jetzt der fertige
# JSON-String je Topic verglichen:
#     if (!discoveredMessage || discoveredMessage.payload !== payloadStr)
# Damit ist eine Inhaltsänderung automatisch ein Neu-Publish, und ein
# unveränderter Neustart kostet keinen einzigen Publish. _DISC_VER entfällt.
_ha_sent   = {}      # topic → zuletzt publizierter JSON-String
_sent_addr  = {}     # topic → addr   (nur für _purge_addr / Aufräumen)

# Startfenster: Z2M abonniert beim Start 5 s lang das Discovery-Topic, liest
# die dort retained liegenden Configs ein und publiziert nur, was fehlt oder
# abweicht. Das spart beim Neustart hunderte Publish-Vorgänge – und ist die
# einzige Chance, überhaupt zu erfahren, was der Broker schon hat.
_DISC_WAIT   = 5.0
_disc_ready  = True     # vor dem ersten Verbindungsaufbau: sofort publizieren
_disc_remote = {}       # topic → payload (aus dem Broker gelesen)
_disc_defer  = {}       # topic → payload (während des Fensters zurückgehalten)

# Nur noch dokumentarisch: der Zähler wird nicht mehr in Schlüssel eingerechnet.
#   1 → erste Fassung
#   2 → "has_entity_name" (Namen waren doppelt: "HGZB-043 HGZB-043 上 开关")
#   3 → event-Entity für Tastenpanels (_ha_publish_event)
_DISC_VER = 3

# Herkunft der Entities – HA zeigt sie im Geräte-Dialog an. Z2M setzt das
# Feld ebenfalls (origin: {name, sw, url}).
def _gw_version():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "config.json"), encoding="utf-8") as f:
            return json.load(f).get("version") or "dev"
    except Exception:
        return "dev"

_GW_ORIGIN = {"name": "ESP32-C6 Gateway", "sw": _gw_version(),
              "url": "https://github.com/Koenkk/zigbee-herdsman-converters"}


def _ha_avail(addr=None):
    """Verfügbarkeit im aktuellen Format (Liste) statt des alten Einzelfelds.

    ★ 2026-10-26: 以前只有网关自己这一个 Topic ⇒ 网关在线时，**已经掉线的
      开关也照样显示 "关"**（HA 只是显示最后一次 payload），用户点了没反应
      还以为是网络问题。现在每台设备再挂一个自己的 Topic，模式改 "all"：
      网关在线 **并且** 这台设备在线，实体才可用。

    为什么是 "all" 而不是原来的 "latest"：latest 是"最后收到的那个消息说了算"，
    两个来源会互相覆盖，取决于到达顺序；all 是"所有来源都说 online 才算可用"，
    结果确定的。只有一个 Topic 时两者等价。
    """
    av = {
        "availability": [{
            "topic": f"{BASE}/status",
            "payload_available":     "online",
            "payload_not_available": "offline",
        }],
        "availability_mode": "all",
    }
    if addr and DEVICE_AVAIL:
        # 注意顺序：gateway 必须在 [0]。_disc_purge_orphans() 靠
        # availability[0].topic 判断"这条 discovery 是不是我们发的"。
        av["availability"].append({
            "topic": _avail_topic(addr),
            "payload_available":     "online",
            "payload_not_available": "offline",
        })
    return av


# ── 每台设备的可用性（device availability）──────────────────────────────────
# 判活用"记分板"而不是单一时钟：任何能证明设备存在的证据都打一个时间戳，
# 取最大值为"最后一次被证实存活"。
#   · 它发来的任意 APS/ZCL 帧            → 最硬的证据
#   · ZDO node_desc 探测成功              → 拓扑轮询每 60 s 就探一次，全覆盖
#   · 出现在协调器邻居表里                → 协调器刚听见它
#   · 自己上报邻居表（Mgmt_Lqi_rsp）
# 反证只有一条：node_desc 探测**失败**（0x85 超时等），且比最后一次成功更晚。
# ★ 为什么不用"多久没上报"这一条判：只在按键时才上报的开关（0x023b 那种）
#   会长期没帧，但它们每 60 s 的探测都答得上 —— 靠超时会误杀。
_avail_last_ok  = {}     # addr → 最后一次被证实存活的 time.time()
_avail_last_bad = {}     # addr → 最后一次探测失败的 time.time()
_avail_pub      = {}     # addr → 已发布的 "online"/"offline"（去抖）

# 启动宽限：刚开机设备表/邻居表都还是空的，这时候判死必然是误杀。
_AVAIL_GRACE   = 300.0
_AVAIL_NBR_MAX = 600.0   # 邻居表超过这么久没更新，就不拿它当"看不见"的证据
_avail_boot    = time.time()


def _avail_topic(addr: str) -> str:
    return f"{BASE}/dev/{addr}/status"


def _avail_touch(addr) -> None:
    """记下「这台设备刚刚被证实存活」。"""
    if addr:
        _avail_last_ok[addr] = time.time()


def _avail_timeout(power) -> float:
    """多久没有任何证据就判掉线。市电 30 min（探测每 60 s 一次）、电池 24 h。"""
    p = (power or "").lower()
    if "battery" in p or "batt" in p:
        return 24 * 3600.0
    if "mains" in p:
        return 30 * 60.0
    return 6 * 3600.0


def _avail_vanished(addr) -> bool:
    """历史上有实体，但本次启动以来毫无音讯，而且协调器也听不见它。

    这是"设备真的不在了"的唯一可判定形态：
      · 地址还在实体表里（有过 discovery）→ 说明不是陌生地址；
      · 启动宽限期已过（设备表/邻居表都已建立）；
      · 不在设备表里（没有任何上报、也没有被拓扑采集补进来）；
      · 不在协调器邻居表里，而邻居表本身是新鲜的。
    换过地址的设备不在这里：_purge_addr() 会把旧地址从 _addr_nodes 删掉。
    """
    if addr not in _addr_nodes:
        return False
    if time.time() - _avail_boot < _AVAIL_GRACE:
        return False
    if addr in (_state.get("devices") or {}):
        return False
    tp = _state.get("topo") or {}
    if (time.time() - (tp.get("done_at") or 0.0)) > _AVAIL_NBR_MAX:
        return False                        # 邻居表太旧，不敢据此判死
    nbr = tp.get("nbr") or []
    return not any(n.get("a") == addr for n in nbr)


def _avail_online(addr) -> bool:
    ok  = _avail_last_ok.get(addr, 0.0)
    bad = _avail_last_bad.get(addr, 0.0)
    if ok == 0.0 and bad == 0.0:
        # 一点证据都没有（刚启动、设备表还没建立）→ **不主动判死**，
        # 保持过去的行为（可用）。宁可不显示不可用，也不要误杀。
        # 例外：实体还在、但启动后彻底没影的设备（见 _avail_vanished）。
        return not _avail_vanished(addr)
    if bad > ok:
        return False
    nd = (_state.get("node_desc") or {}).get(addr) or {}
    return (time.time() - ok) <= _avail_timeout(nd.get("power"))


def _avail_sync(addr, force: bool = False) -> None:
    """按当前判断发布 availability（retained；状态没变就不发）。"""
    if not HA_ENABLED or not addr or not DEVICE_AVAIL:
        return
    st = "online" if _avail_online(addr) else "offline"
    if not force and _avail_pub.get(addr) == st:
        return
    was = _avail_pub.get(addr)
    _avail_pub[addr] = st
    mq_put(_avail_topic(addr), st, retain=True, qos=1)
    if was != st:
        print(f"[AVAIL] {addr} → {st}")


def _avail_task(interval: float = 30.0):
    """定期重算并发布每台设备的 availability。

    探测结果由拓扑轮询（每 60 s 对每台设备发 node_desc）持续刷新，
    这里只负责"把记分板换算成 online/offline 并推给 HA"。
    """
    while True:
        try:
            with _state_lock:
                addrs = set((_state.get("devices") or {}).keys())
                nbr = (_state.get("topo") or {}).get("nbr") or []
            addrs |= {n.get("a") for n in nbr if n.get("a")}
            # 历史上发过 discovery 的地址也要算进去：只有把它们纳进来，
            # "设备已经不在网络上"才能从 online 翻成 offline（否则那条
            # retained 的 online 永远没人去改，HA 里就是一条僵尸实体）。
            addrs |= set(_addr_nodes.keys())
            for a in sorted(addrs):
                _avail_sync(a)
        except Exception as e:                          # noqa: BLE001
            print(f"[AVAIL] 循环出错: {e}")
        time.sleep(interval)


def _ago(ts) -> str:
    """把 time.time() 时间戳说成「多久以前」。"""
    if not ts:
        return "—"
    d = max(0, int(time.time() - ts))
    if d < 60:
        return f"{d} 秒前"
    if d < 3600:
        return f"{d // 60} 分钟前"
    if d < 86400:
        return f"{d // 3600} 小时前"
    return f"{d // 86400} 天前"



def _disc_begin_window():
    """Startfenster öffnen: vorhandene Discovery-Configs einlesen."""
    global _disc_ready
    if not HA_ENABLED:
        return
    _disc_ready = False
    _disc_remote.clear()
    try:
        mq.subscribe(f"{HA_PREFIX}/+/+/+/config", qos=0)
        print(f"[HA] lese vorhandene Discovery-Configs ({_DISC_WAIT:.0f} s)")
        threading.Timer(_DISC_WAIT, _disc_flush).start()
    except Exception as e:
        print(f"[HA] Startfenster nicht möglich: {e}")
        _disc_ready = True


def _disc_flush():
    """Startfenster schließen: nur abweichende Configs wirklich publizieren."""
    global _disc_ready
    _disc_ready = True
    try:
        mq.unsubscribe(f"{HA_PREFIX}/+/+/+/config")
    except Exception:
        pass
    neu = gleich = 0
    for topic, payload in list(_disc_defer.items()):
        if _disc_remote.get(topic) == payload:
            _ha_sent[topic] = payload      # Broker hat sie schon, nichts zu tun
            gleich += 1
            continue
        mq_put(topic, payload, retain=True, qos=1)
        _ha_sent[topic] = payload
        neu += 1
    _disc_defer.clear()
    print(f"[HA] Discovery-Abgleich: {neu} neu/geändert, {gleich} unverändert "
          f"({len(_disc_remote)} Configs im Broker)")
    _disc_purge_orphans()
    _disc_upgrade_avail()


def _disc_purge_orphans():
    """Retained Discovery-Configs entfernen, die zu nichts mehr gehören.

    Zwei Fälle, beide schon aufgetreten:
      * Gerät ist nicht mehr da (Kurzadresse unbekannt) → der ganze Satz
        bleibt sonst als Waise im Broker und in HA als nicht verfügbare
        Karteileiche stehen.
      * Endpunkt wurde als reiner Sender eingestuft → ein switch darf dort
        nicht (mehr) existieren.
    """
    if not HA_ENABLED or not _disc_remote:
        return
    if not _addr_nodes and not _ieee_nodes:
        # Ohne jede Kenntnis über die eigenen Geräte (ha_nodes.json fehlt,
        # Neuinstallation) ist "unbekannter Knoten" nicht von "Gerät noch
        # nicht wieder aufgetaucht" zu trennen. Dann lieber nichts löschen –
        # ein falsch gelöschter Satz kostet den Nutzer alle Entities.
        print("[HA] kein Gerätebestand bekannt – Aufräumen übersprungen")
        return
    bekannt = set(_addr_nodes.values()) | set(_ieee_nodes.values())
    rueck   = {v: k for k, v in _addr_nodes.items()}
    eigen   = f"{BASE}/status"
    weg = 0
    for topic, payload in list(_disc_remote.items()):
        parts = topic.split("/")
        if len(parts) < 5:
            continue
        comp, node, obj_id = parts[1], parts[2], parts[3]
        # Zugehörigkeit nicht am Namenspräfix festmachen, sondern am
        # availability-Topic der Config selbst. Z2M prüft genauso
        # (message.availability[0].topic.startsWith(baseTopic)). Sonst würde
        # eine ZWEITE Gateway-Instanz am selben Broker die Entities der
        # ersten wegräumen – deren Knoten heißen genauso "c6_…".
        try:
            cfg = json.loads(payload)
        except Exception:
            continue
        av = cfg.get("availability")
        if isinstance(av, list) and av and isinstance(av[0], dict):
            gehoert_uns = av[0].get("topic") == eigen
        else:
            gehoert_uns = cfg.get("availability_topic") == eigen   # alte Fassung
        if not gehoert_uns:
            continue
        grund = None
        if node not in bekannt:
            grund = "Gerät unbekannt"
        elif comp == "switch" and obj_id.startswith("ep") and "_" in obj_id:
            try:
                ep = int(obj_id[2:].split("_", 1)[0])
            except ValueError:
                ep = 0
            addr = rueck.get(node)
            if ep and addr and _ep_key(addr, ep) in _ctrl:
                grund = "Endpunkt ist reiner Sender"
        if grund:
            mq_put(topic, "", retain=True, qos=1)
            _ha_sent.pop(topic, None)
            _sent_addr.pop(topic, None)
            _disc_remote.pop(topic, None)
            weg += 1
            print(f"[HA] verwaiste Config entfernt ({grund}): {topic}")
    if weg:
        print(f"[HA] {weg} veraltete Discovery-Config(s) aufgeräumt")


def _node_addr_map() -> dict:
    """node-ID → aktuelle Kurzadresse (Umkehrung von _addr_nodes / _ieee_nodes)."""
    m = {n: a for a, n in _addr_nodes.items() if n and a}
    for ieee, node in _ieee_nodes.items():
        if node and node not in m:
            a = _ieee_addr.get(ieee)
            if a:
                m[node] = a
    return m


def _disc_upgrade_avail():
    """Alte, stehengebliebene Discovery-Configs um die Geräte-Availability ergänzen.

    Warum das nötig ist: eine Discovery-Config wird **nur dann** neu geschrieben,
    wenn das Gerät wieder ein Telegramm schickt (_disc_send hängt am Datenpfad).
    Ein Panel, das ausschließlich rohe APS-Tastenrahmen sendet (0x747d), oder ein
    Gerät, das seit dem Update still ist, behält deshalb für immer die alte
    Config mit nur EINEM Availability-Topic (Gateway). Folge: das Gerät ist
    längst weg, in HA steht aber "aus" statt "nicht verfügbar" – eine
    Karteileiche, die man von einem echten "aus" nicht unterscheiden kann.

    Das Startfenster ist der einzige Moment, in dem _disc_remote **alle**
    retained Configs des Brokers im Speicher hat. Genau hier nachziehen.

    Reihenfolge: Gateway-Topic bleibt auf [0], weil _disc_purge_orphans() die
    Zugehörigkeit daran festmacht.
    """
    if not HA_ENABLED or not DEVICE_AVAIL or not _disc_remote:
        return
    n2a  = _node_addr_map()
    eigen = f"{BASE}/status"
    gw = {"topic": eigen, "payload_available": "online",
          "payload_not_available": "offline"}
    auf = 0
    for topic, payload in list(_disc_remote.items()):
        parts = topic.split("/")
        if len(parts) < 5 or parts[4] != "config":
            continue
        node = parts[2]
        try:
            cfg = json.loads(payload)
        except Exception:
            continue
        if not isinstance(cfg, dict):
            continue
        av = cfg.get("availability")
        if isinstance(av, list) and av and isinstance(av[0], dict):
            if av[0].get("topic") != eigen:
                continue                    # fremde Instanz – nicht anfassen
        elif cfg.get("availability_topic") == eigen:
            av = []                         # uralte Einzel-Topic-Fassung
        else:
            continue
        addr = n2a.get(node)
        if not addr:
            continue                        # Adresse unbekannt – nichts raten
        want = _avail_topic(addr)
        if any(isinstance(x, dict) and x.get("topic") == want for x in av):
            continue
        rest = [x for x in av
                if isinstance(x, dict) and x.get("topic") not in (eigen, want)]
        cfg.pop("availability_topic", None)
        cfg["availability"] = [gw] + rest + [
            {"topic": want, "payload_available": "online",
             "payload_not_available": "offline"}]
        cfg["availability_mode"] = "all"
        neu = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
        if neu == payload:
            continue
        mq_put(topic, neu, retain=True, qos=1)
        _ha_sent[topic]    = neu
        _disc_remote[topic] = neu
        auf += 1
        print(f"[HA] availability ergänzt: {topic} → {want}")
    if auf:
        print(f"[HA] {auf} Discovery-Config(s) auf Geräte-Availability umgestellt")
    else:
        print("[HA] availability: alle Discovery-Configs bereits aktuell")


def _disc_send(comp, node, obj_id, cfg, addr=""):
    """Eine Discovery-Config setzen – aber nur, wenn sie sich geändert hat."""
    topic   = f"{HA_PREFIX}/{comp}/{node}/{obj_id}/config"
    payload = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
    if _ha_sent.get(topic) == payload:
        return
    _sent_addr[topic] = addr
    if not _disc_ready:
        # Noch im Startfenster: merken und nach dem Abgleich nachziehen.
        # Ohne diese Warteschlange ginge die Entity verloren, denn die
        # Discovery hängt am eintreffenden Telegramm und wird nicht wiederholt.
        _disc_defer[topic] = payload
        return
    mq_put(topic, payload, retain=True, qos=1)
    _ha_sent[topic] = payload
    print(f"[HA] discovery {comp}/{node}/{obj_id} → {cfg.get('name')}")

# ── Persistente Knoten-Zuordnung ───────────────────────────────────────────
# Ohne diese Datei wurde die Node-ID nach jedem Add-on-Start neu aus der
# AKTUELLEN Short-Adresse gebildet. Wechselt ein Gerät beim Rejoin die
# Adresse (nach Stromausfall, Umstecken, Neuaufnahme), entstand ein zweiter
# Entity-Satz; die alten retained Discovery-Configs blieben als Waisen im
# Broker und in HA standen Doppel-Entities.
#
# Gespeichert wird beides:
#   addr_nodes: {addr: node}   – friert bestehende Namen ein (keine Migration)
#   ieee_nodes: {ieee: node}   – findet das Gerät nach Adresswechsel wieder
#   addr_ieee : {addr: ieee}   – gelernte Zuordnung
_DATA_DIR   = "/data" if os.path.isdir("/data") else "/tmp"
_NODES_FILE = os.path.join(_DATA_DIR, "ha_nodes.json")

# ── Sendeleistung merken und nach einem C6-Boot wiederherstellen ───────────
# `set_tx_power` wirkt in der Firmware nur zur LAUFZEIT (main.c schreibt keinen
# NVS-Eintrag). Nach einem Stromausfall oder Reset steht die Leistung wieder auf
# dem SDK-Default – und es fällt niemandem auf, weil `lqi`/`rssi` die
# GEGENrichtung beschreiben und sich beim Ändern der Gateway-Leistung praktisch
# nicht bewegen (§4). Genau so entsteht die Fehlannahme "das Gateway sendet mit
# 20 dBm", während es längst mit 0 dBm läuft.
# Deshalb: letzten Wunschwert in /data ablegen und nach jedem C6-Boot erneut
# senden, bis der Rücklesewert (`eff`) ihn bestätigt.
_TXP_FILE = os.path.join(_DATA_DIR, "gw_txpower.json")
_TXP_DBM  = None
_TXP_LOCK = threading.Lock()


def _txp_load():
    global _TXP_DBM
    try:
        with open(_TXP_FILE, "r", encoding="utf-8") as f:
            _TXP_DBM = int(json.load(f).get("dbm"))
        print(f"[TX] gemerkte Sendeleistung: {_TXP_DBM} dBm")
    except FileNotFoundError:
        _TXP_DBM = None
    except Exception as e:                              # noqa: BLE001
        _TXP_DBM = None
        print(f"[TX] Sendeleistung nicht lesbar ({_TXP_FILE}): {e}")


def _txp_save(dbm):
    global _TXP_DBM
    _TXP_DBM = int(dbm)
    try:
        tmp = _TXP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"dbm": _TXP_DBM}, f)
        os.replace(tmp, _TXP_FILE)
    except Exception as e:                              # noqa: BLE001
        print(f"[TX] Sendeleistung nicht speicherbar: {e}")


def _txp_reapply(delay: float = 3.0, versuche: int = 3):
    """Nach einem C6-Boot den gemerkten Wunschwert erneut senden.

    Mehrere Versuche, weil das Kommando direkt nach `boot` noch ins Leere
    laufen kann (ZBOSS meldet erst danach "ZDO Config Ready"). Abgebrochen
    wird, sobald der Rücklesewert den Wunsch bestätigt.
    """
    if _TXP_DBM is None:
        return

    def _go():
        for i in range(versuche):
            try:
                with _TXP_LOCK:
                    ser.write(_js({"cmd": "set_tx_power", "dbm": _TXP_DBM}).encode())
            except Exception as e:                      # noqa: BLE001
                print(f"[TX] Wiederherstellen fehlgeschlagen: {e}")
                return
            time.sleep(4.0)
            with _state_lock:
                tp = dict(_state.get("tx_power") or {})
            if tp.get("eff") == _TXP_DBM:
                print(f"[TX] Sendeleistung nach C6-Boot wiederhergestellt: "
                      f"{_TXP_DBM} dBm (Versuch {i + 1})")
                return
        print(f"[TX] WARNUNG: {_TXP_DBM} dBm nach C6-Boot nicht bestätigt "
              f"(eff={tp.get('eff')}) – bitte /api/tx_power prüfen")

    threading.Thread(target=_go, daemon=True).start()


_addr_nodes = {}
_ieee_nodes = {}
_addr_ieee  = {}
_ieee_addr  = {}      # ieee → aktuelle addr  (für Aufräumen der alten Adresse)
_addr_subs  = {}      # addr → [sub, …]      (was wir je auf diesem Topic publiziert haben)


def _nodes_load():
    global _addr_nodes, _ieee_nodes, _addr_ieee, _ieee_addr, _addr_subs
    try:
        with open(_NODES_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return
    except Exception as e:
        print(f"[HA] Knoten-Zuordnung nicht lesbar ({_NODES_FILE}): {e}")
        return
    _addr_nodes = dict(d.get("addr_nodes") or {})
    _ieee_nodes = dict(d.get("ieee_nodes") or {})
    _addr_ieee  = dict(d.get("addr_ieee") or {})
    _ieee_addr  = dict(d.get("ieee_addr") or {})
    _addr_subs  = {k: list(v) for k, v in (d.get("addr_subs") or {}).items()}
    _noread.clear()
    _noread.update(d.get("noread") or [])
    _ctrl.clear()
    _ctrl.update(d.get("ctrl") or [])
    print(f"[HA] Knoten-Zuordnung: {len(_addr_nodes)} Adressen, "
          f"{len(_ieee_nodes)} IEEE ({_NODES_FILE})")
    if _noread or _ctrl:
        print(f"[HA] bekannt: {len(_noread)} Endpunkt(e) ohne Rücklesen, "
              f"{len(_ctrl)} reine Sender: {sorted(_ctrl) or '-'}")


def _nodes_save():
    try:
        tmp = _NODES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"addr_nodes": _addr_nodes, "ieee_nodes": _ieee_nodes,
                       "addr_ieee": _addr_ieee, "ieee_addr": _ieee_addr,
                       "addr_subs": _addr_subs,
                       "noread": sorted(_noread), "ctrl": sorted(_ctrl)},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, _NODES_FILE)
    except Exception as e:
        print(f"[HA] Knoten-Zuordnung nicht speicherbar: {e}")


def _ha_slug(s):
    return "".join(c if c.isalnum() else "_" for c in str(s)).strip("_").lower()

def _purge_addr(addr, why):
    """Alle retained Topics einer alten Kurzadresse entfernen.

    Ohne das bleibt nach jedem Rejoin ein kompletter zweiter Satz im Broker:
    am 2026-10-17 lagen 0xb2a2 und 0x747d parallel da (dieselbe IEEE!), und
    für ep1 existierte zusätzlich ein veralteter ep1/on_off-Wert neben on_off.
    """
    subs = _addr_subs.pop(addr, [])
    for sub in subs:
        mq_put(f"{BASE}/zigbee/{addr}/{sub}", "", retain=True, qos=1)
    gone = 0
    for topic in [t for t, a in _sent_addr.items() if a == addr]:
        mq_put(topic, "", retain=True, qos=1)
        _ha_sent.pop(topic, None)
        _sent_addr.pop(topic, None)
        gone += 1
    _addr_nodes.pop(addr, None)
    _ha_nodes.pop(addr, None)
    _addr_ieee.pop(addr, None)
    # Kenntnisse über DIESE Adresse mitnehmen – sonst gälte die alte Adresse
    # ewig als "nicht rücklesbar" und die neue würde neu beurteilt.
    for k in [k for k in _noread if k.split("/", 1)[0] == addr]:
        _noread.discard(k)
    for k in [k for k in _ctrl if k.split("/", 1)[0] == addr]:
        _ctrl.discard(k)
    if subs or gone:
        print(f"[HA] {addr} verworfen ({why}): {len(subs)} Zustands-Topics, "
              f"{gone} Discovery-Configs entfernt")
    _nodes_save()


def _ha_learn_ieee(addr, ieee):
    """IEEE zu einer Adresse merken und an die bestehende Node-ID binden.

    Damit findet ein Gerät, das später unter einer NEUEN Short-Adresse
    auftaucht, seine alte Entity wieder (statt einen zweiten Satz anzulegen).
    Die alte Adresse wird dabei komplett aus dem Broker entfernt.

    Wird bei JEDER zigbee-Meldung mit ieee aufgerufen (auch bei on_off-
    Reports). Deshalb nur schreiben, wenn sich wirklich etwas ändert –
    sonst würde pro Tastendruck die JSON-Datei neu geschrieben.
    """
    if not ieee:
        return
    changed = _addr_ieee.get(addr) != ieee

    prev = _ieee_addr.get(ieee)
    if prev and prev != addr:
        prev_node = _addr_nodes.get(prev) or _ha_nodes.get(prev)
        if prev_node:
            _ieee_nodes[ieee] = prev_node      # Name überlebt den Adresswechsel
            print(f"[HA] IEEE {ieee}: Kurzadresse {prev} → {addr}")
            # Was wir über das Gerät gelernt haben, gilt weiter – nur unter
            # der neuen Adresse. Sonst würde ein Panel nach jedem Rejoin
            # erneut drei Befehle pro Klick schicken.
            for s in (_noread, _ctrl):
                for k in [k for k in s if k.split("/", 1)[0] == prev]:
                    s.discard(k)
                    s.add(f"{addr}/{k.split('/', 1)[1]}")
            _purge_addr(prev, "Gerät hat neue Kurzadresse")
        changed = True
    _ieee_addr[ieee] = addr

    _addr_ieee[addr] = ieee
    _ha_devices.setdefault(addr, {})["ieee"] = ieee
    node = _ha_nodes.get(addr) or _addr_nodes.get(addr)
    if node and _ieee_nodes.get(ieee) != node:
        _ieee_nodes[ieee] = node
        changed = True
    if changed:
        _nodes_save()


def _ha_node(addr):
    """Node-ID – pro Adresse UND pro IEEE stabil.

    Reihenfolge:
      1. Adresse schon bekannt (Sitzung oder Datei) → diesen Namen behalten.
         Das friert bestehende Entities ein; keine Migration, keine Waisen.
      2. IEEE bekannt und schon vergeben (Gerät kam mit neuer Adresse
         zurück) → die ALTE Node-ID übernehmen und die neue Adresse darauf
         abbilden.
      3. Sonst neu bilden – aus der ADRESSE, genau wie vor 1.2.0.

    Warum in Schritt 3 nicht die IEEE? Weil das Bestandsinstallationen
    migrieren würde: bisher hiessen alle Knoten c6_0x07da. Würde jetzt
    bevorzugt die IEEE genommen, bekäme JEDES Gerät beim Update eine neue
    Node-ID – also exakt die Doppel-Entities, die diese Datei verhindern
    soll. Die IEEE-Tabelle dient nur dazu, den einmal vergebenen Namen
    nach einem Adresswechsel wiederzufinden.
    """
    node = _ha_nodes.get(addr) or _addr_nodes.get(addr)
    if node:
        _ha_nodes[addr] = node
        return node

    ieee = _ha_devices.get(addr, {}).get("ieee") or _addr_ieee.get(addr, "")
    if ieee and ieee in _ieee_nodes:
        node = _ieee_nodes[ieee]
        print(f"[HA] {addr} hat neue Kurzadresse → bestehende Entities "
              f"{node} übernommen (gleiche IEEE)")
    else:
        node = "c6_" + _ha_slug(addr)

    _ha_nodes[addr]   = node
    _addr_nodes[addr] = node
    if ieee:
        _ieee_nodes[ieee] = node
        _addr_ieee[addr]  = ieee
        _ha_devices.setdefault(addr, {})["ieee"] = ieee
    _nodes_save()
    return node


def _ha_state(addr, sub, p, retain=True):
    """Zustand auf das state_topic legen – retained.

    Vorher wurde ohne retain publiziert: nach jedem HA-Neustart standen alle
    Schalter wieder auf "unknown", weil weder Broker noch HA den letzten Wert
    hatte. Zusätzlich merkt sich _ha_last den Wert, damit ein "toggle" ohne
    Gerätereport in die richtige Richtung gedreht werden kann.
    """
    topic = f"{BASE}/zigbee/{addr}/{sub}"
    mq_put(topic, json.dumps(p), retain=retain, qos=1)
    if isinstance(p, dict):
        _ha_last.setdefault(addr, {})[sub] = p
    # Merken, welche Topics wir je gesetzt haben – nur damit lässt sich eine
    # alte Kurzadresse später restlos aufräumen (_purge_addr).
    subs = _addr_subs.setdefault(addr, [])
    if sub not in subs:
        subs.append(sub)
        _nodes_save()

def _ha_device(addr):
    d  = _ha_devices.get(addr, {})
    db = d.get("db") or {}
    ids = ["c6_" + _ha_slug(addr)]
    if d.get("ieee"):
        ids.append("c6_" + _ha_slug(d["ieee"]))
    return {
        "identifiers": ids,
        "name": db.get("model") or d.get("model") or ("Zigbee " + addr),
        "manufacturer": db.get("vendor") or d.get("mfr") or "未知厂商",
        "model": db.get("model") or d.get("model") or "未知型号",
    }

# Endpunkt-Namen aus der Z2M-DB (top/center/bottom/left/right …) → Anzeigename
_EP_ZH = {"top": "上", "center": "中", "bottom": "下", "left": "左", "right": "右",
          "top_left": "左上", "top_right": "右上",
          "bottom_left": "左下", "bottom_right": "右下"}

def _ha_ep_label(addr, epn):
    """'ep16' → '上' wenn die Z2M-Definition diesen Endpunkt so benennt."""
    db = _ha_devices.get(addr, {}).get("db") or {}
    for name, num in db.get("endpoints", {}).items():
        if f"ep{num}" == epn:
            return _EP_ZH.get(name, name)
    return epn.upper()

# sub-Basisname → (HA-Komponente, Anzeigename, value_template, unit, device_class, state_class)
_HA_MAP = {
    "on_off":      ("switch",        "开关",   "{{ value_json.v }}",                                    None, None,           None),
    "temperature": ("sensor",        "温度",   "{{ (value_json.raw / 100) | round(1) }}",               "°C", "temperature", "measurement"),
    "humidity":    ("sensor",        "湿度",   "{{ (value_json.raw / 100) | round(1) }}",               "%",  "humidity",    "measurement"),
    "illuminance": ("sensor",        "光照度", "{{ (10 ** ((value_json.raw - 1) / 10000)) | round(1) }}", "lx", "illuminance", "measurement"),
    "occupancy":   ("binary_sensor", "占用",   "{{ value_json.occ }}",                                  None, "occupancy",   None),
    "ias_zone":    ("binary_sensor", "报警",   "{{ value_json.motion }}",                               None, None,          None),
}

def _ha_publish(addr, sub, p):
    """Meldet (addr, sub) automatisch bei Home Assistant an (MQTT Discovery).

    Wird pro eingehendem Telegramm aufgerufen, publiziert die Config aber nur
    einmal pro (Node, Objekt) – mit retain, damit sie einen HA-Neustart
    überlebt. Kein YAML nötig: HA legt die Entity selbst an.
    """
    if not HA_ENABLED:
        return

    # ★ discovery 里引用了 {BASE}/dev/<addr>/status 这条 availability 主题，
    #   如果主题还没消息，availability_mode="all" 会让实体一开始就是不可用。
    #   所以发 config 之前先把状态补上（retained；去抖，不会每帧都发）。
    _avail_sync(addr)

    base_name = sub.rsplit("/", 1)[-1]
    epn       = sub.split("/", 1)[0] if "/" in sub else "ep1"
    node      = _ha_node(addr)
    dev       = _ha_device(addr)
    topic     = f"{BASE}/zigbee/{addr}/{sub}"
    # Endpunkte, die als reiner Sender erkannt wurden, bekommen KEINEN
    # Schalter zurück – sonst würde das nächste Telegramm die eben
    # abgemeldete Kachel sofort wieder anlegen (siehe _ha_drop_switch).
    if base_name == "on_off":
        try:
            _epn_num = int(epn[2:]) if epn.startswith("ep") else 1
        except ValueError:
            _epn_num = 1
        if _ep_key(addr, _epn_num) in _ctrl:
            return
    # "EP1" ist bei Einzel-Endpunkt-Geräten nur Rauschen im Namen
    ep_label  = _ha_ep_label(addr, epn)
    ep_part   = "" if ep_label == "EP1" else ep_label + " "
    avail     = _ha_avail(addr)

    objects = []   # (component, object_id, config)

    if base_name == "battery":
        if "pct" in p:
            objects.append(("sensor", f"{epn}_battery_pct", {
                "name": f"{ep_part}电量",
                "has_entity_name": True,
                "state_topic": topic,
                "value_template": "{{ value_json.pct }}",
                "device_class": "battery", "unit_of_measurement": "%",
                "state_class": "measurement", **avail,
            }))
        if "voltage_mv" in p:
            objects.append(("sensor", f"{epn}_battery_volt", {
                "name": f"{ep_part}电池电压",
                "has_entity_name": True,
                "state_topic": topic,
                "value_template": "{{ value_json.voltage_mv }}",
                "device_class": "voltage", "unit_of_measurement": "mV",
                "state_class": "measurement", **avail,
            }))
    elif base_name in _HA_MAP:
        comp, label, tpl, unit, dc, sc = _HA_MAP[base_name]
        # "has_entity_name": HA stellt dem Namen den Gerätenamen voran. Stand
        # der Gerätename vorher zusätzlich IM name-Feld, kam "HGZB-043
        # HGZB-043 上 开关" heraus. Also nur noch den Teil nennen, der das
        # Gerät unterscheidet (Endpunkt + Rolle).
        cfg = {
            "name": f"{ep_part}{label}",
            "has_entity_name": True,
            "state_topic": topic,
            "value_template": tpl,
            **avail,
        }
        if dc: cfg["device_class"] = dc
        if sc: cfg["state_class"]  = sc
        if unit: cfg["unit_of_measurement"] = unit
        if comp == "switch":
            cfg.update({
                "command_topic": f"{BASE}/set/{addr}/{epn}/on_off",
                "payload_on": "1", "payload_off": "0",
                "state_on": "1", "state_off": "0",
            })
        elif comp == "binary_sensor":
            cfg.update({"payload_on": "1", "payload_off": "0"})
        objects.append((comp, f"{epn}_{base_name}", cfg))

    for comp, obj_id, cfg in objects:
        # addr gehört mit in die Buchführung: wechselt das Gerät die
        # Kurzadresse, müssen state_topic/command_topic neu publiziert werden
        # und die alten Topics restlos verschwinden (_purge_addr).
        # Ob wirklich gesendet wird, entscheidet _disc_send am Inhalt.
        cfg["unique_id"] = f"{node}_{obj_id}"
        cfg["device"]    = dev
        cfg["origin"]    = _GW_ORIGIN
        # HA 2025.10 hat object_id durch default_entity_id abgelöst; Z2M
        # setzt beide, damit bestehende Entity-IDs die Migration überleben.
        cfg["default_entity_id"] = f"{comp}.{_ha_slug(node)}_{_ha_slug(obj_id)}"
        _disc_send(comp, node, obj_id, cfg, addr)

def _db_kind_of(entry):
    """'panel' | 'maybe' | 'switch' | None – Einordnung eines DB-Eintrags."""
    caps = entry.get("caps") or []
    desc = (entry.get("description") or "").lower()
    if ("scene" in desc or "selector" in desc) and ("switch" in caps or "action" in caps):
        return "panel"
    if "switch" in caps and "action" in caps:
        return "maybe"          # mehrdeutig, siehe _ha_db_hint
    if "switch" in caps or "light" in caps:
        return "switch"
    return None


def _ha_db_family(model):
    """Gleiche Baureihe in der DB suchen – für Modelle, die dort fehlen.

    FTB56+ZSN18ZY1.1 steht nicht in der DB, FTB56+ZSN15HG1.0 und
    FTB56+ZSN16HG1.0 schon („Smart N key scene wall switch“). Über das
    längste gemeinsame Präfix lässt sich die Einordnung erben.

    Das Präfix darf nicht zu kurz werden: „FTB56+“ enthält auch
    FTB56+SKT1BCW1.0 – eine Steckdose MIT Relais. Deshalb mindestens 9
    Zeichen, und nur bei einstimmigem Votum der ganzen Familie.
    """
    if not model:
        return None, []
    m = model.strip()
    for L in range(len(m) - 1, 8, -1):          # 9 Zeichen … len-1
        pre = m[:L]
        hits = [k for k in _ha_db if k.startswith(pre)]
        if hits:
            return pre, hits
    return None, []


def _ha_db_hint(addr):
    """Was die Z2M-Datenbank über die Bauart sagt – nur ein HINWEIS.

    Warum kein Urteil: die DB unterscheidet "Smart 2 key scene wall switch"
    von echten Schaltern nur über die Beschreibung. Die naheliegenden
    Felder taugen nicht (am 2026-10-17 gegen alle 3188 Einträge geprüft):

      * caps enthält "action": 31 Treffer, darunter MEAZON_BIZY_PLUG,
        DinRail 1-phase meter, Power switch S1, LK FUGA wiser wireless
        double relay – alles Geräte MIT Relais. Würde sie zu Panels machen.
      * endpoints leer: 219 der reinen switch-Modelle haben keine
        Endpunkt-Map, obwohl sie schalten (Z2M benennt sie nur nicht).
      * components=switch: 618 Modelle, davon nur 7 mit "scene" im Text.

    Und das Gerät, um das es hier geht (FTB56+ZSN18ZY1.1), steht gar nicht
    in der DB – deshalb der Familien-Umweg über _ha_db_family().
    Entschieden wird trotzdem über Descriptor/Probe, nie über diesen Text.
    """
    d = _ha_devices.get(addr, {})
    model = (d.get("model") or "").strip()
    entry = d.get("db") or _ha_db.get(model) or {}
    if entry:
        return _db_kind_of(entry), \
            f"DB: „{entry.get('description')}“ ({entry.get('model')})"

    pre, hits = _ha_db_family(model)
    if not pre:
        return None, ""
    kinds = {_db_kind_of(_ha_db[h]) for h in hits}
    kinds.discard(None)
    names = ", ".join(sorted(hits))[:56]
    if kinds == {"panel"}:
        return "panel", f"DB-Familie „{pre}*“ (nur Szenen-Panels: {names})"
    if kinds and kinds <= {"panel", "maybe"}:
        return "maybe", f"DB-Familie „{pre}*“ (Panel-Verdacht: {names})"
    return None, f"DB-Familie „{pre}*“ gemischt {sorted(kinds)} – kein Hinweis"


def _ha_prepublish(addr):
    """Meldet alle aus der Z2M-DB bekannten Endpunkte sofort an, damit das Gerät
    direkt nach dem Join in HA auftaucht – nicht erst nach dem ersten Tastendruck.
    Nur bei expliziter Endpoint-Map, sonst würden Phantom-Entities entstehen."""
    db  = _ha_devices.get(addr, {}).get("db") or {}
    eps = db.get("endpoints") or {}
    if not eps or "switch" not in (db.get("caps") or []):
        return
    for _name, num in sorted(eps.items(), key=lambda kv: kv[1]):
        _ha_publish(addr, f"ep{num}/on_off", {"v": 0})

def _ha_forget(addr):
    """Discovery-Cache für ein Gerät verwerfen (z.B. nach neuem Basic-Read),
    damit die Entities mit korrektem Modellnamen neu angemeldet werden."""
    node = _ha_node(addr)
    for t in [t for t in _ha_sent if f"/{node}/" in t]:
        _ha_sent.pop(t, None)


# ── Tastenereignisse ───────────────────────────────────────────────────────
# Der C6 gibt JEDEN eingehenden APS-Frame roh als {"t":"aps", …,"hex":…} aus.
# Damit lassen sich Tastendrücke im Add-on dekodieren, ohne im C6 eine
# cluster-spezifische Empfangslogik zu brauchen:
#
#   cluster 0x0006 (On/Off), Frame-Typ 0x01 (cluster-spezifisch),
#   Richtung Client→Server (Bit 0x08 NICHT gesetzt), cmd 0x00/0x01/0x02
#   cluster 0x0005 (Scenes), cmd 0x05 (recall), Payload = Gruppe(2) + Szene(1)
#
# Eine Default-Response des Geräts (cmd 0x0b) hat Bit 0x08 gesetzt und wird
# dadurch nie als Tastendruck gewertet – sonst würde jeder eigene Befehl ein
# Tastenereignis auslösen.
_ONOFF_ACT = {0x00: "off", 0x01: "on", 0x02: "toggle"}


def _event_types():
    """Erlaubte Werte der HA-event-Entity.

    ★ HA verwirft jedes Ereignis, dessen Wert NICHT in `event_types` steht
    („Invalid event type“ im Log, Entity bleibt unverändert). Die Liste muss
    also alle Werte im Voraus enthalten, die das Panel je senden kann – sie
    lässt sich nicht nachträglich erweitern, ohne die Entity neu anzulegen.

    Quelle für die Werte ist der Upstream-Converter des Geräts:
    `FTB56+ZSN15HG1.0` / `FTB56+ZSN16HG1.0` (nue_3a.ts) benutzen
    `fromZigbee: [fz.command_recall]` → `action = "recall_<sceneid>"`.
    Für den 2-Tasten-Betrieb ist die **Szenen-ID** das, was die Taste
    unterscheidet; die Endpunkt-Nummer trennt zusätzlich die Wippen.
    """
    t = ["on", "off", "toggle", "scene"]        # "scene" = Altbestand (1.2.2)
    t += ["recall_%d" % i for i in range(32)]   # genScenes recall
    t += ["store_%d" % i for i in range(32)]    # genScenes store
    return t


_EVENT_TYPES = _event_types()
ESP_ZB_CL_ON_OFF = 0x0006     # ZCL-Cluster On/Off
ESP_ZB_CL_SCENES = 0x0005     # ZCL-Cluster Scenes

# HA-Profil: Device-IDs, deren ROLLE das Schalten ist (…Switch). Ein Gerät
# mit so einer ID ist ein Sender, kein Aktor. Absichtlich knapp gehalten und
# nur als Zweitmerkmal benutzt – ein Fehlgriff hier kann keinen funktionierenden
# Schalter entfernen, weil dafür zusätzlich "kein Rücklesen" nötig ist.
_SWITCH_DEV_IDS = {0x0005, 0x0008, 0x0009, 0x000A}


def _ha_event_sub(ep):
    """Gleiche Namensregel wie überall: EP1 ohne Präfix."""
    return f"ep{ep}/action" if ep > 1 else "action"


# ★ HA verlangt für event-Entities ein JSON-Payload mit event_type-Element
#   (Dokumentation MQTT Event: "The JSON payload should contain the
#   event_type element"). Ein nacktes "on" ist kein JSON → HA verwirft es
#   STILL (Entity bleibt auf "unbekannt"). Genau deshalb zeigte die Kachel
#   nichts, obwohl die Nachrichten sichtbar im Broker ankamen.
#
# Kein Dedupe: Doppelte Ereignisse (zwei Frames pro Taste) sind ABSICHT des
# Geräts – der Nutzer hat es 2026-10-17 so bestätigt: eine ganz kurze
# Berührung löst kein Umschalten aus, wird aber trotzdem gemeldet. Das
# zweite identische Ereignis ist also ein echtes Ereignis, kein Artefakt.
def _action_payload(act):
    return json.dumps({"event_type": act})


def _ha_publish_event(addr, ep):
    """Event-Entity (Taste) für einen Endpunkt anmelden."""
    if not HA_ENABLED:
        return
    _avail_sync(addr)          # availability 主题要先存在，否则实体一开始不可用
    epn    = f"ep{ep}" if ep > 1 else "ep1"
    label  = _ha_ep_label(addr, epn)
    ep_part = "" if label == "EP1" else label + " "
    node   = _ha_node(addr)
    obj_id = f"{epn}_action"
    cfg = {
        "name": f"{ep_part}按键",
        "has_entity_name": True,
        "state_topic": f"{BASE}/zigbee/{addr}/{_ha_event_sub(ep)}",
        "event_types": list(_EVENT_TYPES),
        "device_class": "button",
        "unique_id": f"{node}_{obj_id}",
        "device": _ha_device(addr),
        "origin": _GW_ORIGIN,
        "default_entity_id": f"event.{_ha_slug(node)}_{_ha_slug(obj_id)}",
        **_ha_avail(addr),
    }
    _disc_send("event", node, obj_id, cfg, addr)


def _ha_button_from_report(addr, sub, p, was_expected=False):
    """Tastendruck aus einem on/off-**Report** ableiten.

    ★ Messung 2026-10-17 18:53 an 0x747d (FTB56+ZSN18ZY1.1): beim Drücken
    meldet das Panel NICHT die im Upstream-Converter hinterlegte Szene
    (`fromZigbee: [fz.command_recall]`), sondern schlicht das OnOff-Attribut
    des betroffenen Endpunkts – ep2 → `{"v": 1}`, 2 s später ep1 → `{"v": 1}`.

    Ein Report hat Frame-Type 0 (allgemein) und Direction Server→Client.
    `_ha_aps_action` verwirft beides mit Absicht (sonst würde jede Antwort
    auf unseren eigenen Befehl als Tastendruck gelten). Genau deshalb kam
    der Tastendruck dort nie an – er ist keine Kommando-Nachricht.

    Nur für Endpunkte, die als reiner Sender eingestuft sind (`_ctrl`):
    bei einem echten Schalter ist ein Report eine Zustandsänderung, kein
    Tastendruck und darf kein Ereignis erzeugen.
    """
    if not HA_ENABLED or not sub.endswith("on_off"):
        return
    ep = _sub_ep(sub)
    if ep is None:
        return
    k = _ep_key(addr, ep)
    if k not in _ctrl:
        # Zweitweg: das Gerät ist bereits als Panel bekannt (irgendein
        # Endpunkt in _ctrl) UND die Modellfamilie ist einstimmig ein
        # Szenen-Panel. Deckt den Fall ab, dass der Simple-Descriptor nur
        # einen Endpunkt geliefert hat, die andere Wippe aber trotzdem
        # Tasten meldet. Die Familienbedingung ist streng (2/2 einstimmig),
        # damit kein echtes Relais versehentlich Tastendrücke erzeugt.
        if _ha_db_hint(addr)[0] != "panel":
            return
        if not any(x.split("/", 1)[0] == addr for x in _ctrl):
            return
    if was_expected:
        # Antwort auf unser eigenes Read-Attributes → kein Tastendruck
        return
    v = (p or {}).get("v")
    if v is None:
        return
    act = "on" if int(v) else "off"
    topic = f"{BASE}/zigbee/{addr}/{_ha_event_sub(ep)}"
    mq_put(topic, _action_payload(act), retain=False, qos=1)
    print(f"[MQTT] {topic} {act}   (Taste ep{ep} aus Report)")
    _ha_publish_event(addr, ep)


def _ha_drop_switch(addr, ep, why):
    """switch-Entity eines reinen Senders wieder abmelden.

    Ein Panel ohne Relais nimmt On/Off-Befehle an, quittiert sie mit SUCCESS
    und tut nichts – die switch-Kachel in HA verspricht also etwas, das es
    nicht gibt. Deshalb wird sie entfernt und durch die event-Entity ersetzt.
    Rückgängig: "addr/ep" aus "ctrl" in /data/ha_nodes.json löschen, Add-on
    neu starten.
    """
    if not HA_ENABLED:
        return
    epn    = f"ep{ep}" if ep > 1 else "ep1"
    node   = _ha_node(addr)
    obj_id = f"{epn}_on_off"
    topic  = f"{HA_PREFIX}/switch/{node}/{obj_id}/config"
    mq_put(topic, "", retain=True, qos=1)
    _ha_sent.pop(topic, None)
    _sent_addr.pop(topic, None)
    print(f"[HA] {addr} ep{ep}: switch/{node}/{obj_id} abgemeldet – {why}")


_dropped = set()       # "addr/ep" – switch-Entity in dieser Laufzeit schon abgemeldet


def _classify(addr, ep, why=""):
    """Aus den zwei Merkmalen die richtige Entity-Form ableiten.

    Regeln (beide Merkmale werden automatisch erhoben, keins von Hand):

      A  OnOff-Attribut rücklesbar  → echter Schalter (switch)
      B  Gerät schickt selbst Tastenbefehle → reiner Sender (event)
      B + kein Rücklesen (A verletzt) → Panel ohne Relais: switch abmelden
      B + Rücklesen möglich          → Gerät MIT Relais, das zusätzlich
                                       Tasten sendet: switch UND event behalten

    Aufrufstellen: _ha_mark_controller (Merkmal B neu), _noread_add
    (Merkmal A widerlegt) und _classify_from_desc (Simple-Descriptor).
    """
    k = _ep_key(addr, ep)
    if k not in _ctrl:
        return
    _ha_publish_event(addr, ep)
    if k in _noread and k not in _dropped:
        _dropped.add(k)
        _ha_drop_switch(addr, ep, why or "Gerät ist reiner Sender (kein Rücklesen, "
                                        "schickt aber eigene Tastenbefehle)")


def _ha_mark_controller(addr, ep):
    """Endpunkt als reinen Sender einstufen (Merkmal B)."""
    k = _ep_key(addr, ep)
    if k in _ctrl:
        _classify(addr, ep)
        return
    _ctrl.add(k)
    _nodes_save()
    if k in _noread:
        print(f"[HA] {addr} ep{ep}: schickt eigene Tastenbefehle und ist nicht "
              f"rücklesbar → Tastenpanel ohne Relais")
    else:
        print(f"[HA] {addr} ep{ep}: schickt eigene Tastenbefehle – "
              f"als Taste angemeldet (Schalter bleibt, Rücklesen geht)")
    _classify(addr, ep)


# ── Automatische Einstufung beim Join ─────────────────────────────────────
# Zwei unabhängige Quellen, damit nichts von Hand entschieden werden muss:
#
#  1. Simple-Descriptor (kommt beim Interview): steht 0x0006 in der
#     INPUT-Liste, ist das Gerät Server für On/Off = echter Schalter.
#     Steht 0x0006/0x0005 nur in der OUTPUT-Liste, kann es ausschließlich
#     Befehle SCHICKEN = Tastenpanel.
#  2. Probe: ein Read-Attributes je Endpunkt. Kommt ein Wert → Schalter.
#     Kommt nichts → kein Attribut vorhanden → Panel.
#
# Quelle 1 ist eindeutig, kommt aber nur beim Join. Quelle 2 läuft immer und
# heilt eine falsche Einstufung wieder (siehe _pending_ack → _noread_del).
_PROBE_AFTER = 2.5     # s nach "interview done"
_PROBE_WAIT  = 4.0     # s Frist für die Antwort
_probe = {}            # (addr, ep) → Ablaufzeit
_desc  = {}            # addr → {ep: (in_cluster, out_cluster)} aus dem Simple-Desc


def _probe_one(addr, ep):
    key = (addr, ep)
    with _pending_lock:
        if key in _probe:
            return
        _probe[key] = time.time() + _PROBE_WAIT
    _send_read_onoff(addr, ep)
    threading.Timer(_PROBE_WAIT + 0.2, _probe_done, (addr, ep)).start()


def _probe_start(addr, eps):
    """Nur Endpunkte mit On/Off-Cluster probieren.

    Ein Temperatursensor hat kein OnOff-Attribut; ihn zu fragen ergäbe
    "nicht rücklesbar" und würde das Log mit Meldungen fluten, die für ihn
    völlig bedeutungslos sind. Der Simple-Descriptor sagt vorher, wer hier
    überhaupt ein On/Off hat – nur die werden geprüft.
    """
    if not eps:
        return
    known = _desc.get(addr) or {}
    hint, _why = _ha_db_hint(addr)
    todo  = []
    for ep in sorted(set(int(e) for e in eps)):
        if ep <= 0:
            continue
        cl = known.get(ep)
        if cl is None:
            # Kein Descriptor (Interview unvollständig). Nur dann auf die
            # DB verlassen, wenn sie das Gerät als Panel ausweist – sonst
            # nicht raten.
            if hint == "panel":
                todo.append(ep)
            continue
        if ESP_ZB_CL_ON_OFF in cl[0] or ESP_ZB_CL_ON_OFF in cl[1]:
            todo.append(ep)
    if not todo:
        return
    print(f"[HA] {addr}: prüfe OnOff-Rücklesbarkeit auf {todo}")
    for i, ep in enumerate(todo):
        threading.Timer(_PROBE_AFTER + i * 0.4, _probe_one, (addr, ep)).start()


def _probe_seen(addr, ep):
    """Es ist ein echter Zustand eingetroffen → Endpunkt ist lesbar."""
    key = (addr, ep)
    with _pending_lock:
        hit = _probe.pop(key, None)
    if hit is not None:
        print(f"[HA] {addr} ep{ep}: antwortet auf Read-Attributes → echter Schalter")
    if _ep_key(addr, ep) in _noread:
        _noread_del(addr, ep)


def _probe_done(addr, ep):
    key = (addr, ep)
    with _pending_lock:
        if key not in _probe:
            return                       # es kam eine Antwort
        _probe.pop(key, None)
    _noread_add(addr, ep, "OnOff-Attribut beim Join nicht lesbar")


# ── 型号补读（ZCL Basic 0x0004 厂商 / 0x0005 型号）────────────────────────
# ★ 为什么需要：型号**只在入网那一刻**（interview）读一次，读完网关就把
#   interviewed=true 写进 NVS，之后永远不会再读。设备要是那一次没答上
#   （休眠端点在睡、刚入网就抖、或者当年固件还没有「逐端点扫 Basic」这段），
#   型号就**永久缺失**，而且没有任何重试。
#   实测（2026-10-26）：经中继的 0x159e 就是这样 —— 它不是通讯不通，
#   手动发一条 probe 型号立刻回来（Feibit Inc co. / FB56+ZSW1GKJ2.5），
#   纯粹是当年那次没读到、之后没人再问。
#   所以这里定期给「有 cluster 数据但没有 basic」的设备补一次 probe，
#   指数退避（90 s 起，最长 6 h 一轮），读到就自动停。
_MB_FIRST   = 90          # s，第一次失败后多久重试
_MB_MAXWAIT = 6 * 3600    # 最长退避
_MB_PER_ROUND = 3         # 每轮最多补几台，别把串口刷满
_mb_next  = {}            # addr → 下次允许尝试的 ts
_mb_tries = {}            # addr → 累计尝试次数


def _eps_of(dev):
    """从 cluster 键（`ep16/on_off`）抠出端点号；抠不到就按 ep1 试。"""
    eps = []
    for k in (dev.get("clusters") or {}):
        if k.startswith("ep"):
            try:
                eps.append(int(k[2:].split("/")[0]))
            except ValueError:
                pass
    return sorted(set(eps)) or [1]


def _model_missing(dev):
    b = (dev.get("clusters") or {}).get("basic")
    if isinstance(b, dict):
        if ((b.get("payload") or {}).get("model") or "").strip():
            return False
    return True


def _probe_basic(addr, eps=None):
    """让网关读一次 Basic（0x0004/0x0005）。逐端点、间隔 0.9 s 发。"""
    with _state_lock:
        dev = json.loads(json.dumps((_state.get("devices") or {}).get(addr) or {}))
    eps = sorted(set(int(e) for e in (eps or _eps_of(dev))))
    for i, ep in enumerate(eps):
        threading.Timer(i * 0.9, lambda e=ep: mq_put(
            f"{BASE}/cmd/probe", f"{addr},{e}", qos=1)).start()
    print(f"[PROBE] {addr} 读 Basic（厂商/型号）@ ep{eps}")
    return eps


def _model_backfill_once():
    now = time.time()
    todo = []
    with _state_lock:
        for a, d in (_state.get("devices") or {}).items():
            if not _model_missing(d):
                _mb_next.pop(a, None)     # 读到了，清掉退避状态
                _mb_tries.pop(a, None)
                continue
            if now < _mb_next.get(a, 0):
                continue
            todo.append(a)
    for a in todo[:_MB_PER_ROUND]:
        try:
            _probe_basic(a)
        except Exception as e:             # noqa: BLE001
            print(f"[PROBE] {a}: {e}")
            continue
        n = _mb_tries.get(a, 0) + 1
        _mb_tries[a] = n
        _mb_next[a]  = now + min(_MB_MAXWAIT, _MB_FIRST * (2 ** (n - 1)))
    if todo:
        print(f"[PROBE] 缺型号 {len(todo)} 台，本轮补 {min(len(todo), _MB_PER_ROUND)} 台")


def _model_backfill_task():
    time.sleep(45)          # 等串口就绪、设备表从 NVS 载入完
    while True:
        try:
            _model_backfill_once()
        except Exception as e:             # noqa: BLE001
            print(f"[PROBE] backfill: {e}")
        time.sleep(180)


def _classify_from_desc(addr, ep, in_cl, out_cl, dev_id=0):
    """Einstufung aus dem Simple-Descriptor (eindeutig, kommt beim Join).

    Der Descriptor ist die belastbarste Auskunft, die ein Zigbee-Gerät über
    sich selbst gibt – belastbarer als Hersteller/Modell, weil er aus dem
    ZDO-Interview stammt und nicht aus einer Textdatenbank:
        profile_id, device_id, Input-Cluster (Server), Output-Cluster (Client).

    Entscheidend ist die RICHTUNG von Cluster 0x0006:
        Input/Server  → das Gerät hält einen On/Off-Zustand (schaltbar)
        Output/Client → das Gerät SCHICKT On/Off, hat keinen eigenen Zustand
    Zusätzlich wird die Device-ID mitgeschrieben; sie dient als zweites
    Merkmal, wenn die Cluster-Listen nichts hergeben.
    """
    k = _ep_key(addr, ep)
    # Für die Join-Probe merken, wer überhaupt ein On/Off hat.
    _desc.setdefault(addr, {})[ep] = (list(in_cl), list(out_cl))
    if ESP_ZB_CL_ON_OFF in in_cl:
        # On/Off als Server-Cluster: das Gerät hält einen Zustand → Schalter.
        if k in _ctrl:
            _ctrl.discard(k)
            _nodes_save()
            print(f"[HA] {addr} ep{ep}: On/Off ist Server-Cluster → "
                  f"doch ein echter Schalter (Einstufung revidiert)")
        _noread_del(addr, ep)
        return
    if (ESP_ZB_CL_ON_OFF in out_cl or ESP_ZB_CL_SCENES in out_cl
            or dev_id in _SWITCH_DEV_IDS):
        _what = ("On/Off nur Client-Cluster" if ESP_ZB_CL_ON_OFF in out_cl
                 else "Scenes nur Client-Cluster" if ESP_ZB_CL_SCENES in out_cl
                 else "Device-ID der Schalter-Klasse")
        print(f"[HA] {addr} ep{ep}: {_what} "
              f"(device=0x{dev_id:04x}, in={in_cl}, out={out_cl}) → Tastenpanel")
        _ha_mark_controller(addr, ep)


def _zcl_head(b):
    """ZCL-Header zerlegen → (fc, mfr, tsn, cmd, payload_start) oder None.

    ★ Frame-Control-Bit 2 (0x04) = manufacturer specific. Dann steht ein
    2-Byte-Herstellercode ZWISCHEN Frame-Control und TSN:

        fc(1) | mfr(2, LSB first) | tsn(1) | cmd(1) | payload…

    Die frühere Fassung las `cmd` immer aus `b[2]`. Bei einem
    herstellerspezifischen Frame ist das das LOW-Byte des Herstellercodes –
    also praktisch nie 0x00/0x01/0x02 (On/Off) oder 0x05 (Scene recall).
    Folge: der Tastendruck wurde **stillschweigend verworfen**, ohne jede
    Logzeile. Genau das passt zum Symptom „Taste wird nicht gelesen“.
    """
    if len(b) < 3:
        return None
    fc = b[0]
    i = 1
    mfr = None
    if fc & 0x04:                      # manufacturer specific
        if len(b) < 5:
            return None
        mfr = b[1] | (b[2] << 8)
        i = 3
    if len(b) < i + 2:
        return None
    return fc, mfr, b[i], b[i + 1], i + 2


_aps_unbekannt = set()   # (cl, ftype, cmd) – schon protokolliert (kein Spam)


def _ha_aps_action(msg):
    """Tastendruck aus einem rohen APS-Frame erkennen und nach HA geben."""
    if not HA_ENABLED:
        return
    hx = msg.get("hex") or ""
    if len(hx) < 6:
        return
    try:
        b = bytes.fromhex(hx)
    except ValueError:
        return
    head = _zcl_head(b)
    if not head:
        return
    fc, mfr, tsn, cmd, ps = head
    if fc & 0x08:            # Server→Client = Antwort auf unseren Befehl
        return
    ftype = fc & 0x03        # 0 = allgemein, 1 = clusterspezifisch
    cl   = int(str(msg.get("cl") or "0"), 0)
    sep  = int(msg.get("sep") or 0)
    addr = msg.get("src") or ""
    if not addr or sep == 0:
        return
    act = None
    if cl == ESP_ZB_CL_ON_OFF and ftype == 0x01 and cmd in _ONOFF_ACT:
        act = _ONOFF_ACT[cmd]
    elif cl == ESP_ZB_CL_SCENES and ftype == 0x01 and len(b) >= ps + 3:
        # recall(0x05) / store(0x04): groupid(2, LSB first) + sceneid(1)
        grp = b[ps] | (b[ps + 1] << 8)
        sid = b[ps + 2]
        if cmd == 0x05:
            act = "recall_%d" % sid
        elif cmd == 0x04:
            act = "store_%d" % sid
        if act:
            print(f"[HA] {addr} ep{sep}: Szene {'recall' if cmd == 0x05 else 'store'}"
                  f" – Gruppe 0x{grp:04x}, Szene {sid}"
                  + (f", Herstellercode 0x{mfr:04x}" if mfr is not None else ""))
    if not act:
        # Von einem Endpunkt, der als Sender gilt, sind Tastendrücke zu
        # erwarten. Was wir nicht zuordnen können, wird EINMAL protokolliert –
        # sonst ist die nächste Fehlermeldung wieder blind.
        if _ep_key(addr, sep) in _ctrl or _ha_db_hint(addr)[0] == "panel":
            key = (cl, ftype, cmd)
            if key not in _aps_unbekannt:
                _aps_unbekannt.add(key)
                print(f"[HA] {addr} ep{sep}: unerkannte Tasten-Nachricht "
                      f"cl=0x{cl:04x} type={ftype} cmd=0x{cmd:02x} "
                      f"mfr={'0x%04x' % mfr if mfr is not None else '-'} "
                      f"hex={hx} – wird nicht ausgewertet")
        return
    topic = f"{BASE}/zigbee/{addr}/{_ha_event_sub(sep)}"
    mq_put(topic, _action_payload(act), retain=False, qos=1)
    print(f"[MQTT] {topic} {act}")
    _ha_publish_event(addr, sep)
    _ha_mark_controller(addr, sep)

# ── Dispatcher ─────────────────────────────────────────────────────────────
# ── 网络拓扑：直连还是经中继 ───────────────────────────────────────────────
# 数据来自协调器的三张表（见 main/zb_topo.c）：
#   nbr 邻居表  = 协调器直接听得到谁（含 LQI/RSSI/关系/深度）
#   rt  路由表  = 到某地址的**下一跳**（最可靠的「经谁转发」证据）
#   rr  源路由  = 从协调器到目的的完整路径（多级中继）
#
# ★ 判读优先级：rt.next_hop > rr.path[0] > nbr.relationship。
#   原因：路由表是转发时真正用到的表项；邻居表只说明「听得到」，不代表
#   那台设备的父节点就是协调器（路由器的邻居关系是 sibling，不等于父子）。
#
# ★ Zigbee 是按需建路，路由表只保留近期用过的路径（几十秒后过期）。
#   所以刷新拓扑时要先给每台设备发一帧（node_desc）逼出路由，再 dump。

_TOPO_MAXW = 980          # SVG 画布最大宽
_TOPO_MINS = 440          # 节点很少时也别缩得太窄
_TOPO_PAD  = 56           # 左右留白（圆半径 + 型号文字外扩）
_TOPO_XGAP = 150          # 相邻同级节点的最小水平间距
_TOPO_YGAP = 130          # 层间距


def _topo_ingest(kind, msg):
    """把网关发来的四类表 + 汇总头收进 _state["topo"]。

    topo_mesh = 每台设备**自己上报**的邻居表（Mgmt_Lqi_rsp），这是拓扑的
    主证据：里面 rel=0 那条就是它的父节点，lqi 是这一跳的链路质量。
    其余三张表是协调器 NIB 的快照，只当兜底。
    """
    with _state_lock:
        tp = _state["topo"]
        if kind == "topo_nbr":
            tp["nbr"] = msg.get("n") or []
            # ★ 邻居表 = 协调器**刚刚听见的**名单。顺手在这里"种设备"：
            #   进程重启后设备表是空的（只由上报驱动），用户会以为"设备没了"。
            #   邻居表里有的先挂个占位，等它真上报再把 clusters 补齐。
            coord = tp.get("addr") or "0x0000"
            for n in tp["nbr"]:
                a = n.get("a")
                if not a or a == coord:
                    continue
                _avail_touch(a)          # 协调器刚听见它 = 存活的硬证据
                dev = _state["devices"].setdefault(a, {
                    "ieee": "", "name": a, "last_seen": "",
                    "clusters": {}, "synthetic": True,
                })
                # 邻居表里的 lqi/rssi 是**这一条链路**的实测值，比心跳里那个
                # "radio 最近一帧"可信。设备自己没有有效值时就用它补上。
                if dev.get("lqi") in (None, 0) and "lqi" in n:
                    dev["lqi"] = n["lqi"]
                    dev["lqi_src"] = "nbr"
                if dev.get("rssi") is None and "rssi" in n:
                    dev["rssi"] = n["rssi"]
        elif kind == "topo_rt":
            tp["rt"] = msg.get("n") or []
        elif kind == "topo_rr":
            tp["rr"] = msg.get("n") or []
        elif kind == "topo_mesh":
            a = msg.get("a")
            if a:
                _avail_touch(a)              # 它自己答了 Mgmt_Lqi = 活着
                tp.setdefault("mesh", {})[a] = {
                    "st":  msg.get("st", 0),
                    "tot": msg.get("tot", 0),
                    "nb":  msg.get("nb") or [],
                    "rd":  tp.get("round", 0),   # 本轮序号，收尾时用来淘汰旧条目
                }
        else:
            tp["pan"]  = msg.get("pan", tp["pan"])
            tp["ch"]   = msg.get("ch", tp["ch"])
            tp["addr"] = msg.get("addr", tp["addr"])
            if msg.get("done"):
                # ★ 只在「设备真的不在了」时才剔掉自报表。以前按「本轮有没有
                #   报到」剔，但 Mgmt_Lqi 的回包比汇总消息慢好几秒（实测 4 台
                #   里有 3 台在 done 之后才到）⇒ 每轮都把 4 台砍成 1 台，
                #   拓扑图上一半的连线来回闪。现在只清掉已经从设备表消失的地址。
                mesh = tp.get("mesh") or {}
                keep_addrs = set((_state.get("devices") or {}).keys())
                kept = {a: e for a, e in mesh.items() if a in keep_addrs}
                dropped = [a for a in mesh if a not in kept]
                tp["mesh"] = kept
                tp["done"] = True
                tp["ts"]   = _now()
                tp["done_at"] = time.time()   # 供拓扑页显示「几秒前采的」
                print("[TOPO] 邻居 %d / 路由 %d / 源路由 %d / 设备自报 %d%s"
                      % (len(tp["nbr"]), len(tp["rt"]), len(tp["rr"]),
                         len(tp["mesh"]),
                         ("（设备已不在表中、已剔除: %s）" % " ".join(dropped))
                         if dropped else ""))


def _topo_refresh(wait_per_dev=0.45):
    """让网关逐台做 Mgmt_Lqi 采集（顺便先发 node_desc 触发路由发现）。

    串口写不阻塞 HTTP 线程 → 整个流程放后台线程跑。
    """
    # ★ done=False 必须在**起线程之前**同步置好。自动采集靠轮询 done 判断
    #   本轮是否结束；如果还留在 _run() 里异步置位，"刚起线程就去检查"会读到
    #   上一轮遗留的 True，误判成本轮已完成 → 采集会越叠越多。
    with _state_lock:
        addrs = sorted(_state["devices"].keys())
        # 新一轮采集：**不清空 mesh**，只把序号 +1。旧条目还留着（图不会闪成
        # 空白），等本轮 done 时再淘汰没被重新采到的 —— 见 _topo_ingest()。
        _state["topo"]["round"] = int(_state["topo"].get("round", 0)) + 1
        _state["topo"]["done"] = False

    def _run():
        for a in addrs:
            try:
                ser.write(_js({"cmd": "node_desc", "addr": a}).encode())
            except Exception as e:                     # noqa: BLE001
                print(f"[TOPO] node_desc {a} 失败: {e}")
                break
            time.sleep(wait_per_dev)
        try:
            delay = int(len(addrs) * wait_per_dev * 1000) + 1200
            ser.write(_js({"cmd": "topo", "delay": delay}).encode())
            print(f"[TOPO] 已请求拓扑（{len(addrs)} 台，等 {delay} ms 后逐台采集）")
        except Exception as e:                         # noqa: BLE001
            print(f"[TOPO] topo 请求失败: {e}")

    threading.Thread(target=_run, daemon=True).start()


# ── 定时自动采集拓扑 ────────────────────────────────────────────────────────
# 为什么放服务端、而不是页面里 setInterval：
#   拓扑页自己有 15 s 整页重载，但重载只重新读 _state["topo"]，
#   **不会触发新的采集** —— 所以图上永远是旧数据，除非人手点一次刷新。
#   在前端放计时器也不行：15 s 的重载会把计时器一起清掉，永远到不了 60 s。
#   放在服务端就没这个矛盾，页面照常重载就能看到新值（效果类似 HA 的 BLE 拓扑）。
_topo_busy = threading.Lock()


def _topo_refresh_blocking(timeout: float = 40.0) -> bool:
    """触发一轮采集并等它结束；上一轮还没完就跳过，避免采集叠加。"""
    if not _topo_busy.acquire(blocking=False):
        print("[TOPO] 上一轮还没结束，跳过本轮自动采集")
        return False
    try:
        _topo_refresh()
        deadline = time.time() + timeout
        while time.time() < deadline:
            with _state_lock:
                if _state["topo"].get("done"):
                    return True
            time.sleep(0.5)
        print("[TOPO] 自动采集超时（done 没置位）")
        return False
    finally:
        _topo_busy.release()


def _topo_auto_ready() -> str:
    """本轮能不能采。返回 "" = 可以，否则返回跳过原因（只用于日志）。"""
    with _state_lock:
        st    = _state.get("status")
        coord = (_state.get("topo") or {}).get("addr") or "0x0000"
        # 别把协调器自己算成"一台设备"
        n     = len([a for a in (_state.get("devices") or {}) if a != coord])
    if st != "online":
        return f"网关状态 {st!r}，还没上线"
    if n == 0:
        return "还没有任何设备上报，扫了也是空图"
    return ""


def _topo_auto_loop(interval: int):
    # 开机先等一轮心跳：否则 _state["devices"] 还是空的，会扫 0 台设备，
    # 而且要等满一个 interval 才有第一张图。
    time.sleep(min(interval, 30))
    while True:
        try:
            why = _topo_auto_ready()
            if why:
                print(f"[TOPO] 本轮跳过：{why}")
            elif _topo_refresh_blocking():
                print(f"[TOPO] 自动采集完成，{interval} s 后再来一轮")
        except Exception as e:                          # noqa: BLE001
            print(f"[TOPO] 自动采集出错: {e}")
        time.sleep(interval)


def _topo_links(tp):
    """把所有已知的「谁听得见谁」汇总成无向链路图。

    返回 {(a, b)} → {"lqi": int|None, "n": 见到这条链路的次数}，地址对按
    字典序归一（a < b），所以同一条链路从两端各报一次只会合并成一个键，
    n == 2 就说明**双方互相都听得见**（比单方面汇报可信）。

    来源：
      · 协调器自己的邻居表（它直接听得到的设备）
      · 每台设备自报的邻居表（Mgmt_Lqi_rsp）

    ★ 为什么不用 rel / depth 直接读父节点：实测（2026-10-26）本网络的
      Feibit 开关回 0x84 NOT_SUPPORTED（根本不支持 Mgmt_Lqi），而支持它的
      ESP32-C6 中继虽然答了，但 rel 一律是 2(sibling)、depth 一律是 0 ——
      ZBOSS 没维护这两个字段。所以只能退回「实测链路 + 图搜索」，
      这条路不依赖任何设备自愿汇报父子关系。
    """
    coord = tp.get("addr") or "0x0000"
    links = {}

    def _add(x, y, lqi):
        if not x or not y or x == y:
            return
        k = (x, y) if x < y else (y, x)
        e = links.setdefault(k, {"lqi": None, "n": 0})
        e["n"] += 1
        if isinstance(lqi, int) and lqi > 0:
            e["lqi"] = lqi if e["lqi"] is None else max(e["lqi"], lqi)

    for n in tp.get("nbr") or []:
        _add(coord, n.get("a"), n.get("lqi"))
    for a, m in (tp.get("mesh") or {}).items():
        for e in m.get("nb") or []:
            _add(a, e.get("a"), e.get("lqi"))
    return links


def _topo_rel(tp, claims=None):
    """算出 addr → 父节点。

    返回 (parent, how, hop_lqi, depth, links)。

    算法（每次刷新都重算，纯函数，没有隐藏状态）：
      ① 在**实测链路图**上从协调器做 BFS：父节点 = 深度最小的可达邻居，
         同一层里挑 **LQI 最高**的那条（链路越好越可能真的在用它转发）。
      ② 设备**明确自报** rel==0 的父节点，只在「更靠近协调器」时才用来改写
         ①的结果 —— 自报值不校验会造出环（实测用例：某路由器自报自己的
         终端子节点是爹，整棵树倒挂）。
      ③ 完全不可达的设备 → 不在 parent 里，页面显示「未定位」。
      ④ `claims`（中继固件自报的父节点，0xFC00/0x0002）优先级最高，
         直接改写 ①，只做防环校验。

    ★ 都不依赖路由表。旧实现死磕路由表和邻居表，而这两张表按 Zigbee
      的设计就拿不到父子关系：邻居表只装一跳，路由表按需建、直接邻居
      根本不建表项（`rt` 常为 0）。
    """
    coord = tp.get("addr") or "0x0000"
    parent, how, hop_lqi, depth = {}, {}, {}, {}
    mesh = tp.get("mesh") or {}
    links = _topo_links(tp)

    # ① 建邻接表（LQI 高的在前 = 同层择优），从协调器 BFS 出生成树
    adj = {}
    for (x, y), e in links.items():
        adj.setdefault(x, []).append((y, e))
        adj.setdefault(y, []).append((x, e))
    for v in adj.values():
        v.sort(key=lambda t: (t[1].get("lqi") or 0), reverse=True)

    depth[coord] = 0
    queue = [coord]
    seen = {coord}
    while queue:
        cur = queue.pop(0)
        for nxt, e in adj.get(cur, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            parent[nxt]  = cur
            how[nxt]     = "链路图·直连" if cur == coord else "链路图·经中继"
            hop_lqi[nxt] = e.get("lqi")
            depth[nxt]   = depth.get(cur, 0) + 1
            queue.append(nxt)

    # ② 设备自报的父节点只能「改写」已测到的父节点，必须更靠近协调器
    for a, m in mesh.items():
        if not a or a == coord or a not in depth:
            continue
        for e in m.get("nb") or []:
            p = e.get("a")
            if (e.get("rel") == 0 and p and p != a
                    and p in depth and depth[p] < depth[a]):
                parent[a]  = p
                how[a]     = "设备自报父节点"
                hop_lqi[a] = e.get("lqi")
                break

    # ③ 中继固件**自己报**的父节点（zb_nwk_get_parent → 0xFC00/0x0002）。
    #    这是最终裁决：设备自己说的，比任何推断都准。唯一要防的是成环。
    def _cyc(a, p):
        cur, n = p, 0
        while cur and n < 32:
            if cur == a:
                return True
            cur = parent.get(cur)
            n += 1
        return False

    for a, p in sorted((claims or {}).items()):
        if not a or a == coord or not p or p == "0xffff" or _cyc(a, p):
            continue
        parent[a]  = p
        how[a]     = "设备自报父节点（中继自己报的）"
        hop_lqi[a] = (links.get((a, p) if a < p else (p, a)) or {}).get("lqi")

    # 改写后重算深度（沿 parent 链上溯，最多 32 层防环）
    for a in list(parent):
        d, cur, n = 1, parent.get(a), 0
        while cur and cur != coord and n < 32:
            d += 1
            cur = parent.get(cur)
            n += 1
        depth[a] = d

    parent.pop(coord, None)
    depth.pop(coord, None)
    return parent, how, hop_lqi, depth, links


def _topo_svg(tp, names=None, meta=None, claims=None):
    """把 parent 关系画成一棵树。返回 (svg, parent, how, hop_lqi, depth, links)。

    边的标注用**子设备自己上报的**这一跳 LQI（Mgmt_Lqi_rsp），而不是协调器
    邻居表里那条「最后一帧」的旧值 —— 后者只能反映协调器能不能听见它，
    中间隔了一台路由器时完全不对。

    names: {addr: 型号}，节点标签优先显示型号（短、能认人），没有就退回地址；
           地址永远在鼠标悬停卡片里（和蓝牙设备列表一个习惯）。
    meta:  {addr: {"ieee","rssi"}}，只给悬停卡片补充信息，缺了不影响画图。
    """
    names = names or {}
    meta = meta or {}
    coord = tp.get("addr") or "0x0000"
    parent, how, hop_lqi, depth, links = _topo_rel(tp, claims)
    nbr  = {n["a"]: n for n in (tp.get("nbr") or []) if n.get("a")}
    mesh = tp.get("mesh") or {}

    nodes = {coord} | set(parent) | set(nbr) | set(mesh)
    for (x, y) in links:
        nodes.add(x)
        nodes.add(y)
    # 完全不可达的设备（既不在协调器邻居表、也没被任何设备报过）：BFS 到不了它，
    # parent 里没有它。旧实现直接 get(a, coord) 会把它画成协调器的直连子节点 ——
    # 那是**编造出来的边**。这里单独标出来：只放位置、不画线。
    orphan = {a for a in nodes if a != coord and a not in parent}
    kids = {a: [] for a in nodes}
    for a in sorted(nodes):
        if a == coord:
            continue
        p = parent.get(a, coord)
        if p not in kids:
            p = coord
        kids[p].append(a)

    # 子树宽度（以「一个叶位」为单位）
    width = {}

    def _w(a):
        ch = kids.get(a, [])
        width[a] = sum(_w(c) for c in ch) if ch else 1
        return width[a]

    _w(coord)

    scale = min(_TOPO_XGAP, (_TOPO_MAXW - 80) / max(1, width[coord]))
    pos, maxdepth = {}, [0]

    def _place(a, depth_, x0):
        w = width[a]
        pos[a] = (40 + (x0 + w / 2.0) * scale, 46 + depth_ * _TOPO_YGAP)
        maxdepth[0] = max(maxdepth[0], depth_)
        cx = x0
        for c in kids.get(a, []):
            _place(c, depth_ + 1, cx)
            cx += width[c]

    _place(coord, 0, 0)

    # ★ 画布固定 980 宽、而小网络只占左边一小块（4 台设备仅 300 px），
    #   右边全是大片空白、整张图缩在左上角。改成按实际跨度收窄画布并水平居中。
    xs = [x for x, _ in pos.values()]
    span = max(xs) - min(xs)
    W = int(min(_TOPO_MAXW, max(_TOPO_MINS, span + 2 * _TOPO_PAD)))
    shift = (W - span) / 2.0 - min(xs)
    pos = {a: (x + shift, y) for a, (x, y) in pos.items()}

    H = 46 + maxdepth[0] * _TOPO_YGAP + 70

    def _type_of(a):
        if a == coord:
            return "coord"
        n = nbr.get(a) or {}
        if not n:
            # 协调器邻居表里没有（隔了一台路由器），退回设备自报的类型
            for e in (mesh.get(a) or {}).get("nb") or []:
                if e.get("a") == a and isinstance(e.get("ty"), int):
                    return {0: "coord", 1: "router", 2: "ed"}.get(e["ty"], "ed")
        return {0: "coord", 1: "router", 2: "ed"}.get(n.get("ty"), "ed")

    fill = {"coord": "#c0392b", "router": "#2980b9", "ed": "#6c7a89"}

    parts = [
        f'<svg viewBox="0 0 {W} {H}" width="100%" '
        f'style="max-width:{W}px;background:#12121f;border-radius:6px" '
        f'xmlns="http://www.w3.org/2000/svg">'
    ]

    # 先画「额外链路」：两端都听得见、但不在生成树上的那条，画得很淡。
    # 这是 mesh 的真实样子 —— 树只为了让层级看得懂，链路才是实测数据。
    for (x, y), e in links.items():
        if (parent.get(y) == x) or (parent.get(x) == y):
            continue
        if x not in pos or y not in pos:
            continue
        (x1, y1), (x2, y2) = pos[x], pos[y]
        lqi = e.get("lqi")
        parts.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                     f'stroke="#2e4a6a" stroke-width="1" '
                     f'stroke-dasharray="2 5"/>')
        if isinstance(lqi, int):
            parts.append(f'<text x="{(x1 + x2) / 2:.1f}" y="{(y1 + y2) / 2 + 10:.1f}" '
                         f'fill="#4a6a8a" font-size="9" text-anchor="middle">{lqi}</text>')

    # 再画生成树的边，后画节点（节点盖住线头）
    for a in sorted(nodes):
        if a == coord:
            continue
        if a in orphan:
            continue                      # 没有实测链路 → 不画线（不编造边）
        p = parent.get(a, coord)
        if p not in pos:
            p = coord
        x1, y1 = pos[p]
        x2, y2 = pos[a]
        relayed = (p != coord)
        dash = ' stroke-dasharray="6 4"' if relayed else ""
        col = "#e6b422" if relayed else "#4a6aaa"
        lbl = hop_lqi.get(a)
        # LQI 低于 40 连不成样子了，画细画红
        w = "2"
        if isinstance(lbl, int):
            if lbl < 40:
                col = "#e74c3c"
            w = "3" if lbl >= 150 else ("2" if lbl >= 60 else "1.5")
        parts.append(f'<line x1="{x1:.1f}" y1="{y1 + 20:.1f}" '
                     f'x2="{x2:.1f}" y2="{y2 - 20:.1f}" '
                     f'stroke="{col}" stroke-width="{w}"{dash}/>')
        if isinstance(lbl, int):
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            parts.append(f'<text x="{mx:.1f}" y="{my:.1f}" fill="#8fa8c8" '
                         f'font-size="10" text-anchor="middle">LQI {lbl}</text>')

    # 标签最多能放几个字：相邻叶位间距就是 scale，等宽字体约 6.2 px/字
    lbl_max = max(8, int(scale / 6.2))

    for a in sorted(nodes):
        x, y = pos[a]
        ty = _type_of(a)
        r = 20 if ty == "coord" else 17
        # 标签：有型号显示型号（短、能认人），没型号就退回地址
        model = (names.get(a) or "").strip()
        if a == coord:
            plain = "协调器"
        elif model:
            plain = model if len(model) <= lbl_max else model[:lbl_max - 1] + "…"
        else:
            plain = a
        # 采集失败（st != 0）圈起来：0x84=不支持算「灰」，其它算真失败「暗红」
        m = mesh.get(a) or {}
        st = m.get("st") or 0
        stroke = "#0d0d1f"
        if a != coord:
            if st == 0x84:
                stroke = "#55556a"
            elif st:
                stroke = "#a03a3a"
        if a in orphan:
            stroke = "#5a3a5a"
        mm = meta.get(a) or {}
        via = parent.get(a)
        if a == coord:
            via_t = "—"
        elif a in orphan:
            via_t = "未知（没采到任何实测链路）"
        elif via == coord:
            via_t = "直连协调器"
        else:
            via_t = f"经中继 {via}"
        coll_t = ("未采集" if a not in mesh else
                  "不支持 Mgmt_Lqi" if st == 0x84 else
                  f"失败 st=0x{st:02x}" if st else f"✓ {m.get('tot', 0)} 条")
        tip = json.dumps({
            "name": model or plain, "addr": a,
            "ieee": mm.get("ieee") or "", "role": _DEV_TYPE.get(ty, ty),
            "via": via_t,
            "lqi": hop_lqi.get(a) if isinstance(hop_lqi.get(a), int) else
                   (nbr.get(a) or {}).get("lqi") or "",
            "depth": depth.get(a, 0),
            "rssi": mm.get("rssi") if isinstance(mm.get("rssi"), int) else "",
            "coll": coll_t,
        }, ensure_ascii=False)
        tip = (tip.replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;").replace('"', "&quot;"))
        parts.append(f'<g class="tn" data-tip="{tip}" style="cursor:pointer">')
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" '
                     f'fill="{fill[ty]}" stroke="{stroke}" stroke-width="2"/>')
        parts.append(f'<text x="{x:.1f}" y="{y + r + 14:.1f}" fill="#dfe6f0" '
                     f'font-size="11" text-anchor="middle">{plain}</text>')
        if a in orphan:
            parts.append(f'<text x="{x:.1f}" y="{y + r + 26:.1f}" '
                         f'fill="#9a7a9a" font-size="9" '
                         f'text-anchor="middle">未定位</text>')
        elif a != coord and parent.get(a, coord) != coord:
            parts.append(f'<text x="{x:.1f}" y="{y + r + 26:.1f}" '
                         f'fill="#e6b422" font-size="9" '
                         f'text-anchor="middle">中继</text>')
        parts.append("</g>")

    parts.append("</svg>")
    return "".join(parts), parent, how, hop_lqi, depth, links


_DEV_TYPE = {"coord": "协调器", "router": "路由器", "ed": "终端设备"}

# 拓扑图悬停卡片。单独放成常量是为了不写进 f-string（里面全是花括号，转义太容易错）。
_TOPO_TIP_JS = r"""
<div id="tt"></div>
<script>
(function () {
  var tt = document.getElementById('tt'), cur = null;
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }
  function row(k, v) {
    if (v === '' || v === null || v === undefined) return '';
    return '<div><span class="k">' + k + '</span><span class="v">' + esc(v) + '</span></div>';
  }
  document.querySelectorAll('.tn').forEach(function (g) {
    g.addEventListener('mouseenter', function () {
      var d;
      try { d = JSON.parse(g.dataset.tip); } catch (e) { return; }
      cur = JSON.stringify(d);
      tt.innerHTML = '<div class="h">' + esc(d.name || d.addr) + '</div>'
        + row('地址：', d.addr)
        + row('型号：', d.name && d.name !== d.addr ? d.name : '')
        + row('IEEE：', d.ieee)
        + row('角色：', d.role)
        + row('连接：', d.via)
        + row('该跳 LQI：', d.lqi)
        + row('深度：', d.depth)
        + row('RSSI：', d.rssi === '' ? '' : d.rssi + ' dBm')
        + row('自报采集：', d.coll);
      tt.style.display = 'block';
    });
    g.addEventListener('mousemove', function (ev) {
      if (!tt.style.display || tt.style.display === 'none') return;
      var x = ev.clientX + 16, y = ev.clientY + 16, r = tt.getBoundingClientRect();
      if (x + r.width > innerWidth - 8) x = ev.clientX - r.width - 16;
      if (y + r.height > innerHeight - 8) y = ev.clientY - r.height - 16;
      tt.style.left = Math.max(4, x) + 'px';
      tt.style.top = Math.max(4, y) + 'px';
    });
    g.addEventListener('mouseleave', function () { tt.style.display = 'none'; cur = null; });
  });
})();
</script>
"""


def _dev_model(d):
    """从设备状态里取 ZCL Basic.model（设备型号），取不到返回 ""。"""
    cl = (d or {}).get("clusters") or {}
    b = cl.get("basic")
    if isinstance(b, dict):
        return ((b.get("payload") or {}).get("model") or "").strip()
    return ""


def _lqi_cell(lqi, width: int = 50, ts=None) -> str:
    """渲染 LQI。★ LQI == 0 不是「信号为 0」。

    802.15.4 的 LQI 由收到的帧算相关性得到，取值范围 1..255；
    **0 表示这一帧没带有效的 LQI 值**（网关的 `ind.lqi` 在弱链路 / 某些
    帧类型下就是 0）。以前直接把 0 画成一个空 meter，用户看到「LQI 0」
    会以为显示坏了 —— 这里明确标成"未测"，真实强度请看同一行的 RSSI。

    `ts` = 这个值是**什么时候**测的（epoch 秒，来自固件心跳里的 `lqia`）。
    固件现在只在 LQI 有效时才覆盖（不再被 0 抹掉），所以静默设备的 LQI
    会一直保留旧值 —— 超过 2 分钟就标上年龄，别让人把陈旧值当实时值。
    """
    if not isinstance(lqi, int):
        return "–"
    if lqi == 0:
        return ('<span style="color:#888" title="最后几帧没有带有效 LQI，'
                '不是信号为 0；实际强度看 RSSI">– （未测）</span>')
    col = "#f66" if lqi < 40 else ("#fc6" if lqi < 100 else "#7fff7f")
    age = ""
    if ts:
        secs = int(time.time() - ts)
        if secs >= 120:
            age = (' <span style="color:#888;font-size:.85em" '
                   f'title="固件只在 LQI 有效时更新，这个值是最后一次测到的">'
                   f'（{_ago(ts)}测的）</span>')
    return (f'<meter value="{lqi}" min="0" max="255" '
            f'style="width:{width}px;vertical-align:middle"></meter> '
            f'<span style="color:{col}">{lqi}</span>{age}')


def _rssi_cell(rssi) -> str:
    """渲染 RSSI。这个是实测 dBm，LQI 拿不到时它才是可信的强度指标。

    顺手给个粗档位，省得用户自己把 dBm 换算成"到底算好不好"：
    经验阈值（2.4 GHz、板载天线）≥-60 优 / ≥-70 良 / ≥-80 弱 / 更低很差。
    """
    if not isinstance(rssi, int):
        return "–"
    if rssi >= -60:
        q, col = "优", "#7fff7f"
    elif rssi >= -70:
        q, col = "良", "#c8e06a"
    elif rssi >= -80:
        q, col = "弱", "#fc6"
    else:
        q, col = "很差", "#f66"
    return f'<span style="color:{col}">{rssi} dBm · {q}</span>'


def _topo_html():
    with _state_lock:
        tp = json.loads(json.dumps(_state.get("topo") or {}))
        devs = json.loads(json.dumps(_state.get("devices") or {}))
        # 中继自报的父节点（0xFC00/0x0002）—— 权威数据，判定优先级最高
        tp["claims"] = json.loads(json.dumps(_state.get("parent") or {}))

    # ★ 「这一轮是什么时候采的」必须显示出来：自动采集每 60 s 一轮，页面每
    #   15 s 重载，图里的连线/数字其实一直在这两个节奏之间变；不写时间戳，
    #   用户会觉得"刷新了也没变"，只能靠手动点。
    if tp.get("done_at"):
        topo_stamp = (f"本轮采集完成于 {tp.get('ts') or '?'}"
                      f"（{_ago(tp.get('done_at'))}，第 {tp.get('round', 0)} 轮）")
    elif tp.get("round"):
        topo_stamp = f"第 {tp.get('round', 0)} 轮采集中……（上一轮的图先留着）"
    else:
        topo_stamp = "还没有采集过拓扑"
    topo_iv = int(getattr(args, "topo_interval", 0) or 0)

    # 节点标签用型号（认设备），地址放到悬停卡片里；没型号的节点标签就退回地址
    names = {a: _dev_model(d) for a, d in devs.items() if _dev_model(d)}
    meta  = {a: {"ieee": d.get("ieee") or "", "rssi": d.get("rssi")}
             for a, d in devs.items()}
    svg, parent, how, hop_lqi, depth, links = _topo_svg(
        tp, names, meta, (tp.get("claims") or {}))
    coord = tp.get("addr") or "0x0000"
    nbr  = {n["a"]: n for n in (tp.get("nbr") or []) if n.get("a")}
    mesh = tp.get("mesh") or {}

    rows = ""
    for a in sorted(set(list(parent) + list(nbr) + list(devs) + list(mesh))):
        if a == coord:
            continue
        p = parent.get(a)
        if p == coord:
            way = '<span style="color:#7fff7f">直连</span>'
            ptxt = "–"
        elif p:
            way = '<span style="color:#e6b422">经中继</span>'
            ptxt = f'<a href="/device/{p}" style="color:#e6b422">{p}</a>'
        else:
            way = '<span style="color:#888">未知</span>'
            ptxt = "–"
        n = nbr.get(a) or {}
        m = mesh.get(a) or {}
        d = devs.get(a) or {}
        model = _dev_model(d)
        tyv = n.get("ty")
        if tyv is None:
            for e in m.get("nb") or []:
                if e.get("a") == a and isinstance(e.get("ty"), int):
                    tyv = e["ty"]
                    break
        ty = {0: "协调器", 1: "路由器", 2: "终端设备"}.get(tyv, "终端设备")
        # 该跳 LQI：优先设备自报（隔了中继也准），否则协调器邻居表的旧值
        hl = hop_lqi.get(a)
        if not isinstance(hl, int):
            hl = n.get("lqi")
        rssi = n.get("rssi")
        if a not in mesh:
            coll = '<span style="color:#666">未采集</span>'
        elif m.get("st") == 0x84:
            coll = '<span style="color:#888">不支持</span>'
        elif m.get("st"):
            coll = f'<span style="color:#f66">失败 st=0x{m.get("st"):02x}</span>'
        else:
            coll = f'<span style="color:#7fff7f">✓ {m.get("tot", 0)} 条</span>'
        # 这台设备总共被几条链路看到（含它自己报的和别人报的）
        n_lnk = sum(1 for (x, y) in links if a in (x, y))
        dv = depth.get(a)
        rows += f"""
        <tr>
          <td><a href="/device/{a}" style="color:#7ecbff;text-decoration:none">{a}</a></td>
          <td>{ty}</td>
          <td>{model or "–"}</td>
          <td>{way}</td>
          <td>{ptxt}</td>
          <td>{dv if isinstance(dv, int) else "–"}</td>
          <td>{_lqi_cell(hl)}</td>
          <td>{_rssi_cell(rssi)}</td>
          <td>{coll}</td>
          <td>{n_lnk}</td>
          <td style="color:#888;font-size:.85em">{how.get(a, "")}</td>
        </tr>"""

    n_relay = sum(1 for a, p in parent.items() if p and p != coord)
    n_mesh_ok  = sum(1 for m in mesh.values() if not m.get("st"))
    n_mesh_bad = len(mesh) - n_mesh_ok
    ts = tp.get("ts") or "（还没有数据，点下面的「刷新拓扑」）"

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<title>网络拓扑 – ESP32-C6 网关</title>
<style>
  body{{background:#1a1a2e;color:#e0e0e0;font-family:monospace;margin:2em}}
  h1{{color:#00d4ff;font-size:1.4em}} h2{{color:#a0c4ff;border-bottom:1px solid #333;padding-bottom:.3em}}
  table{{border-collapse:collapse;width:100%;margin:.5em 0}}
  th,td{{border:1px solid #444;padding:.35em .7em;text-align:left}}
  th{{background:#2a2a4a}} tr:nth-child(even){{background:#1e1e3a}}
  button{{background:#2a4a7a;color:#e0e0e0;border:1px solid #4a6a9a;
          padding:.4em 1em;cursor:pointer;border-radius:3px}}
  button:hover{{background:#3a6aaa}}
  .legend span{{display:inline-block;margin-right:1.2em;font-size:.9em}}
  .dot{{display:inline-block;width:10px;height:10px;border-radius:50%;
        margin-right:.35em;vertical-align:middle}}
  section{{background:#16213e;padding:1.2em;margin:1.2em 0;border-radius:6px}}
  a{{color:#7ecbff}}
  /* 悬停卡片：跟蓝牙设备列表一个习惯 —— 图上只写型号，地址等信息放这里 */
  #tt{{position:fixed;z-index:50;pointer-events:none;display:none;
       background:#20202e;border:1px solid #3d3d60;border-radius:8px;
       padding:.65em .9em;font-size:.85em;line-height:1.65;
       box-shadow:0 8px 24px #000c;max-width:340px;white-space:nowrap}}
  #tt .h{{color:#fff;font-weight:bold;font-size:1.1em;margin-bottom:.15em}}
  #tt .k{{color:#8a8aa8;display:inline-block;min-width:5.6em}}
  #tt .v{{color:#dfe6f0}}
  .tn:hover circle{{filter:brightness(1.35)}}
</style></head>
<body>
<h1>网络拓扑 <small style="font-size:.6em;color:#666">
  PAN {tp.get("pan") or "–"} · 信道 {tp.get("ch") or "–"} ·
  邻居 {len(tp.get("nbr") or [])} · 路由 {len(tp.get("rt") or [])} ·
  源路由 {len(tp.get("rr") or [])} ·
  设备自报 {n_mesh_ok}/{len(mesh)}{(" · 失败 " + str(n_mesh_bad)) if n_mesh_bad else ""}</small></h1>
<p><a href="/">← 返回仪表盘</a> &nbsp;|&nbsp;
   <a href="/api/topo">JSON</a> &nbsp;|&nbsp;
   采集时间 {ts}</p>

<form method="POST" action="/api/topo">
  <button type="submit">刷新拓扑（逐台 Mgmt_Lqi 采集，约 {max(3, len(devs) * 2)} 秒）</button>
</form>
<p style="color:#888;font-size:.85em">
  <b>怎么算的</b>：先向每台设备发 <code>Mgmt_Lqi_Req</code>（ZDO 0x0031）收它的邻居表，
  再把「协调器的邻居表 + 各设备自报的邻居表」合并成一张<b>实测链路图</b>
  （每条边带 LQI），最后从协调器做 BFS 生成树：父节点 = 深度最小的可达邻居，
  同层挑 LQI 最高的那条。<br>
  <b>为什么不用设备自报的父子关系</b>：本网络的 Feibit 开关回
  <code>0x84 NOT_SUPPORTED</code>（不支持 Mgmt_Lqi）；支持它的 ESP32-C6 中继虽然答了，
  但 ZBOSS 把 <code>relationship</code> 一律填 2、<code>depth</code> 一律填 0，
  不可用。所以只能靠实测链路推 —— 好处是不依赖设备配合。<br>
  虚线 = 这台的流量要过一台中继；线越粗 LQI 越好，&lt;40 画红色。
  <span style="color:#2e4a6a">点线</span> = 两端都听得见、但没用在生成树上的额外链路。<br>
  <b>节点上写的是型号</b>（ZCL Basic.model，取不到型号就写地址）；
  <b>鼠标悬停</b>看地址 / IEEE / 角色 / 连接方式 / 该跳 LQI / RSSI。
  圈的颜色：<span style="color:#55556a">灰</span>=不支持 Mgmt_Lqi、
  <span style="color:#a03a3a">暗红</span>=采集失败、
  <span style="color:#5a3a5a">紫</span>=未定位（没采到任何实测链路）。
</p>

<section>
<h2>拓扑图</h2>
{svg}
<p class="legend">
  <span><i class="dot" style="background:#c0392b"></i>协调器</span>
  <span><i class="dot" style="background:#2980b9"></i>路由器（可转发）</span>
  <span><i class="dot" style="background:#6c7a89"></i>终端设备</span>
  <span style="color:#e6b422">— — 虚线 = 经中继</span>
  <span style="color:#2e4a6a">···· 点线 = 额外链路（实测能听见，未用于转发路径）</span>
</p>
<p style="color:#888;font-size:.85em">共 {n_relay} 台设备的流量经过中继 · 实测链路 {len(links)} 条。</p>
</section>

<section>
<h2>明细</h2>
<table>
<tr><th>地址</th><th>类型</th><th>型号</th><th>连接方式</th><th>父节点/下一跳</th>
    <th>深度</th><th>该跳 LQI</th><th>本机 RSSI</th><th>自报采集</th><th>链路</th><th>判定依据</th></tr>
{rows or '<tr><td colspan="11"><em>还没有数据</em></td></tr>'}
</table>
</section>
<footer style="margin-top:2em;color:#666;font-size:.85em">
  <span id="rl" style="color:#666">自动刷新 15 秒</span> &nbsp;|&nbsp;
  <a href="javascript:location.reload()">立即刷新</a> &nbsp;|&nbsp;
  <a href="/api/topo">JSON</a>
  <br>
  <span style="color:#8ab4f8">{topo_stamp}</span>
  &nbsp;·&nbsp; 服务端每 {topo_iv} 秒自动重采一轮（0 = 关闭）
</footer>
{_autoreload_js(15)}
{_TOPO_TIP_JS}
</body></html>"""


def dispatch(line: str):
    mq_put(f"{BASE}/console", line, qos=0)

    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return

    t = msg.get("t")
    if not t:
        return

    if t == "boot":
        global _gw_boot_ts, _fw_ota_restarts
        _gw_boot_ts = time.monotonic()
        mq_put(f"{BASE}/status", "online", retain=True, qos=1)
        db_upsert_gateway("online")
        with _state_lock:
            _state["status"] = "online"
            fw = dict(_state.get("fw_ota") or {})
        print("[C6] boot")
        # Sendeleistung ist in der Firmware NICHT persistent (kein NVS) –
        # nach jedem C6-Boot den gemerkten Wunschwert erneut setzen.
        _txp_reapply()
        # ★ Ist der C6 mitten im Firmware-Transfer neu gestartet, ist die
        # OTA-Session weg – die restlichen Frames würden stumm verpuffen
        # (genau das passierte: Transfer stand bei 30720/656560). Also von
        # vorne, aber höchstens 3-mal.
        if (fw.get("status") == "transferring"
                and fw.get("bin_path")
                and (fw.get("off") or 0) < (fw.get("size") or 0)
                and _fw_ota_restarts < 3):
            _fw_ota_restarts += 1
            print(f"[OTA] C6-Neustart mitten im Transfer – starte neu "
                  f"({_fw_ota_restarts}/3): {fw['bin_path']}")
            threading.Timer(4.0, _fw_ota_start, (fw["bin_path"],)).start()

    elif t == "vitals":
        p = msg.get("p")
        if p:
            mq_put(f"{BASE}/mr60bha2", json.dumps(p))

    elif t == "zigbee":
        addr = "0x{:04x}".format(msg.get("addr", 0))
        sub  = msg.get("sub", "raw")
        p    = msg.get("p")
        if p is not None:
            _ha_state(addr, sub, p)          # retained – überlebt HA-Neustart
            print(f"[MQTT] {BASE}/zigbee/{addr}/{sub} {p}")
            # War dieser Wert von uns angefordert? Das muss VOR _pending_ack
            # festgestellt werden – danach ist der Eintrag aufgeräumt.
            _erwartet = False
            if sub.endswith("on_off"):
                _e = _sub_ep(sub)
                _erwartet = _e is not None and (addr, _e) in _pending
            # Ein echter Gerätewert beendet eine offene Befehls-Bestätigung
            # bzw. löst eine Wiederholung aus (siehe _pending_ack).
            _pending_ack(addr, sub, p)
            # Tastendruck aus dem Report ableiten (Panels melden on/off,
            # keine Scene – siehe _ha_button_from_report).
            _ha_button_from_report(addr, sub, p, _erwartet)
            ieee = msg.get("ieee", "")
            if ieee:
                _ha_learn_ieee(addr, ieee)
            # Basic-Read (Hersteller/Modell) → Device-Info für die Discovery.
            # Danach Cache verwerfen, damit Entities mit echtem Modellnamen
            # neu angemeldet werden (vorher hießen sie noch "Zigbee 0x…").
            if sub == "basic":
                d = _ha_devices.setdefault(addr, {})
                if p.get("mfr"):   d["mfr"]   = p["mfr"]
                if p.get("model"): d["model"] = p["model"]
                hit = _ha_db.get((p.get("model") or "").strip())
                if hit:
                    d["db"] = hit
                    print(f"[HA] Modell erkannt: {p.get('model')} → {hit['model']} "
                          f"({hit['vendor']}) eps={hit.get('endpoints')}")
                else:
                    print(f"[HA] Modell unbekannt: {p.get('model')} – generische Discovery")
                _knd, _why = _ha_db_hint(addr)
                if _knd == "panel":
                    print(f"[HA] {addr}: Hinweis {_why} → Tastenpanel vermutet "
                          f"(entschieden wird über Descriptor/Probe)")
                elif _knd == "maybe":
                    print(f"[HA] {addr}: Hinweis {_why} – mehrdeutig, "
                          f"entscheidet Descriptor/Probe")
                elif _knd is None and _why:
                    print(f"[HA] {addr}: {_why}")
                _ha_forget(addr)
                _ha_prepublish(addr)
            else:
                _ha_publish(addr, sub, p)
            db_upsert_zigbee_device(addr, ieee)
            db_insert_zigbee_data(addr, sub, p)
            with _state_lock:
                # Alten Eintrag mit gleicher IEEE entfernen (Adresse nach Rejoin geändert)
                if ieee:
                    old = [a for a, d in _state["devices"].items()
                           if d.get("ieee") == ieee and a != addr]
                    for a in old:
                        del _state["devices"][a]
                dev = _state["devices"].setdefault(addr, {
                    "ieee": ieee, "name": addr, "last_seen": "", "clusters": {}
                })
                dev["ieee"] = ieee
                # Temperatur-Cluster: 0x8000 ist Ungültig → raw:null
                if sub == "temperature" and isinstance(p, dict) and p.get("raw") == -32768:
                    p = dict(p); p["raw"] = None
                dev["clusters"][sub] = {"payload": p, "ts": _now()}
                dev["last_seen"] = _now()   # 固件心跳批次时间（整批一起刷，别当真）
                dev["seen"]      = time.time()   # 这台设备**自己**最后一次说话的时刻
                dev.pop("synthetic", None)       # 真上报了，占位标记可以摘了
            _avail_touch(addr)
            _avail_sync(addr)

    elif t == "parent":
        # 中继自报父节点（固件把 zb_nwk_get_parent() 写进 0xFC00/0x0002）。
        # 这是**设备自己说的**，拓扑里优先级最高，不用再靠邻居表猜。
        a = msg.get("addr")
        p = msg.get("p")
        if a and p and a != p:
            with _state_lock:
                _state.setdefault("parent", {})[a] = p
            print(f"[PARENT] {a} 自报父节点 {p}")

    elif t == "heartbeat":
        mq_put(f"{BASE}/heartbeat", line, retain=True, qos=1)
        db_upsert_gateway("online")
        with _state_lock:
            _state.update({
                "status":  "online",
                "uptime":  msg.get("uptime", 0),
                "channel": msg.get("ch", 0),
                "pan":     msg.get("pan", "–"),
            })
            for d in msg.get("dev", []):
                addr = d.get("addr")
                if addr:
                    dev = _state["devices"].setdefault(addr, {
                        "ieee": "", "name": addr, "last_seen": "", "clusters": {}
                    })
                    if "lqi"  in d: dev["lqi"]  = d["lqi"]
                    if "rssi" in d: dev["rssi"] = d["rssi"]
                    # lqia = 固件说"这个 lqi 是几秒前测的"。换成绝对时刻存，
                    # 之后页面自己算年龄，不用每轮刷新也能显示对不对。
                    if "lqia" in d:
                        _age = d["lqia"]
                        dev["lqi_ts"] = (0 if _age is None or _age >= 0xFFFFFFFF
                                         else time.time() - _age)
                    dev["last_seen"] = _now()
                    db_upsert_zigbee_device(addr)
        devs = msg.get("dev", [])
        lqi_str = " ".join(f"{d['addr']}={d['lqi']}" for d in devs) if devs else "–"
        print(f"[HB] uptime={msg.get('uptime')}s ch={msg.get('ch')} pan={msg.get('pan')} lqi=[{lqi_str}]")

    elif t == "permit_join":
        p = msg.get("p", {})
        if p:
            mq_put(f"{BASE}/permit_join", json.dumps(p), qos=1)
            with _state_lock:
                _state["permit_join"] = p
            print(f"[MQTT] {BASE}/permit_join {p}")

    elif t == "scan":
        ch = msg.get("ch")
        if ch is not None:
            nets = msg.get("nets", [])
            db_insert_chan_scan(ch, msg.get("count", 0), json.dumps(nets))
            print(f"[SCAN] ch={ch} count={msg.get('count', 0)} nets={nets}")
        else:
            print(f"[SCAN] state={msg.get('state')}")

    elif t == "interview":
        # Join-Interview: Active-EP → Simple-Desc → Bind → Configure-Reporting
        act = msg.get("act")
        # Der C6 schickt die Adresse im interview-Objekt als String ("0x07da"),
        # im zigbee-Objekt als Zahl. Hier auf dieselbe Schreibweise bringen,
        # sonst passt der Schlüssel in _ctrl/_noread nicht.
        _raw = msg.get("addr")
        try:
            a = "0x{:04x}".format(int(_raw, 0) if isinstance(_raw, str) else int(_raw))
        except (TypeError, ValueError):
            a = str(_raw)
        if act:
            print(f"[IV] {act} addr={a} ep={msg.get('ep')} "
                  f"cluster={msg.get('cluster')} status={msg.get('status')}")
        elif msg.get("in") is not None or msg.get("out") is not None:
            # Simple-Descriptor: die Cluster-Listen sind die eindeutige
            # Auskunft darüber, ob ein Endpunkt schalten kann oder nur
            # Befehle sendet (siehe _classify_from_desc).
            print(f"[IV] desc addr={a} ep={msg.get('ep')} "
                  f"device={msg.get('device')} in={msg.get('in')} out={msg.get('out')}")
            try:
                _dev = msg.get("device") or "0x0000"
                _classify_from_desc(a, int(msg.get("ep") or 0),
                                    [int(x, 0) for x in (msg.get("in") or [])],
                                    [int(x, 0) for x in (msg.get("out") or [])],
                                    int(_dev, 0) if isinstance(_dev, str) else int(_dev))
            except (TypeError, ValueError) as e:
                print(f"[IV] desc nicht auswertbar: {e}")
        else:
            print(f"[IV] {msg.get('state')} addr={a} "
                  f"eps={msg.get('eps')} status={msg.get('status')}")
            if msg.get("state") == "done":
                # Ein Read-Attributes je Endpunkt entscheidet automatisch,
                # ob dort ein Zustand existiert (siehe _probe_*).
                _probe_start(a, msg.get("eps") or [])

    elif t == "aps":
        # Roher APS-Frame: enthält Tastendrücke (siehe _ha_aps_action).
        # 能收到它的帧 = 这台设备刚刚真的发了东西，判活最硬的证据。
        _src = msg.get("src")
        if _src:
            _avail_touch(_src)
            with _state_lock:
                _d = _state["devices"].get(_src)
                if _d is None:
                    # 纯面板只发按键的裸 APS 帧，永远不会有 zigbee/cluster
                    # 上报。旧代码只给"已存在"的设备打时间戳，于是这类设备
                    # 在网页设备表和可用性检查里**完全不存在**（0x747d 就是
                    # 这么消失的）。这里给它建档，标 synthetic 等真上报来摘。
                    _d = _state["devices"].setdefault(_src, {
                        "ieee": "", "name": _src, "last_seen": "",
                        "clusters": {}, "synthetic": True,
                    })
                _d["seen"] = time.time()
            _avail_sync(_src)
        _ha_aps_action(msg)

    elif t == "txpower":
        # Antwort auf set_tx_power: Wunschwert und von der PA zurückgelesener
        # Istwert. Weichen sie ab, hat die Änderung nicht gegriffen – dann ist
        # jede Messung mit diesem Wert wertlos (siehe §21 im Bericht).
        want = msg.get("want")
        eff  = msg.get("eff")
        with _state_lock:
            _state["tx_power"] = {"want": want, "eff": eff, "ts": _now()}
        mq_put(f"{BASE}/tx_power", line, retain=True, qos=1)
        abw = ""
        if want is not None and eff is not None and want != eff:
            abw = f"   (!) Wunsch {want} dBm, wirksam {eff} dBm – Anpassung griff nicht"
        print(f"[TX] Sendeleistung: Wunsch {want} dBm, wirksam {eff} dBm{abw}")

    elif t == "lqi":
        # Momentaufnahme auf Anfrage (statt bis zum 60-s-Heartbeat zu warten).
        # Wird für den Sendeleistungs-Vergleich gebraucht: gleiche Stelle,
        # gleiches Gerät, nur die Leistung anders.
        devs = msg.get("dev") or []
        with _state_lock:
            for d in devs:
                addr = d.get("addr")
                if not addr:
                    continue
                dev = _state["devices"].setdefault(addr, {
                    "ieee": "", "name": addr, "last_seen": "", "clusters": {}
                })
                if "lqi"  in d: dev["lqi"]  = d["lqi"]
                if "rssi" in d: dev["rssi"] = d["rssi"]
                if "lqia" in d:
                    _age = d["lqia"]
                    dev["lqi_ts"] = (0 if _age is None or _age >= 0xFFFFFFFF
                                     else time.time() - _age)
        mq_put(f"{BASE}/lqi", line, retain=True, qos=1)
        if devs:
            print("[LQI] " + " ".join(
                f"{d.get('addr')}={d.get('lqi')}/{d.get('rssi')}dBm" for d in devs))
        else:
            print("[LQI] keine Geräte bekannt")

    elif t == "keepalive":
        # Keep-Alive-Test: periodische Read-Attribute nach Autorisierung
        print(f"[KA] addr={msg.get('addr')} ep={msg.get('ep')} "
              f"round={msg.get('round')}")

    elif t == "node_desc":
        # Antwort auf ZDO Node-Descriptor (0x0002). Entscheidet, ob das Gerät
        # als Router (ZR) Fremdverkehr weiterleiten kann oder nur End Device ist.
        # ★ 顺便当"活体探测"：拓扑轮询每 60 s 对每台设备发一次，答了就是活着，
        #   回错误（0x85 超时等）就是掉线的**反证** —— 见 _avail_* 记分板。
        a = msg.get("addr", "?")
        # 广播/组播地址永远不会有 node_desc 应答（0xffff 实测回 0x85 超时）。
        # 若当成设备记录，会污染 node_desc 表、把"探测失败"计数抬高，
        # 还会在可用性记分板上留下一条假的"掉线设备"。直接丢弃。
        if str(a).lower() in ("0xffff", "0xfffe", "0xfffd", "0xfffc", "?"):
            return
        if msg.get("error"):
            print(f"[ND] {a}: Abfrage fehlgeschlagen ({msg.get('error')})")
            _avail_last_bad[a] = time.time()
        else:
            print(f"[ND] {a}: role={msg.get('role')} power={msg.get('power')} "
                  f"rx_idle={msg.get('rx_idle')} cap={msg.get('cap')}")
            _avail_touch(a)
        with _state_lock:
            nd = dict(msg)
            nd["_ts"] = time.time()
            _state.setdefault("node_desc", {})[a] = nd
        _avail_sync(a)

    elif t == "fw_ota":
        st = msg.get("st")
        if st == "begin":
            print(f"[OTA] coordinator: begin – Part {msg.get('part','?')}, "
                  f"size={msg.get('size','?')} – übertrage …")
            _fw_ota_send_batch()
        elif st == "write":
            with _state_lock:
                _state["fw_ota"]["off"] = msg.get("off", 0)
            if _fw_ota_off < _fw_ota_size:
                _fw_ota_send_batch()
        elif st == "done":
            # written = was der C6 tatsächlich in die Partition geschrieben hat.
            # Weicht es von der Dateigröße ab, ist beim Upload/Framing etwas
            # danebengegangen (z. B. +44 B Schwanz). Nicht kritisch, solange
            # esp_ota_end zustimmt – aber es soll sichtbar sein, nicht stumm.
            written = msg.get("written")
            if isinstance(written, int) and written != _fw_ota_size:
                print(f"[OTA] WARNUNG: geschrieben {written} B, Datei {_fw_ota_size} B "
                      f"(Differenz {written - _fw_ota_size:+d} B)")
            print("[OTA] coordinator: Update fertig – Neustart …")
            with _state_lock:
                _state["fw_ota"]["status"] = "done"
            _fw_ota_close()
        elif st == "error":
            why = msg.get("why")
            print(f"[OTA] coordinator FEHLER: {why}")
            if why == "busy":
                print("[OTA] Ein abgebrochener Transfer steckt noch im C6 "
                      "(s_active=true). Der C6 muss neu gestartet werden, "
                      "bevor ein neuer Versuch möglich ist.")
            with _state_lock:
                _state["fw_ota"]["status"] = "error"
            _fw_ota_close()

    elif t == "zb_signal":
        print(f"[ZB] {msg.get('sig')} addr={msg.get('addr')} "
              f"status={msg.get('status')} id={msg.get('id')} err={msg.get('err')}")

    elif t == "cmd":
        print(f"[CMD] addr={msg.get('addr')} ep={msg.get('ep')} "
              f"cluster={msg.get('cluster')} v={msg.get('v')}")

    elif t == "ota_req":
        _ota_handle_req(msg.get("off", 0), msg.get("sz", 64))

    elif t == "ota_status":
        st = msg.get("status", "?")
        with _state_lock:
            _state["ota"]["status"] = st
        print(f"[OTA] coordinator: {st} size={msg.get('size')} ver={msg.get('ver')}")

    elif t == "ota_srv_status":
        st = msg.get("status", "?")
        addr = msg.get("addr", "?")
        print(f"[OTA] client {addr}: {st}")
        if st == "done":
            with _state_lock:
                _state["ota"]["status"] = "done"
            _ota_close()

    elif t == "ota_dbg":
        # ★ 网关 OTA server 的内部计数（来自 main/zb_ota_server.c）。
        #   命中/未命中/超时是判断「窗口缓存够不够、串口来不来得及」的唯一依据。
        with _state_lock:
            d = _state["ota"].setdefault("dbg", {})
            for k in ("n", "hit", "miss", "to", "seq", "idx", "sz", "off",
                      "win", "load", "len", "timeout", "stop", "src", "cmd"):
                if k in msg:
                    d[k] = msg[k]
        loud = ("miss" in msg or "timeout" in msg or "stop" in msg)
        if loud or (msg.get("load", -1) % 8192 == 0):
            print("[OTA-gw] " + ", ".join(
                f"{k}={msg[k]}" for k in msg if k != "t"))

    elif t in ("topo_nbr", "topo_rt", "topo_rr", "topo_mesh", "topo"):
        _topo_ingest(t, msg)

    elif t == "wr_power":
        # 网关已把 0xFC00/0x0001 写给了中继；中继是否真的改了，
        # 只能看它自己的串口日志（属性写入不回读）。
        addr = msg.get("addr", "?")
        with _state_lock:
            _state["wr_power"][addr] = {
                "dbm": msg.get("dbm"),
                "tsn": msg.get("tsn"),
                "ts":  _now(),
            }
        print(f"[PWR] → {addr} ep{msg.get('ep')} {msg.get('dbm')} dBm "
              f"(tsn {msg.get('tsn')})")


def _ota_start(bin_path: str, image_type: int = 0x0001):
    """Startet OTA: öffnet Binary, sendet ota_start an Coordinator.

    image_type 决定这份 Image für welche Geräteklasse gilt:
        0x0001 = Endgerät (client/)      0x0002 = Repeater (router/)
    Der Coordinator schickt den Typ im Image-Notify mit; nur Clients mit
    demselben Typ antworten überhaupt. Falscher Typ = niemand antwortet
    (kein Schaden, aber auch kein Update).
    """
    global _ota_file
    import os as _os
    if not bin_path:
        if image_type == 0x0002:
            print("[OTA] Repeater-Image ohne Pfad – Upload nötig")
            with _state_lock:
                _state["ota"]["status"] = "error"
            return
        bin_path = _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
            "client", "build", "zigbee-client.bin"
        )
    if not _os.path.isfile(bin_path):
        print(f"[OTA] Datei nicht gefunden: {bin_path}")
        with _state_lock:
            _state["ota"]["status"] = "error"
        return
    size = _os.path.getsize(bin_path)
    # ★ Plausibilitätsprüfung: ein ESP-App-Image beginnt mit dem Magic-Byte 0xE9.
    #   Fehlt es, ist die Datei verstümmelt (klassiker: multipart-Header im Image)
    #   oder es ist ein .zip/.ota-Wrapper. Dann lieber laut abbrechen, statt
    #   20 Minuten lang Blöcke zu schicken, die das Gerät am Ende verwirft.
    try:
        with open(bin_path, "rb") as _mf:
            _magic = _mf.read(1)
        if _magic != b"\xE9":
            print(f"[OTA] ABBRUCH: {bin_path} beginnt mit 0x{_magic.hex() or '??'}, "
                  f"erwartet 0xe9 (kein ESP-App-Image).")
            with _state_lock:
                _state["ota"]["status"] = "error"
            return
    except OSError as _e:
        print(f"[OTA] ABBRUCH: {bin_path} nicht lesbar ({_e})")
        with _state_lock:
            _state["ota"]["status"] = "error"
        return
    # Versionsnummer aus Datei-Mtime ableiten (Unix-Timestamp, 32-bit)
    version = int(_os.path.getmtime(bin_path)) & 0xFFFFFFFF
    _ota_close()
    _ota_file = open(bin_path, "rb")
    global _ota_hash, _ota_sent, _ota_next
    _ota_hash = _OTA_FNV0
    _ota_sent = 0
    _ota_next = 0
    with _state_lock:
        _state["ota"].update({
            "status":      "pending",
            "bin_path":    bin_path,
            "size":        size,
            "version":     version,
            "transferred": 0,
            "image_type":  image_type,
            "sent":        0,
            "sent_hash":   "0x%08x" % _OTA_FNV0,
        })
    ser.write(_j({"cmd": "ota_start", "size": size, "ver": version,
                  "type": image_type}))
    print(f"[OTA] gestartet: {bin_path} size={size} ver=0x{version:08x} "
          f"type=0x{image_type:04x}")


def _ota_handle_req(offset: int, size: int):
    """Antwortet auf ota_req: liest Chunk aus Binärdatei und sendet hex-kodiert。

    ★ 判「有没有在传」只看 _ota_file（打开着就说明 _ota_start 成功过）。
      旧实现白名单只写了 pending/transferring，而网关在 ota_start 之后会先
      发一条 status="started" —— 于是客户端第一次 Query Next Image 就被判
      「kein Transfer aktiv」，10 秒后客户端 abort，**OTA 永远起不来**。
      （实测 2026-10-26：中继每 45 s 问一次，每次都卡在这里。）"""
    global _ota_file
    if _ota_file is None:
        print(f"[OTA] ota_req off={offset} 但当前没有打开的镜像（先上传/选择 bin）")
        return

    global _ota_hash, _ota_sent, _ota_next
    try:
        _ota_file.seek(offset)
        data = _ota_file.read(size)
        hex_data = data.hex()
        cmd = json.dumps({"cmd": "ota_data", "off": offset, "data": hex_data})
        ser.write((cmd + "\n").encode())
        # 只对「连续的、第一次发」的块累加，重发不重复计
        if offset == _ota_next and data:
            h = _ota_hash
            for b in data:
                h = ((h ^ b) * 16777619) & 0xFFFFFFFF
            _ota_hash = h
            _ota_sent += len(data)
            _ota_next = offset + len(data)
        with _state_lock:
            _state["ota"]["status"] = "transferring"
            _state["ota"]["transferred"] = offset + len(data)
            _state["ota"]["sent"] = _ota_sent
            _state["ota"]["sent_hash"] = "0x%08x" % _ota_hash
    except Exception as e:
        print(f"[OTA] Fehler beim Lesen: {e}")
        _ota_close()
        with _state_lock:
            _state["ota"]["status"] = "error"


def _ota_close():
    global _ota_file
    if _ota_file:
        _ota_file.close()
        _ota_file = None

# ── Web-Dashboard ──────────────────────────────────────────────────────────
def _autoreload_js(secs: int) -> str:
    """条件自动刷新。

    旧实现用 <meta http-equiv="refresh">，它会在固定秒数后无条件整页重载，
    从而把 <input type="file"> 里已选中的文件清空（页面回到"未选择任何文件"），
    用户根本来不及点上传。这里改成脚本驱动：只要页面上有"未提交的输入"
    （已选文件 / 输入框有焦点 / 输入框内容与初始值不同）就暂停刷新，
    并允许用户手动点击"立即刷新"。
    """
    return f"""<script>
(function () {{
  var SECS = {secs};
  /* 记录页面加载时的输入框内容作为"未修改"基线。
     不能直接用 defaultValue：Firefox 整页重载后会恢复用户已输入的内容，
     那样 defaultValue 与 value 会一直不相等，页面将永远不再刷新。 */
  function snapshot() {{
    var ins = document.querySelectorAll('input, textarea');
    for (var j = 0; j < ins.length; j++) {{
      var t = (ins[j].type || '').toLowerCase();
      if (t === 'file' || t === 'submit' || t === 'button' || t === 'checkbox' || t === 'radio') continue;
      ins[j].setAttribute('data-init', ins[j].value || '');
    }}
  }}
  function busy() {{
    var fs = document.querySelectorAll('input[type=file]');
    for (var i = 0; i < fs.length; i++) {{
      if (fs[i].files && fs[i].files.length) return 'file';
    }}
    var a = document.activeElement;
    if (a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.tagName === 'SELECT')) return 'focus';
    var ins = document.querySelectorAll('input, textarea');
    for (var j = 0; j < ins.length; j++) {{
      var t = (ins[j].type || '').toLowerCase();
      if (t === 'file' || t === 'submit' || t === 'button' || t === 'checkbox' || t === 'radio') continue;
      var init = ins[j].getAttribute('data-init');
      if (init === null) continue;
      if ((ins[j].value || '') !== init) return 'dirty';
    }}
    return '';
  }}
  function label(msg, color) {{
    var e = document.getElementById('rl');
    if (e) {{ e.textContent = msg; e.style.color = color; }}
  }}
  function tick() {{
    var why = busy();
    if (why === 'file')  {{ label('已选择文件，自动刷新已暂停', '#7fff7f'); return; }}
    if (why === 'focus') {{ label('正在输入，自动刷新已暂停', '#ffcc66'); return; }}
    if (why === 'dirty') {{ label('有未提交的输入，自动刷新已暂停', '#ffcc66'); return; }}
    location.reload();
  }}
  setInterval(tick, SECS * 1000);
  document.addEventListener('DOMContentLoaded', function () {{
    snapshot();
    var fs = document.querySelectorAll('input[type=file]');
    for (var i = 0; i < fs.length; i++) {{
      fs[i].addEventListener('change', function () {{
        if (this.files && this.files.length) {{
          label('已选择 ' + this.files[0].name + '（' + this.files[0].size + ' 字节），自动刷新已暂停', '#7fff7f');
        }} else {{
          snapshot();
        }}
      }});
    }}
  }});
  /* 从往返缓存恢复时重新取基线 */
  window.addEventListener('pageshow', function (ev) {{ if (ev.persisted) snapshot(); }});
}})();
</script>"""


def _html():
    with _state_lock:
        s = json.loads(json.dumps(_state))   # deep copy

    online = s["status"] == "online"
    badge  = ("online" if online else "offline")

    pj     = s["permit_join"]
    pj_txt = f"🔓 已开启 ({pj.get('seconds',0)} 秒)" if pj.get("open") else "🔒 已关闭"

    # 发射功率：want=命令值，eff=PA 回读值。只有 eff 是实测。
    _tx = s.get("tx_power") or {}
    if _tx.get("eff") is None:
        tx_txt = "– （固件还没上报；旧固件无此功能）"
    else:
        tx_txt = f"<b>{_tx['eff']} dBm</b>"
        if _tx.get("want") is not None and _tx["want"] != _tx["eff"]:
            tx_txt += f"（命令 {_tx['want']}，未生效）"
        if _tx.get("ts"):
            tx_txt += f" · {_tx['ts']}"

    # 裸机时长估算：每批 0.55 s（含 UART 传输 + ACK 往返）
    _frames  = -(-789760 // FW_CHUNK)
    _batches = -(-_frames // FW_BATCH)
    _est_min = _batches * 0.55 / 60
    _flash   = flash_status()

    import os as _os
    default_bin = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "client", "build", "zigbee-client.bin"
    )

    dev_rows = ""
    for addr, d in s["devices"].items():
        clusters_html = ""
        for k, v in d["clusters"].items():
            if isinstance(v, dict) and "payload" in v:
                payload_str = json.dumps(v["payload"], indent=2, ensure_ascii=False)
                ts_str = v.get("ts", "")
            else:
                payload_str = json.dumps(v, indent=2, ensure_ascii=False)
                ts_str = ""
            clusters_html += (
                f'<div style="margin-bottom:.6em">'
                f'<b>{k}</b>'
                + (f' <span style="color:#888;font-size:.85em">{ts_str}</span>' if ts_str else "")
                + f'<pre style="margin:.2em 0 0 0;background:#0d0d1f;padding:.4em;'
                  f'white-space:pre-wrap;word-break:break-all;border-left:2px solid #4a6aaa">'
                  f'{payload_str}</pre></div>'
            )
        clusters_html = clusters_html or "<em>–</em>"
        lqi  = d.get("lqi",  "–")
        rssi = d.get("rssi", "–")
        lqi_html  = _lqi_cell(lqi, 50, d.get("lqi_ts"))
        rssi_html = _rssi_cell(rssi)
        dev_rows += f"""
        <tr style="cursor:pointer" onclick="location.href='/device/{addr}'"
            title="查看详情">
          <td><a href="/device/{addr}" style="color:#7ecbff;text-decoration:none">{addr}</a></td>
          <td style="font-size:.85em;color:#aaa">{d.get("mfr","") or "–"}<br>{d.get("model","") or ""}</td>
          <td>{clusters_html}</td>
          <td>{lqi_html}</td>
          <td>{rssi_html}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
{_autoreload_js(10)}
<title>ESP32-C6 网关</title>
<style>
  body{{background:#1a1a2e;color:#e0e0e0;font-family:monospace;margin:2em}}
  h1{{color:#00d4ff}} h2{{color:#a0c4ff;border-bottom:1px solid #333;padding-bottom:.3em}}
  h3{{color:#7ecbff;margin-top:1.2em}}
  table{{border-collapse:collapse;width:100%;margin:.5em 0}}
  th,td{{border:1px solid #444;padding:.4em .8em;text-align:left}}
  th{{background:#2a2a4a}} tr:nth-child(even){{background:#1e1e3a}}
  .badge{{padding:.2em .6em;border-radius:4px;font-size:.85em;margin-left:.5em}}
  .online{{background:#1a5c1a;color:#7fff7f}}
  .offline{{background:#5c1a1a;color:#ff7f7f}}
  button{{background:#2a4a7a;color:#e0e0e0;border:1px solid #4a6a9a;
          padding:.3em .8em;cursor:pointer;border-radius:3px;margin-right:.4em}}
  button:hover{{background:#3a6aaa}}
  section{{background:#16213e;padding:1.2em;margin:1.2em 0;border-radius:6px}}
  footer{{margin-top:2em;color:#666;font-size:.85em}}
</style></head>
<body>
<h1>ESP32-C6 Zigbee Gateway <small style="font-size:.5em;color:#666">v{GW_VERSION}</small></h1>
<p><span id="rl" style="color:#666">自动刷新 10 秒</span> &nbsp;|&nbsp;
   <a href="javascript:location.reload()" style="color:#7ecbff">立即刷新</a> &nbsp;|&nbsp;
   <a href="/topology" style="color:#7ecbff">🗺 网络拓扑</a> &nbsp;|&nbsp;
   <a href="/api/state" style="color:#7ecbff">JSON</a></p>
<section>
  <h2>协调器 <code>{BASE}</code>
    <span class="badge {badge}">{'在线' if online else '离线'}</span>
  </h2>
  <table>
    <tr><th>运行时间</th><td>{s['uptime']} 秒</td></tr>
    <tr><th>信道</th><td>{s['channel']}</td></tr>
    <tr><th>PAN</th><td>{s['pan']}</td></tr>
    <tr><th>允许加入</th><td>{pj_txt}</td></tr>
    <tr><th>发射功率</th><td>{tx_txt}</td></tr>
  </table>
  <h3>允许加入</h3>
  <form method="POST" action="/api/permit_join">
    <button name="secs" value="180">🔓 开启 180 秒</button>
    <button name="secs" value="0">🔒 关闭</button>
  </form>
  <h3>发射功率（dBm）</h3>
  <p style="font-size:.85em;color:#888">改完看 <code>{BASE}/tx_power</code> 的
     <code>eff</code>：那才是 PA 真正在跑的档位。<br>
     注意方向：<b>LQI/RSSI 反映的是设备→网关</b>（网关收到的帧强度），
     改网关发射功率<b>不会</b>改变它。降档唯一能影响的是
     <b>网关→设备</b>这一侧，也就是「命令还能不能送到」。
     要判断哪档合适，看命令成功率，不要看 LQI。</p>
  <form method="POST" action="/api/tx_power">
    <button name="dbm" value="20">20（最大）</button>
    <button name="dbm" value="14">14</button>
    <button name="dbm" value="8">8</button>
    <button name="dbm" value="2">2</button>
    <button name="dbm" value="0">0</button>
  </form>
  <form method="POST" action="/api/lqi">
    <button type="submit">📶 立即取一次 LQI/RSSI</button>
  </form>
  <h3>OTA 固件升级（Zigbee 无线推送）</h3>
  <table>
    <tr><th>Status</th><td>{s['ota']['status']}</td></tr>
    <tr><th>Binary</th><td>{s['ota']['bin_path'] or '–'}</td></tr>
    <tr><th>Image type</th><td>0x{s['ota']['image_type']:04x}</td></tr>
    <tr><th>进度</th><td>{s['ota']['transferred']} / {s['ota']['size']} Byte</td></tr>
  </table>
  <p style="font-size:.85em;color:#888">
    目标类型必须和设备的固件一致：<code>0x0001</code> 终端节点（client/）·
    <code>0x0002</code> 中继器（router/）。选错不会刷坏，只是没人应答。<br>
    设备页里也能单独上传 —— 每个 image type 各存一份，互不覆盖。
  </p>
  <form method="POST" action="/api/ota" enctype="multipart/form-data">
    <select name="type" style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em">
      <option value="0x0001">0x0001 · 终端节点 (client)</option>
      <option value="0x0002">0x0002 · 中继器 (router)</option>
    </select>
    <input type="file" name="file" accept=".bin"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    <button type="submit">&#x2B06; 上传并开始 OTA</button>
  </form>
  <form method="POST" action="/api/ota">
    <select name="type" style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em">
      <option value="0x0001">0x0001 · 终端节点</option>
      <option value="0x0002">0x0002 · 中继器</option>
    </select>
    <input name="path" value="{s['ota']['bin_path'] or default_bin}" size="50"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    <button type="submit">⬆ 按路径开始 OTA</button>
  </form>
  <h3>协调器固件升级（串口 OTA）</h3>
  <p style="font-size:.85em;color:#888">帧 {FW_CHUNK} B ≈ {2*FW_CHUNK+50} 字符/行
     （旧固件 256 字节缓冲也能装下）· {FW_BATCH} 帧/批 = {FW_CHUNK*FW_BATCH} B
     · 789 KB 预计约 {_est_min:.0f} 分钟</p>
  <form method="POST" action="/api/fw_ota" enctype="multipart/form-data">
    <input type="file" name="file" accept=".bin"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    <button type="submit">&#x2B06; 上传并升级协调器</button>
  </form>
  <p style="font-size:.85em;color:#888">或填写容器内已有文件的路径（<code>/share/…</code> 可由 Samba 访问）：</p>
  <form method="POST" action="/api/fw_ota">
    <input name="path" value="" placeholder="/share/zigbee-vital-sensor.bin" size="50"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    <button type="submit">按路径升级</button>
  </form>
  <p style="font-size:.85em;color:#888">状态: {s['fw_ota']['status']}
     &nbsp;|&nbsp; {s['fw_ota']['off']} / {s['fw_ota']['size']} Bytes</p>

  <h3>应急：esptool 直刷（应用层 OTA 卡死时用）</h3>
  <p style="font-size:.85em;color:#ffcc66">绕过应用层 OTA，直接用 Bootloader 通过 USB
     写入 <code>{APP_OFFSET}</code>（ota_0）。刷写期间串口会被独占、桥接暂停，
     完成后自动重开。适用于旧固件收不到 <code>fw_ota_data</code>（一直停在 1280 字节）的情况。</p>
  <form method="POST" action="/api/fw_flash" enctype="multipart/form-data">
    <input type="file" name="file" accept=".bin"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    <button type="submit">&#x26A1; 上传并用 esptool 直刷</button>
  </form>
  <p style="font-size:.85em;color:#888">或按容器内路径直刷：</p>
  <form method="POST" action="/api/fw_flash">
    <input name="path" value="" placeholder="/share/zigbee-vital-sensor-devcmd-fix.bin" size="50"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    <button type="submit">按路径直刷</button>
  </form>
  <p style="font-size:.85em;color:#888">直刷状态: <b>{_flash['status']}</b>
     {('· ' + _flash['bin']) if _flash['bin'] else ''}</p>
  {('<pre style="background:#0d0d1f;padding:.6em;font-size:.8em;max-height:10em;overflow:auto;white-space:pre-wrap">' + chr(10).join(_flash['tail']) + '</pre>') if _flash['tail'] else ''}
  <h3>Zigbee 设备 ({len(s['devices'])})</h3>
  <table>
    <tr><th>地址</th><th>制造商 / 型号</th><th>Payload</th><th>LQI</th><th>RSSI</th></tr>
    {dev_rows or '<tr><td colspan="5"><em>暂无设备</em></td></tr>'}
  </table>
</section>
<section>
  <h2>原始状态</h2>
  <pre style="background:#0d0d1f;padding:1em;overflow-x:auto;white-space:pre-wrap;word-break:break-all;font-size:.85em">{json.dumps(s, indent=2, ensure_ascii=False)}</pre>
</section>
<footer>
  主题前缀: <code>{BASE}</code> &nbsp;|&nbsp; 数据库: <code>{DB_HOST}/{DB_NAME}</code>
  &nbsp;|&nbsp; OTA 帧: <code>{FW_CHUNK} B</code> &nbsp;|&nbsp; 版本: <code>v{GW_VERSION}</code>
</footer>
</body></html>"""


def _device_html(addr):
    """Detail-Seite für ein einzelnes Zigbee-Gerät."""
    with _state_lock:
        s = json.loads(json.dumps(_state))
    dev = s["devices"].get(addr)
    if dev is None:
        return None

    clusters_html = ""
    for k, v in dev["clusters"].items():
        if isinstance(v, dict) and "payload" in v:
            payload_str = json.dumps(v["payload"], indent=2, ensure_ascii=False)
            ts_str = v.get("ts", "")
        else:
            payload_str = json.dumps(v, indent=2, ensure_ascii=False)
            ts_str = ""
        clusters_html += f"""
        <section style="margin-bottom:1.2em">
          <h3 style="color:#7ecbff;margin-bottom:.4em">{k}
            {"<span style='color:#888;font-size:.8em;margin-left:.5em'>" + ts_str + "</span>" if ts_str else ""}
          </h3>
          <pre style="background:#0d0d1f;padding:1em;white-space:pre-wrap;
               word-break:break-all;border-left:3px solid #4a6aaa;margin:0;
               font-size:.9em">{payload_str}</pre>
        </section>"""

    lqi  = dev.get("lqi",  "–")
    rssi = dev.get("rssi", "–")
    # 型号/厂商来自 Basic 簇（0x0005 / 0x0004）。只在入网那一刻读过一次，
    # 那一次没读到的设备这里就是空的 —— 给个按钮再问一遍。
    _bsc  = (dev.get("clusters") or {}).get("basic")
    _bpl  = (_bsc.get("payload") or {}) if isinstance(_bsc, dict) else {}
    model = (_bpl.get("model") or "").strip()
    mfr   = (_bpl.get("mfr") or "").strip()
    model_html = (model if model else
                  '<span style="color:#f90">未读到</span> '
                  '<span style="color:#888;font-size:.85em">（入网时那次 Basic 没答上，'
                  '点下面「重新读型号」）</span>')
    lqi_bar = _lqi_cell(lqi, 80, dev.get("lqi_ts"))

    # 这台设备**自己**最后一次发帧的时刻（不是心跳批次时间）
    _seen = dev.get("seen")
    _seen_txt = (_ago(_seen) if _seen else
                 '<span style="color:#888">—（还没有帧 / 刚重启）</span>')
    if _avail_online(addr):
        _avail_txt = ('<b style="color:#6f6">在线</b> '
                      '<span style="color:#888">（HA 实体可用）</span>')
    else:
        _avail_txt = ('<b style="color:#f66">不可用</b> '
                      '<span style="color:#888">（ZDO 探测失败或长时间没有证据，'
                      'HA 里对应实体会变灰）</span>')

    # Node-Descriptor-Ergebnis (Router oder End Device?) – nur wenn schon abgefragt
    nd = (_state.get("node_desc") or {}).get(addr)
    if nd and nd.get("role"):
        role_txt = ("<b style='color:#6f6'>Router (ZR) – leitet Fremdverkehr weiter</b>"
                    if nd["role"] == "router" else
                    "<b style='color:#f90'>End Device (ZED) – leitet NICHT weiter</b>")
        role_html = (f'{role_txt}<br><span style="color:#888">cap={nd.get("cap")} · '
                     f'{nd.get("power")} · rx_idle={nd.get("rx_idle")}</span>')
    elif nd and nd.get("error"):
        role_html = f'<span style="color:#f66">Abfrage fehlgeschlagen ({nd.get("error")})</span>'
    else:
        role_html = '<span style="color:#888">未查询（下方“命令”里选 node_desc）</span>'

    # 连接方式：直连还是经中继。数据来自 /topology 采集的三张表，
    # 没采集过就提示去点一下（Zigbee 按需建路，不探测就查不到）。
    _tp = s.get("topo") or {}
    _parent, _how, _hlqi, _depth, _links = _topo_rel(_tp, s.get("parent") or {})
    _coord = _tp.get("addr") or "0x0000"
    _hl = _hlqi.get(addr)
    _hl_txt = f" · 该跳 LQI {_hl}" if isinstance(_hl, int) else ""
    if addr == _coord:
        link_html = '<b style="color:#c0392b">协调器本身</b>'
    elif _parent.get(addr) == _coord:
        link_html = ('<b style="color:#6f6">直连协调器</b> '
                     f'<span style="color:#888">（一跳，不经过中继{_hl_txt}）</span>')
    elif _parent.get(addr):
        _p = _parent[addr]
        link_html = (f'<b style="color:#e6b422">经中继 {_p}</b> '
                     f'<span style="color:#888">（依据：{_how.get(addr, "?")}{_hl_txt}）</span> '
                     f'· <a href="/topology" style="color:#7ecbff">看拓扑图</a>')
    else:
        link_html = ('<span style="color:#888">未知 – 先到 '
                     '<a href="/topology" style="color:#7ecbff">网络拓扑</a>'
                     ' 点一次「刷新拓扑」</span>')

    # Image-Typ vorbelegen:中继固件的 model 是 ESP32-C6-Repeater。
    # 认不出来时默认终端节点（0x0001），用户可以自己改。
    model_l = (dev.get("model") or "").lower()
    default_type = 0x0002 if ("repeater" in model_l or "中继" in model_l) else 0x0001
    sel_ed = "selected" if default_type == 0x0001 else ""
    sel_rp = "selected" if default_type == 0x0002 else ""

    wr = (s.get("wr_power") or {}).get(addr)
    if wr:
        last_wr = (f"{wr.get('ts','')} 已下发 <b>{wr.get('dbm')} dBm</b>"
                   f"（tsn {wr.get('tsn','?')}；以中继自己的串口日志为准）")
    else:
        last_wr = '<span style="color:#888">本次运行还没下发过</span>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
{_autoreload_js(15)}
<title>设备 {addr}</title>
<style>
  body{{background:#1a1a2e;color:#e0e0e0;font-family:monospace;margin:2em}}
  h1{{color:#00d4ff}} h2{{color:#a0c4ff;border-bottom:1px solid #333;padding-bottom:.3em}}
  h3{{color:#7ecbff;margin-top:1.2em}}
  table{{border-collapse:collapse;width:100%;margin:.5em 0}}
  th,td{{border:1px solid #444;padding:.4em .8em;text-align:left}}
  th{{background:#2a2a4a}} tr:nth-child(even){{background:#1e1e3a}}
  section{{background:#16213e;padding:1.2em;margin:1.2em 0;border-radius:6px}}
  a{{color:#7ecbff}}
  button{{background:#2a4a7a;color:#e0e0e0;border:1px solid #4a6a9a;
          padding:.3em .8em;cursor:pointer;border-radius:3px}}
  button:hover{{background:#3a6aaa}}
</style></head>
<body>
<p><a href="/">← 返回仪表盘</a></p>
<h1>设备 <code>{addr}</code></h1>
<section>
<table>
  <tr><th>地址</th><td>{addr}</td></tr>
  <tr><th>Name</th><td>{dev.get('name', addr)}</td></tr>
  <tr><th>型号</th><td>{model_html}</td></tr>
  <tr><th>厂商</th><td>{mfr or '–'}</td></tr>
  <tr><th>IEEE</th><td>{dev.get('ieee', '–')}</td></tr>
  <tr><th>LQI</th><td>{lqi_bar}</td></tr>
  <tr><th>RSSI</th><td>{_rssi_cell(rssi)}</td></tr>
  <tr><th>角色</th><td>{role_html}</td></tr>
  <tr><th>连接方式</th><td>{link_html}</td></tr>
  <tr><th>最后上报</th><td>{_seen_txt}
    <span style="color:#888;font-size:.85em">（这台设备<b>自己</b>最后一次发帧的时刻；
    重启 add-on 会清零，所以重启后显示「—」是正常的，不代表掉线）</span></td></tr>
  <tr><th>可用性</th><td>{_avail_txt}</td></tr>
  <tr><th>状态刷新</th><td>{dev.get('last_seen', '–')}
    <span style="color:#888;font-size:.85em">（固件心跳批次时间：整批设备一起刷新，
    不是这台设备的通信时刻）</span></td></tr>
</table>
</section>
<h2>Cluster 数据 ({len(dev["clusters"])})</h2>
{clusters_html or "<em>暂无数据</em>"}
<section>
  <h2>命令</h2>
  <h3 style="color:#7ecbff;margin-top:.2em">重新读型号（ZCL Basic 0x0004 / 0x0005）</h3>
  <p style="font-size:.85em;color:#888;line-height:1.6;margin:.2em 0 .5em">
    型号<b>只在入网那一刻读一次</b>，网关随后把「已面试」写进 NVS，不会自动重读 ——
    那次没答上（休眠端点 / 刚入网抖动 / 当年固件还没逐端点扫 Basic）就永久缺失。
    这里手动补问一次；补读按端点逐个发，2~3 秒后刷新页面看结果。
  </p>
  <form method="POST" action="/api/device/{addr}/probe">
    <button type="submit">重新读型号</button>
  </form>
  <h3 style="color:#7ecbff;margin-top:1em">其他命令</h3>
  <form method="POST" action="/api/device/{addr}/cmd">
    <select name="cmd" style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em">
      <option value="switch2wifi">switch2wifi</option>
      <option value="node_desc">node_desc（查角色：Router / End Device）</option>
      <option value="leave">leave</option>
    </select>
    <button type="submit">发送</button>
  </form>
  <form method="POST" action="/api/device/{addr}/delete"
        onsubmit="return confirm('确定移除设备 {addr} 吗？')">
    <button type="submit" style="margin-top:.5em;background:#5a1a1a;color:#ffaaaa;border:1px solid #aa3333;padding:.25em .75em">&#x1F5D1; 从列表移除</button>
  </form>
  <h3 style="color:#7ecbff;margin-top:1em">休眠间隔</h3>
  <form method="POST" action="/api/sleep">
    <input name="addr" type="hidden" value="{addr}">
    <input name="secs" type="number" value="600" min="10" max="86400"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;
                  padding:.25em;width:80px"> 秒
    <button type="submit">设置</button>
  </form>
  <h3 style="color:#7ecbff;margin-top:1em">OTA 固件升级</h3>
  <p style="font-size:.85em;color:#888;line-height:1.6">
    不同设备跑的是不同固件，<b>必须先选对目标类型</b>：网关只把该 image type 的
    镜像通知给对应设备，类型不符的设备不会应答（不会刷坏，但也没反应）。<br>
    <code>0x0001</code> = 终端节点（client/，switch2wifi）&nbsp;·&nbsp;
    <code>0x0002</code> = 中继器（router/）<br>
    版本号取镜像文件的 mtime —— 重新编译出来的镜像自动就算是“更新”，不用手工填版本。
  </p>
  <form method="POST" action="/api/ota" enctype="multipart/form-data">
    <p style="margin:.3em 0">
      <select name="type" style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em">
        <option value="0x0001" {sel_ed}>0x0001 · 终端节点 (client)</option>
        <option value="0x0002" {sel_rp}>0x0002 · 中继器 (router)</option>
      </select>
      <input type="file" name="file" accept=".bin"
             style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    </p>
    <button type="submit">&#x2B06; 上传并开始 OTA</button>
  </form>
  <p style="font-size:.85em;color:#888;margin-top:.9em">或使用容器内已有的路径（<code>/share/…</code> 可由 Samba 访问）：</p>
  <form method="POST" action="/api/ota">
    <select name="type" style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em">
      <option value="0x0001" {sel_ed}>0x0001 · 终端节点</option>
      <option value="0x0002" {sel_rp}>0x0002 · 中继器</option>
    </select>
    <input name="path" value="" placeholder="/share/zigbee-repeater.bin" size="40"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    <button type="submit">按路径开始</button>
  </form>
  <p style="font-size:.85em;color:#888;margin-top:.8em">
    OTA 状态: <b>{s['ota']['status']}</b>
    &nbsp;·&nbsp; image type 0x{s['ota']['image_type']:04x}
    &nbsp;·&nbsp; {s['ota']['transferred']} / {s['ota']['size']} Byte<br>
    {s['ota']['bin_path'] or '（尚未选择镜像）'}<br>
    中继是常电 Router，不涉及休眠设备的轮询窗口；64 B/块的速度下
    569 KB 大约 10~30 分钟，期间别断电也别关这个页面所在的 add-on。
  </p>
  <h3 style="color:#7ecbff;margin-top:1em">中继发射功率（运行时，不用重刷固件）</h3>
  <p style="font-size:.85em;color:#888;line-height:1.6">
    写入中继固件里的厂商专有簇 <code>0xFC00 / 0x0001</code>（S8，dBm）：
    中继收到就立刻改 PA 并写进自己的 NVS，掉电再上电还是这个值。<br>
    <b>只对中继固件有效</b>（普通开关没有这个簇，写了也不会有应答）。<br>
    20 dBm 电流峰值最高，供电偏弱会触发 Brownout 反复复位 —— 遇到就降到 8/14。
  </p>
  <form method="POST" action="/api/device/{addr}/tx_power">
    <button name="dbm" value="20">20（最大）</button>
    <button name="dbm" value="14">14</button>
    <button name="dbm" value="8">8</button>
    <button name="dbm" value="2">2</button>
    <button name="dbm" value="0">0</button>
  </form>
  <p style="font-size:.85em;color:#888;margin-top:.5em">
    下发记录: {last_wr}
  </p>
</section>
<footer style="margin-top:2em;color:#666;font-size:.85em">
  <span id="rl" style="color:#666">自动刷新 15 秒</span> &nbsp;|&nbsp;
  <a href="javascript:location.reload()">立即刷新</a> &nbsp;|&nbsp; <a href="/api/state">JSON 状态</a>
</footer>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass   # kein Access-Log im stdout

    def _send(self, code, ctype, body):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(data))
        # ★ 禁止浏览器缓存：拓扑页每 15 s 用 location.reload() 自刷新，
        #   一旦被缓存就会"看起来一直不变"，非得手动点刷新才更新。
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _html())
        elif path.startswith("/device/"):
            addr = path[len("/device/"):]
            page = _device_html(addr)
            if page:
                self._send(200, "text/html; charset=utf-8", page)
            else:
                self._send(404, "text/html; charset=utf-8",
                           f"<h2>设备 {addr} 不存在</h2><a href='/'>← 返回</a>")
        elif path == "/api/state":
            with _state_lock:
                self._send(200, "application/json", json.dumps(_state, indent=2))
        elif path in ("/topology", "/topo"):
            self._send(200, "text/html; charset=utf-8", _topo_html())
        elif path == "/api/topo":
            with _state_lock:
                self._send(200, "application/json",
                           json.dumps(_state.get("topo") or {}, indent=2))
        else:
            self._send(404, "text/plain", "Not Found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        # ★ Content-Type NICHT für die Multipart-Auswertung klein schreiben:
        #   der boundary-Parameter ist **case-sensitive**. Chrome und curl
        #   schicken gemischte Namen (----WebKitFormBoundary…, ----2YSIdO…).
        #   Mit .lower() findet body.split(boundary) nichts, der ganze Body
        #   gilt dann als EIN Part – Folge: image_type fällt auf 0x0001 zurück
        #   und in die Datei landen ~218 Byte Part-Header VOR dem Image
        #   (OTA scheitert dann an der Image-Prüfung).
        raw_ctype = self.headers.get("Content-Type") or ""
        ctype  = raw_ctype.lower()
        raw    = self.rfile.read(length) if length else b""
        # Datei-Upload (multipart) nicht durch parse_qs schicken – das würde
        # die Binärdaten verstümmeln.
        if ctype.startswith("multipart/form-data"):
            params = {}
        else:
            params = parse_qs(raw.decode("utf-8", errors="replace"))
        # Query-String zusätzlich übernehmen: parse_qs(raw) liest nur den Body,
        # dadurch fiel /api/device/x/tx_power?dbm=20 stillschweigend auf den
        # Default (8 dBm) zurück – ohne Fehlermeldung.
        for _k, _v in parse_qs(urlparse(self.path).query).items():
            params.setdefault(_k, _v)
        path   = urlparse(self.path).path
        if path == "/api/topo":
            # 刷新拓扑：后台逐台探测 + dump，立即 303 回拓扑页
            _topo_refresh()
            self.send_response(303)
            self.send_header("Location", "/topology")
            self.end_headers()
            return

        if path.startswith("/api/device/") and path.endswith("/probe"):
            # 手动补读型号：型号只在入网时读一次，错过就永久缺失，
            # 这里给一次「再问一遍」的机会（读 Basic 0x0004/0x0005）。
            addr = path[len("/api/device/"):-len("/probe")]
            _mb_next.pop(addr, None)
            _mb_tries.pop(addr, None)
            _probe_basic(addr)
            self.send_response(303)
            self.send_header("Location", f"/device/{addr}")
            self.end_headers()
            return

        if path.startswith("/api/device/") and path.endswith("/cmd"):
            addr = path[len("/api/device/"):-len("/cmd")]
            cmd  = params.get("cmd", [""])[0]
            if cmd == "switch2wifi":
                uart_msg = _js({"cmd": "switch2wifi"})
                ser.write(uart_msg.encode())
                print(f"[→C6] switch2wifi → {addr}")
            elif cmd == "node_desc":
                # ZDO 0x0002: MAC-Capability verrät, ob das Gerät als Router
                # arbeitet (bit1) und am Netz hängt (bit2).
                uart_msg = _js({"cmd": "node_desc", "addr": addr})
                ser.write(uart_msg.encode())
                print(f"[→C6] {uart_msg.strip()}")
            elif cmd == "leave":
                uart_msg = _js({"cmd": "leave"})
                ser.write(uart_msg.encode())
            self.send_response(302)
            self.send_header("Location", f"/device/{addr}")
            self.end_headers()
            return

        if path.startswith("/api/device/") and path.endswith("/tx_power"):
            addr = path[len("/api/device/"):-len("/tx_power")]
            dbm = params.get("dbm", ["8"])[0].strip()
            try:
                dbm_i = int(dbm)
            except ValueError:
                dbm_i = 8
            dbm_i = max(-24, min(20, dbm_i))
            # 网关把它翻成 ZCL Write Attribute 0xFC00/0x0001 发给中继
            mq_put(f"{BASE}/cmd/set_dev_power",
                   json.dumps({"addr": addr, "ep": 1, "dbm": dbm_i},
                              separators=(",", ":")), qos=1)
            print(f"[Web] set_dev_power {addr} → {dbm_i} dBm")
            self.send_response(303)
            self.send_header("Location", f"/device/{addr}")
            self.end_headers()
            return

        if path.startswith("/api/device/") and path.endswith("/delete"):
            addr = path[len("/api/device/"):-len("/delete")]
            with _state_lock:
                _state["devices"].pop(addr, None)
            try:
                with _db_lock:
                    cx = _db_connect()
                    if cx:
                        with cx.cursor() as c:
                            c.execute("DELETE FROM esp32_zigbee_devices WHERE mac=%s AND addr=%s",
                                      (DB_MAC, addr))
                        cx.commit()
            except Exception as e:
                print(f"[DB] delete {addr}: {e}")
            print(f"[Web] device {addr} deleted")
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if path == "/api/permit_join":
            secs = params.get("secs", ["0"])[0]
            mq_put(f"{BASE}/cmd/permit_join", secs, qos=1)
            print(f"[Web] permit_join {secs}s")

        elif path == "/api/tx_power":
            dbm = params.get("dbm", [""])[0].strip()
            mq_put(f"{BASE}/cmd/set_tx_power", dbm, qos=1)
            print(f"[Web] set_tx_power {dbm} dBm (Rücklesewert folgt)")

        elif path == "/api/lqi":
            mq_put(f"{BASE}/cmd/lqi", "", qos=1)
            print("[Web] LQI-Momentaufnahme angefordert")

        elif path == "/api/fw_ota":
            if ctype.startswith("multipart/form-data"):
                bin_path = _save_upload(raw_ctype, raw) or ""
            else:
                bin_path = params.get("path", [""])[0].strip()
            if bin_path:
                # Manueller Start setzt das automatische Neustart-Budget zurück
                globals()["_fw_ota_restarts"] = 0
                _fw_ota_start(bin_path)
            else:
                print("[OTA] kein Firmware-Pfad / Upload leer – abgebrochen")

        elif path == "/api/fw_flash":
            # Notfall-Weg über den Bootloader (esptool), unabhängig davon,
            # ob die laufende Firmware fw_ota_data versteht.
            if ctype.startswith("multipart/form-data"):
                bin_path = _save_upload(raw_ctype, raw) or ""
            else:
                bin_path = params.get("path", [""])[0].strip()
            if bin_path and os.path.isfile(bin_path):
                size = os.path.getsize(bin_path)
                print(f"[FLASH] angefordert: {bin_path} ({size} B @ {APP_OFFSET})")
                threading.Thread(target=flash_apply, args=(bin_path,),
                                 daemon=True).start()
            else:
                print("[FLASH] kein Firmware-Pfad / Upload leer – abgebrochen")

        elif path == "/api/ota/stop":
            # ★ 中止 OTA：否则一次误点会白占一个多小时的无线带宽（~110 B/s）。
            # 网关侧 zb_ota_server_stop() 会停掉预取任务，客户端等不到数据就自己放弃。
            ser.write(_js({"cmd": "ota_stop"}).encode())
            _ota_close()
            with _state_lock:
                _state["ota"]["status"] = "idle"
            print("[OTA] 已中止（ota_stop 已下发）")

        elif path == "/api/serial_reopen":
            # 手动兜底：串口 EIO 卡住时不用重启整个 add-on。
            # 先重开 fd；再硬复位一次 C6（端口活着但芯片挂了也一并治好）。
            # ⚠ 最多阻塞 ~45 s（6 次指数退避重试），浏览器可能先超时，属正常。
            print("[Serial] 收到手动重开请求")
            ok = _serial_reopen("手动 /api/serial_reopen")
            print(f"[Serial] 手动重开结果: {'成功' if ok else '失败（见上面的重试日志）'}")
            if ok:
                _serial_kick_reset("手动 /api/serial_reopen 配套复位")

        elif path == "/api/ota":
            # image type: 0x0001 终端节点 / 0x0002 中继器
            if ctype.startswith("multipart/form-data"):
                image_type = _ota_image_type(_mp_field(raw_ctype, raw, "type"))
                # 每个类型独立的落盘文件：否则上传中继镜像会覆盖终端节点镜像
                base = "/data" if os.path.isdir("/data") else "/tmp"
                dst = f"{base}/ota_0x{image_type:04x}.bin"
                bin_path = _save_upload(raw_ctype, raw, dst=dst) or ""
            else:
                image_type = _ota_image_type(params.get("type", [None])[0])
                bin_path = params.get("path", [""])[0].strip()
            print(f"[OTA] /api/ota type=0x{image_type:04x} path={bin_path or '(leer)'}")
            _ota_start(bin_path, image_type)

        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()


class _ReuseHTTPServer(HTTPServer):
    allow_reuse_address = True

def _run_web():
    srv = _ReuseHTTPServer(("0.0.0.0", WEB_PORT), _Handler)
    print(f"[Web] Dashboard → http://0.0.0.0:{WEB_PORT}/")
    srv.serve_forever()

# ── Start ───────────────────────────────────────────────────────────────────
db_init()

print(f"[Serial] öffne {args.port} @ {args.baud} …")
# ★ 开机打不开端口时**不要**直接抛异常退出：那会被 Supervisor 判定成
#   "启动失败" 反复重启（2026-10-26 重建后就是这样）。先用占位串口把
#   Web UI / MQTT 拉起来，主循环的看门狗会自己重连。
ser = _serial_open(args.port, args.baud)

# Persistente Knoten-Zuordnung laden – noch VOR dem ersten Discovery-Publish,
# sonst entstehen nach einem Neustart neue Node-IDs und damit neue Entities.
_nodes_load()
_txp_load()

print(f"[MQTT] verbinde {args.broker}:{args.mqport} …")
mq.connect(args.broker, args.mqport, keepalive=60)
mq.loop_start()

threading.Thread(target=_run_web, daemon=True).start()
# 型号补读：入网那次没读到 Basic 的设备，定期补问（指数退避，读到就停）
threading.Thread(target=_model_backfill_task, daemon=True).start()
# 拓扑自动采集：让拓扑页像 HA 的 BLE 图那样自己更新，不用手点刷新
if args.topo_interval and int(args.topo_interval) > 0:
    threading.Thread(target=_topo_auto_loop,
                     args=(int(args.topo_interval),), daemon=True).start()
    print(f"[TOPO] 自动采集已开启：每 {int(args.topo_interval)} s 一轮")
else:
    print("[TOPO] 自动采集已关闭（--topo-interval 0）")
# 每台设备的可用性：把"记分板"换算成 online/offline 推给 HA
if DEVICE_AVAIL and HA_ENABLED:
    threading.Thread(target=_avail_task, daemon=True).start()
    print("[AVAIL] 设备级可用性已开启（掉线设备的 HA 实体会变灰）")
else:
    print("[AVAIL] 设备级可用性已关闭（所有实体只看网关在线）")

# ★ 只在 boot 时重发还不够：如果 C6 没重启、只有 add-on 重启了，C6 那边
#   其实还留着上次设的功率，但 `_state["tx_power"]` 是空的、网页显示 "—"。
#   这里补一发：功率本来就在，属于幂等操作；顺便把回读值填上。
_txp_reapply(8.0)

# ── Hauptschleife ───────────────────────────────────────────────────────────
print("[Bridge] läuft – Ctrl+C zum Beenden")
buf = b""
read_err  = 0        # 连续读失败次数（成功一次就归零）
err_since = 0.0      # 第一连败的时刻，用来限速日志
last_rx   = time.time()   # 最后一次收到任意字节的时刻（判断"端口活着但没数据"）
last_kick = 0.0      # 上一次强制复位的时刻（限速，别把 C6 反复复位）
try:
    while True:
        # Während eines esptool-Flashs ist der Port geschlossen bzw. vom
        # Bootloader belegt – Leseschleife so lange anhalten.
        if _serial_pause.is_set():
            time.sleep(0.2)
            continue
        # ★ 饿死看门狗：端口不报错但**一个字节都没有**。协调器正常情况下
        #   每几秒就有心跳/上报，连续 45 s 静默 = 芯片侧挂了（固件卡死、
        #   USB-JTAG 端点停了），重开 fd 治不好，只能硬复位。见 _serial_kick_reset。
        if time.time() - last_rx > 45 and time.time() - last_kick > 90:
            last_kick = time.time()
            _serial_kick_reset(f"已 {int(time.time() - last_rx)} s 没收到任何字节")
            last_rx = time.time()      # 给它 45 s 重新起来
        try:
            raw = ser.read(ser.in_waiting or 1)
        except Exception as e:
            # ★ 关键：**重试同一个 fd 是没用的**。USB-CDC 端点失效后旧 fd
            #   永远返回 EIO（实测 100+ 次连败），必须关掉重开。见 _serial_reopen。
            read_err += 1
            now = time.time()
            if read_err == 1:
                err_since = now
                print(f"[Serial] Lese-Fehler: {e} – 累计到 10 次就重开端口")
            elif read_err == 10 or (read_err % 60 == 0):
                print(f"[Serial] 读失败 {read_err} 次 / {now - err_since:.0f} s"
                      f"（最后一次: {e}）→ 重开端口")
            if read_err >= 10 and not _serial_pause.is_set():
                if _serial_reopen(f"连续 {read_err} 次读失败: {e}"):
                    buf = b""       # 半行必须丢掉，否则和下一行粘成非法 JSON
                    read_err = 0
                    last_rx  = time.time()
            time.sleep(0.5)
            continue
        if read_err:
            print(f"[Serial] 读取恢复正常（此前连续失败 {read_err} 次）")
            read_err = 0
        if raw:
            last_rx = time.time()
            buf += raw
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line:
                    decoded = line.decode("utf-8", errors="replace")
                    print(f"[C6] {decoded}")
                    dispatch(decoded)
except KeyboardInterrupt:
    print("\nBeendet.")
finally:
    mq_put(f"{BASE}/status", "offline", retain=True, qos=1)
    db_upsert_gateway("offline")
    mq.loop_stop()
    ser.close()
