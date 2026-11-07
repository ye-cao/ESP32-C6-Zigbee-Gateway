# Base image for the add-on runtime (Alpine + s6-overlay).
# NOTE: recent Supervisor versions no longer inject BUILD_FROM when building
# local add-ons (they only pass BUILD_VERSION/BUILD_ARCH), so this default is
# what actually gets used. Multi-arch tag: works on aarch64 (Pi) and amd64.
ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM $BUILD_FROM

# Python 3 runtime (base image is Alpine and does not ship Python).
# jq: run.sh parses /data/options.json with it.
# bash: run.sh uses a bash shebang.
# tzdata: 没有时区数据，libc 会忽略 TZ=Asia/Shanghai，日志时间戳会停在 UTC。
RUN apk add --no-cache python3 jq bash tzdata

# Isolated venv so pip never fights PEP 668 on newer Alpine.
# esptool: 当运行中的固件不认识应用层 OTA（fw_ota_data）时，
#          经 Bootloader（USB-Serial-JTAG）走的应急刷机通道。
#          固定在 <5，因为那个版本里命令还叫 "write_flash"。
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir pyserial paho-mqtt pymysql "esptool>=4.7,<5"

COPY serial_gateway.py /serial_gateway.py
COPY device_db.json /device_db.json
COPY run.sh /run.sh

# Windows 编辑器会存成 CRLF。行尾多一个 \r 会把 "#!/usr/bin/env bash"
# 变成 "#!/usr/bin/env bash\r"，容器启动直接报
#   env: can't execute 'bash<CR>': No such file or directory
# 所以这里强制统一成 LF（用 Alpine 镜像里的 Busybox sed）。
RUN sed -i 's/\r$//' /run.sh /serial_gateway.py /device_db.json \
    && chmod a+x /run.sh \
    && ! head -1 /run.sh | od -c | grep -qF '\r' \
    && echo "run.sh: shebang 无 CR，检查通过"

CMD [ "/run.sh" ]
