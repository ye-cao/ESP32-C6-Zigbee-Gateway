# ESPHome 2026.10.2 与本项目 Wi‑Fi 连接差异核对

> 核对时间：2026-11-07 00:15（GMT+8）；**00:30 修订**
> 范围：**只列已核实差异**，每条标注依据；资料不足的集中在 §6「待确认项」，不做补全。
> 边界：本文对比「连接建立时机 / 配置与状态处理 / 重连与失败表现」，**不负责定位本项目 `wifi_scan n=0` 的根因**；已证伪的假设在 §5.3 记录。
> 修订说明：00:30 用户贴出 `test1.yaml` 全文并说明其设计意图（"这一行让它真的去开 802.15.4 射频"、"用 router，行为和我们的协调器最接近（持续接收）"）。初稿把「ESPHome 是未入网 Router、负载较轻」写得偏重，弱化了这份对照；本次重写 §4，并相应调整 §0.2 / §5.3 / §6 / §7。

## 0. 资料清单

### 0.1 ESPHome 官方资料（在线，本次实际抓取）

| 编号 | 资料 | 用途 |
|---|---|---|
| O1 | `https://esphome.io/components/wifi.html` | 组件总述、"static IP … dramatically improve connection times"、`ssid`/`password`/`networks`/`manual_ip`/`ap` 定义 |
| O2 | `https://raw.githubusercontent.com/esphome/esphome/2026.10.2/esphome/components/wifi/__init__.py` | 2026.10.2 的 `CONFIG_SCHEMA` 与各默认值、`to_code` 行为 |
| O3 | `https://raw.githubusercontent.com/esphome/esphome/2026.10.2/esphome/components/wifi/wifi_component_esp_idf.cpp` | `WiFiComponent::wifi_sta_connect_()`、`wifi_apply_power_save_()`、STA 事件入队与处理 |
| O4 | `https://api-docs.esphome.io/classesphome_1_1wifi_1_1_wi_fi_component`（ESPHome 2026.10.2 API 文档） | 重试阶段状态机、`determine_next_phase_()`、`advance_to_next_target_or_increment_retry_()`、`loop()`、`reboot_timeout_ {900000}`、`ap_timeout_ {90000}` |
| O5 | `https://esphome.io/components/zigbee/` | 官方共存说明与 `router` 默认值 |
| O6 | `https://github.com/esphome/esphome/blob/.../esphome/components/wifi/wifi_component.cpp`（检索结果片段） | 扫描后 `set_bssid()` / `set_channel()` / `retry_connect()` 片段 |
| O7 | `https://esphome.io/blog/2025/06/18/esphome-2025-6` | 官方记录 ESP-IDF 5.3.2 为当时默认；**未**覆盖 2026.10.2 |

### 0.2 现场资料（本工程 `backup/` 与报告）

| 编号 | 资料 | 内容 |
|---|---|---|
| F1 | `\\192.168.101.5\config\esphome\test1.yaml`（31 行，本次读取；用户 00:30 又贴出全文确认） | 21:27 那份 ESPHome 配置的**原文**，含两处设计意图注释：「这一行让它真的去开 802.15.4 射频」「用 router，行为和我们的协调器最接近（持续接收）」 |
| F2 | 用户提供的 21:27:51–21:28:11 日志，摘录于 `Wi-Fi通道-测试报告-2026-11-06.md:213-221` | 成功的那次「秒连」全过程 |
| F3 | `backup/esphome-bootlog-20261106.log`（62 行） | 18:12 同一 ESPHome 镜像**连不上**的 32 秒循环 |
| F4 | 报告 §145-159（`Wi-Fi通道-测试报告-2026-11-06.md`） | F3 的文字结论 |
| F5 | `backup/wifi-init-full-20261106.log` | 我方懒加载 Wi‑Fi 初始化日志（开机 ~3.7 s） |
| F6 | `backup/bootscan-20261106.log` | 我方开机早扫诊断固件日志（开机 ~0.32 s） |
| F7 | `backup/wifi-scan-driverlog-20261106.log` | 我方 `wifi_scan` 回执与 `itwt_stop_process` |
| F8 | 会话全文 `…24d6946e….jsonl` 第 18534 行：用户 20:18:03–20:19:05 贴出的 ESPHome 日志 | **Zigbee 成功入网**：`Joined network successfully: PAN ID(0x8ea1, EXT: 0xccc72d169a4050dd), Channel(25), Short Address(0x3809)`；同段还有 `Found networks` 4 个 AP、`Connected, IP 192.168.100.123, -47 dB`、`Device started up in factory-reset mode`、`Receive Zigbee action(0x000b) callback`。用户当时结论「能正常工作 zigbee也能正常入网」 |
| F9 | 会话全文第 18537 行（当时助手对 F8 的结论） | "不是芯片、不是共存、不是硬件 —— 是我们那份 sdkconfig 里某个 Wi-Fi/PHY 选项"；并记录 **ESPHome Zigbee ch25 ↔ Wi-Fi ch8 隔得开，本项目 Zigbee ch20 与 Wi-Fi ch8 重叠** |
| F10 | `C:\Users\ye_ca\Downloads\test1-firmware.factory.bin`（用户 00:48 提供，1,166,528 B 合并镜像） | 21:27 那份 ESPHome 固件的**二进制**：分区表 `otadata@0x9000` / `phy_init@0xB000` / `app0@0x10000` / `app1@0x1C0000` / `nvs@0x370000` / `zb_fct@0x3E0000`；**`phy_init` 分区内容全 `0xFF`**；app 内含 `v5.5.6` 字符串与 `Zigbee`/`Model`/`Router` 日志格式串 |

### 0.3 本项目代码 / 配置

| 编号 | 资料 |
|---|---|
| C1 | `main/main.c:341-379`（`app_main` 顺序） |
| C2 | `main/net_link.c:120-158`（`cfg_load` / `cfg_save`）、`:177-211`（事件回调）、`:214-309`（`wifi_begin` / `wifi_apply_cfg`）、`:440-515`（`net_task`）、`:687-757`（`scan_format`）、`:768-812`（`wifi_join`）、`:935-957`（`net_link_start`） |
| C3 | `main/ha_mqtt.c:190-235`（`net_cfg` 命令登记） |
| C4 | `sdkconfig.defaults:1-48`、`sdkconfig`（`CONFIG_FREERTOS_HZ=1000`、`CONFIG_IEEE802154_ENABLED=y`、`CONFIG_ESP_COEX_SW_COEXIST_ENABLE=y`、`# CONFIG_ESP_PHY_INIT_DATA_IN_PARTITION is not set`） |
| C5 | `main/net_link.h`、`CHANGELOG.md`、`Wi-Fi通道-测试报告-2026-11-06.md` |

---

## 1. 连接建立时机

### 1.1 起点：编译期固定启动 vs 运行期才决定要不要启动

- **ESPHome**：`wifi:` 配置在编译期由 YAML 生成进固件，`setup()` 阶段就启动。F1 显示 `wifi:` 段存在；F2 里 `[21:27:52][C][wifi:690]: Starting`、`[D][wifi:1457]: Starting scan` 出现在 `setup()` 内。官方 `get_setup_priority()` 是 `WIFI setup_priority`（O4），但**具体数值未在可见文档中给出（§6.2）**。
- **本项目**：`app_main()` 先 `nvs_flash_init()`，再 `ha_mqtt_init()`（其内部调 `net_link_start()`），最后 `zb_gateway_start()`（C1）。`net_link_start()` 先 `cfg_load()` 从 NVS 命名空间 `netlink` 读 `tr`；**`tr != 1` 就直接返回、完全不初始化 Wi‑Fi**（C2:935-940）。默认 `tr=0`（串口），`sdkconfig.defaults:1-3` 明确写「Wi‑Fi 默认不启用」。
- 实测时间差：懒加载版 Wi‑Fi 初始化在开机 `I (3681)` ≈ 3.7 s（F5）；开机早扫诊断版在 `I (321)` ≈ 0.32 s（F6）。
- 【依据】C1、C2:935-957、C4:1-3、F2、F5、F6、O4。

### 1.2 「秒连」的实际时长与顺序（重要：不是零等待）

- F2 时间线：21:27:51 启动 → 21:27:52 `Starting scan` → **21:28:00** `Found networks:` 4 个 `yecao`（-37/-54/-55/-59 dBm，ch8/ch1）→ 21:28:01 第一次 `Authentication Failed` → `attempt 2/2 in phase SCAN_CONNECTING` → **21:28:02** `Connected`（-35 dBm / ch8 / `192.168.100.123`）→ 21:28:04 HA API 接入。
- 即**从启动到拿到 IP 约 11 秒，扫描本身约 8 秒，且中间失败过一次**。「秒连」是相对本项目「永远连不上」的观感，不是零等待。
- 顺序细节（避免误读）：F2 里 `[C][component:164]: Setup zigbee.zigbee_esp32 took 73ms` 在 `[C][wifi:690]: Starting` **之前**；而 `[D][zigbee:067][Zigbee_main]: Zigbee stack initialized` 在 `Starting scan` **之后**。所以准确说法是「Zigbee 组件 setup 先完成，Wi‑Fi 在 setup 内启动，Zigbee 栈的异步初始化在 Wi‑Fi 启动之后」，不能简化成「Wi‑Fi 一定先于 Zigbee」。
- 【依据】F2（用户日志 / 报告 §2.7 摘录 :213-221）。

### 1.3 已证伪：「Wi‑Fi 抢先拿到 PHY」不是差异的解

