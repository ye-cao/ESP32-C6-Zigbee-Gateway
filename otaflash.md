# ESP32-C6 Zigbee 固件的 OTA 刷机

> ⚠️ **历史文档**：这里走的是 **ESPHome espota2（TCP 3232）** 那条老路，只在设备
> 还跑着 ESPHome 固件时可用。首次刷成 Zigbee 固件后 3232 端口就不存在了。
> 现行刷协调器的方式是 add-on 侧直刷：`POST /api/fw_flash`（esptool，约 2 秒），
> 见 `CHANGELOG.md` 与 `main/` 相关说明。

## 前提

- ESP32C6 在同一网络可达（已知 IPv4）
- 装了 `esphome` Python 包（`pip install esphome`）
- 固件二进制已就绪：`firmware/zigbee-vital-sensor.bin`
- ESP32C6 当前跑的是 **ESPHome 固件**（3232 端口开着）

## 第 1 步：探测开放端口

```bash
nmap -p 3232,80,8080,8266 <IP>
```

3232 端口（ESPHome OTA）必须是 `open`。

## 第 2 步：测试 OTA 协议

```bash
python3 -c "
import socket
MAGIC = [0x6C, 0x26, 0xF7, 0x5C, 0x45]
s = socket.socket(); s.settimeout(5)
s.connect(('<IP>', 3232))
s.sendall(bytes(MAGIC))
print([hex(b) for b in s.recv(10)])
s.close()
"
```

预期返回：`['0x0', '0x2']` → RESPONSE_OK，OTA 版本 2.0

## 第 3 步：烧写固件（经 espota2 做 OTA）

```bash
python3 -c "
import socket, logging, sys
from pathlib import Path
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
from esphome.espota2 import perform_ota

bin_path = Path('firmware/zigbee-vital-sensor.bin')
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(120)
sock.connect(('<IP>', 3232))
with open(bin_path, 'rb') as f:
    perform_ota(sock, None, f, bin_path)
sock.close()
"
```

耗时：约 1.47 MB 大致 8 秒。

## 第 4 步：改 MQTT broker 地址

文件：`main/main.c` 第 21 行：

```c
#define MQTT_BROKER_URI  "mqtt://192.168.178.218"
```

## 第 5 步：重新编译固件

```bash
cd /home/pi/esp32c6-repo
source /home/pi/esp-idf/export.sh
idf.py build
cp build/zigbee-vital-sensor.bin firmware/zigbee-vital-sensor.bin
```

## 第 6 步：烧写新编译的固件

用更新后的二进制重复第 3 步。

## 重要提示

第一次刷成 **Zigbee 固件**之后，3232 端口（ESPHome OTA）**就不存在了**——
Zigbee 固件没有实现 ESPHome OTA。后续想再刷固件只有两条路：

- Zigbee 固件自己实现一套 OTA，或者
- 用 **USB/串口** 重新烧写：

```bash
python3 -m esptool --chip esp32c6 -b 460800 \
  --before default_reset --after hard_reset \
  write_flash --flash_mode dio --flash_size 2MB --flash_freq 80m \
  0x0  build/bootloader/bootloader.bin \
  0x8000 build/partition_table/partition-table.bin \
  0x10000 build/zigbee-vital-sensor.bin
```

> 注：现行分区表把 app 放在 `0x20000`（不是这里的 `0x10000`），
> 且有两份 ota 槽；以上命令仅适用于文档当时那版固件。

## WiFi 凭据

编译前在 `main/main.c` 里填（当前是占位符）：

```c
#define WIFI_SSID        "DeinNetzwerk"
#define WIFI_PASSWORD    "DeinPasswort"
```

`firmware/` 里那份预编译二进制当初是把真实凭据编进去的。
