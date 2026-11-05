# 分离网关设计：ESP32-C6 只做最小化，Linux 主机做智能部分

> ⚠️ **历史设计文档**。本文描述的是早期“分离网关”方案（C6 自己连 WiFi 并发 MQTT，
> 主机侧跑 `gateway_service.py`）。现行实现已改为 **USB 串口 JSON-Lines + 单一
> add-on**（`host/addon/esp32c6_gateway/serial_gateway.py`），不再有 WiFi 与
> MR60BHA1 雷达传感部分。保留本文仅为记录当初的分层思路。

## 架构总览

```
┌─────────────────────────────────┐       MQTT        ┌────────────────────────────────┐
│        ESP32-C6 (C6)            │ ─────────────────▶ │    Linux-Host (Pi / Server)    │
│        Radio Bridge             │                    │    gateway_service.py          │
│                                 │ ◀───────────────── │                                │
│  ┌──────────────────────────┐   │  gw/<mac>/cmd/+   │  ✓ HA Auto-Discovery           │
│  │ Zigbee Coordinator       │   │                    │  ✓ Device-Name Store           │
│  │ (802.15.4 Radio)         │   │                    │  ✓ Web-Dashboard Port 8080     │
│  └──────────────────────────┘   │                    │  ✓ REST-API for Control        │
│  ┌──────────────────────────┐   │                    │  ✓ JSON-Logging                │
│  │ MR60BHA1 UART-Driver     │   │                    └────────────────────────────────┘
│  └──────────────────────────┘   │                              │
│  ┌──────────────────────────┐   │                              │ MQTT
│  │ MQTT Bridge (no HA)    │     │                              ▼
│  └──────────────────────────┘   │                    ┌────────────────────────────────┐
└─────────────────────────────────┘                    │     Mosquitto Broker           │
                                                        │     + Home Assistant           │
```
（图中标签用等宽英文以保证 ASCII 对齐：Radio Bridge = 射频桥；no HA = 不含 HA 逻辑）

---

## 第一层 —— ESP32-C6（射频桥，能多薄就多薄）

### C6 负责什么

| 职责 | 文件 |
|------|------|
| Zigbee 协调器（ZCL 分发 → 原始 MQTT） | `zb_gateway.c` |
| MR60BHA1 UART → 原始 MQTT | `mr60bha1.c` + `ha_mqtt.c` |
| MQTT 发布/订阅 | `ha_mqtt.c` |
| 通过 MQTT 命令开配对窗口 | `main.c` + `ha_mqtt.c` |
| 通过 Boot 键开配对窗口（兜底） | `main.c` |

### C6 不再做的事

- ❌ HA 自动发现
- ❌ HTTP 服务器
- ❌ 设备名数据库
- ❌ 字符串分类（只给原始整数）
- ❌ 任何超出传感器数据本身的逻辑

---

## MQTT 话题约定

### 发布（C6 → 主机）

| 话题 | 负载 | 说明 |
|------|------|------|
| `gw/<mac>/status` | `"online"` / `"offline"` | LWT + 上线通告 |
| `gw/<mac>/mr60bha1` | `{"bpm":72,"rpm":16,"bpm_cat":1,"rpm_cat":1,"status":2,"bpm_wave":0.85,"rpm_wave":0.42}` | 生命体征（原始整数） |
| `gw/<mac>/zigbee/0x1234/on_off` | `{"v":1}` | 1=开，0=关 |
| `gw/<mac>/zigbee/0x1234/temperature` | `{"raw":2150}` | 1/100 °C，由主机换算 |
| `gw/<mac>/zigbee/0x1234/humidity` | `{"raw":5500}` | 1/100 %，由主机换算 |
| `gw/<mac>/zigbee/0x1234/illuminance` | `{"raw":320}` | 勒克斯 |
| `gw/<mac>/zigbee/0x1234/occupancy` | `{"occ":1}` | 1=有人，0=无人 |
| `gw/<mac>/zigbee/0x1234/join` | `{"event":"joined","addr":"0x1234","ieee":"01:02:03:04:05:06:07:08"}` | 设备入网 |
| `gw/<mac>/zigbee/0x1234/raw` | `{"cluster":"0x0300","attr":"0x0000"}` | 未知簇 |
| `gw/<mac>/permit_join` | `{"open":true,"seconds":180}` | 配对窗口状态 |

### 订阅（主机 → C6）

| 话题 | 负载 | 说明 |
|------|------|------|
| `gw/<mac>/cmd/permit_join` | `"180"` 或 `"0"` | 开/关配对窗口 |

### 分类映射（bpm_cat / rpm_cat）

