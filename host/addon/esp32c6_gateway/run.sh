#!/usr/bin/env bash
# ESP32-C6 Zigbee 网关 add-on 入口脚本。
# 从 /data/options.json（Supervisor 按配置页生成）读选项，
# 转换成 serial_gateway.py 的命令行参数与环境变量。
set -euo pipefail

CONFIG=/data/options.json
get() { jq -r "$1 // empty" "$CONFIG"; }

LINK=$(get '.link')
SERIAL=$(get '.serial_port')
TCP_PORT=$(get '.tcp_port')
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
TOPO_INT=$(get '.topo_interval')

: "${LINK:=uart}"
: "${SERIAL:=/dev/ttyACM0}"
: "${TCP_PORT:=12345}"
: "${BAUD:=115200}"
: "${MQTT_HOST:=127.0.0.1}"
: "${MQTT_PORT:=1883}"
: "${BASE:=gw/coordinator}"
: "${WEB_PORT:=8082}"
: "${TZ_NAME:=Asia/Shanghai}"
: "${FW_CHUNK:=80}"
# 0 = 关闭。和其他字段不同，这里的 "0" 是**合法值**，
# 所以不能用 "${VAR:=60}"（它只在变量为空/未设置时生效）—— 这正是想要的效果。
: "${TOPO_INT:=60}"

export WEB_PORT
# 容器默认没有 TZ，跑在 UTC 下 —— 日志时间戳会写 02:33 而不是 10:33。
# LOG_TZ 由 serial_gateway.py 读取，TZ 对所有子工具生效。
export LOG_TZ="$TZ_NAME"
export TZ="$TZ_NAME"
echo "[addon] 时区: $TZ_NAME"
# 串口 OTA 的单帧字节数。80 B ≈ 每行 209 个字符，
# 还能塞进老固件构建的 256 字节行缓冲里。
export FW_CHUNK
echo "[addon] OTA 帧长: $FW_CHUNK B"
# 数据库是可选功能：只有配置了 DB_HOST 才导出，否则网关不连库
#（历史数据交给 HA recorder 保存）。
if [ -n "$DB_HOST" ]; then
  export DB_HOST
  echo "[addon] 数据库: $DB_HOST"
else
  echo "[addon] 数据库: 未启用"
fi

echo "[addon] 拓扑自动采集: 每 ${TOPO_INT}s 一次（0 = 关闭）"

ARGS=(--link "$LINK" --port "$SERIAL" --baud "$BAUD"
      --tcp-port "$TCP_PORT"
      --broker "$MQTT_HOST" --mqport "$MQTT_PORT"
      --base "$BASE" --topo-interval "$TOPO_INT")
if [ -n "$MQTT_USER" ]; then
  ARGS+=(--user "$MQTT_USER" --pass "$MQTT_PASS")
fi

# 开发者便利：/serial_gateway.py 是在构建时拷进镜像的
#（Dockerfile: COPY）。所以改 add-on 目录里的文件**必须重建**才生效，
# 单纯重启还是跑旧镜像。为了免去每次重建，优先使用 share 目录里那份
#（可用 Samba 直接改）—— 这样只要重启就够了。
if [ -f /share/serial_gateway.py ]; then
  if /opt/venv/bin/python -c \
      "compile(open('/share/serial_gateway.py', encoding='utf-8').read(), 'x', 'exec')" 2>/dev/null; then
    cp /share/serial_gateway.py /serial_gateway.py
    echo "[addon] CODE: 已采用 /share/serial_gateway.py（无需重建）"
  else
    echo "[addon] CODE: 警告 —— /share/serial_gateway.py 有语法错误，"
    echo "[addon] CODE: 改用镜像里的版本（旧版本！）"
  fi
else
  echo "[addon] CODE: 使用镜像内版本（改动需要重建）"
fi

# 型号库同样支持 /share 覆盖：Z2M 每隔一阵就新增设备定义，
# 为了加几条型号就重建整个镜像太不划算。校验 JSON 合法后才替换，
# 免得一个手滑的半截文件把网关的设备识别全搞挂。
if [ -f /share/device_db.json ]; then
  if /opt/venv/bin/python -c \
      "import json; json.load(open('/share/device_db.json', encoding='utf-8'))" 2>/dev/null; then
    cp /share/device_db.json /device_db.json
    echo "[addon] DB: 已采用 /share/device_db.json（无需重建）"
  else
    echo "[addon] DB: 警告 —— /share/device_db.json 不是合法 JSON，沿用镜像内版本"
  fi
else
  echo "[addon] DB: 使用镜像内版本"
fi

echo "[addon] 通道=$LINK 串口=$SERIAL tcp=$TCP_PORT broker=$MQTT_HOST:$MQTT_PORT base=$BASE web=$WEB_PORT"
# -u：不缓冲 stdout/stderr。否则 add-on 日志会长时间空着
#（Python 在 stdout 是管道/容器日志时默认块缓冲）。
exec /opt/venv/bin/python -u /serial_gateway.py "${ARGS[@]}"