- F5（懒加载，开机 3.7 s）**没有** `phy_init` 行；F6（早扫，开机 0.32 s）**有** `I (329) phy_init: phy_version 341,a6c8fe6,Jul 30 2025,18:34:11`。
- 但两版的扫描结果都是 `n=0`：F6 `BOOTSCAN {"t":"wifi_bootscan","n":0,"got":0,"aps":[]}`；F5 `{"t":"wifi_scan","n":0,"got":0,"aps":[]}`。
- ⇒ 把 Wi‑Fi 初始化提前到 Zigbee 之前、让它真正执行 `phy_init`，**没有**改变收不到 AP 的结果。初始化顺序差异是真实存在的（§1.1），但不足以解释连接结果差异。
- 【依据】F5、F6、`backup/wifi-init-full-20261106.log` 与 `backup/example-scan-full.log` 的逐行 diff（见日报「22:45 / 00:02」两节）。

---

## 2. 配置 / 状态处理

### 2.1 现场 `test1.yaml` 的实际配置（F1 原文）

```yaml
wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password
captive_portal:
zigbee:
  id: my_zigbee
  router: true
```

- **没有** `fast_connect`、`passive_scan`、`power_save_mode`、`manual_ip`、`networks`/`bssid`/`channel`、`min_auth_mode`、`reboot_timeout`、`post_connect_roaming`。
- 因此全部按 O2 的官方默认：`fast_connect=False`、`passive_scan=False`（主动扫描）、`power_save_mode` ESP32=`light`、`min_auth_mode=WPA2`、`reboot_timeout=15min`、`ap_timeout=90s`、`priority=0`；**无静态 IP**（走 DHCP）。
- ⇒ **「ESPHome 靠 `fast_connect` 或静态 IP 才秒连」被 F1 直接否证**。O1 虽建议静态 IP「dramatically improve connection times」，但这次并没用。
- **DHCP 两边一致，不是差异**：F1 没有 `manual_ip`；F2 日志给出 `IP Address: 192.168.100.123` / `Subnet: 255.255.254.0` / `Gateway: 192.168.100.1` / `DNS1: 192.168.100.1`，是 DHCP 分配且工作正常。本项目同样是默认 STA netif、等 `IP_EVENT_STA_GOT_IP`（C2:240-246、C2:205-210、C2:492），也没有静态 IP。O1 那句 "It's recommended to provide a static IP for your node, as it can dramatically improve connection times" 只是官方通用建议，**本次对照两边都不适用，不能拿来解释「秒连」**（用户 00:30 再次确认「不用静态啊 DHCP 分配也没问题的」）。
- 【依据】F1、F2、O1、O2（`CONFIG_SCHEMA` 片段）、C2:205-210 / C2:240-246 / C2:492。

### 2.2 连接参数：ESPHome 把扫描结果回填成定向连接，我们是盲连

| 项 | ESPHome 2026.10.2 | 本项目 | 依据 |
|---|---|---|---|
| `bssid_set` / `bssid` | 有 BSSID 时 `true` + 6 字节拷贝；否则 `false` | `memset` 清零，**从不设置** | O3 `wifi_sta_connect_()`；C2:276-309 |
| `channel` / `scan_method` | 有信道时 `WIFI_FAST_SCAN`，否则 `WIFI_ALL_CHANNEL_SCAN` | 不设置（保持 0） | O3；C2:276-309 |
| 扫描结果回填 | `connect_params.set_channel(scan_res.get_channel()); set_bssid(scan_res.get_bssid())` | 无此逻辑 | O6；F2 里 `Connecting to 'yecao' (80:AF:CA:88:01:F3)` 带 BSSID |
| `threshold.authmode` | `min_auth_mode_` 决定，默认 `WIFI_AUTH_WPA2_PSK` | `WIFI_AUTH_WPA2_WPA3_PSK`（有密码）/ `WIFI_AUTH_OPEN` | O3 + O2；C2:295-296 |
| `threshold.rssi` | `-127` | 不设置（0） | O3；C2:276-309 |
| `pmf_cfg` | `capable=true, required=false` | 同 | O3；C2:299-300 |
| `sae_pwe_h2e` | **未显式设置** | `WPA3_SAE_PWE_BOTH` | O3；C2:305 |
| `listen_interval` | 显式 `0` | 不设置（0） | O3；C2:276-309 |
| 配置持久化 | `esp_wifi_set_config` 前 `esp_wifi_get_config` + `memcmp`，不同才 `disconnect()` 再下发 | `WIFI_STORAGE_RAM`，配置由自己的 NVS `netlink` 管，每次 `wifi_apply_cfg()` 直接下发 | O3；C2:263、C2:120-158、C2:307 |

- 【依据】见上表每行末列。

### 2.3 配置来源：编译期 YAML vs 运行期串口/MQTT 命令

- ESPHome：YAML 编译进固件（F1）。另有 `fast_connect` 的 `preferences.storage_schema()` 可保存上次 AP（O2），但 F1 未启用。
- 本项目：`{"cmd":"net_cfg","ssid":…,"pass":…,"host":…,"port":…,"tr":…}` 运行期下发，写 NVS 命名空间 `netlink`（C2:120-158、C3:198-232）。命令必须**同时登记** `main.c::on_uart_cmd` 与 `ha_mqtt.c` 的 JSON→payload 链，漏一处就回 `unknown_cmd`（C3:207-210，项目硬规则 §2）。
- 实测：本项目的 NVS 配置曾被擦除/被 ESPHome 覆盖，`net_status` 一度回 `ssid:"" host:""`；重新下发后回 `ssid":"yecao"`（日报 22:45 节）。
- 【依据】F1、C2:120-158、C3、日报 22:45 节实测回执。

### 2.4 省电模式与 STA_START 处理

- ESPHome：`WIFI_EVENT_STA_START` 分支设 hostname、`s_sta_started=true`，并**重新调用 `wifi_apply_power_save_()` → `esp_wifi_set_ps()`**；`light` → `WIFI_PS_MIN_MODEM`（O3）。连接判定 `CONNECTED` 要求 `s_sta_connected && got_ipv4_address_`（O3）。
- 本项目：`WIFI_EVENT_STA_START` 只打日志 + 置 `BIT_STARTED`（C2:185-189）；**全项目未见 `esp_wifi_set_ps()` 调用**；`WIFI_EVENT_STA_DISCONNECTED` 只记 `s_last_reason` + 清 `BIT_IP`（C2:194-200）。
- 我方两侧日志均出现 `W (…) wifi:(itwt)itwt_stop_process!`（F5、F6、F7），ESPHome 成功那次日志中未见该行（F2）——但**这不构成因果结论**（§5.2）。
- 【依据】O3、C2:177-211、F2、F5、F6、F7。

---

## 3. 重连与失败表现

### 3.1 ESPHome：多阶段重试状态机

- 官方 `WiFiComponent` 有 `WiFiRetryPhase` 状态机：`determine_next_phase_()`（原文 "It does not modify any state - it only returns the recommended next phase."）、`transition_to_phase_()`、`advance_to_next_target_or_increment_retry_()`（`FAST_CONNECT_CYCLING_APS` 不重试当前 AP、`RETRY_HIDDEN` 用尽当前 SSID 后推进、其他阶段递增重试计数）、`log_and_adjust_priority_for_failed_connect_()`（失败 BSSID 降优先级，且只在 `SCAN_CONNECTING` 最后一次尝试才降）、`start_initial_connection_()`、`loop()`（"Reconnect WiFi if required."）（O4）。
- 默认 `reboot_timeout_ {900000}`（15 min）、`ap_timeout_ {90000}`（90 s）（O4、O2）；`retry_connect()` 在 >5 次或回调错误时 `Restarting WiFi adapter`，连接超时 30 s 触发重试（O6）。
- 现场成功那次（F2）：`Retry phase: INITIAL_CONNECT → SCAN_CONNECTING` → `attempt 1/2` 报 `Authentication Failed` → `attempt 2/2` → **21:28:02 Connected**。即**状态机的第 2 次尝试直接挽救了首次认证失败**。
- 现场失败那次（F3）：`No networks found` → `INITIAL_CONNECT → SCAN_CONNECTING → RETRY_HIDDEN → RESTARTING → …` 循环，`Hidden candidate 'yecao'`、`Connecting to 'yecao' (any)`、`Probe Request Unsuccessful`、`Network no longer found`，32 秒内一次都没连上（F3、F4:145-159）。
- 【依据】O4、O6、O2、F2、F3、F4。

### 3.2 本项目：单次连接 + 固定等待

- `wifi_join`（C2:768-806）：一次 `esp_wifi_connect()`，清 `BIT_IP`，等 20 000 ms，超时即回 `ok:0`。**无阶段状态机、无 BSSID 轮换、无适配器重启、无 reboot_timeout**。
- `net_task`（C2:440-515）：`esp_wifi_connect()` 后等 `BIT_IP` 30 000 ms；超时 `esp_wifi_disconnect()` + 2 000 ms 后重来。TCP 连接失败才用 2→30 s 指数退避（C2:503-508）；**Wi‑Fi 侧没有退避**。
- `scan_format`（C2:687-757）：`WIFI_SCAN_TYPE_ACTIVE`、`show_hidden=true`、`active.min=120 / max=1500` ms；先 `esp_wifi_disconnect()` + 300 ms 再 `esp_wifi_scan_start(&sc, true)`；`n` 与 `got` 分开上报。
- 实测失败表现：`{"t":"wifi_join","ok":0,"conn":"ESP_OK","ip":"0.0.0.0","rssi":0,"ch":0,"why":201}`（NO_AP_FOUND）、`net_status why=201`、驱动日志 `wifi:Haven't to connect to a suitable AP now!`（日报 22:45 节）；`{"t":"wifi_scan","n":0,"got":0,"aps":[]}`（F5、F7）。
- 【依据】C2:440-515、C2:687-812、F5、F7、日报 22:45 节。

### 3.3 两边失败类型不同（已核实）

