# ESP32-C6 Zigbee Gateway

Split-architecture Zigbee 网关：**ESP32-C6** 作为 Zigbee 协调器，通过 USB 串口以
JSON-Lines 协议把设备数据转发给 **Home Assistant OS Add-on**，由 add-on 完成
MQTT 桥接、HA 自动发现和 Web 面板，无需任何 YAML 配置。

```
Zigbee 设备 ──802.15.4──► ESP32-C6（协调器, main/）
                              │ USB 串口（JSON-Lines, 115200）
                     HAOS Add-on（host/addon/esp32c6_gateway/）
                     serial_gateway.py v1.2.6
                              │
              ┌───────────────┼────────────────┐
        MQTT Broker      Web 面板 :8082    MariaDB（可选）
              │
        Home Assistant（MQTT 自动发现）
```

## 组件

| 路径 | 说明 |
|---|---|
| `main/` | 协调器固件（ESP-IDF v5.5.1 / ESP32-C6，esp-zigbee-lib 1.6.8）：64 设备容量、配对信息跨重启保持、运行时发射功率调节、LQI/RSSI 快照、串口 OTA |
| `client/` | 自研 Zigbee 终端节点固件（OnOff + 温度 + OTA client） |
| `host/addon/esp32c6_gateway/` | **主线网关（HAOS Add-on）**：HA MQTT 自动发现、情景面板按键事件、设备型号库（4 882 型号）、发现去重与孤儿清理、Web 面板 |
| `host/serial_gateway.py` | 旧版宿主机网关（systemd 路径），保留作对照 |
| `DEVICE_INTEGRATION_REPORT.md` | 完整的接入、定位与修复记录 |

## 核心功能

- **大容量与配对保持**：网络规模 64 设备（`esp_zb_overall_network_size_set` 扩表 +
  `max_children` 同步），NVRAM 永不在启动时清除，网络密钥/PAN/信道/地址表跨重启
  延续；自有设备表 {短址, IEEE} 也落 NVS，重启后 LQI/命令无需等设备重新播报
- **HA MQTT 自动发现**：switch / event / sensor 实体自动生成；情景面板（genScenes
  / genOnOff 按键上报）自动识别为 event 实体，真开关与纯面板靠 Simple-Descriptor
  簇方向 + Read-Attributes 应答自动判定
- **发现去重与孤儿清理**：discovery 配置按成品 JSON 比对去重（同 Z2M 做法），
  启动时回读 broker 上的既有配置只发差异，孤儿配置按 availability topic 归属清理
- **运行时发射功率**：−24…20 dBm 网页/MQTT 可调，回读实际生效值（`eff`），
  芯片会静默夹值所以只有回读值算数
- **LQI/RSSI 快照**：按需触发，网页一键获取全网链路质量
- **串口 OTA**：Web 界面直刷协调器固件；终端节点支持应用层 OTA
- **Web 面板**：设备列表、实时状态、配网（permit-join）、单设备命令、固件管理

## 安装与运行

### 1. 烧录协调器固件

```bash
source ~/esp-idf/export.sh
idf.py build
idf.py -p <COM口> flash
```

预编译镜像（可用 `esptool` 或 Web OTA）：

| 文件 | 说明 |
|---|---|
| `firmware/zigbee-vital-sensor-sdk16.bin` | **当前版本**：esp-zigbee-lib 1.6.8 / 64 设备 / 设备表 NVS 持久化 |
| `firmware/zigbee-vital-sensor-txpower.bin` | 旧栈（1.0.9）镜像，留作回退 |

> ⚠️ **从旧栈（1.0.9）升级到 SDK 1.6 会重建网络，设备必须重新配对一次** —— 已实测确认
> （2026-10-22）：1.6 的 ZBOSS 不认旧 NVRAM 数据集。重建之后的 NVRAM 即为 1.6 格式，
> 后续同大版本内的 OTA 不再需要重配。细节见 `DEVICE_INTEGRATION_REPORT.md` §23.6。
>
> 另：运行期 ESP_LOG 被静音（`ha_mqtt.c` 的 `null_vprintf` + console 走 USB-JTAG），
> 所以 `Netzgröße…` / `Neustart – Netzwerk wiederhergestellt` 这类日志在 UART/USB 上
> 都看不到；判断是否重启成功看 `[C6] boot` 与心跳 `uptime` 归零，判断网络是否重建看 PAN ID。
终端节点固件在 `client/`（`idf.py build` 编译）。

### 2. 安装 HAOS Add-on

1. 把 `host/addon/esp32c6_gateway/` 整个目录复制到 HAOS 的 `/addons/`
2. 设置 → 插件 → 右上角刷新 → 找到「ESP32-C6 Zigbee Gateway」→ 安装 → 启动
3. 面板 `http://<ha-ip>:8082/` 检查状态，`permit_join` 配网后设备自动出现在 HA

Add-on 配置（options）：

