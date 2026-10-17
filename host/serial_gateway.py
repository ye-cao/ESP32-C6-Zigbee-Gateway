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
GW_VERSION = "1.1.6"

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

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"[MQTT] verbunden → {BASE}")
        client.publish(f"{BASE}/status", "online", retain=True, qos=1)
        client.subscribe(f"{BASE}/cmd/+", qos=1)
        client.subscribe(f"{BASE}/set/#", qos=1)   # HA-Entity-Befehle
        db_upsert_gateway("online")
        with _state_lock:
            _state["status"] = "online"
    else:
        print(f"[MQTT] Fehler rc={rc}")

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
    uart_msg = _js({"cmd": "dev_cmd", "addr": addr, "ep": ep, "sw": act})
    ser.write(uart_msg.encode())
    print(f"[→C6] {uart_msg.strip()}")

def on_message(client, userdata, msg):
    """Kommandos vom Host → an C6 über Serial weiterleiten."""
    payload = msg.payload.decode()
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
_ha_devices = {}   # addr → {"ieee", "mfr", "model"}
_ha_sent    = set()  # (node, object_id) – verhindert Neu-Publish pro Telegramm

def _ha_slug(s):
    return "".join(c if c.isalnum() else "_" for c in str(s)).strip("_").lower()

def _ha_node(addr):
    ieee = _ha_devices.get(addr, {}).get("ieee", "")
    return "c6_" + _ha_slug(ieee)[-16:] if ieee else "c6_" + _ha_slug(addr)

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
    # "EP1" ist bei Einzel-Endpunkt-Geräten nur Rauschen im Namen
    ep_label  = _ha_ep_label(addr, epn)
    ep_part   = "" if ep_label == "EP1" else ep_label + " "
    avail     = {
        "availability_topic":     f"{BASE}/status",
        "payload_available":      "online",
        "payload_not_available":  "offline",
    }

    objects = []   # (component, object_id, config)

    if base_name == "battery":
        if "pct" in p:
            objects.append(("sensor", f"{epn}_battery_pct", {
                "name": f"{dev['name']} {ep_part}电量",
                "state_topic": topic,
                "value_template": "{{ value_json.pct }}",
                "device_class": "battery", "unit_of_measurement": "%",
                "state_class": "measurement", **avail,
            }))
        if "voltage_mv" in p:
            objects.append(("sensor", f"{epn}_battery_volt", {
                "name": f"{dev['name']} {ep_part}电池电压",
                "state_topic": topic,
                "value_template": "{{ value_json.voltage_mv }}",
                "device_class": "voltage", "unit_of_measurement": "mV",
                "state_class": "measurement", **avail,
            }))
    elif base_name in _HA_MAP:
        comp, label, tpl, unit, dc, sc = _HA_MAP[base_name]
        cfg = {
            "name": f"{dev['name']} {ep_part}{label}",
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
        key = (node, obj_id)
        if key in _ha_sent:
            continue
        cfg["unique_id"] = f"{node}_{obj_id}"
        cfg["device"]    = dev
        mq.publish(f"{HA_PREFIX}/{comp}/{node}/{obj_id}/config",
                   json.dumps(cfg), retain=True, qos=1)
        _ha_sent.add(key)
        print(f"[HA] discovery {comp}/{node}/{obj_id} → {cfg['name']}")

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
    for k in [k for k in _ha_sent if k[0] == node]:
        _ha_sent.discard(k)

# ── Dispatcher ─────────────────────────────────────────────────────────────
def dispatch(line: str):
    mq.publish(f"{BASE}/console", line, qos=0)

    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        return

    t = msg.get("t")
    if not t:
        return

    if t == "boot":
        mq.publish(f"{BASE}/status", "online", retain=True, qos=1)
        db_upsert_gateway("online")
        with _state_lock:
            _state["status"] = "online"
        print("[C6] boot")

    elif t == "vitals":
        p = msg.get("p")
        if p:
            mq.publish(f"{BASE}/mr60bha2", json.dumps(p))

    elif t == "zigbee":
        addr = "0x{:04x}".format(msg.get("addr", 0))
        sub  = msg.get("sub", "raw")
        p    = msg.get("p")
        if p is not None:
            topic = f"{BASE}/zigbee/{addr}/{sub}"
            mq.publish(topic, json.dumps(p))
            print(f"[MQTT] {topic} {p}")
            ieee = msg.get("ieee", "")
            if ieee:
                _ha_devices.setdefault(addr, {})["ieee"] = ieee
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
        mq.publish(f"{BASE}/heartbeat", line, retain=True, qos=1)
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
            mq.publish(f"{BASE}/permit_join", json.dumps(p), qos=1)
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
        if act:
            print(f"[IV] {act} addr={msg.get('addr')} ep={msg.get('ep')} "
                  f"cluster={msg.get('cluster')} status={msg.get('status')}")
        else:
            print(f"[IV] {msg.get('state')} addr={msg.get('addr')} "
                  f"eps={msg.get('eps')} status={msg.get('status')}")

    elif t == "keepalive":
        # Keep-Alive-Test: periodische Read-Attribute nach Autorisierung
        print(f"[KA] addr={msg.get('addr')} ep={msg.get('ep')} "
              f"round={msg.get('round')}")

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
  </table>
  <h3>允许加入</h3>
  <form method="POST" action="/api/permit_join">
    <button name="secs" value="180">🔓 开启 180 秒</button>
    <button name="secs" value="0">🔒 关闭</button>
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
            mq.publish(f"{BASE}/cmd/permit_join", secs, qos=1)
            print(f"[Web] permit_join {secs}s")

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
    mq.publish(f"{BASE}/status", "offline", retain=True, qos=1)
    db_upsert_gateway("offline")
    mq.loop_stop()
    ser.close()