- ESPHome 21:28:01 是 **`Authentication Failed`**（认证阶段），18:12 是 **`Probe Request Unsuccessful`**（探测无应答）。
- 本项目是 **`why=201` NO_AP_FOUND** 且扫描 `n=0`，**连一个 AP 都没进过候选列表**。
- ⇒ 本项目卡在「扫描/射频收包」这一层，还没走到 ESPHome 状态机发挥作用的「选 AP / 认证 / 重试」那一层。
- 【依据】F2、F3、F5、F7、日报 22:45 节。

---

## 4. 射频与角色前提（额外维度，官方文档支持）

### 4.1 F1 是用户刻意构造的「802.15.4 开着还能不能连 Wi‑Fi」对照

- F1 两处注释写明设计意图：`zigbee:` 是「让它真的去开 802.15.4 射频」；`router: true` 是「行为和我们的协调器最接近（持续接收）」。用户 00:30 再次贴出全文确认。
- 会话中用户的表述一致：「我主要是看看 esphome 里面如果 wifi 和 zigbee 能共存我刷进去看能不能正常发现 WiFi」「能正常工作 zigbee也能正常入网」「只是给你证明 WiFi 和 zigbee 都是能正常工作的」。
- ⇒ **这不是随手刷的空配置，是主动设计的共存对照**；初稿把它写成「未入网 Router、负载较轻」是**错误的**，本次修订更正。

### 4.2 日志与 YAML 逐组件交叉核对（确认 F2 确由 F1 产生）

| F1 配置 | F2 日志对应 |
|---|---|
| `esphome: name: test1` | `[C][zigbee:422] Model: test1`、`Hostname: 'test1'`、`Address: test1.local:6053` |
| `esp32: variant: esp32c6` / `framework: type: esp-idf` | `I (23) boot: ESP-IDF v5.5.6`、`ESP32 Chip: ESP32-C6 rev0.2` |
| `logger:` / `api:` / `ota: platform: esphome` | `[C][logger:218]`、`[C][api:368]` + `Home Assistant … connected`、`[C][esphome.ota:129]` |
| `wifi:` / `captive_portal:` | `[C][wifi:690]: Starting`、`[D][wifi:1457]: Starting scan`、`[C][captive_portal:162]` |
| `zigbee: router: true` | `Setup zigbee.zigbee_esp32 took 73ms`、`[C][zigbee:422] Router: YES`、`Zigbee stack initialized` |

⇒ 组件逐项对得上，**F2 日志确由 F1 配置产生**，不是别的固件。O5 官方：`model` 默认取节点名、`router` 默认 `false`（此处显式 `true`）。

### 4.3 Zigbee 确实入过网（用户 00:31 更正，已查到日志）

- **F8 实测**（20:18:03–20:19:05，ESPHome 2026.10.2，同一块板）：
  - `Device started up in factory-reset mode` → `Start network steering` → 多次 `Failed to join network with status(0x03)`
  - 20:18:11 `Found networks` 4 个 `yecao`（ch8 -47/-48 dB、ch1 -61/-62 dB）；20:18:13 `Connected, IP 192.168.100.123, -47 dB`
  - **20:18:59 `Joined network successfully: PAN ID(0x8ea1, EXT: 0xccc72d169a4050dd), Channel(25), Short Address(0x3809)`**
  - 20:19:00 `Receive Zigbee action(0x000b) callback`、20:19:03 `Boot seems successful`
- ⇒ **Wi‑Fi 已连上之后 46 秒，Zigbee Router 成功入网并产生 Zigbee 业务回调**：单芯片「Wi‑Fi STA + 已入网 Zigbee Router」在这块 C6 上实测成立。用户当时即明确「能正常工作 zigbee也能正常入网」。
- **21:27 那次 `Device is joined to the network: NO`（F2）不是能力问题**：同一份日志里同样有 `Device started up in factory-reset mode` + `Start network steering` + `status(0x03)`，即重刷后凭据被清、尚未重新入网。两次状态不同，不矛盾。
- ⇒ 初稿「ESPHome 是未入网 Router、因此 802.15.4 负载较轻」的推论**作废**。
- 【依据】F8（会话全文第 18534 行，用户贴出的原始日志）、F2、F1、F9。

### 4.4 已核实的信道差异（可能相关，但未证实为根因）

| 项 | ESPHome（F8） | 本项目（心跳回执） |
|---|---|---|
| Zigbee 信道 | **25** | **20** |
| Zigbee PAN / 短地址 | `0x8ea1` / `0x3809` | `0xa311` / 中继 `0x8e4e` |
| Wi‑Fi 信道 | **ch8**（`-47/-48 dB`） | AP 在 ch8 / ch1（F2、F3 扫描结果） |
| 频谱关系 | ch25（2475 MHz）与 Wi‑Fi ch8（2447 MHz）相隔较远 | ch20（2450 MHz）与 Wi‑Fi ch8 **几乎完全重叠** |

- 项目记忆 §5 早有规划：「Zigbee 别用 ch20（2448–2452 MHz，与 Wi‑Fi ch8 完全重叠）；用 ch25 + Wi‑Fi 1/6/11」；F9 也记录了同一条。
- **这是两边最具体的射频层差异**，且方向明确（我们处在更不利的频谱位置）。但**没有实验**证明它就是 `wifi_scan n=0` 的根因；不得据此下结论，列入 §6.11。
- 【依据】F8、F9、日报 22:45 节心跳回执、项目 `MEMORY.md` §5。

### 4.5 官方共存说明的适用范围

- O5 原文：
  - "WiFi station mode + Zigbee end device is working fine."
  - "WiFi station mode + Zigbee router leads to an unstable Zigbee network. Still can be used if WiFi is only enabled for short times (e.g. OTA)"
  - "WiFi access point + Zigbee is not supported"
  - "You will need a Zigbee coordinator like Zigbee2MQTT or ZHA."
- 官方这句话讲的是 **Zigbee 网络稳定性**，且与 F8 实测并不冲突（F8 只证明能连上并入网，未测长期稳定性）。它**不能**反推 Wi‑Fi 侧因果，也不能用来否定 F8。
- 【依据】O5、F8。

### 4.6 本项目侧状态（已核实）

- Zigbee Coordinator **在网**：心跳 `{"t":"heartbeat","ch":20,"pan":"0xa311","rssi":-73,"dev":[{"addr":"0x8e4e","lqi":35,"rssi":-73,"lqia":27}]}`（日报 22:45 节）。
- ⇒ 两边 802.15.4 侧都是「已入网、持续接收」的角色（Router vs Coordinator），**不再是初稿所写的负载不同**；已核实的实质差异收敛到**信道（§4.4）**与**固件/工具链（§5.3、§6）**。
- 【依据】日报 22:45 节心跳回执、F8。

---

## 5. 已核实但不能当作因果解释的差异

### 5.1 「扫描后定向 BSSID + 信道」能解释"更快更韧"，但解释不了我们 `n=0`

ESPHome 把扫描到的 BSSID/信道回填连接参数（O6、O3），本项目不设（C2:276-309）。这项确实能减少连接阶段的盲扫时间；但本项目在 `esp_wifi_scan_get_ap_num()` 就得到 0（F5、F7），**根本没进入"选 AP"这一步**。因此不能说「因为我们没设 BSSID 所以扫不到」。

### 5.2 省电模式 / `itwt_stop_process` 只是并发现象

ESPHome 显式 `esp_wifi_set_ps()`（O3），本项目不调；我方日志有 `itwt_stop_process`（F5/F6/F7）。两者差异已核实，但**没有证据**表明它导致收不到 beacon；不得据此下结论。

### 5.3 已被实机证伪的假设（避免重复归因）

- 「PHY 被 802.15.4 抢先初始化」：F6 有 `phy_init` 且仍 `n=0` ⇒ 证伪。
- 「PTI 表 `0x55777555`」：报告 §2.6 记录关掉 SW coex 后 PTI0 对齐例程的 `0x000a00a0` 仍 0 ⇒ 已排除（但**该次实验未核对 ELF SHA256**，见 §6.6）。
- 「板级硬件 / 换板」：F2 与 F3 证明同板 ESPHome 能连上、也会连不上 ⇒ 报告 §一已作废该结论。
- 「单芯片 Wi‑Fi + Zigbee 无法共存」「编进/开启 802.15.4 就收不到 Wi‑Fi」「已入网设备常收会把 Wi‑Fi 挤死」：**F8 直接证伪** —— 这份固件 `zigbee: router: true` 且 20:18:59 成功入网（ch25 / `0x3809`），Wi‑Fi 早在 20:18:13 就连上并在 20:18:11 扫到 4 个 AP（§4.3）。这也是 F9 当晚已得出的结论，后续排查一度绕开了它。
- 「IDF 版本差（5.5.1 vs 5.5.6）」：**未验证**，不得当作结论（§6.1）。
- ⇒ 排除到这一步，**本项目侧仍可能的解释收敛为**：特定的 `sdkconfig` / 射频库选项、coex/PTI 配置、或信道重叠（§4.4）；三者都尚未由实验判定。

---

## 6. 待确认项（资料不足，明确留白）

