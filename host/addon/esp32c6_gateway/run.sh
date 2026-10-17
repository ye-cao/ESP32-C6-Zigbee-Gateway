#!/usr/bin/env bash
# ESP32-C6 Zigbee Gateway add-on entrypoint.
# Reads /data/options.json (written by the Supervisor from the add-on config UI)
# and starts serial_gateway.py with matching CLI/env settings.
set -euo pipefail

CONFIG=/data/options.json
get() { jq -r "$1 // empty" "$CONFIG"; }

SERIAL=$(get '.serial_port')
BAUD=$(get '.baud')
MQTT_HOST=$(get '.mqtt_host')
MQTT_PORT=$(get '.mqtt_port')
MQTT_USER=$(get '.mqtt_user')
MQTT_PASS=$(get '.mqtt_pass')
BASE=$(get '.base_topic')
WEB_PORT=$(get '.web_port')
DB_HOST=$(get '.db_host')
TZ_NAME=$(get '.timezone')
FW_CHUNK=$(get '.fw_chunk')

: "${SERIAL:=/dev/ttyACM0}"
: "${BAUD:=115200}"
: "${MQTT_HOST:=127.0.0.1}"
: "${MQTT_PORT:=1883}"
: "${BASE:=gw/coordinator}"
: "${WEB_PORT:=8082}"
: "${TZ_NAME:=Asia/Shanghai}"
: "${FW_CHUNK:=80}"

export WEB_PORT
# Ohne TZ laeuft der Container in UTC – dann zeigt der Log-Zeitstempel 02:33
# statt 10:33. LOG_TZ liest serial_gateway.py aus, TZ gilt fuer alle Tools.
export LOG_TZ="$TZ_NAME"
export TZ="$TZ_NAME"
echo "[addon] timezone: $TZ_NAME"
# Frame-Groesse fuer das Seriell-OTA. 80 B = ~209 Zeichen pro Zeile und passt
# noch in den 256-Byte-Zeilenpuffer alter Firmware-Builds.
export FW_CHUNK
echo "[addon] ota frame: $FW_CHUNK B"
# DB is opt-in: only export DB_HOST when configured, otherwise the gateway
# runs without a database (HA recorder keeps the history).
if [ -n "$DB_HOST" ]; then
  export DB_HOST
  echo "[addon] database: $DB_HOST"
else
  echo "[addon] database: disabled"
fi

ARGS=(--port "$SERIAL" --baud "$BAUD" --broker "$MQTT_HOST" --mqport "$MQTT_PORT" --base "$BASE")
if [ -n "$MQTT_USER" ]; then
  ARGS+=(--user "$MQTT_USER" --pass "$MQTT_PASS")
fi

# Entwickler-Komfort: /serial_gateway.py wird beim Bauen ins Image kopiert
# (Dockerfile: COPY). Eine geaenderte Datei im Add-on-Ordner wirkt deshalb
# erst nach einem REBUILD – ein reiner Neustart faehrt weiter das alte Image.
# Damit man nicht jedes Mal neu bauen muss, wird eine im share-Ordner
# abgelegte (per Samba erreichbare) Kopie bevorzugt – dann genuegt Neustart.
if [ -f /share/serial_gateway.py ]; then
  if /opt/venv/bin/python -c \
      "compile(open('/share/serial_gateway.py', encoding='utf-8').read(), 'x', 'exec')" 2>/dev/null; then
    cp /share/serial_gateway.py /serial_gateway.py
    echo "[addon] CODE: /share/serial_gateway.py uebernommen (kein Rebuild noetig)"
  else
    echo "[addon] CODE: WARNUNG – /share/serial_gateway.py hat Syntaxfehler,"
    echo "[addon] CODE: Image-Version wird benutzt (alte Fassung!)"
  fi
else
  echo "[addon] CODE: Image-Version (Aenderungen brauchen ein Rebuild)"
fi

echo "[addon] serial=$SERIAL baud=$BAUD broker=$MQTT_HOST:$MQTT_PORT base=$BASE web=$WEB_PORT"
# -u: unbuffered stdout/stderr, otherwise the add-on log stays empty for a long
# time (Python block-buffers stdout when it is a pipe/container log).
exec /opt/venv/bin/python -u /serial_gateway.py "${ARGS[@]}"
