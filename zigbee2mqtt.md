# ESP32-C6 Zigbee → MQTT 网关

> ⚠️ **历史文档**：描述的是树莓派上 systemd 直跑 `host/serial_gateway.py` 的部署方式。
> 该脚本已于 2026-11-05 删除（早已分流、不再同步）；现行部署是 HAOS 上的 add-on
> `host/addon/esp32c6_gateway/`，端口 8082，话题前缀 `gw/coordinator`。

## 概览

网关把 **ESP32-C6**（Zigbee 协调器）通过 USB 串口接到 MQTT broker。ESP32-C6 通过
UART 发 JSON-Lines，网关解析后发布成 MQTT 话题。

```
Zigbee 设备 → ESP32-C6 → /dev/ttyACM1 → serial_gateway.py → MQTT (192.168.178.218:1883)
```

---

## 文件

| 路径 | 说明 |
|------|-------------|
| `/home/pi/python/esp32c6-zigbee-vital-gateway/host/serial_gateway.py` | 网关主脚本 |
| `/etc/systemd/system/zigbee-mqtt-gateway.service` | systemd 服务 |

---

## systemd 服务

### 管理命令

```bash
sudo systemctl start zigbee-mqtt-gateway     # 启动
sudo systemctl stop zigbee-mqtt-gateway      # 停止
sudo systemctl restart zigbee-mqtt-gateway   # 重启
sudo systemctl status zigbee-mqtt-gateway    # 查看状态
journalctl -u zigbee-mqtt-gateway -f         # 实时日志
journalctl -u zigbee-mqtt-gateway -n 100     # 最近 100 行
```

### 服务配置

```ini
User=pi  Group=dialout
Port:   /dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_B0:A6:04:87:10:E0-if00
Broker: 192.168.178.218:1883
Base:   gw/coordinator
Restart: on-failure (delay 5 s)
```

用稳定的 `by-id` 路径，可以保证 USB 重新插拔后仍绑定到正确的那块 ESP32-C6
（MAC `B0:A6:04:87:10:E0`）。

---

## 串口协议（ESP32-C6 → Pi）

ESP32-C6 通过 UART1（`115200 8N1`）发 JSON-Lines。DTR/RTS 保持 LOW，避免意外触发复位。

### 消息类型

| `"t"` | 含义 | 示例 |
|-------|-----------|---------|
| `boot` | ESP32-C6 已启动 | `{"t":"boot"}` |
| `heartbeat` | 心跳，约每 30 s | `{"t":"heartbeat","uptime":120,"ch":15,"pan":4660}` |
| `vitals` | MR60BHA2 生命体征 | `{"t":"vitals","p":{"hr":72,"rr":16,...}}` |
| `zigbee` | Zigbee 设备数据 | `{"t":"zigbee","addr":4919,"sub":"temp","p":{"temp":21.5}}` |
| `permit_join` | 配对窗口状态 | `{"t":"permit_join","p":{"open":true,"seconds":180}}` |

---

## MQTT 话题

### 网关发布（ESP32-C6 → broker）

| 话题 | 内容 | Retain | QoS |
|-------|--------|--------|-----|
| `gw/coordinator/status` | `online` / `offline` | 是 | 1 |
| `gw/coordinator/console` | 串口原始输出（每一行） | 否 | 0 |
| `gw/coordinator/heartbeat` | 心跳 JSON | 是 | 1 |
| `gw/coordinator/mr60bha2` | 生命体征 JSON | 否 | 0 |
| `gw/coordinator/zigbee/0x{addr}/{sub}` | Zigbee 设备数据 | 否 | 0 |
| `gw/coordinator/permit_join` | 配对状态 JSON | 否 | 1 |

**遗嘱（Last Will）：** `gw/coordinator/status` → `"offline"`（retain，QoS 1）——
断连时自动写入。

### 命令（broker → ESP32-C6）

网关订阅 `gw/coordinator/cmd/+`，把命令经串口转发给 ESP32-C6。

| 话题 | 负载 | 说明 |
|-------|---------|-------------|
| `gw/coordinator/cmd/permit_join` | 秒数（如 `180`） | 开配对窗口，`0` = 关闭 |
| `gw/coordinator/cmd/set_channel` | 信道 `11`–`26` | 切换 Zigbee 信道 |

**示例 —— 打开配对：**

```bash
mosquitto_pub -h 192.168.178.218 -t gw/coordinator/cmd/permit_join -m 180
```

---

## 脚本参数

```
serial_gateway.py [--port PORT] [--baud BAUD] [--broker HOST]
                  [--mqport PORT] [--user USER] [--pass PASS]
                  [--base BASE_TOPIC]
```

| 参数 | 默认值 | 说明 |
|-----------|----------|-------------|
| `--port` | `/dev/ttyACM0` | 串口设备 |
| `--baud` | `115200` | 波特率 |
| `--broker` | `192.168.178.218` | MQTT broker 地址 |
| `--mqport` | `1883` | MQTT 端口 |
| `--user` | – | MQTT 用户名（可选） |
| `--pass` | – | MQTT 密码（可选） |
| `--base` | `gw/c6serial` | MQTT 基础话题 |

---

## 硬件

| 部件 | 详情 |
|-----------|---------|
| 微控制器 | ESP32-C6 |
| USB ID | `Espressif USB JTAG/serial debug unit` |
| MAC | `B0:A6:04:87:10:E0` |
| 设备节点 | `/dev/ttyACM1`（走 USB-JTAG） |
| 传感器 | Seeed MR60BHA2（呼吸与心率） |
| 协议 | Zigbee（协调器模式） |

---

## 排错

**服务起不来：**

```bash
journalctl -u zigbee-mqtt-gateway -n 50
ls -l /dev/serial/by-id/usb-Espressif*
```

**找不到端口** → 检查 USB 线，`lsusb | grep Espressif`

**没有 MQTT 数据** → `mosquitto_sub -h 192.168.178.218 -t 'gw/coordinator/#' -v`

**Pi 用户不在 dialout 组：**

```bash
sudo usermod -aG dialout pi
# 之后重新登录，或重启服务
```