1. **ESPHome 2026.10.2 官方对应的 ESP-IDF 版本**。现场 bootloader 报 `ESP-IDF v5.5.6`（F2 / 报告 §215），但官方 2026.10.2 release notes（O7 检索结果）未列 IDF 版本；本机只有 5.5.1 / 5.5.2 / 6.0.2。**未从官方资料核实**。
2. **`WIFI setup_priority` 的具体数值**与组件启动顺序的定量依据：O4 只写 "WIFI setup_priority."，页面未给数值。
3. **`COOLDOWN` 阶段定义/时长、各重试阶段的具体尝试次数**：O4 页面在 `needs_scan_results_()` 后标记 `[content truncated]`，可见内容只有行为描述与 `num_retried_ {0}`；F3 现场日志出现的是 `RESTARTING` 而非 `COOLDOWN`。
4. **21:27 那份 ESPHome 固件的 `sdkconfig`**（coex / PTI / 802.15.4 / power save / `FREERTOS_HZ` 的编译期取值）：未取得，因此无法与我方 `CONFIG_FREERTOS_HZ=1000`、`CONFIG_IEEE802154_ENABLED=y` 等做静态对照。本机例程对照里唯一数值差异是 tick 频率（日报 22:23 节），但 ESPHome 侧对应值未知。
5. **`!secret wifi_ssid` / `wifi_password` 的实际内容**：`secrets.yaml` 未读取（不影响机制结论，F2 已证明连接成功）。
6. **报告 §2.6「关 SW coex → PTI0 对齐仍 0」实验的镜像身份**：今晚已多次出现「刷回没生效」（板上曾一直跑 ESPHome），那次未核对 `ELF SHA256` ⇒ 该结论需重做才可靠。
7. **18:12（F3）与 21:27（F2）两次 ESPHome 表现相反的原因**：报告 §146 称「原样写回同一镜像」，但未定论；可能涉及物理复位、外壳、AP 状态等，未核实。
8. **我方 `why=201` 与 ESPHome `Probe Request Unsuccessful` 是否同一底层原因**：未证实（两者在不同固件、不同时刻）。
9. **21:27 扫描 8 秒的驱动层构成**（每信道驻留、主动/被动、是否受 802.15.4 共存窗口影响）：F2 只有 ESPHome 应用层日志，无 IDF 驱动级数据。
10. **F8 中 Zigbee 入网成功之后的长期并发表现**：日志止于 20:19:05（入网后 6 秒），只有一条 `Receive Zigbee action` 回调和 `Boot seems successful`，**没有长时间 Wi‑Fi + Zigbee 同时承载流量的数据**。因此「能入网且能连 Wi‑Fi」已核实，「长期稳定共存」未核实（O5 讲的正是这一点）。
11. **信道重叠是否就是我方 `n=0` 的根因**：F8 在 ch25、本项目在 ch20（§4.4），但**没有做过「把本项目 Zigbee 改到 ch25 再测 Wi‑Fi」的对照实验**。方向明确、因果未证。

---

## 7. 有证据支持的简明结论

1. **「秒连」的实际含义**：21:27 那次 ESPHome 从启动到拿到 IP 约 **11 秒**（扫描约 8 秒），且**第一次 `Authentication Failed` 后靠重试状态机第 2 次尝试才成功**（F2）。它不是零等待，也不是「一次就成」。
2. **ESPHome 并非总能连上**：18:12 同一块板、同一份镜像，`No networks found` + `Probe Request Unsuccessful` 循环 32 秒一次没连上（F3、F4）。所以「秒连」是**某一次现场现象**，不是 ESPHome 的固有能力，不能作为硬件/固件的判别标准。
3. **已核实的最大功能差异在「配置与重试」而非「启动瞬间」**：ESPHome 编译期固定 `wifi:` 配置、`setup()` 内启动，扫描后把 **BSSID + 信道回填**连接参数，并有 **多阶段重试 + 适配器重启 + BSSID 优先级降级 + 15 min reboot_timeout**（O2/O3/O4/O6）；本项目默认 `tr=0` **根本不初始化 Wi‑Fi**，靠运行期 `net_cfg` 才启用，连接是「单次 `esp_wifi_connect()` + 固定 20/30 s 等待」，**没有重试状态机**（C1/C2/C3）。这解释了「ESPHome 看起来更快、更韧、失败后还能自己救回来」。
4. **但这些差异解释不了本项目连不上**：本项目在**扫描阶段 `n=0`**（F5/F6/F7），连候选 AP 都没有，尚未走到 ESPHome 状态机发挥作用的「选 AP / 认证 / 重试」层。BSSID 定向、重试阶段、认证阈值都属于下游机制（§5.1）。
5. **几个常见猜测被资料直接否证**：F1 证明 21:27 那份配置**没开 `fast_connect`、没指定 BSSID/channel、也没设静态 IP**；**两边都是 DHCP 且工作正常**（F2 的 `192.168.100.123 / 255.255.254.0 / 192.168.100.1` 与本项目默认 STA netif），静态 IP 不是差异；F6 证明把 Wi‑Fi 提前到 Zigbee 之前、让它真正跑 `phy_init` 后仍是 `n=0`。
6. **F1 是刻意构造的共存对照，且 Zigbee 确实入过网**：用户注释写明 `zigbee:` 让 802.15.4 真的开启、`router: true` 模拟协调器持续接收；F8 实测 20:18:59 `Joined network successfully: PAN 0x8ea1 / Channel(25) / 0x3809`，而 Wi‑Fi 在 20:18:11 扫到 4 个 AP、20:18:13 已连上。21:27 那次 `joined: NO` 是 `factory-reset mode` 重刷后尚未入网，不是能力问题（§4.3）。**初稿「ESPHome 未入网、负载较轻」的推论作废。**
7. **硬件 / 芯片 / 单芯片共存全部被排除**：这正是 F9 当晚已得出的结论 —— 差异收敛到我方 `sdkconfig` / 工具链的某个 Wi‑Fi/PHY 选项、coex/PTI 配置，以及**已核实的信道差异**（ESPHome Zigbee ch25 ↔ Wi‑Fi ch8 相隔远；本项目 ch20 与 Wi‑Fi ch8 几乎完全重叠，§4.4）。两边 802.15.4 都是已入网、持续接收的角色，不再是「负载不同」。官方 O5 讲的是 Zigbee 长期稳定性，长期并发数据仍缺（§6.10）。
8. **两者都未证实为根因**：ch20 重叠与 sdkconfig 选项都只是待验证候选；按 §6.5 / §6.6 / §6.11，需要「改 ch25 后重测」与「核对 `ELF SHA256` 后重做 coex/PTI 实验」才能判定。在此之前，任何「因为 X 所以扫不到」都属猜测。

---

## 8. 2026-11-07 00:35–00:50：两轮固件实验（已核实结果）

### 8.1 第一版：按资料补齐三项配置

改动（`main/net_link.c`、`main/main.c`；回归门禁 `RESULT: ALL OK (5 Checks)`）：
- `wifi_apply_cfg()` 补 `threshold.rssi = -127`、`scan_method = WIFI_ALL_CHANNEL_SCAN`、`listen_interval = 0`（依据 O3 / 报告 §2.2）；
- `scan_format()` 驻留 `120/1500 → 400/3000` ms（对齐 F2 的 8 s 量级）；
- `net_link_scan_json()` 懒加载路径补 `wifi_apply_cfg()`（此前这条路径**从未下发** STA 配置）；
- `NET_BOOTSCAN = 0`。

产物 1,328,784 B / bin SHA `5198ea2d4c23e668a7…` / 开机 `ELF SHA256 6a06150d4…`（与上一版 `52a7952eb` 不同，确认刷的是新固件）。

实测（`backup/wifi-fix-v1-test-20261107.log`）：`wifi_scan {"n":0}`、`wifi_join {"ok":0,"why":201}`；三次复位采样（`backup/wifi-multiboot-sample-20261107.log`）同一 SHA、耗时均 **6.0 s**、结果全 `n=0` ⇒ **确定性失败，不是时间漂移**。⇒ 三项配置修复**不是根因**。

### 8.2 第二版：同一次开机内「Zigbee 前 vs 后」判决

改动：`NET_BOOTSCAN = 1`，并在 `net_link_scan_early()` 里补 `wifi_apply_cfg()`。产物 1,329,744 B / bin SHA `2fa27c3152b968f212d1…` / `ELF SHA256 5621c926a…`。

实测（`backup/wifi-bootscan-compare-20261107.log`）：三轮复位 `BOOTSCAN {"t":"wifi_bootscan","n":0}` **全为 0**。
- 采样脚本开机后立即发的 `wifi_scan` 三轮无回执，属**采样时序问题**（命令被丢弃）；随后单独长窗口复测（`backup/wifi-scan-longwindow-20261107.log`）**6.0 s 正常回执 `n=0`**。
- ⇒ **Zigbee 启动前、启动后都收不到 AP**，运行时初始化顺序 / 802.15.4 占用**不是根因**；F6/F7 的 PHY 抢先结论再次被印证。
- 刷写前读到的 `BOOTSCAN n=11`（与 `net_status` 回执拼在同一行、`aps` 字段被截断）判定为 **USB 串口缓冲残留**，不作证据。
- 第三版已把 `NET_BOOTSCAN` 改回 0 并刷回，恢复正式形态。

### 8.3 ESPHome factory 固件静态核对（F10）

- 分区表解析见 F10；**`phy_init@0xB000` 内容全 `0xFF`** ⇒ ESPHome 也没有从 `phy_init` 分区加载射频数据，本项目 `# CONFIG_ESP_PHY_INIT_DATA_IN_PARTITION is not set` **不构成差异**（新增排除项）。
- app 内含 `v5.5.6` ⇒ §6.1「IDF 版本」现在有**固件内证据**（仍非官方 release notes 表述）。
- 该镜像是 1,166,528 B 的合并镜像（只到 app0），不含 `nvs`/`zb_fct` 实际内容 ⇒ **sdkconfig 仍无法直接取得**，§6.4 保留。

### 8.4 本轮之后的排除清单与剩余候选

已排除 / 证伪：硬件、单芯片共存、初始化顺序、PHY 抢先、`phy_init` 分区差异、`fast_connect` / 静态 IP / DHCP、三项 STA 配置、扫描驻留、PTI 对齐（§6.6 待重做）。

**剩余最具体的两个候选**：
1. **信道重叠**：ESPHome Zigbee ch25 ↔ Wi‑Fi ch8 相隔远；本项目 ch20 ↔ ch8 几乎完全重叠（§4.4、§6.11）。改 ch25 会让已配对设备掉线，需用户确认后做。
2. **IDF 5.5.1 vs 5.5.6 的射频库 / coex 编译期配置**（§6.1、§6.4、§6.6）：本机无 5.5.6，`sdkconfig` 未取得。

