#!/usr/bin/env python3
"""
ESP32-C6 Matter over Thread – Linux Host Gateway Service
=========================================================

Datenfluss:
  ESP32-C6 (RCP) ──USB/spinel──► otbr-agent ──Thread──► Matter 设备
                                                                │
  python-matter-server ◄──────────────────────────────────────┘
         │  (WebSocket ws://localhost:5580/ws)
         ▼
  gateway_service.py → MariaDB + Web-Dashboard (Port 8080)

Matter Custom Clusters (vendor-specific):
  0xFFF10001  HeartRate   attr 0x0000=bpm, 0x0001=bpm_cat, 0x0002=radar_status
  0xFFF10002  BreathRate  attr 0x0000=rpm, 0x0001=rpm_cat
"""

import asyncio
import json
import logging
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp
import pymysql
import websockets

# ── Konfiguration ─────────────────────────────────────────────────────────────
MATTER_WS_URL  = os.getenv("MATTER_WS_URL",  "ws://localhost:5580/ws")
OTBR_REST_URL  = os.getenv("OTBR_REST_URL",  "http://localhost:8081")
WEB_PORT       = int(os.getenv("WEB_PORT",   "8080"))
DEVICES_FILE   = Path(os.getenv("DEVICES_FILE", "/etc/esp32-gw/devices.json"))
LOG_LEVEL      = os.getenv("LOG_LEVEL", "INFO")

DB_HOST = os.getenv("DB_HOST", "192.168.178.218")
DB_USER = os.getenv("DB_USER", "gh")
DB_PASS = os.getenv("DB_PASS", "a12345")
DB_NAME = os.getenv("DB_NAME", "wagodb")
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s %(levelname)-7s %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gw")

# ── Custom Matter Cluster IDs ─────────────────────────────────────────────────
CLUSTER_HEARTRATE   = 0xFFF10001
CLUSTER_BREATHRATE  = 0xFFF10002
ATTR_VALUE          = 0x0000
ATTR_CATEGORY       = 0x0001
ATTR_RADAR_STATUS   = 0x0002

CAT_NAMES = {0: "none", 1: "normal", 2: "fast", 3: "slow"}
STATUS_NAMES = {0: "init", 1: "calibrating", 2: "measuring"}

# ── MariaDB ───────────────────────────────────────────────────────────────────
_db_lock = threading.Lock()
_db_conn = None


def _db_connect():
    return pymysql.connect(
        host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME,
        charset="utf8mb4", autocommit=True, connect_timeout=10,
    )