| 值 | 含义 |
|------|------|
| 0 | none（无信号） |
| 1 | normal（正常） |
| 2 | fast（偏快） |
| 3 | slow（偏慢） |

### 状态映射（status）

| 值 | 含义 |
|------|------|
| 0 | init（初始化） |
| 1 | calibrating（校准中） |
| 2 | measuring（测量中） |

---

## 第二层 —— Linux 主机（gateway_service.py）

### 功能

- **MQTT 订阅者**：监听 `gw/+/#`（所有桥实例）
- **HA 自动发现**：自动发送 `homeassistant/sensor/.../config`
  - 首次收到 `online` 状态 → 建 MR60BHA1 实体
  - 每个 Zigbee 簇的首包 → 建对应 Zigbee 实体
- **设备名持久化**：`/etc/esp32-gw/devices.json`
- **Web 面板**：8080 端口，每 10 秒自动刷新
- **REST 端点**：
  - `POST /api/permit_join` → 向 C6 发 MQTT 命令
  - `POST /api/rename` → 保存设备名
  - `GET  /api/state` → 完整状态（JSON）

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MQTT_HOST` | `localhost` | Mosquitto 地址 |
| `MQTT_PORT` | `1883` | Mosquitto 端口 |
| `MQTT_USER` | 空 | MQTT 用户名（可选） |
| `MQTT_PASS` | 空 | MQTT 密码（可选） |
| `WEB_PORT` | `8080` | Web 面板端口 |
| `DEVICES_FILE` | `/etc/esp32-gw/devices.json` | 设备名持久化文件 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

---

## 在 Linux 主机（树莓派）上安装

### 一次性配置（用 SSH 密钥免密）

```bash
# 在开发机上执行：
ssh-copy-id pi@192.168.178.218

# 然后：
scp -r host/ pi@192.168.178.218:~/esp32-gw/
ssh pi@192.168.178.218 'cd ~/esp32-gw && bash install.sh'
```

### install.sh 做了什么

1. 安装 `mosquitto` + `python3-venv`
2. 配置 Mosquitto 监听 1883（所有网卡，匿名）
3. 建 `/opt/esp32-gw/`，创建 Python venv，安装 paho-mqtt
4. 把 `esp32gw.service` 注册进 systemd 并启动

### 服务管理

```bash
# 查看状态
systemctl status esp32gw
journalctl -fu esp32gw

# 重启
systemctl restart esp32gw

# 手动开配对窗口（不用 Web 界面）
mosquitto_pub -t gw/<mac>/cmd/permit_join -m 180
mosquitto_pub -t gw/<mac>/cmd/permit_join -m 0

# 抓取全部 MQTT 报文
mosquitto_sub -t 'gw/#' -v
```

---

## 配置 ESP32-C6 固件

在 `main/main.c` 里改：

```c
#define WIFI_SSID        "DeinNetz"
#define WIFI_PASSWORD    "DeinPasswort"
#define MQTT_BROKER_URI  "mqtt://192.168.178.218"  // Pi 的 IP
```

编译与烧写：

```bash
cd zigbee-vital-sensor
idf.py build flash monitor
```

基础话题由 MAC 地址自动派生：

```
gw/a1b2c3d4/...
```

---

## 健壮性

| 场景 | 行为 |
|------|------|
| MQTT broker 不可达 | ESP32 自动重连；主机侧自动重连 |
| 主机服务崩溃 | systemd `RestartSec=5`，自动拉起 |
| C6 掉 WiFi | 重连 + MQTT LWT 发布 `offline` |
| C6 重启 | Zigbee 网络从 NVS 恢复 |
| 主机重启 | 下次 `online` 事件时重新发送 HA 发现 |
| 主机不在时配对 | Boot 键（GPIO9）直接开 180 秒 |
| 未知 Zigbee 簇 | 作为 `raw` 话题发布，不会崩 |

---

## 文件结构

```
esp-dev/
├── zigbee-vital-sensor/          ESP32-C6 固件（最小化）
│   └── main/
│       ├── main.c                WiFi + MQTT 初始化 + btn_task
│       ├── ha_mqtt.c/h           MQTT 桥（不含任何 HA 知识）
│       ├── zb_gateway.c/h        Zigbee 协调器（不含命名）
│       └── mr60bha1.c/h          MR60BHA1 UART 驱动（未改动）
│
└── host/                         Linux 主机
    ├── gateway_service.py        主服务（MQTT + Web + HA 发现）
    ├── requirements.txt          paho-mqtt
    ├── esp32gw.service           systemd unit
    └── install.sh                安装脚本
```
