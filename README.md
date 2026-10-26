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
| `firmware/zigbee-vital-sensor-sdk16.bin` | **当前版本**：esp-zigbee-lib 1.6.8 / 64 设备 / 设备表 NVS 持久化 / OTA image type + 中继功率写入 |
| `firmware/zigbee-vital-sensor-txpower.bin` | 旧栈（1.0.9）镜像，留作回退 |
| `firmware/zigbee-repeater.bin` | 中继器（纯 Router）镜像，见 §1b |

> ⚠️ **从旧栈（1.0.9）升级到 SDK 1.6 会重建网络，设备必须重新配对一次** —— 已实测确认
> （2026-10-22）：1.6 的 ZBOSS 不认旧 NVRAM 数据集。重建之后的 NVRAM 即为 1.6 格式，
> 后续同大版本内的 OTA 不再需要重配。细节见 `DEVICE_INTEGRATION_REPORT.md` §23.6。
>
> 另：运行期 ESP_LOG 被静音（`ha_mqtt.c` 的 `null_vprintf` + console 走 USB-JTAG），
> 所以 `Netzgröße…` / `Neustart – Netzwerk wiederhergestellt` 这类日志在 UART/USB 上
> 都看不到；判断是否重启成功看 `[C6] boot` 与心跳 `uptime` 归零，判断网络是否重建看 PAN ID。
终端节点固件在 `client/`（`idf.py build` 编译）。

### 1b. 中继器固件（可选，扩展覆盖）

`router/` 是一份**纯 Zigbee Router** 固件：常电 ESP32-C6 加入现有网络后只转发报文。
业务端点只注册 Basic + Identify，不挂任何可控/可上报的业务簇，所以 HA 里
**不会多出虚假的开关/温度实体**。

在此基础上多了两样东西：

- **OTA client**（image type `0x0002`）：能被网关无线推送固件，见 §1c
- **厂商专有簇 `0xFC00` / 属性 `0x0001`**（S8，dBm）：Web 端写进去就立刻改发射功率
  并落 NVS —— **改功率不再需要重新刷固件**

```bash
cd router
idf.py build
idf.py -p <COM口> flash
```

预编译镜像：`firmware/zigbee-repeater.bin`

| 操作 | 说明 |
|---|---|
| 上电 | 自动寻找网络；协调器需处于允许入网状态（网页 `permit_join`，或按网关 BOOT 键开 180 s 窗口） |
| **短按 BOOT（<1 s）** | **循环功率档 8 → 14 → 20 → 8 dBm**（立即生效 + 存 NVS），网络不通时的兜底 |
| 长按 BOOT 1~5 s | 再试一次入网 |
| 长按 BOOT ≥5 s | 恢复出厂（退网 + 清 NVRAM），用于换网络 |

改功率有两条路，效果一样：

1. **Web 面板** → 设备页 → 「中继发射功率」→ 点 8 / 14 / 20
   （走 MQTT `gw/coordinator/cmd/set_dev_power` → 网关 ZCL Write Attribute）
2. **设备上的 BOOT 键短按**

⚠️ 两条硬约束：

1. **必须常电**。一旦进入休眠省电，它就从 Router 退化成终端设备，不再转发。
2. **20 dBm 本板打不住**。实测本项目的板子/USB 口在 20 dBm 下必然进 Brownout 复位循环
   （`E BOD: Brownout detector was triggered` + `rst:0x3`，约 15 秒一轮，触发点在射频开始
   连发的瞬间）。同位置实测网关侧 RSSI：**20 dBm → −84 dBm，14 dBm → −90 dBm**。
   ⇒ 默认出厂档已改为 **14 dBm**（8 dBm 在隔墙场景连协调器都应答不了）。
   想用 20 dBm 必须先换供电（主板后置 USB 口 / 独立 5V ≥1 A + 粗线）再试。
   覆盖范围主要还是取决于**摆放位置**，功率不是唯一因素。
3. **自检**：每 30 秒向协调器 0x0000 发一次 ZDO Node_Desc_req，串口会打
   `网络自检 OK – 协调器应答 …` 或 `网络自检失败 – 协调器未应答 (status 0x85)`。
   0x85 = TIMEOUT，不是「没网络」。连续 6 次失败（≈3 分钟）或收到
   `NO_ACTIVE_LINKS_LEFT`（一个邻居都没有）时会自动重新寻找网络，无需人工按键。