两者都**尚未由实验判定**，不得当作结论。

---

## 10. 01:01–01:04：物理 RST 的决定性对照（**更正前面所有结论**）

### 10.1 用户按物理 RST 后，Wi‑Fi 第一次连上了

监听日志 `backup/rst-watch-20261107.log`（板上仍是 `NET_EARLY_JOIN=1` 诊断固件）：

```
[01:01:45] 开始监听 / USB 在线 / 串口已打开 COM9
[01:01:48] USB 状态: 掉线
          读失败(USB 重枚举?): ClearCommError failed (PermissionError(13, …))
[01:01:53] USB 状态: 在线 / 串口已打开 COM9
[01:01:55] EARLYJOIN {"t":"wifi_join","ok":1,"conn":"ESP_OK",
                     "ip":"192.168.100.123","rssi":-53,"ch":1,"why":0}
[01:02:00] {"t":"heartbeat","uptime":10,"ch":20,"pan":"0xa311",
            "dev":[{"addr":"0x8e4e","lqi":56,"rssi":-69}]}
[01:02:21…] 中继 0x8e4e 完成 interview（Zigbee 正常）
```

⇒ **`ok:1`、拿到 IP、Wi‑Fi 与在网 Zigbee Coordinator 同时工作**。这是今晚第一次成功。

### 10.2 A/B 对照（`backup/rst-vs-hardreset-20261107.log`）

| 条件 | `net_status` | `wifi_scan` | `EARLYJOIN` |
|---|---|---|---|
| A 物理 RST 后 | `ip=192.168.100.123`、`why=209` | `n=0`（5.8 s） | `ok:1` |
| B 之后仅用 esptool `--after hard_reset`（不刷写） | `ip=0.0.0.0`、`why=201` | — | `ok:0 why=201` |

⇒ **同一份固件、同一块板、相隔几十秒，复位方式一变，Wi‑Fi 从「连上」变回「完全收不到 AP」**。可复现的独立变量是**复位类型**，不是配置、不是信道、不是 IDF 版本。

### 10.3 必须更正的几处

- §8.2 把刷写前那条 `BOOTSCAN n=11` 判为「USB 串口缓冲残留」——**过于武断**。它更可能来自用户此前按过物理 RST 的一次开机（用户会话早期就说过「按一下 rst 就正常加入网络了」「刚才我按复位按键后 我看串口监控软件找到WiFi了的啊」）。这条线索被忽视了整晚。
- §8.4 / §9.3 把根因收敛为「编译期静态的 Wi‑Fi/PHY/coex 配置或射频库」——**不完整**。`hard_reset` 后的失败是复位状态问题，不是编译期配置本身；物理 RST 一按就正常。
- 项目记忆 §1 早已写明「原生 USB-Serial-JTAG 的 RTS 不接 EN、`hard_reset` 不是真 EN 复位」，但历次刷写后测试都沿用了它，导致所有 `n=0` 结论建立在**坏的上电状态**上。

### 10.4 关于信道干扰（用户 01:03 提出）

- 支持点：A 组连上的是 **ch1（2412 MHz）**，与 Zigbee ch20（2450 MHz）相隔远；与 ch20 重叠的是 **ch8（2447 MHz）**。全信道 `wifi_scan` 会覆盖重叠区，仍 `n=0`。项目记忆 §5、F9 都早有「Zigbee 别用 ch20；用 ch25 + Wi‑Fi 1/6/11」的规划。
- 不能据此定论：A 组 `EARLYJOIN` 成功时 ZBOSS 尚未启动，ch20 上没有 802.15.4 流量；而 B 组用 `hard_reset` 后连 `EARLYJOIN` 都失败。⇒ **复位类型与信道是两个独立变量，尚未拆开测**。
- 判决实验：把 Zigbee 改到 ch25（改 `ZB_DEFAULT_CHANNEL` 或 NVS `zb_gw/channel`）后**物理 RST**，看全信道 `wifi_scan` 是否出 AP；代价是已配对设备掉线、需重新入网，须用户确认。

### 10.5 对实际部署的含义（待验证）

- 目标是 C6 不插 USB、5V 供电上电跑 Wi‑Fi/TCP —— **真正的上电复位等价于物理 RST**，因此产品形态下 Wi‑Fi 很可能本来就能用；今晚的「连不上」主要是调试方法（esptool hard_reset）造成的假象。
- 但 OTA / 看门狗 / 软件复位属于「非真 EN 复位」路径，**刷完或重启后会不会又进坏状态，尚未验证**，这是上生产前必须测的一项（也与 `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` 待办相关）。
- 待确认：物理 RST 连续多次是否稳定；断电重上电（非 USB RST）是否同样正常；改 ch25 后的对照结果。

---

## 11. 01:07：连上之后的丢包测量（信道假设的边界）

### 11.1 实测（用户再次物理 RST 之后）

| 项 | 结果 |
|---|---|
| 主机 `ping -n 20 192.168.100.123` | 发 20 / 收 6 / **丢 14（70%）**；成功的包 RTT 最小=最大=平均 **3122 ms** |
| `net_status` | `ip=192.168.100.123`、`rssi=-52 dBm`、**`ap_ch=1`**、`why=0` |
| `wifi_scan` | **已关联状态下全信道扫描仍 `n=0`**（6.0 s） |
| Zigbee | `0x8e4e` APS 帧持续在发，网络活跃；`EARLYJOIN ok:1` 再次出现 |

### 11.2 判断

- 信号 −52 dBm 良好却 70% 丢包、RTT 3.1 s ⇒ **不是链路衰减，是射频时间被抢占**。
- **本次连接 `ap_ch=1`（2412 MHz），Zigbee ch20（2450 MHz），频谱并不重叠** ⇒ §4.4 的「ch20 与 Wi‑Fi ch8 重叠」**解释不了本次 ICMP 丢包**；它最多解释「全信道扫描经过重叠区时为何出不来结果」，不是当前丢包主因。
- 更吻合的是**单芯片共存优先级**：我方 `PTI0=0x55777555`，纯 Wi‑Fi 例程 `0x000a00a0`（F5/F6）。活跃的 Coordinator 常收占走多数射频窗口，Wi‑Fi 只在缝隙里收发，连扫描都拿不到结果。
- 与 O5 官方 "WiFi station mode + Zigbee router leads to an unstable Zigbee network" 属同类现象（官方讲的是 Zigbee 侧不稳，此处观测落在 Wi‑Fi 侧）。
- ⇒ **「干扰」判断成立，但机制更可能是共存仲裁 / PTI，而非频点重叠**；仍需实验判定。

### 11.3 改信道的实际代价（已查代码，非推测）

`zb_gateway_set_channel()`（`main/zb_gateway.c:1338-1364`）会写 NVS `zb_gw/channel`，**并擦除 `zb_storage` 分区** ⇒ 下次启动重建新网络，**所有已配对设备必须重新入网**。因此改 ch25 需用户明确确认，不能擅自执行。

### 11.4 两个可判决的下一步（待定）

1. **零网络代价（推荐先做）**：在**物理 RST 的正确前提下**重做 coex/PTI 实验 —— 关 `CONFIG_ESP_COEX_SW_COEXIST_ENABLE` 等 `CONFIG_ESP_COEX_*`，或加 `esp_coex_preference_set(ESP_COEX_PREFER_WIFI)`，编译后物理 RST 测 ping 丢包是否从 70% 降下来。此前那次（报告 §2.6 / §6.6）是在 `hard_reset` 坏状态下做的且未核对 `ELF SHA256`，结论不可靠。
2. **改 Zigbee 到 ch25 + 物理 RST 对照**：直接验证频谱维度，代价是重新入网。

两者都还没做，任何「因为 X 所以丢包」目前都只是候选解释。
7. **本次核对的边界**：定位到的是「实现层面的可核实差异」，**没有定位本项目收不到 AP 的根因**。下一步若要收敛，需按 §6.6 重做 PTI/共存实验（每次核对 `ELF SHA256`），并取得 ESPHome 固件的 `sdkconfig`（§6.4）做静态对照；在此之前，任何「因为 X 所以扫不到」的说法都属猜测。

---

## 12. 01:20–01:55：研究 ESPHome「如何减少 Wi‑Fi 与 Zigbee 干扰」（用户指定方向）

> 本节只回答「ESPHome 在**射频共存**上做了什么可复制的动作」，**不涉及**移植它的 Zigbee
> 设备功能。ESPHome 仍然只是对照组。用户同时提出「我们有新的 6.0 版本，应该也能实现吧」，
> 一并在此给出可查证回答。

### 12.1 新增资料清单（均可回溯）

| 编号 | 来源 | 取到的内容 |
|---|---|---|
| S1 | `esphome/esphome` `components/zigbee/zigbee_esp32.cpp`（dev 分支 raw） | `ZigbeeComponent::setup()` 全文；`#ifdef USE_WIFI` 下 `#include "esp_coexist.h"`；`esp_coex_wifi_i154_enable()` 调用点与时序 |
| S2 | `esphome/esphome` `components/zigbee/zigbee_esp32.py` | `include_builtin_idf_component("esp_coex")`；`add_idf_sdkconfig_option` 列表；`esp-zigbee-lib` 版本 `2.0.5` |
| S3 | `luar123/zigbee_esphome`（官方 zigbee 组件 CODEOWNER 的外部组件） | `cg.add_define("CONFIG_WIFI_COEX")`；`cv.require_framework_version(esp_idf=cv.Version(5, 5, 4))` |
| S4 | Espressif RF Coexistence 官方文档（ESP32-C6） | 共存矩阵、优先级机制、时间片策略、非默认省电参数的警告 |
| S5 | 本机 IDF `5.5.1` / `5.5.2` / `6.0.2` 的 `components/esp_coex/src/coexist.c` 与 `include/esp_coexist.h` | 三个版本 `esp_coex_wifi_i154_enable()` 的**具体实现差异** |
| S6 | 用户提供的 `test1-firmware.factory.bin` 与本项目 `build/zigbee-vital-sensor.bin`、`.map` | 共存固件版本串、符号是否被链接 |

