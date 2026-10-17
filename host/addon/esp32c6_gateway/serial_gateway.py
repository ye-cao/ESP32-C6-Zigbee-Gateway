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
args = ap.parse_args()

BASE   = args.base
DB_MAC = BASE.split("/")[-1][:8]

# Muss zu config.json passen. Steht im Startbanner und im Web-UI – damit ist
# sofort erkennbar, ob der Container wirklich den neuen Code fährt.
GW_VERSION = "1.2.6"

# ── Home Assistant MQTT Auto-Discovery ────────────────────────────────────
# Jedes Zigbee-Gerät/Endpoint wird beim ersten Daten-Telegramm automatisch als
# HA-Entity angemeldet – es muss NICHTS in configuration.yaml eingetragen
# werden (vorausgesetzt die HA-MQTT-Integration ist aktiv).
HA_PREFIX  = os.getenv("HA_PREFIX", "homeassistant")
HA_ENABLED = os.getenv("HA_DISCOVERY", "1").lower() not in ("0", "no", "off", "false")

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
    "ota": {
        "status":      "idle",   # idle | pending | transferring | done | error
        "bin_path":    "",
        "size":        0,
        "version":     1,
        "transferred": 0,
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

# ── Coordinator-Firmware-OTA über Serial ────────────────────────────────────
# Bin liegt auf dem Pi; Blöcke gehen per UART an den C6, der sie via esp_ota
# in die nächste OTA-Partition schreibt. Flow-Control: FW_BATCH Blöcke pro
# Stoß, C6 quittiert alle FW_ACK_EVERY (=16) Blöcke mit st="write".
_fw_ota_file = None
_fw_ota_size = 0
_fw_ota_off  = 0


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

def _save_upload(ctype: str, body: bytes):
    """Speichert die erste Datei aus einem multipart/form-data-Body.

    Nötig, weil /data im Add-on-Container liegt und auf dem HA-Host nicht
    sichtbar ist – Hochladen im Browser ist der einzige Weg ohne Samba.
    Gibt den geschriebenen Pfad zurück (oder None).
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
        dst = "/data/fw_upload.bin" if os.path.isdir("/data") else "/tmp/fw_upload.bin"
        with open(dst, "wb") as f:
            f.write(data)
        print(f"[OTA] Upload: {len(data)} Bytes → {dst}")
        return dst
    return None

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
    def write(self, d):  return self.ser.write(d)
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
        try:
            ser = CoordinatorSerial(args.port, args.baud)
            print("[FLASH] Serial wieder offen – Brücke läuft weiter")
        except Exception as e:
            print(f"[FLASH] Port {args.port} konnte nicht neu geöffnet werden: {e}")
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


def _ha_avail():
    """Verfügbarkeit im aktuellen Format (Liste) statt des alten Einzelfelds.

    availability_mode "latest": die Entity gilt als verfügbar, sobald EIN
    Topic 'online' meldet. Mit nur einem Topic ist das gleichbedeutend mit
    dem alten Verhalten – aber die Form ist die, die HA weiterentwickelt
    (mehrere Quellen, z. B. Gateway UND Gerät, lassen sich später ergänzen,
    ohne alle Configs erneut zu schreiben).
    """
    return {
        "availability": [{
            "topic": f"{BASE}/status",
            "payload_available":     "online",
            "payload_not_available": "offline",
        }],
        "availability_mode": "latest",
    }


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
    avail     = _ha_avail()

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
        **_ha_avail(),
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
        mq_put(f"{BASE}/status", "online", retain=True, qos=1)
        db_upsert_gateway("online")
        with _state_lock:
            _state["status"] = "online"
        print("[C6] boot")

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
                dev["last_seen"] = _now()

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
        a = msg.get("addr", "?")
        if msg.get("error"):
            print(f"[ND] {a}: Abfrage fehlgeschlagen ({msg.get('error')})")
        else:
            print(f"[ND] {a}: role={msg.get('role')} power={msg.get('power')} "
                  f"rx_idle={msg.get('rx_idle')} cap={msg.get('cap')}")
        with _state_lock:
            _state.setdefault("node_desc", {})[a] = msg

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


def _ota_start(bin_path: str):
    """Startet OTA: öffnet Binary, sendet ota_start an Coordinator."""
    global _ota_file
    import os as _os
    if not bin_path:
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
    # Versionsnummer aus Datei-Mtime ableiten (Unix-Timestamp, 32-bit)
    version = int(_os.path.getmtime(bin_path)) & 0xFFFFFFFF
    _ota_close()
    _ota_file = open(bin_path, "rb")
    with _state_lock:
        _state["ota"].update({
            "status":      "pending",
            "bin_path":    bin_path,
            "size":        size,
            "version":     version,
            "transferred": 0,
        })
    cmd = json.dumps({"cmd": "ota_start", "size": size, "ver": version})
    ser.write((cmd + "\n").encode())
    print(f"[OTA] gestartet: {bin_path} size={size} ver=0x{version:08x}")


def _ota_handle_req(offset: int, size: int):
    """Antwortet auf ota_req: liest Chunk aus Binärdatei und sendet hex-kodiert."""
    global _ota_file
    with _state_lock:
        ota = _state["ota"]
        active = ota["status"] in ("pending", "transferring")

    if not active or _ota_file is None:
        print(f"[OTA] ota_req off={offset} aber kein Transfer aktiv")
        return

    try:
        _ota_file.seek(offset)
        data = _ota_file.read(size)
        hex_data = data.hex()
        cmd = json.dumps({"cmd": "ota_data", "off": offset, "data": hex_data})
        ser.write((cmd + "\n").encode())
        with _state_lock:
            _state["ota"]["status"] = "transferring"
            _state["ota"]["transferred"] = offset + len(data)
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
        lqi_html  = (f'<meter value="{lqi}" min="0" max="255" style="width:50px"></meter> {lqi}'
                     if isinstance(lqi, int) else "–")
        rssi_html = (f'{rssi} dBm' if isinstance(rssi, int) else "–")
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
  <h3>OTA 固件升级</h3>
  <table>
    <tr><th>Status</th><td>{s['ota']['status']}</td></tr>
    <tr><th>Binary</th><td>{s['ota']['bin_path'] or '–'}</td></tr>
    <tr><th>进度</th><td>{s['ota']['transferred']} / {s['ota']['size']} Byte</td></tr>
  </table>
  <form method="POST" action="/api/ota">
    <input name="path" value="{s['ota']['bin_path'] or default_bin}" size="60"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    <button type="submit">⬆ 开始 OTA</button>
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
    lqi_bar = (f'<meter value="{lqi}" min="0" max="255" style="width:80px;vertical-align:middle"></meter> {lqi}'
               if isinstance(lqi, int) else "–")

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
  <tr><th>IEEE</th><td>{dev.get('ieee', '–')}</td></tr>
  <tr><th>LQI</th><td>{lqi_bar}</td></tr>
  <tr><th>RSSI</th><td>{"" + str(rssi) + " dBm" if isinstance(rssi, int) else "–"}</td></tr>
  <tr><th>角色</th><td>{role_html}</td></tr>
  <tr><th>最近通信</th><td>{dev.get('last_seen', '–')}</td></tr>
</table>
</section>
<h2>Cluster 数据 ({len(dev["clusters"])})</h2>
{clusters_html or "<em>暂无数据</em>"}
<section>
  <h2>命令</h2>
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
  <h3 style="color:#7ecbff;margin-top:1em">OTA Firmware-Update</h3>
  <form method="POST" action="/api/ota">
    <input name="path" value="" placeholder="留空 = client/build/zigbee-client.bin" size="50"
           style="background:#2a2a4a;color:#e0e0e0;border:1px solid #555;padding:.25em .4em">
    <button type="submit">&#x2B06; 开始 OTA</button>
  </form>
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
        else:
            self._send(404, "text/plain", "Not Found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        ctype  = (self.headers.get("Content-Type") or "").lower()
        raw    = self.rfile.read(length) if length else b""
        # Datei-Upload (multipart) nicht durch parse_qs schicken – das würde
        # die Binärdaten verstümmeln.
        if ctype.startswith("multipart/form-data"):
            params = {}
        else:
            params = parse_qs(raw.decode("utf-8", errors="replace"))
        path   = urlparse(self.path).path
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
                bin_path = _save_upload(ctype, raw) or ""
            else:
                bin_path = params.get("path", [""])[0].strip()
            if bin_path:
                _fw_ota_start(bin_path)
            else:
                print("[OTA] kein Firmware-Pfad / Upload leer – abgebrochen")

        elif path == "/api/fw_flash":
            # Notfall-Weg über den Bootloader (esptool), unabhängig davon,
            # ob die laufende Firmware fw_ota_data versteht.
            if ctype.startswith("multipart/form-data"):
                bin_path = _save_upload(ctype, raw) or ""
            else:
                bin_path = params.get("path", [""])[0].strip()
            if bin_path and os.path.isfile(bin_path):
                size = os.path.getsize(bin_path)
                print(f"[FLASH] angefordert: {bin_path} ({size} B @ {APP_OFFSET})")
                threading.Thread(target=flash_apply, args=(bin_path,),
                                 daemon=True).start()
            else:
                print("[FLASH] kein Firmware-Pfad / Upload leer – abgebrochen")

        elif path == "/api/ota":
            bin_path = params.get("path", [""])[0].strip()
            _ota_start(bin_path)

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
ser = CoordinatorSerial(args.port, args.baud)

# Persistente Knoten-Zuordnung laden – noch VOR dem ersten Discovery-Publish,
# sonst entstehen nach einem Neustart neue Node-IDs und damit neue Entities.
_nodes_load()

print(f"[MQTT] verbinde {args.broker}:{args.mqport} …")
mq.connect(args.broker, args.mqport, keepalive=60)
mq.loop_start()

threading.Thread(target=_run_web, daemon=True).start()

# ── Hauptschleife ───────────────────────────────────────────────────────────
print("[Bridge] läuft – Ctrl+C zum Beenden")
buf = b""
try:
    while True:
        # Während eines esptool-Flashs ist der Port geschlossen bzw. vom
        # Bootloader belegt – Leseschleife so lange anhalten.
        if _serial_pause.is_set():
            time.sleep(0.2)
            continue
        try:
            raw = ser.read(ser.in_waiting or 1)
        except Exception as e:
            # Port wurde unter uns geschlossen/neu geöffnet → kurz warten
            if not _serial_pause.is_set():
                print(f"[Serial] Lese-Fehler: {e} – versuche weiter")
            time.sleep(0.5)
            continue
        if raw:
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