> 参数位置：功率档位表在 `router/main/repeater.c` 的 `POWER_STEPS[]`，
> 默认值在 `s_tx_power` 初始化处（NVS 里有值就以 NVS 为准，NVS 优先）。

### 1c. Zigbee 无线 OTA（终端节点 / 中继器）

网关是 Zigbee OTA **服务端**，通过无线电给设备推固件，不用接线。
关键是 **image type 必须和设备的固件一致**——网关只把该类型的镜像通知给对应设备，
类型不符的设备不会应答（不会刷坏，只是没反应）。

| image type | 对应固件 | 目录 |
|---|---|---|
| `0x0001` | 终端节点（switch2wifi，带 On/Off + 温度） | `client/` |
| `0x0002` | **中继器**（纯 Router） | `router/` |

共同参数：manufacturer code `0x131B`；版本号 = **镜像文件的 mtime**（不用手工填）。

**操作**：Web 面板 → 某个设备页（或仪表盘）→「OTA 固件升级」→ 选类型 →
选文件 → 上传并开始。每个 image type 各存一份（`/data/ota_0x0001.bin` /
`/data/ota_0x0002.bin`），互不覆盖。

> ⚠️ **第一次必须用 USB 刷**。OTA client 是本次才加进中继固件的，
> 之前烧在板子上的版本没有这个簇，收不到任何推送。刷一次之后就能一直无线升级。
>
> ✅ **刷完不需要重新配对**。网络凭据存在 `zb_storage` 分区（`0x3A0000`），
> `idf.py flash` 只写 app 分区，不动它；重启后 ZBOSS 从 NVRAM 恢复网络。
> 只有 `esptool erase_flash`（整片擦除）或**跨 SDK 大版本**才会要求重配。

**速度**：现在是 64 B/块（`OTA_MAX_DATA`），569 KB 的镜像约 8,900 块，
按串口 + MQTT 往返估算 **10~30 分钟**。要更快就把 `OTA_MAX_DATA` 两边
**同时**提到 128~240（`main/zb_ota_server.c` 的 `s_buf`、`router/main/zb_ota_client.c`
的 `OTA_MAX_DATA` 与 `max_data_size`），只改一边会被截断。

中继是**常电 Router**，不涉及休眠设备的轮询窗口（fast-poll / turbo-poll），
OTA 过程比终端设备简单：网关广播 → 中继应答 → 连续单播分片 → 写完切换分区重启。
中继的固件版本号存在自己的 NVS（`rep/ota_ver`），所以升过一次之后不会反复重下同一版。

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
| `GET /topology` | **网络拓扑图**（直连 / 经中继，SVG） |
| `GET /api/topo` | 拓扑原始数据（邻居表 / 路由表 / 源路由） |
| `POST /api/topo` | 刷新拓扑（逐台探测后重新 dump） |

### 网络拓扑：直连还是经中继

`/topology` 页面用协调器自己的三张表画出拓扑，不靠猜：

| 表 | 来源 | 作用 |
|---|---|---|
| 邻居表 | `esp_zb_nwk_get_next_neighbor()` | 协调器直接听得到谁（LQI / RSSI / 关系 / 深度） |
| 路由表 | `esp_zb_nwk_get_next_route()` | 到某地址的**下一跳** — 判「经谁转发」的最硬证据 |
| 源路由 | `esp_zb_nwk_get_next_route_record()` | 从协调器到目的的完整路径（多级中继） |

判读优先级：**路由表 > 源路由 > 邻居表**。邻居表的 `relationship` 只说明「听得到」，
路由器的邻居关系是 sibling，不等于父子，所以不能单靠它。

⚠️ **Zigbee 是按需建路**：路由表只保留最近用过的路径，几十秒后会过期。
所以要看拓扑就得点「刷新拓扑」——它会先逐台发 `node_desc` 逼出路由，再 dump 三张表。
干等是看不出东西的。

设备详情页也多了一行「连接方式」，直接写「直连协调器」或「经中继 0x2ad6（依据：路由表）」。

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