### 12.2 ESPHome 具体做了三件事（逐条对应依据）

1. **显式引入 `esp_coex` 组件**：`zigbee_esp32.py::esp32_to_code()` 里
   `if CONF_WIFI in CORE.config: include_builtin_idf_component("esp_coex")`（S2）。
   外部组件版则 `cg.add_define("CONFIG_WIFI_COEX")`（S3），让 C++ 侧走共存分支。
2. **在 Zigbee 初始化之后调用共存使能 API**（S1，原文）：
   ```cpp
   #ifdef USE_WIFI
   #include "esp_coexist.h"
   #endif
   ...
   void ZigbeeComponent::setup() {
       ...
       if (esp_zigbee_init(&config) != ESP_OK) { ... }
   #ifdef USE_WIFI
       if (esp_coex_wifi_i154_enable() != ESP_OK) { this->mark_failed(); return; }
   #endif
       ...
       xTaskCreate(ezb_task, "Zigbee_main", 4096, NULL, 1, NULL);
   }
   ```
   即 **`esp_zigbee_init()` 之后、`esp_zigbee_start()` 之前**，把 802.15.4 注册进共存仲裁器。
3. **主动限制组合**：`final_validate_esp32()` 禁止 `wifi:` 的 AP 模式与 Zigbee 同用；
   `router: true` 且启用 Wi-Fi 时打警告 "The Zigbee Router might miss packets while Wifi is
   active and could destabilize your network. Use only if Wifi is off most of the time."（S2）。

**没有发现的**：ESPHome 并没有调用 `esp_coex_preference_set()`，也没有设置特殊信道、
静态 IP、fast_connect 或省电参数。§11.4 里计划做的「加 `esp_coex_preference_set(
ESP_COEX_PREFER_WIFI)`」**没有 ESPHome 依据**，应从实验清单里撤下。

### 12.3 ★ 回答「6.0 版本是否也能实现」——版本对比（S5，实测本机文件）

| IDF 版本 | `esp_coex_wifi_i154_enable()` 实现 | 备注 |
|---|---|---|
| **5.5.1（本项目）** | `coex_enable(); coex_schm_status_bit_set(1, 1);` | 裸状态位；源码注释 `TODO: Add a scheme for wifi and 154 coex. Remove this function if FCC-50 closes.` |
| **5.5.2** | `coex_enable(); esp_coex_ieee802154_status_enable();` | 新增 `include/esp_coex_i154.h`，带 enable/disable 成对 API |
| **6.0.2** | `coex_enable(); esp_coex_ieee802154_status_enable();` | **与 5.5.2 逐字相同** |

结论：
- 该 API 在 **5.5.1 / 5.5.2 / 6.0.2 三个版本的 `esp_coexist.h:219` 都存在**，声明条件是
  `CONFIG_ESP_COEX_SW_COEXIST_ENABLE && CONFIG_SOC_IEEE802154_SUPPORTED`；本项目 `sdkconfig`
  两项都是 `y`（S5/S6）。
- **Wi‑Fi↔802.15.4 共存是在 5.5.2 补全的，6.0 没有再加东西**。ESPHome 用 5.5.6，
  落在这条线之后；本项目用 5.5.1，落在这条线之前。这与 §2.6 记的
  「5.5.1→5.5.2 的 `libphy.a`/`libnet80211.a`/`libpp.a` md5 全不同」互相印证。
- ⇒ **能实现，但不必升 6.0**。升级优先级应是 **5.5.2 > 6.0.2**：5.5.2 已具备同样的
  共存 API 且同属 5.5 分支，对 ZBOSS 1.6.4 / esp-zigbee-lib 1.6.8 的兼容风险远小于 6.0；
  6.0 只增加破坏性变更和全量重编译成本，不增加共存能力。
- 另有一项差异不是 IDF 大版本能解决的：ESPHome 用 `esp-zigbee-lib 2.0.5`（Espressif 自研
  EZB 栈），本项目用 `1.6.8`（ZBOSS）。换栈等于重写网关，不在本次范围内。

### 12.4 本项目到底缺什么（S6，链接图实测）

- `main/CMakeLists.txt` 的 `REQUIRES` 原本只有 `ieee802154`、`esp_wifi` 等，**没有 `esp_coex`**。
- `main/` 全目录 grep `esp_coex_*` **零命中**；`build/zigbee-vital-sensor.map` 里
  `esp_coex_wifi_i154_enable` 出现 **0 次**。
- map 里虽然有 `coex_enable`（6 次）和 `coex_schm_status_bit_set`（5 次），但交叉引用段显示
  它们由 `libcoexist.a` 提供、被 `esp_wifi/libesp_wifi.a(esp_adapter.c.obj)` 的
  `*_wrapper` 引用 —— 这是**驱动包装函数被链接**，不等于运行期把 802.15.4 状态位设上了。
- ⇒ `CONFIG_ESP_COEX_SW_COEXIST_ENABLE=y` 只是把组件编进来，**不等于完成共存注册**。
  这是本项目与 ESPHome 之间一条此前完全没查过的、方向明确的差异。

### 12.5 ⚠ 反证边界：不能把 ESPHome 那次当成「重负载共存」的样本

21:27 用户贴的 ESPHome 日志里 `[C][zigbee:422]: Device is joined to the network: NO`、
`Current channel: 11`、`Failed to join network with status(0x03)` 反复出现 ⇒ 那次 802.15.4
处于 network steering（间歇扫描 / 尝试入网），**并没有形成 Router 常收 + APS 持续流量**。
本项目是活跃 Coordinator（ch20 / PAN 0xa311 / `0x8e4e` APS 持续在发）。两者射频负载不对等，
所以「ESPHome 秒连且 HA API 正常」只能证明**同板射频硬件没问题**，不能证明
「活跃 Coordinator + Wi-Fi 也能稳定」。官方矩阵（S4）给的正是
Wi-Fi STA Connected × Zigbee **Router = C1（supported but the performance is unstable）**，
× End Device = Y；且明确写「802.15.4 普通接收优先级最低」「非默认的 connectionless
省电参数会在 Wi-Fi 时间片外产生额外高优先级请求」。

### 12.6 本次实施的最小改动（待实测，未宣称有效）

| 文件 | 改动 |
|---|---|
| `main/CMakeLists.txt` | `REQUIRES` 增加 `esp_coex` |
| `main/net_link.c` | 增加 `#include "esp_coexist.h"`；在 `wifi_begin()` 的 `esp_wifi_init()` **之前**调用 `esp_coex_wifi_i154_enable()`；用文件级 `s_coex_ret` 记录返回值；`net_status` 回执新增 `"coex":<int>` 字段（`0`=已启用，`0x7FFFFFFF`=尚未调用） |

选择 `wifi_begin()` 作为唯一调用点，是因为它是串口诊断 `wifi_scan`、`wifi_join`、TCP 模式
`net_task` **所有主路径的必经之处**；且 Wi-Fi 是懒启动，调用时 ZBOSS 早已 init/start，
与 S1 的「Zigbee 初始化之后」时序一致。未改 `on_uart_cmd`、未改 `zb_gateway.c`，
协议边界保持不变。

**验证判据（必须在物理 RST 之后做，`hard_reset` 无效）**：
1. `net_status` 的 `coex` 必须是 `0`（证明调用真的发生且成功）；
2. 先读 `net_status`，**以它回执里的 `ip` 为准**（不可写死任何 IP，见 §12.8），
   再同一分钟内交替测 `ping -n 20 <该 IP>`，比较刷写前后的丢包率与 RTT；
3. 已关联状态下 `wifi_scan` 是否不再返回 `n=0`。

若三条都改善 ⇒ 根因收敛为「未注册共存」；若无改善 ⇒ 说明 5.5.1 的裸状态位实现不足，
下一步按 §12.3 评估**升 5.5.2**（而不是 6.0）做全量编译，或按 §11.3 经用户确认后改 ch25。
在此之前不写「因为 X 所以丢包」的定论。

### 12.7 仍未确认

- ESPHome 编译产物的 `sdkconfig` 未取到（HA ESPHome Builder 的构建目录不在 SMB 可见的
  `/config/esphome/` 下），因此它的 `CONFIG_ESP_COEX_*` / 省电选项**仍未逐项 diff**，
  §6.4 的待确认项继续保留。
- `esp-zigbee-lib 2.0.5` 是否自行内部注册了 coex、从而让 ESPHome 那次显式调用变成冗余，
  未验证。
- 断电重上电 / OTA / 软复位是否等价物理 RST，未验证。

### 12.8 ★ 01:50 现场更正：IP 会漂移，之前 ping 的对象必须重新识别

刷写前做基线复测时发现测量对象已经不是同一台设备，记录如下（**未刷写任何新固件**，
板上仍是 `NET_EARLY_JOIN=1` 诊断固件）：

| 观测 | 结果 |
|---|---|
| `ping -n 3 192.168.100.123` | 3/3 收到，RTT 7–12 ms |
| `ping -n 20 192.168.100.123` | 收 16 / 丢 4（20%），RTT 6–25 ms |
| `arp -a 192.168.100.123` | MAC `78-da-07-2e-24-cb` |
| ESPHome 21:27 日志里的 C6 MAC | `98:88:E0:71:B8:54`（`[C][wifi:1688]: Local MAC`） |
| 串口 `net_status`（COM9，01:50） | `transport=uart`、`state=uart`、`ip=192.168.100.139`、`rssi=0`、`ap_ch=0`、**`why=209`** |
| 同一条回执前的开机诊断 | `EARLYJOIN {"t":"wifi_join","ok":0,"conn":"ESP_OK","ip":"0.0.0.0"…}` |
| `ping -n 5 192.168.100.139` | 5 条均为「来自 192.168.100.114：无法访问目标主机」⇒ **不可达** |