def _db_init():
    global _db_conn
    _db_conn = _db_connect()
    cur = _db_conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS matter_nodes (
            node_id     INT UNSIGNED NOT NULL,
            name        VARCHAR(64)  NOT NULL DEFAULT '',
            status      VARCHAR(16)  NOT NULL DEFAULT 'offline',
            last_seen   DATETIME,
            PRIMARY KEY (node_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS matter_vitals (
            id           INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            node_id      INT UNSIGNED NOT NULL,
            ts           DATETIME(3)  NOT NULL,
            bpm          SMALLINT UNSIGNED,
            rpm          SMALLINT UNSIGNED,
            bpm_cat      TINYINT UNSIGNED,
            rpm_cat      TINYINT UNSIGNED,
            radar_status TINYINT UNSIGNED,
            INDEX (node_id, ts)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    cur.close()
    log.info("DB initialisiert auf %s/%s", DB_HOST, DB_NAME)


def _db_cursor():
    global _db_conn
    try:
        _db_conn.ping(reconnect=True)
    except Exception:
        _db_conn = _db_connect()
    return _db_conn.cursor()


def db_upsert_node(node_id, status):
    with _db_lock:
        try:
            cur = _db_cursor()
            cur.execute("""
                INSERT INTO matter_nodes (node_id, status, last_seen)
                VALUES (%s, %s, NOW())
                ON DUPLICATE KEY UPDATE status=VALUES(status), last_seen=NOW()
            """, (node_id, status))
            cur.close()
        except Exception as e:
            log.warning("DB node upsert: %s", e)


def db_insert_vitals(node_id, vitals):
    with _db_lock:
        try:
            cur = _db_cursor()
            cur.execute("""
                INSERT INTO matter_vitals
                    (node_id, ts, bpm, rpm, bpm_cat, rpm_cat, radar_status)
                VALUES (%s, NOW(3), %s, %s, %s, %s, %s)
            """, (node_id,
                  vitals.get("bpm"), vitals.get("rpm"),
                  vitals.get("bpm_cat"), vitals.get("rpm_cat"),
                  vitals.get("radar_status")))
            cur.close()
        except Exception as e:
            log.warning("DB vitals insert: %s", e)


# ── Globaler State ────────────────────────────────────────────────────────────
_lock  = threading.Lock()
_nodes = {}   # node_id → {status, vitals:{bpm,rpm,...}, last_seen}
_names = {}   # str(node_id) → friendly_name
_thread_state = {"role": "unknown", "network": {}}


def _load_names():
    global _names
    try:
        if DEVICES_FILE.exists():
            _names = json.loads(DEVICES_FILE.read_text())
            log.info("已加载设备名：%d 条", len(_names))
    except Exception as e:
        log.warning("加载设备名失败：%s", e)


def _save_names():
    try:
        DEVICES_FILE.parent.mkdir(parents=True, exist_ok=True)
        DEVICES_FILE.write_text(json.dumps(_names, indent=2))
    except Exception as e:
        log.warning("保存设备名失败：%s", e)


def _node(node_id):
    if node_id not in _nodes:
        _nodes[node_id] = {
            "status":   "online",
            "vitals":   {"bpm": None, "rpm": None,
                         "bpm_cat": None, "rpm_cat": None,
                         "radar_status": None},
            "last_seen": None,
        }
    return _nodes[node_id]


# ── Matter Attribute Handler ──────────────────────────────────────────────────
def _on_attribute_updated(node_id, endpoint_id, cluster_id, attribute_id, value):
    """Verarbeitet ein Matter attribute_updated Event."""
    with _lock:
        nd = _node(node_id)
        nd["last_seen"] = datetime.utcnow().isoformat() + "Z"
        v = nd["vitals"]

        if cluster_id == CLUSTER_HEARTRATE:
            if attribute_id == ATTR_VALUE:
                v["bpm"] = value
            elif attribute_id == ATTR_CATEGORY:
                v["bpm_cat"] = value
            elif attribute_id == ATTR_RADAR_STATUS:
                v["radar_status"] = value

        elif cluster_id == CLUSTER_BREATHRATE:
            if attribute_id == ATTR_VALUE:
                v["rpm"] = value
            elif attribute_id == ATTR_CATEGORY:
                v["rpm_cat"] = value

        else:
            return  # anderer Cluster → ignorieren

        # 每次生命体征变化都写库（前提是所有值都齐了）
        if v["bpm"] is not None and v["rpm"] is not None:
            db_upsert_node(node_id, "online")
            db_insert_vitals(node_id, v)
            log.debug("[node %d] bpm=%s rpm=%s cat=%s/%s status=%s",
                      node_id, v["bpm"], v["rpm"],
                      v["bpm_cat"], v["rpm_cat"], v["radar_status"])


def _on_node_added(node_id):
    with _lock:
        _node(node_id)["status"] = "online"
    db_upsert_node(node_id, "online")
    log.info("[node %d] 已加入 / 在线", node_id)


def _on_node_removed(node_id):
    with _lock:
        if node_id in _nodes:
            _nodes[node_id]["status"] = "offline"
    db_upsert_node(node_id, "offline")
    log.info("[node %d] 已移除 / 离线", node_id)


# ── python-matter-server WebSocket Client ─────────────────────────────────────
async def _matter_ws_loop():
    """给 python-matter-server 用的常驻 WebSocket 客户端。"""
    retry_delay = 5
    msg_id = 0

    while True:
        try:
            log.info("正在连接 Matter 服务器：%s", MATTER_WS_URL)
            async with websockets.connect(
                MATTER_WS_URL,
                ping_interval=30,
                ping_timeout=10,
                open_timeout=10,
            ) as ws:
                retry_delay = 5
                log.info("Matter-Server verbunden")

                # Alle bekannten Nodes abfragen
                msg_id += 1
                await ws.send(json.dumps({
                    "message_id": str(msg_id),
                    "command": "get_nodes",
                }))

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type") or msg.get("message_type", "")

                    # ── get_nodes 的应答 ────────────────────────────────────
                    if msg.get("result") and isinstance(msg["result"], list):
                        for node in msg["result"]:
                            nid = node.get("node_id")
                            if nid is not None:
                                _on_node_added(nid)
                        continue

                    # ── Event ────────────────────────────────────────────────
                    event_data = msg.get("event") or {}
                    event_type = (
                        msg.get("event_type")
                        or event_data.get("event")
                        or ""
                    )

                    if event_type == "attribute_updated":
                        data = event_data.get("data") or event_data
                        node_id = data.get("node_id")
                        path = data.get("attribute_path", "")
                        value = data.get("value")

                        # Pfad-Format: "endpoint_id/cluster_id/attribute_id"
                        parts = str(path).split("/")
                        if len(parts) == 3 and node_id is not None:
                            try:
                                ep_id  = int(parts[0], 0)
                                cl_id  = int(parts[1], 0)
                                att_id = int(parts[2], 0)
                                _on_attribute_updated(
                                    node_id, ep_id, cl_id, att_id, value
                                )
                            except (ValueError, TypeError):
                                pass

                    elif event_type in ("node_added", "node_updated"):
                        data = event_data.get("data") or event_data
                        nid = data.get("node_id")
                        if nid is not None:
                            _on_node_added(nid)

                    elif event_type == "node_removed":
                        data = event_data.get("data") or event_data
                        nid = data.get("node_id")
                        if nid is not None:
                            _on_node_removed(nid)

        except (websockets.ConnectionClosed,
                OSError, asyncio.TimeoutError) as e:
            log.warning("Matter-Server getrennt: %s – Retry in %ds", e, retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
        except Exception as e:
            log.error("Matter WS 出错：%s", e)
            await asyncio.sleep(retry_delay)


# ── OTBR REST API Poller ──────────────────────────────────────────────────────
async def _otbr_poll_loop():
    """每 30 s 轮询一次 OTBR REST API，取 Thread 网络状态。"""
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{OTBR_REST_URL}/node",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        with _lock:
                            _thread_state["role"] = data.get("role", "unknown")
                            _thread_state["network"] = data
                        log.debug("OTBR role=%s", _thread_state["role"])
        except Exception as e:
            log.debug("OTBR poll: %s", e)
        await asyncio.sleep(30)


# ── Web Dashboard ─────────────────────────────────────────────────────────────
def _html_dashboard():
    with _lock:
        nodes_snap = json.loads(json.dumps(_nodes, default=str))
        ts_state   = dict(_thread_state)
        names_snap = dict(_names)

    thread_role  = ts_state.get("role", "?")
    net          = ts_state.get("network", {})
    network_name = net.get("NetworkName", "–")
    channel      = net.get("Channel", "–")
    panid        = net.get("PanId", "–")

    node_html = ""
    for node_id, nd in nodes_snap.items():
        v     = nd.get("vitals", {})
        name  = names_snap.get(str(node_id), f"Node {node_id}")
        ts    = (nd.get("last_seen") or "–")[:19]
        badge = "online" if nd.get("status") == "online" else "offline"

        bpm_cat     = CAT_NAMES.get(v.get("bpm_cat"), "–")
        rpm_cat     = CAT_NAMES.get(v.get("rpm_cat"), "–")
        radar_str   = STATUS_NAMES.get(v.get("radar_status"), "–")

        node_html += f"""
        <section>
          <h2>
            <form method='POST' action='/api/rename' style='display:inline'>
              <input name='node_id' type='hidden' value='{node_id}'>
              <input name='name' value='{name}' style='font-size:1.1em;font-weight:bold;
                background:transparent;border:none;border-bottom:1px dashed #555;
                color:#a0c4ff;width:200px'>
              <button style='font-size:.8em'>✎</button>
            </form>
            <span class='badge {badge}'>{nd.get("status","?")}</span>
            <span style='font-size:.8em;color:#888'>Node {node_id}</span>
          </h2>
          <table>
            <tr><th>Herzrate</th>
                <td>{v.get("bpm", "–")} BPM <span class='cat'>({bpm_cat})</span></td></tr>
            <tr><th>Atemrate</th>
                <td>{v.get("rpm", "–")} /min <span class='cat'>({rpm_cat})</span></td></tr>
            <tr><th>Radar</th>
                <td>{radar_str}</td></tr>
            <tr><th>Letzte Messung</th>
                <td>{ts}</td></tr>
          </table>
        </section>"""

    return f"""<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="10">
<title>Matter Gateway</title>
<style>
  body{{background:#1a1a2e;color:#e0e0e0;font-family:monospace;margin:2em}}
  h1{{color:#00d4ff}} h2{{color:#a0c4ff;border-bottom:1px solid #333;padding-bottom:.3em}}
  table{{border-collapse:collapse;width:100%;margin:.5em 0}}
  th,td{{border:1px solid #444;padding:.4em .8em;text-align:left}}
  th{{background:#2a2a4a;width:160px}} tr:nth-child(even){{background:#1e1e3a}}
  .badge{{padding:.2em .6em;border-radius:4px;font-size:.85em;margin-left:.5em}}
  .online{{background:#1a5c1a;color:#7fff7f}}
  .offline{{background:#5c1a1a;color:#ff7f7f}}
  .cat{{color:#888;font-size:.9em}}
  button{{background:#2a4a7a;color:#e0e0e0;border:1px solid #4a6a9a;
          padding:.2em .6em;cursor:pointer;border-radius:3px}}
  section{{background:#16213e;padding:1.2em;margin:1.2em 0;border-radius:6px}}
  .thread-box{{background:#0d1b2a;border:1px solid #2a4a6a;border-radius:6px;
               padding:.8em 1.2em;margin-bottom:1.2em;font-size:.9em}}
  .thread-box span{{color:#7ecbff}}
  footer{{margin-top:2em;color:#666;font-size:.85em}}
</style>
</head><body>
<h1>ESP32-C6 Matter over Thread – Dashboard</h1>
<div class='thread-box'>
  Thread Border Router &nbsp;|&nbsp;
  Role: <span>{thread_role}</span> &nbsp;|&nbsp;
  Network: <span>{network_name}</span> &nbsp;|&nbsp;
  Channel: <span>{channel}</span> &nbsp;|&nbsp;
  PAN: <span>{panid}</span>
</div>
<p>Auto-Refresh 10 s &nbsp;|&nbsp;
   <a href="/api/state" style="color:#7ecbff">JSON API</a> &nbsp;|&nbsp;
   <a href="/api/otbr" style="color:#7ecbff">OTBR Status</a>
</p>
{node_html or '<p><em>还没有连上任何 Matter 设备。</em></p>'}
<footer>
  Matter Server: {MATTER_WS_URL}<br>
  OTBR REST API: {OTBR_REST_URL}
</footer>
</body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code, ct, body):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", len(data))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _html_dashboard())
        elif path == "/api/state":
            with _lock:
                self._send(200, "application/json",
                           json.dumps(_nodes, indent=2, default=str))
        elif path == "/api/otbr":
            with _lock:
                self._send(200, "application/json",
                           json.dumps(_thread_state, indent=2))
        else:
            self._send(404, "text/plain", "Not Found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length).decode("utf-8", errors="replace")
        params = parse_qs(body)
        path   = urlparse(self.path).path

        if path == "/api/rename":
            nid  = params.get("node_id", [""])[0]
            name = params.get("name",    [""])[0].strip()
            if nid and name:
                with _lock:
                    _names[nid] = name
                _save_names()
                log.info("Umbenannt Node %s → %s", nid, name)
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
        else:
            self._send(404, "text/plain", "Not Found")


def _run_web():
    srv = HTTPServer(("0.0.0.0", WEB_PORT), _Handler)
    log.info("Web-Dashboard auf http://0.0.0.0:%d/", WEB_PORT)
    srv.serve_forever()


# ── asyncio event loop in eigenem Thread ─────────────────────────────────────
def _run_async_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    _db_init()
    _load_names()

    # 给 WebSocket + OTBR 轮询用的 asyncio 事件循环
    loop = asyncio.new_event_loop()
    t_async = threading.Thread(target=_run_async_loop, args=(loop,), daemon=True)
    t_async.start()

    asyncio.run_coroutine_threadsafe(_matter_ws_loop(), loop)
    asyncio.run_coroutine_threadsafe(_otbr_poll_loop(), loop)

    log.info("网关服务已启动 – Matter=%s  OTBR=%s",
             MATTER_WS_URL, OTBR_REST_URL)

    # Web-Server blockiert (Haupt-Thread)
    _run_web()


if __name__ == "__main__":
    main()
