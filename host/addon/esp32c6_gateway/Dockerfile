# Base image for the add-on runtime (Alpine + s6-overlay).
# NOTE: recent Supervisor versions no longer inject BUILD_FROM when building
# local add-ons (they only pass BUILD_VERSION/BUILD_ARCH), so this default is
# what actually gets used. Multi-arch tag: works on aarch64 (Pi) and amd64.
ARG BUILD_FROM=ghcr.io/home-assistant/base:latest
FROM $BUILD_FROM

# Python 3 runtime (base image is Alpine and does not ship Python).
# jq: run.sh parses /data/options.json with it.
# bash: run.sh uses a bash shebang.
# tzdata: ohne Zeitzonendaten ignoriert libc TZ=Asia/Shanghai und der
#         Log-Zeitstempel bliebe auf UTC.
RUN apk add --no-cache python3 jq bash tzdata

# Isolated venv so pip never fights PEP 668 on newer Alpine.
# esptool: Notfall-Flash über den Bootloader (USB-Serial-JTAG), wenn die
#          laufende Firmware den App-Level-OTA (fw_ota_data) nicht versteht.
#          Auf <5 festgenagelt, weil dort das Kommando noch "write_flash" heißt.
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir pyserial paho-mqtt pymysql "esptool>=4.7,<5"

COPY serial_gateway.py /serial_gateway.py
COPY device_db.json /device_db.json
COPY run.sh /run.sh

# Windows-Editoren speichern CRLF. Ein \r am Zeilenende macht aus
# "#!/usr/bin/env bash" ein "#!/usr/bin/env bash\r" – der Container stirbt dann
# beim Start mit:  env: can't execute 'bash<CR>': No such file or directory
# Deshalb hier hart auf LF normalisieren (Busybox-sed im Alpine-Image).
RUN sed -i 's/\r$//' /run.sh /serial_gateway.py /device_db.json \
    && chmod a+x /run.sh \
    && ! head -1 /run.sh | od -c | grep -qF '\r' \
    && echo "run.sh: Shebang ohne CR – OK"

CMD [ "/run.sh" ]