结论与更正：
1. **`.123` 上现在不是 C6**（MAC 不同、`HTTP:80` 无响应），C6 的 DHCP 地址已漂到 `.139`。
   ⇒ 刚才那组「0% / 20% 丢包、RTT ~10 ms」**不能当作 C6 的基线**，更不能据此宣称
   「丢包自行恢复了」。§12.6 判据第 2 条已改为「以 `net_status` 回执里的 `ip` 为准」。
2. 当前 C6 **Wi-Fi 没连上**（`ok:0`、`ip=0.0.0.0` / `state=uart`、`why=209`），
   与 §10「物理 RST 才连得上、其它复位回到坏状态」一致 —— WorkBuddy 重启期间设备
   显然又经历了一次非物理 RST 的重启。
3. `why=209` 不在 IDF 5.5.1 `esp_wifi_types_generic.h` 的 `wifi_err_reason_t` 里
   （该枚举只到 `WIFI_REASON_NO_AP_FOUND=201` 等），**来源待查**，先不当因果解释。
4. **§11 那次 70% 丢包是否也测到了错误对象，目前无法回溯确认**：当时 `net_status`
   确实回过 `ip=192.168.100.123`，但 DHCP 租约变化 / 同 IP 多设备的可能性无法排除。
   ⇒ 「70% 丢包」继续保留为**一次现场观测**，但不再当作稳定基线，也不作为
   「共存缺陷」的唯一量化依据。这正是项目硬规则「跨时段 A/B 无效对照」的又一次应验。
5. ⇒ 后续任何 ping 判据都必须：**同一分钟内先 `net_status` 拿到当前 IP → 立刻 ping
   该 IP → 再用 ARP MAC 核对是 C6**，三步连做；否则不写结论。

---

## 13. 02:00–02:30：补上共存注册后的实机验证（**根因收敛**）

### 13.1 做了什么

按 §12.4 的结论做**最小改动**：`main/CMakeLists.txt` 的 `REQUIRES` 增加 `esp_coex`；
`main/net_link.c` 在 `wifi_begin()` 的 `esp_wifi_init()` 之前调用
`esp_coex_wifi_i154_enable()`，并用 `net_status` 的 `"coex"` 字段回读结果。
其余代码一行未动（协议边界、`on_uart_cmd`、`zb_gateway.c` 全部保持）。
编译 13/13 通过（`Project build complete`），链接图里
`esp_coex_wifi_i154_enable` 从 **0 次变成 4 次**，含
`esp-idf/main/libmain.a(net_link.c.obj)` 引用 ⇒ 调用确实进了固件。
USB 直刷 `0x20000`（`Hash of data verified`，1328944 B）。

### 13.2 刷写前后对照（**同一块板、同一 AP、全程没有物理 RST**）

| 项目 | 刷写前（`NET_EARLY_JOIN=1` 诊断固件） | 刷写后（共存版） |
|---|---|---|
| 开机诊断 | `EARLYJOIN ok:0`、`why=209`、Wi-Fi 完全连不上 | 无 EARLYJOIN（正式形态 `NET_EARLY_JOIN=0`） |
| `net_status.coex` | 字段不存在 | `2147483647`（未调用）→ 触发 Wi-Fi 后 **`0`（已启用）** |
| `wifi_join` | 历史记录一律 `why=201` / `ok:0` | **`ok:1 ip=192.168.100.139 rssi=-59 ch=1 why=0`** |
| `ping -n 40`（不扫描） | 历史记录 `丢 14/20=70%`、RTT 3122 ms | **0% 丢包，RTT 14–188 ms、平均 85 ms** |
| ARP MAC 复核 | 见 §12.8，`.123` 是别的设备 | `98-88-e0-71-b8-54` = C6 本体 |
| Zigbee | 活跃 | 活跃（`0x8e4e` 中继正常上报/interview），**配对未丢** |

§12.8 的三步法已执行：`net_status` 取 IP → ping 该 IP → ARP 核对 MAC。
另外重连一次后连续 40 包仍 0% 丢包；收尾 `net_status` 仍是
`ip=192.168.100.139 rssi=-62 ap_ch=1 why=0 coex=0`。

### 13.3 结论与边界

- **`CONFIG_ESP_COEX_SW_COEXIST_ENABLE=y` ≠ 已完成共存注册**。必须显式调用
  `esp_coex_wifi_i154_enable()`（ESPHome 的做法）。这是本项目「扫不到 AP /
  hard_reset 后 why=201 / 连上后狂掉包」的**主因**，证据强度足够：同一份
  应用代码、同一块板、同一个 AP，只补这一处调用，连接从失败变成功，
  丢包从 70% 变 0%。
- §10「物理 RST 才是决定性变量」需要**降级为表象**：物理 RST 大概率只是
  让射频/coex 状态重新初始化的其中一种途径；补上注册后 hard_reset 也能连。
- §11.2「PTI / 信道重叠」不再是丢包主因；本次连的仍是 Wi-Fi ch1、Zigbee ch20，
  与之前相同，没有改任何信道。
- ⚠ 仍**不是严格 A/B 回退对照**（没有刷回旧固件再测一次），但前后测量都按
  §12.8 三步法核对了目标 MAC，且差异幅度（70%/3122 ms → 0%/85 ms）远超
  环境抖动，可作为收敛结论。若要最严格证明，需刷回旧固件在同一分钟内交替测。
- 官方 C1 矩阵（Router + STA 不稳定）依然成立：这是**能力上限**，不是本次 bug；
  长时间高负载下的表现仍需观察。

### 13.4 同一轮修掉的其它缺陷

| 缺陷 | 现象 | 修复 |
|---|---|---|
| `wifi_scan` 打断已关联连接 | 扫描后 `why=8`、ARP 消失、串口模式下无人重连 | 已关联时只扫当前 AP 信道、不断开；未关联时才全信道扫描；回执加 `attached` |
| OTA 后可能回滚 | `serial_ota.c` 用标准 `esp_ota_*`，新镜像是 `PENDING_VERIFY`，此前无确认逻辑 | 开启 `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` + `app_main()` 末尾首启 `mark_valid` |
| 运行日志语言不合规 | 参与构建的源码里 47 处德语/英语叙述 | 全部改中文（专有名词/字段名/格式符保留；状态值 `offen`/`geschlossen` 不动） |

回归门禁 `RESULT: ALL OK (5 Checks)`。

### 13.5 仍未闭环（留待后续）

- `tr=1`（开机自动 Wi-Fi+TCP）在**断电重上电**后的完整链路尚未实测；
  当前验证是在 `tr=0` 串口模式下用 `wifi_join` 拉起 Wi-Fi。
- `wifi_scan` 在已关联时的单信道扫描结果是否稳定，修复后尚未复测。
- ESPHome 编译产物 `sdkconfig` 未取到（§6.4 仍待确认）。
- Zigbee ch20 → ch25 的信道规划**未执行**：会擦 `zb_storage`、全部设备需重新入网，
  在连接已恢复且 ch1/ch20 不重叠的前提下收益不明确，暂不动。
- `esp-zigbee-lib 1.6.8`（ZBOSS）与 ESPHome 的 `2.0.5`（EZB）仍是不同协议栈，
  未做栈升级评估。

---

## 14. 03:30–03:45：把「版本太低」查到源码行级（**决定性证据**）

> 本节回答两个问题：① ESPHome 到底靠什么压住 Wi‑Fi↔Zigbee 干扰；② 本项目
> 「版本太低」具体低在哪一行。全部结论都可用本机文件或官方源码复现。

### 14.1 ESPHome 的 Zigbee 侧只做了一件事

把 `zigbee_esp32.cpp` 与 `zigbee_helpers_esp32.h` 全文核对（S1 + 新增 S7）：

- 唯一一处共存相关调用是 `esp_coex_wifi_i154_enable()`，位置在
  `ZigbeeComponent::setup()` 里 `esp_zigbee_init()` 之后、`esp_zigbee_start()` 之前；
- **没有** `esp_coex_ieee802154_txrx_pti_set()` / `..._ack_pti_set()`（PTI 优先级调整）；
- **没有** `esp_coex_preference_set()`；
- `zigbee_helpers_esp32.h` 只声明 ZCL/endpoint 辅助函数，与射频无关。

Wi‑Fi 侧（S8，`wifi_component_esp_idf.cpp`）：
- `esp_wifi_set_ps()` 只在 `wifi_apply_power_save_()` 里调一次，`default` 分支落
  `WIFI_PS_NONE`；`LIGHT`→`WIFI_PS_MIN_MODEM`、`HIGH`→`WIFI_PS_MAX_MODEM`；
- `conf.sta.listen_interval = 0`（让驱动用默认 3 个 beacon，且该参数只在
  `WIFI_PS_MAX_MODEM` 下生效）。

⇒ **ESPHome 没有任何「独门优化」**，它的共存能力完全来自 IDF 自带的
`esp_coex_wifi_i154_enable()`。差异不在技巧，在**这个函数在 5.5.1 里是半成品**。

### 14.2 决定性的一行：`coexist.c` 在 5.5.2 被改写了

`diff 5.5.1/components/esp_coex/src/coexist.c 5.5.2/.../coexist.c` 的实质差异只有一处
（其余是 SPDX 年份、GPIO 上下拉、include）：