| 选项 | 默认 | 说明 |
|---|---|---|
| `serial_port` | `/dev/ttyACM0` | C6 所在串口 |
| `mqtt_host/port/user/pass` | 本机 1883 / 空 | broker 凭据（空 = 匿名） |
| `base_topic` | `gw/coordinator` | MQTT 主题前缀 |
| `web_port` | `8082` | Web 面板端口 |
| `timezone` | `Asia/Shanghai` | 日志时区 |
| `fw_chunk` | `80` | 串口 OTA 帧大小（旧固件兼容 256 B 缓冲） |

### 3. 旧版 systemd 路径（可选）

树莓派等裸宿主机可用 `host/serial_gateway.py` + `host/esp32gw.service`，
配置方式与 add-on 相同，但不再是主线。

## MQTT 接口

Base topic 默认 `gw/coordinator`（下称 `{BASE}`）。

### 网关发布

| 主题 | 内容 |
|---|---|
| `{BASE}/status` | `online` / `offline`（LWT, retained） |
| `{BASE}/heartbeat` | 运行状态心跳（retained） |
| `{BASE}/tx_power` | `{"want":…,"eff":…}` 发射功率设置值与实际生效值（retained） |
| `{BASE}/lqi` | `{"t":"lqi","dev":{…}}` 全网 LQI/RSSI 快照（retained） |
| `{BASE}/zigbee/0xADDR/<comp>` | 设备数据：`temperature` / `humidity` / `illuminance` / `occupancy` / `ias_zone` / `battery` / `on_off` 等 |
| `{BASE}/zigbee/0xADDR/status` | 入网/离网事件 |
| `{BASE}/action/0xADDR/epN` | 情景面板按键事件（HA event 实体数据源） |
| `{BASE}/console` | 原始串口日志（调试） |

### 命令（发布到 `{BASE}/cmd/<name>`）

| 命令 | 载荷 | 说明 |
|---|---|---|
| `permit_join` | 秒数 | 开放配网 |
| `set_tx_power` | −24…20（dBm） | 设置发射功率 |
| `lqi` | 空 | 触发 LQI/RSSI 快照 |
| `read_onoff` | `"0x747d,2"`（地址,端点） | 读回 OnOff 状态 |
| `set_channel` | 11–26 | 换信道（协调器重启） |
| `scan_chan` | 空 | 全信道能量扫描 |
| `leave` / `switch2wifi` / `set_sleep` / `probe` / `node_desc` | 见 `DEVICE_INTEGRATION_REPORT.md` | 设备管理类 |

### HA 命令入口

发布到 `{BASE}/set/<addr>/<ep>/<comp>` 控制 HA 实体（如开关 `on`/`off`）。
注意 `{BASE}/cmd/*` 与 `{BASE}/set/#` 回声处理策略不同，改动前先看
`DEVICE_INTEGRATION_REPORT.md` §回声保护。

## Web 面板与 REST API

`http://<ha-ip>:8082/`

| 端点 | 说明 |
|---|---|
| `GET /api/state` | 全部设备状态（含发射功率、LQI） |
| `POST /api/permit_join` | 开放配网 |
| `POST /api/tx_power` | 设置发射功率（dBm） |
| `GET /api/lqi` | 触发并返回 LQI 快照 |
| `POST /api/ota` | 上传并串口直刷协调器固件 |
| `POST /api/fw_ota` / `POST /api/fw_flash` | 终端节点 OTA（推送 / 直刷） |
| `POST /api/device/{addr}/cmd` | 单设备命令 |
| `POST /api/device/{addr}/delete` | 删除设备 |
| `POST /api/sleep` | 设置终端节点心跳周期 |

## 测试

```bash
python test_mqtt.py && python test_panel.py && python test_ha_state.py   # 核心三件
python test_packaging.py    # 打包体检：CRLF/BOM、版本一致、Dockerfile COPY 目标
```

其余测试文件（`test_*.py`）覆盖 discovery、节点、读回、重试、OTA、时区、渲染等，
提交前应全部通过。

## 发射功率 A/B 实测

```bash
python txpower_ab.py --addr 0x747d --ep 2 --levels 20,14,8,2 --n 10
```

注意：LQI/RSSI 是在网关侧对**收到的帧**测的，描述**设备→网关（上行）**；
`set_tx_power` 改的是**网关→设备（下行）**。改网关功率不会让 LQI/RSSI 变化，
判读功率档位看**下行命令成功率**（脚本自动测），不要看 LQI 前后对比。

## 凭据与安全

- 凭据不写入仓库：MQTT 密码、HA 令牌一律走 add-on options 或环境变量
  （`MQ_USER` / `MQ_PASS` / `HA_URL` / `HA_TOKEN`）
- `host/device_db.json` 由 `gen_device_db.py` 从 zigbee-herdsman-converters 生成，
  仅供日志提示，不作为设备类型判定依据

## 相关文档

- [`DEVICE_INTEGRATION_REPORT.md`](DEVICE_INTEGRATION_REPORT.md) — 接入、定位与修复全记录
- [`splitgateway.md`](splitgateway.md) / [`splitgateway_en.md`](splitgateway_en.md) — 架构说明
- [`zigbee2mqtt.md`](zigbee2mqtt.md) / [`esp32-c6_zigbee.md`](esp32-c6_zigbee.md) — 上游参考资料
