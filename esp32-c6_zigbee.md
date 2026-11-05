# ESP32-C6 Zigbee 网关 —— 系统观察记录

记录当时运行中系统的真实软硬件结构（2026-03-22，通过 SSH/MQTT 监控分析）。

> ⚠️ **历史文档**：文中提到的 `host/serial_gateway.py`（树莓派 systemd 直跑的那份）
> 已于 2026-11-05 删除（它早已分流、不再同步）。现行部署是 HAOS 上的 add-on，
> 见 `README.md` / `CHANGELOG.md`。

---

## 硬件

### 设备

- **ESP32-C6 DevKit**（QFN40，Revision v0.2）
- 特性：Wi-Fi 6、BT 5 (LE)、IEEE 802.15.4、单核 + LP 核、160 MHz、40 MHz 晶振
- **MAC：** `b0:a6:04:87:10:e0`

### 开发板上的两个 USB 口

| 端口（Pi 侧） | USB 芯片 | ESP32-C6 侧 | 用途 |
|-----------------|----------|----------------|------------|
| `/dev/ttyACM0` | CH340（`usb-1a86_USB_Single_Serial`） | UART0（板载硬件桥） | **仅用于烧写**（esptool） |
| `/dev/ttyACM1` | USB-JTAG（`usb-Espressif_USB_JTAG_serial_debug_unit`） | USB-JTAG / USB-CDC（内置） | **数据通信**（运行期） |

> **重要：** `ttyACM0`（CH340）在运行期是哑的——没有任何输出。
> 所有 JSON 数据只从 `ttyACM1`（USB-JTAG）出来。

---

## 固件通信（`ha_mqtt.c`）

### 设计取舍

```c
CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y   // IDF 控制台 = USB-JTAG 口
esp_log_set_vprintf(null_vprintf)      // 所有 ESP_LOGI 完全静默
printf() / fflush(stdout)              // 只有干净的 JSON 经 stdout → ttyACM1
```

- 线上**没有调试垃圾**，只有 JSON-Lines
- `ESP_LOGI` 的输出在 `null_vprintf` 里被丢弃（包括 `main.c` 里的启动注释）
- `main.c` 里那句注释（`UART1 TX=GPIO16 RX=GPIO17`）**已过时**——UART1 并未使用

### 协议 C6 → Pi（stdout → ttyACM1）

| 消息 | JSON |
|---------|------|
| 启动 | `{"t":"boot"}` |
| 心跳（每 10 s） | `{"t":"heartbeat","uptime":<s>,"ch":<n>,"pan":"0x<hex>"}` |
| Zigbee 数据 | `{"t":"zigbee","addr":<uint>,"sub":"<name>","p":{...}}` |
| 配对窗口状态 | `{"t":"permit_join","p":{"open":true/false,"seconds":<n>}}` |

### 协议 Pi → C6（stdin ← ttyACM1）

| 命令 | JSON |
|----------|------|
| 开配对窗口 | `{"cmd":"permit_join","sec":<n>}` |
| 换信道 | `{"cmd":"set_channel","ch":<11-26>}` |

---

## systemd 服务

```ini
# /etc/systemd/system/zigbee-mqtt-gateway.service
ExecStart=/usr/bin/python3 -u \
    /home/pi/python/esp32c6-zigbee-vital-gateway/host/serial_gateway.py \
    --port /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_B0:A6:04:87:10:E0-if00 \
    --broker 192.168.178.218 \
    --base gw/coordinator
```

`serial_gateway.py` 从 `ttyACM1` 读取，并桥接到 MQTT。

---

## MQTT 话题（`gw/coordinator/...`）

| 话题 | 内容 | 来源 |
|-------|--------|--------|
| `gw/coordinator/status` | `online` / `offline`（LWT） | 服务启动 / 断连 |
| `gw/coordinator/heartbeat` | `{"t":"heartbeat","uptime":...,"ch":...,"pan":"..."}` | 每一条来自 ttyACM1 的心跳行 |
| `gw/coordinator/console` | 与 heartbeat 负载相同（原始） | ttyACM1 的**每一行** |
| `gw/coordinator/zigbee/0x<addr>/<sub>` | 传感器数据 | Zigbee 事件 |
| `gw/coordinator/permit_join` | `{"open":...,"seconds":...}` | 配对窗口状态 |

### 已知问题：`console` = `heartbeat`

在 `serial_gateway.py` 的 `dispatch()` 里：

```python
def dispatch(line: str):
    mq.publish(f"{BASE}/console", line, qos=0)   # ← 每一行原样发布
    ...
    elif t == "heartbeat":
        mq.publish(f"{BASE}/heartbeat", line, retain=True, qos=1)  # ← 又发一次
```

由于 C6 **只**发结构化 JSON（没有调试输出），心跳会落地两次——`console` 和
`heartbeat` 各一份，负载完全相同。

**修法：** 把 `dispatch()` 里的 `mq.publish(f"{BASE}/console", line)` 去掉，
或者只把非心跳行发到 `console`。

---

## Flash 分区表

用 `esptool` 从 `ttyACM0` 读出（偏移 `0x8000`）：

| 名称 | 类型 | 子类型 | 偏移 | 大小 |
|------|-----|--------|--------|-------|
| `nvs` | data | nvs | `0x9000` | 24 KB |
| `otadata` | data | ota | `0xf000` | 8 KB |
| `phy_init` | data | phy | `0x11000` | 4 KB |
| `ota_0` | app | ota_0 | `0x20000` | 1.75 MB |
| `ota_1` | app | ota_1 | `0x1e0000` | 1.75 MB |
| `zb_storage` | data | fat | `0x3a0000` | 512 KB |
| `zb_fct` | data | fat | `0x3a8000` | 64 KB |

- 支持 OTA（两个 app 槽 ota_0 / ota_1）
- Zigbee 网络持久化在 `zb_storage`（FAT，512 KB）
- Zigbee 出厂校准在 `zb_fct`（FAT，64 KB）

---

## 当时在跑的 MQTT 网关（总览）

```
mosquitto_sub -h 192.168.178.218 -t 'gw/#' -v
```

| 基础话题 | 设备 | 脚本 | 状态 |
|------------|-------|--------|--------|
| `gw/coordinator` | ESP32-C6 DevKit（ttyACM1） | `serial_gateway.py`（服务） | ✅ 在用 |
| `gw/c6serial` | ESP32-C6 DevKit（ttyACM1） | `c6_serial_gw.py`（非服务） | ⚠️ 已过时 |
| `gw/c6` | ESP32-C6 DevKit（ttyACM0） | `c6_serial_gw.py` | ❌ 离线 |
| `gw/client` | Zigbee 客户端固件 | — | 离线 |