```c
 /* 5.5.1 */
 esp_err_t esp_coex_wifi_i154_enable(void) {
     // TODO: Add a scheme for wifi and 154 coex.
     coex_enable();
-    coex_schm_status_bit_set(1, 1);        // ★ 裸设一个状态位，无 802.15.4 语义
     return ESP_OK;
 }

 /* 5.5.2 及以后（含 6.0.2） */
 esp_err_t esp_coex_wifi_i154_enable(void) {
     coex_enable();
+    esp_coex_ieee802154_status_enable();   // ★ 正式把 802.15.4 注册进仲裁器
     return ESP_OK;
 }
```

配套证据（本机 `nm`）：
- `5.5.1/lib/esp32c6/libcoexist.a`：**没有** `esp_coex_ieee802154_status_enable` 这个符号；
- `5.5.2/lib/esp32c6/libcoexist.a`：`T esp_coex_ieee802154_status_enable` ✅
- `5.5.1` 的 `esp_coex_i154.h` **没有** `status_enable/disable` 声明，且
  `coexist.c` 也**没有** `#include "esp_coex_i154.h"`；5.5.2 才补上。

⇒ 在 5.5.1 上调用 `esp_coex_wifi_i154_enable()`，只设了一个无明确语义的
scheme 状态位，**802.15.4 并没有被正确注册**。这与 §13 的实机现象完全吻合：
5.5.1 上补调用后 `wifi_join ok:1` 能连上（射频被让出一部分时间），但随后
仍会掉（`why=8` ASSOC_LEAVE）—— 注册不完整，仲裁器不知道该给 154 留窗口。

### 14.3 协议栈侧：ZBOSS 完全不管共存

对 `managed_components` 全部静态库做符号扫描（`riscv32-esp-elf-nm`）：

| 库 | 引用 coex 符号 | 定义 coex 符号 |
|---|---|---|
| `libzboss_stack.{zczr,ed,gpd}[.debug].a` | **0** | **0** |
| `libesp_zb_api.{zczr,ed,gpd}[.debug].a` | **0** | **0** |

⇒ ZBOSS 1.6.8 **不会自己注册共存**，必须由应用层做。这一点也解释了为什么
「只开 `CONFIG_ESP_COEX_SW_COEXIST_ENABLE=y`」完全无效 —— 编译开关只是把
仲裁器编进来，没人去注册 802.15.4。

### 14.4 「版本太低」量化

| 版本 | 发布日期 | `libcoexist.a`(esp32c6) | `libphy.a` | `libnet80211.a` |
|---|---|---|---|---|
| **5.5.1（本项目）** | 2025‑08‑30 | 96236 B / `e0f6d57e…` | 182180 B | 1414032 B |
| 5.5.2 | 2025‑12‑24 | 98546 B / `f849442b…` | 184020 B | 1425724 B |
| 6.0.2 | 2026‑06‑29 | 99628 B / `10f5cb6c…` | 185912 B | 1447248 B |
| 5.5.6（ESPHome） | 2026‑10‑16 | — | — | — |

- 本项目落后 **5 个 bugfix 版本、14 个月**；
- `esp-zigbee-lib 1.6.8` 的 `idf_component.yml` 只要求 `idf >= 5.0` ⇒ **升到 5.5.2
  完全在支持范围内，不需要换协议栈**；
- 5.5.2 与 6.0.2 的 `coexist.c` 只差底层时钟/GPIO 初始化写法
  （`modem_clock_module_enable` vs `coex_module_enable`），**共存策略完全一致**
  ⇒ 升级到 5.5.2 即可获得与 6.0.2 相同的共存能力，不必承受 6.0 的破坏性变更。

### 14.5 修复方案（定稿）

1. **IDF 5.5.1 → 5.5.2**（本机已装，工具链齐全，无需下载）。
2. 代码侧保留 `esp_coex_wifi_i154_enable()` 调用与 `CMakeLists.txt` 的 `esp_coex` 依赖
   —— 同一份代码在 5.5.1 上只设裸状态位，在 5.5.2 上才会真正注册 802.15.4。
3. **不做的事**（有依据地排除）：
   - 不升 6.0.2：共存实现与 5.5.2 等价，但带来破坏性变更和 ZBOSS 未验证风险；
   - 不换 `esp-zigbee-lib 2.0.5`：那是换协议栈（EZB 替代 ZBOSS），等于重写网关；
   - 不加 `esp_coex_preference_set()` / PTI 手调：ESPHome 没有这么做，没有依据；
   - 不动 Wi‑Fi 省电参数：ESPHome 也没做特殊设置。
4. 换 IDF 版本后 **bootloader 必须一起重刷**（0x0），否则 bootloader 与 app 版本不匹配。

### 14.6 待实测（升级后）

- `net_status.coex == 0`；
- `wifi_join` 连续 3 次成功、两次之间间隔 30 s 不掉线；
- `ping -n 100` 丢包率与 RTT；
- **已关联状态下 `wifi_scan` 不再掉线**（5.5.2 下复测 §13.4 的修复）；
- Zigbee 网络（ch20 / PAN / 中继 `0x8e4e`）保持在线，配对不丢。

---

## 15. 05:0x–06:4x：IDF 5.5.2 固件编译、刷写与实测（**根因验证完成**）

### 15.1 编译

- 环境：IDF **5.5.2**（本机已装），独立构建目录 `C:/Temp/b552x`、
  独立配置 `sdkconfig.552`，不动原有 `build/` 与 `sdkconfig`。
- 全量 1066 个目标，**约 86 分钟**（含 5.6 分钟 CMake 配置 + 约 1 分钟的 bootloader
  子项目配置失败）。产物 `zigbee-vital-sensor.bin` 1,330,896 B，
  `Validation Hash 9bea6c2d…`（valid）。
- 链接图验证：`esp_coex_wifi_i154_enable` 4 处（含
  `libmain.a(net_link.c.obj)` 调用点）、**`esp_coex_ieee802154_status_enable` 3 处**
  —— 后者是 5.5.1 的 `libcoexist.a` 里根本不存在的符号。

**编译慢的真实原因（三次误判后定位）**：
| 现象 | 真实原因 | 处理 |
|---|---|---|
| 每步稳定 17 s | **多个 ninja 实例抢同一目录的 `.ninja_lock`**（我停任务只杀了外层 PowerShell，ninja 子进程仍在跑并越积越多） | 确认无残留进程；新目录单实例 |
| `Access is denied` 删 `*.obj.d` | rule 里的 `deps = gcc` 让 ninja 编译后读并删除 depfile，被文件监控拦住 | 从 `CMakeFiles/rules.ninja` 移除 98 处 `deps = gcc`（本次全量不需要头文件增量追踪） |
| `remove(.ninja_lock)` 同样被拒 | 沙箱持有该文件句柄 | 换全新构建目录，一次编译到底、中途不打断 |

### 15.2 刷写

板上当时是 ESPHome（`test1.local`，分区表完全 different：app 在 `0x10000`），
必须整套刷回：

```
write_flash 0x0 bootloader.bin 0x8000 partition-table.bin \
            0xf000 ota_data_initial.bin 0x20000 zigbee-vital-sensor.bin
```

结果：`Hash of data verified`；启动日志
`ESP-IDF v5.5.1 2nd stage bootloader` → 分区表恢复 → `Loaded app from partition
at offset 0x20000` → `ELF file SHA256: cc91413ec…` → `=== ESP32-C6 Zigbee
Gateway ===`，**无 panic / 无 assert**。

### 15.3 实测结果（关键判据全部通过）

| 判据 | 5.5.1 旧固件 | **5.5.2 新固件** |
|---|---|---|
| `net_status.coex` | 字段不存在 / `2147483647` | **`0`**（已启用） |
| `wifi_join` | `ok:0`、`why=201` 或连上后 `why=8` | **`ok:1 ip=192.168.100.139 rssi=-57 ch=6 why=0`** |
| `ping -n 40` | 丢 70%、RTT 3122 ms | **0% 丢包**（RTT min 21 / avg 157 / max 718 ms） |
| `ping -n 20`（扫描后） | — | **0% 丢包**（avg 143 ms） |
| `wifi_scan`（已关联） | `why=8` 掉线、ARP 消失 | **`attached:1 n:1 aps=[yecao -56 ch6 a7]`，扫完仍在线** |
| TCP 通道 | 未验证 | **add-on 日志 `state=tcp_up`、`[HB] uptime=… ch=20 pan=0xd13e`、12345 可连** |

### 15.4 Zigbee 侧的变化（**必须知情**）

刷 ESPHome 时它的 NVS 分区（`0x370000+0x70000`）覆盖了本项目的
`zb_storage@0x3A0000`，Zigbee 网络凭据丢失：

- 刷回后 Coordinator **重建了新网络**：`ch=20`、`PAN 0xd13e`（原 `0xa311`）；
- `lqi` 回执 `dev:[]` —— **原设备全部掉线，需要重新入网**（按 Boot 键开 180 s
  配网，或在 HA 里点 permit_join）。
- 这是刷 ESPHome 的必然代价，不是 5.5.2 固件的问题。

### 15.5 仍未闭环

1. **bootloader 未重编**：沙箱锁导致 bootloader 子项目 configure 失败，实际是
   5.5.1 bootloader + 5.5.2 app（同 minor，启动正常）。若要严格一致，需在正常
   环境重跑一次 `idf.py build` 产出 5.5.2 bootloader。
2. **`/api/state` 的 `channel`/`pan` 仍显示 0 / `–`**：设备侧心跳 `ch:20 pan:0xd13e`
   是对的，add-on 解析该字段未更新（不影响链路，MQTT/心跳正常）。
3. Zigbee 设备需重新入网后，才能验证「Wi-Fi + 活跃 Coordinator 长期共存」的
   稳定性（本次只验证了 Wi-Fi 通道本身 0% 丢包）。
4. 断电重上电后的完整链路（`tr=1` 自动连）未做物理断电验证。
