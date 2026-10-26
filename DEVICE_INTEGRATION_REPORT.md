# Zigbee 设备接入报告

生成时间：2026-10-17 06:00 · **最近核对：2026-10-17 20:20** · 仓库 `esp32c6-zigbee`（ESP32-C6 协调器 + HAOS add-on）

> §一～§五 反映**当前**状态；§六 以后是按时间排列的定位与修复记录，其中的
> 版本号、固件名只在当时有效。**当前有效组合：add-on v1.2.5 + 固件
> `firmware/zigbee-vital-sensor-txpower.bin`（792 880 B，ELF `f7e9fbcd2ab4dad0`）。**

---

## 一、结论速览

| 项目 | 结果 |
|---|---|
| add-on 方案 | ✅ **HA MQTT 自动发现**，`homeassistant/<component>/<node>/<obj>/config`（retain）。**无需改任何 YAML** |
| 型号自动识别 | ✅ 已接入 **Z2M 设备定义库 4 882 个型号**（zigbee-herdsman-converters v26.120.0，二进制型号键 810 → 0） |
| 情景面板 | ✅ `FTB56+ZSN18ZY1.1`（0x747d）按键事件接通（Report 与 genScenes 两种上报形态都收，§20） |
| 开关 vs 面板判别 | ✅ 全自动：Simple-Descriptor 簇方向 + Read-Attributes 应答，双判据（§18） |
| MQTT 层 | ✅ 照 Z2M 重做：发现去重比内容、启动回读窗口、孤儿清理、availability=latest、HA 重启重发（§19） |
| 发射功率 | ✅ 运行时可调（−24…20 dBm）并回读实际值；A/B 实测脚本 `txpower_ab.py`（§21） |
| 单元测试 | ✅ 12 个测试文件 299 项断言全过（`test_mqtt` 35 · `test_panel` 55 · `test_ha_state` 43 …） |
| 待办 | OTA 烧 `txpower` 固件并核对 ELF SHA；重建 add-on 到 1.2.5；跑一次功率 A/B 实测 |

---

## 二、设备清单与接入状态

### 2.1 实测在线设备

| 短地址 | 型号（ZCL Basic 0x0005） | 厂商 | Z2M 识别 | HA 实体 | 状态 |
|---|---|---|---|---|---|
| `0x07da` | `FB56+ZSW1IKJ2.5` | Feibit Inc co. | `HGZB-043`（Nue / 3A） | `switch` ×3（上 ep16 / 中 ep17 / 下 ep18） | ✅ 自动发现，含反向控制；墙内 LQI 15–30 / −77，墙外 153 / −50 |
| `0x747d` | `FTB56+ZSN18ZY1.1` | Feibit CO.,LTD | 库内无此型号，按同族判为**情景面板** | `event` ×2（ep1/ep2，`recall_0..31` 预声明） | ✅ 按键事件已接通（§20）；LQI 61 / −68 |

实测产出（`probe` 命令读取的真实报文）：

```json
{"t":"zigbee","addr":2010,"sub":"basic","p":{"mfr":"Feibit Inc co.  ","model":"FB56+ZSW1IKJ2.5"}}
```

自动生成的 HA 配置：

```
homeassistant/switch/c6_0x07da/ep16_on_off/config   name="HGZB-043 上 开关"
    state_topic  = gw/coordinator/zigbee/0x07da/ep16/on_off   value_template="{{ value_json.v }}"
    command_topic= gw/coordinator/set/0x07da/ep16/on_off
    device = { HGZB-043 / Nue / 3A }   unique_id = c6_0x07da_ep16_on_off
```

### 2.2 仓库内自带的 Zigbee 设备固件（非你需要接入的终端，仅供参考）

| 固件 | 实现的设备类型 | 簇 | 说明 |
|---|---|---|---|
| `firmware/zigbee-client.bin` | `ESP_ZB_HA_ON_OFF_LIGHT_DEVICE_ID`（ep1） | OnOff server + 温度 + OTA client + 私有簇 0xFF01 | `client/main/zb_device.c` 自研节点 |
| `build/zigbee-vital-sensor.bin` | 协调器本体（`ESP_ZB_HA_ON_OFF_SWITCH_DEVICE_ID` EP1 + EP11） | 见下表 | 本项目主固件 |
| `firmware/esp32c6_matter_vital_gateway.bin` | Matter 网关 | — | 与本方案无关 |

### 2.3 型号库覆盖度（4 882 个已知型号，zigbee-herdsman-converters v26.120.0）

> 下面的分类数字来自 v16.7.0 旧库（3 188 型号）时的统计，仅作量级参考；
> v26.120.0 重建后总数 4 882、二进制型号键 810 → 0，能力分类需按新库重新统计。

| 分类 | 数量（v16.7.0 时） | 含义 |
|---|---|---|
| **完全可接入** | 971 | 能力全部落在固件已实现的簇上（开关/温湿度/光照/占用/IAS/电量） |
| **部分可接入** | 1073 | 含本地未实现的簇（调光、窗帘、门锁、电量计量等）→ 只出已支持的那部分实体 |
| **仅知型号、无能力标注** | 1144 | 定义用了运行时逻辑，静态解析取不到能力 → 退化为按簇通用发现 |

**同族前缀继承**（§18）：库里没有的型号（如 `FTB56+ZSN18ZY1.1`）按"最长公共
前缀 ≥ 9 字符且族内判别一致"继承同族设备的判别结果，是未收录型号的唯一兜底。
厂商分布 Top5（旧库）：Philips 649 · Xiaomi 142 · IKEA 133 · HEIMAN 106 · Gledopto 88 · **Nue / 3A 52**

---

## 三、固件能力矩阵（决定"能不能自动发现"）

协调器在 interview 时对以下簇做 bind + configure reporting（`main/zb_gateway.c` 的 `s_iv_input`）：

| ZCL 簇 | ID | HA 实体 | 数值换算 |
|---|---|---|---|
| OnOff | 0x0006 | `switch` | `value_json.v` |
| Temperature Measurement | 0x0402 | `sensor` 温度 °C | `raw / 100` |
| Relative Humidity | 0x0405 | `sensor` 湿度 % | `raw / 100` |
| Illuminance | 0x0400 | `sensor` 光照度 lx | `10 ** ((raw-1)/10000)` |
| Occupancy Sensing | 0x0406 | `binary_sensor` 占用 | `value_json.occ` |
| IAS Zone | 0x0500 | `binary_sensor` 报警 | `value_json.motion` |
| PowerConfig 0x0021 / 0x0020 | 0x0001 | `sensor` 电量 % / 电压 mV | `pct` / `voltage_mv` |

反向控制：`{BASE}/set/{addr}/{epN}/on_off` → 固件 `dev_cmd`（on/off/toggle）。

### 暂不支持（需自定义 converter/handler，本轮未实现）

| 设备类别 | 缺失簇 | 原因 |
|---|---|---|
| 调光灯 / RGBW / 色温 | Level Control 0x0008、Color Control 0x0300 | 固件未注册这些簇的上报与命令处理 |
| 窗帘电机 | Window Covering 0x0102 | 同上 |
| 门锁 | Door Lock 0x0101 | 同上 |
| 电量计量插座 | Metering 0x0702 | 同上 |
| 场景面板 / 无线遥控器 | Scenes、Groups 的 **命令**分支 | 目前只 bind 了 client 簇，未把按键动作转成 HA `device_automation` 事件 |
| Tuya 私有簇设备 | 0xEF00 | 需专用编解码 |
| 小米/Aqara 私有属性 | 0xFCC0 | 需专用编解码 |

> 这些设备**仍能入网**，但只会出现它们已支持的那一小部分实体（如调光灯只出一个开关），或完全不出实体。

---

## 四、本轮改动的文件

**新增**
- `gen_device_db.py` — Z2M converter → 型号库生成器
- `host/addon/esp32c6_gateway/device_db.json` + `host/device_db.json` — 3188 型号库（677 KiB）
- `test_ha_discovery.py` — 11 组自动发现测试
- `test_autoreload.py` — 页面条件自动刷新测试（19 项断言）
- `test_upload.py` — multipart 二进制上传解析测试
- `test_tz.py` — 日志时区测试（6 组）
- `test_fw_ota.py` — 串口 OTA 端到端仿真（复刻 256 字节 `fgets` 的假 C6）
- `test_packaging.py` — 打包体检：无 CRLF/BOM、shebang、版本一致、Dockerfile COPY 目标
- `test_esptool_flash.py` — esptool 直刷测试（19 项，无需硬件）
- `probe_model.py` — 型号探测脚本
- 固件：`zb_gateway_probe_basic()`、`start_basic_scan()`、`request_ieee()` / `ieee_resolved_cb()`、`json_val()`

**修改**
- `host/addon/esp32c6_gateway/serial_gateway.py`（及 `host/serial_gateway.py` 同步）：HA 自动发现全套；条件自动刷新 `_autoreload_js()`（替换 `meta refresh`）；multipart 上传按 Content-Type 分流；本地时区 `_resolve_local_tz()`；OTA 帧 448→80 B 与紧凑 JSON `_j()`/`_js()`
- `main/ha_mqtt.c`、`main/main.c`：新增 `probe` 命令；修 JSON 解析；`dev_cmd` 键名改 `sw`
- `main/zb_gateway.c/.h`：Basic 多端点扫描、IEEE 解析、probe API
- `Dockerfile`：新增 `COPY device_db.json`；`apk add` 增加 `tzdata`
- `run.sh`：新增 `timezone` → `LOG_TZ` / `TZ`，`fw_chunk` → `FW_CHUNK`；新增 `/share/serial_gateway.py` 覆盖机制
- `config.json`：版本 1.0.0 → 1.1.0 → 1.1.1 → 1.1.2 → 1.1.3 → 1.1.4 → 1.1.5 → 1.1.6，新增 `timezone` 与 `fw_chunk` 选项
- `.gitignore`：忽略 `.zhc/`

---

## 五、控制失效修复（HA 能看状态、点不动）

**现象**：HA 里能看到开关状态，点击开关无反应。

**根因**：`zb_gateway_dev_cmd()` 由 UART 接收任务（`rx_task`）直接调用 `esp_zb_zcl_on_off_cmd_req()`。ZBOSS 的 ZCL 请求 API 必须在 **ZBOSS 任务上下文**中执行，从其它 FreeRTOS 任务直接调用会被静默丢弃 —— 不报错、不发帧。

**决定性对照证据**：仓库里已有的、能成功发出 ZCL 命令的函数，全部走调度器：

```c
void zb_gateway_send_switch2wifi_all(void) { esp_zb_scheduler_alarm(send_switch2wifi_cb, 0, 0); }
void zb_gateway_send_set_sleep(uint32_t s) { esp_zb_scheduler_alarm(send_set_sleep_cb, 0, 0); }
```

只有新加的 `dev_cmd` 没走。而 `probe`（读型号）本来就是 `esp_zb_scheduler_alarm(read_basic_cb, 0, 100)` —— 所以**读型号成功、控制失效**，症状完全吻合。

**修复**：

```c
void zb_gateway_dev_cmd(uint16_t addr, uint8_t ep, uint8_t on_off_cmd) {
    ...
    esp_zb_scheduler_alarm(dev_cmd_send, 0, 10);   /* im ZBOSS-Task-Kontext */
}
```

**附带修复**：`_ha_handle_set` 的 payload 映射原先把非 `"1"`/`"0"` 一律当 toggle；现在兼容 `1/0`、`ON/OFF`、`true/false`。

---

## 六、本轮修掉的五个真 bug（都是隐蔽的静默失败）

1. **串口命令 JSON 解析失败**：固件写死 `strstr(buf, "\"addr\":\"")`，而 Python `json.dumps()` 输出 `{"addr": "0x07da"}`（冒号后有空格）→ 永远匹配不到。`dev_cmd` / `probe` / `fw_ota_data` **全部静默失效**，只有 `permit_join` 侥幸能用（`atoi` 会跳过空格）。→ 加容错 `json_val()`。
2. **`dev_cmd` 动作永远是 on**：原设计 `{"cmd":"dev_cmd",...,"cmd":"on"}`，`strstr` 先命中开头的 `dev_cmd`。→ 动作键改名 `sw`。
3. **型号从未被读到**：interview 后只向 `eps[0]` 读 Basic，而 Feibit/Nue 的 Basic 在每个开关端点（ep16/17/18），ep1 根本不存在 → 历史所有日志里 `basic` 事件 **0 命中**（连全新配对的 `pair_test2.log` 也是 0）。→ 改为扫描 ep1 + 全部已知端点。
4. **选了固件文件又变回"未选择任何文件"**：页面头部有 `<meta http-equiv="refresh" content="10">`，**每 10 秒无条件整页重载**，`<input type="file">` 的已选文件被丢弃 —— 用户根本来不及点上传。见六点一。
5. **串口 OTA 发 7168 字节后永久停住**：帧太大（938 字符）被 C6 的 256 字节 `fgets` 缓冲切碎，且 `json.dumps` 的空格让旧 `strstr` 匹配不到。见 7.0 —— 修法是帧缩到 80 B + 发紧凑 JSON。

### 六点一、自动刷新与文件选择的冲突（细节）

**现象**：在 `http://<HA>:8082/` 选好 `.bin` 文件，还没点"上传并升级"，输入框就退回"未选择任何文件"。

**根因**：`_html()` / `_device_html()` 用 `<meta http-equiv="refresh">` 做状态自动刷新，它不看页面状态，到点就整页重载；浏览器重载后 file input 必然清空。

**修复**：删掉两处 `meta refresh`，改为脚本驱动的**条件刷新** `_autoreload_js(secs)`：只要页面上存在"未提交的输入"就暂停刷新，并在页头 `#rl` 位置实时提示原因。

| 暂停条件 | 判定 |
|---|---|
| 已选择文件 | `input[type=file].files.length > 0` |
| 正在输入 | `document.activeElement` 是 INPUT / TEXTAREA / SELECT |
| 内容被改过 | 输入框 `value` 与加载时快照 `data-init` 不一致 |

另外：选中文件后立即把文件名和字节数显示在页头（`已选择 xxx.bin（789760 字节），自动刷新已暂停`），用户能一眼确认选择已被接受；并新增"立即刷新"手动链接。

**注意**：只能用"加载时快照"做基线，**不能用 `defaultValue`** —— Firefox 整页重载后会恢复用户已输入的内容，`value !== defaultValue` 会一直成立，页面将永远不再刷新。

**回归测试**：`test_autoreload.py`（19 项断言，含"两个页面都无 meta refresh"、"JS 大括号配平"、"表单仍完整"），并用 `node --check` 校验生成的 JS 语法。

### 六点三、`run.sh` 带 CRLF → 容器起不来（v1.1.5）

**现象**：Rebuild 后 add-on 启动即死，日志只有 s6-rc 的启动/停止循环，中间夹一行：

```
env: can't execute 'bash
': No such file or directory
```

注意 `bash` 后面那个**换行** —— 那不是排版错误，是 `
`。

**根因**：`run.sh` 被保存成了 Windows 的 CRLF。shell 读 shebang 是逐字节的，`#!/usr/bin/env bash\r\n` 里的解释器名就变成了 `bash\r`，`env` 找不到这个名字，于是报错并换行显示。

**为什么之前能跑**：`run.sh` 原来是 LF，本轮编辑时被写成了 CRLF。`serial_gateway.py` / `config.json` / `device_db.json` 同时中招（Python 和 JSON 解析器容忍 CRLF，所以没暴露）。

**修复（双保险）**：

1. 四个文件全部转回 **LF 且无 BOM**。
2. `Dockerfile` 在 COPY 之后强制归一化，让以后再也踩不到：

```dockerfile
RUN sed -i 's/\r$//' /run.sh /serial_gateway.py /device_db.json \
    && chmod a+x /run.sh \
    && ! head -1 /run.sh | od -c | grep -qF '\r' \
    && echo "run.sh: Shebang ohne CR – OK"
```

最后那行是**硬断言**：万一还有 `\r`，构建直接失败，不会等到启动时才发现。

**回归测试**：`test_packaging.py`（6 组 —— 无 CR / 无 BOM / shebang 精确 / `bash -n` / `config.json` 版本与 `GW_VERSION` 一致 / Dockerfile 的 COPY 目标存在且含 CRLF 守卫）。这个测试已经真的抓到过一次回潮。

> **踩坑记录**：Windows 上 Python 的 `Path.write_text()` 会把 `\n` 自动转成 `\r\n`。
> 改写这些文件时必须用 `write_bytes()` 或 `open(..., newline="\n")`，否则 LF 会被悄悄改回 CRLF。

---

### 六点二、日志时间戳是 UTC，不是本地时间

**现象**：日志写 `[02:33:10]`，墙上时钟是 `10:33`。

**根因**：`print()` 覆盖成 `time.strftime("[%H:%M:%S]")`，它取的是**容器**的 `localtime`。HA add-on 容器默认没有 `TZ`，于是等于 UTC。差 8 小时不是 bug 在算错，是容器根本没配时区。

**修复**（三层兜底，`_resolve_local_tz()`）：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | `LOG_TZ` 环境变量 | add-on 选项 `timezone` 经 `run.sh` 注入 |
| 2 | `TZ` 环境变量 | 但 `UTC`/`GMT`/`ECT/UTC` 会被忽略（那是容器默认值，不是用户选择） |
| 3 | `/etc/timezone` 或 `/etc/localtime` 符号链接 | 常规 Linux 主机 |
| 4 | 系统时间 | 全都没拿到时的兜底，不报错 |

解析成功就用 `datetime.now(ZoneInfo(name))` 格式化，不依赖 `tzset`；`ZoneInfo` 不可用时退回 `tzset()`；再不行用系统时间。启动时打印一行自证：

```
[10:38:32] [Log] Zeitzone Asia/Shanghai · UTC+0800 · jetzt 2026-10-17 10:38:32
```

配套改动：`config.json` 新增 `"timezone": "Asia/Shanghai"` 选项；`run.sh` 导出 `LOG_TZ` 与 `TZ`；`Dockerfile` 的 `apk add` 增加 **`tzdata`**（Alpine 默认不带时区库，缺了 `TZ` 会被 libc 忽略）。`_now()`（写数据库的时间）同步改为本地时间。

**回归测试**：`test_tz.py`（6 组：东八区 +0800、柏林夏令时、`TZ=UTC` 不被误当用户时区、非法时区名不抛异常、格式 `[HH:MM:SS]`、与墙上时钟一致）。本机跑测试需 `pip install tzdata`。

---

## 七、遗留问题清单（2026-10-17 16:05 更新）

| # | 事项 | 状态 |
|---|---|---|
| 1 | OTA 卡在 1280 B | ✅ 已关闭 —— §11 |
| 2 | repcfg / 设备不主动上报 | ✅ 已关闭 —— §11.7、§14 |
| 3 | HA 实体不出现、状态不回、点击弹回 | ✅ 已关闭 —— §10 |
| 4 | 开关能否当中继（是 ZR 还是 ZED） | ✅ 已关闭 —— §12.1（实测 `cap=0x8E` Router） |
| 5 | **开关装到实际位置后复测覆盖（LQI/RSSI）** | ⏳ 待你做，唯一还没在真实条件下测过的指标 |
| 6 | ~~HA 实体命名依赖 short address~~ | ✅ 已关闭 —— §15.1（`ha_nodes.json` 按 IEEE 认回旧名字） |
| 7 | 调光设备（Level 簇）不支持 | ⚠ 未做，只能开/关 |
| 8 | ~~远程命令后无设备确认~~ | ✅ 已关闭 —— §15.2（`read_onoff` 读回真状态） |
| 9 | OTA 传输字节比文件多 44 B | ⚠ 未定位，已加告警（下次 OTA 现形） |
| 10 | 每条 basic 报告都重发一次 discovery | 🟡 噪音，无功能影响 |
| 11 | 开机自动开 180 s 配对窗 | 🟡 安全取舍，可改默认为 0 |
| 12 | 命令识别是白名单 + 静默丢弃 | 🟡 已加 `unknown_cmd` 兜底，结构性问题仍在 |
| 13 | 工作区未提交 git、临时脚本堆积 | 🟡 待整理 |
| 14 | ~~端点 1 的开关回声发错 topic~~ | ✅ 已关闭 —— §15.3（顺手发现，影响所有单端点设备） |
| 15 | ~~实体名重复 `HGZB-043 HGZB-043 上 开关`~~ | ✅ 已关闭 —— §16.3 B（`has_entity_name`） |
| 16 | ~~命令丢失被乐观回声掩盖（显示已关、其实没关）~~ | ✅ 已关闭 —— §16.3 A（确认 + 重试） |
| 17 | ~~设备换地址后 Broker 留整套孤儿 retained~~ | ✅ 已关闭 —— §16.3 C（`_purge_addr` 自动清） |
| 18 | 信号 LQI 15–30（挪出机柜后二联 15 → 45） | ⚠ 部分改善；仍建议继续优化位置 |
| 19 | ~~读回功能静默死亡（固件环形队列卡死）~~ | ✅ 已关闭 —— §17（固定槽位；当时烧 `readslot` 固件，现已并入当前 `txpower` 镜像） |


### 7.0 ~~OTA 卡住 —— 帧/JSON 已修但仍不通，改用 esptool 直刷（v1.1.6）~~

> **已解决，保留作历史记录。** 真正的根因是 `fgets()` 在 USB-Serial-JTAG 上只拿到
> 约 65 字节并当成行尾（不是 `BUF_SIZE`，也不是 JSON 空格），完整定位与修复见**第十一章**。
> 下面这段是当时的（部分正确、部分错误的）分析与绕行方案。

实际日志：

```
[10:45:22] [OTA] coordinator: begin – Part ota_1, size=789804 – übertrage …
[10:45:24] [OTA] coordinator 7168/789804 Bytes gesendet (0 %)
[10:45:47] [OTA] WARNUNG: seit 20 s keine Quittung vom C6 (steht bei 7168/789804).
```

之后**再无任何 OTA 行**。7168 = `FW_CHUNK 448 × FW_BATCH 16`，正好一个整批 —— 第一批发完就再没有下文。C6 一条 `st:"write"` 都没回。

握手时序（`serial_ota.c`）：网关发 16 帧 → C6 每写满 16 块回一条 `st:"write"` → 网关才发下一批。**C6 一块都没写进去**（`s_chunks` 停在 0），所以永远不回 ACK。

两个叠加的原因，都在 C6 侧的解析：

| # | 原因 | 后果 |
|---|---|---|
| 1 | `rx_task` 用 `fgets(buf, BUF_SIZE, stdin)`，旧构建 **`BUF_SIZE=256`**；而一帧约 **938 字符**。`fgets` 读满 255 字符就返回且**不消费行尾**，一帧被切成 4 段碎片：第 1 段以 `"data":"abc…` 结尾被截断，`strchr(pd,'"')` 找不到收尾引号 → **静默丢弃**；其余段没有 `fw_ota_data` 关键字 → 全不匹配 | 帧被碎尸 |
| 2 | `json.dumps()` 默认输出 `"data": "…"`（**冒号后有空格**），而旧固件用 `strstr(buf, "\"data\":\"")` 精确匹配 | 即使帧完整也匹配不到 |

`begin` 却能发出来，是因为 `fw_ota_start` 那行只有 ~40 字符，两个坑都踩不到 —— 症状"begin 正常、数据全丢"完全吻合。

**修复：两边都让一步，不需要碰固件。**

1. **帧缩到 80 B**（`FW_CHUNK`，可用 add-on 选项 `fw_chunk` 调）→ 实测最长 202 字符，稳稳装进 255。
2. **所有发往 C6 的 JSON 改成紧凑格式**（`_j()` / `_js()`，`separators=(",", ":")`）→ `"data":"abc…"` 连旧的 `strstr` 都能命中；新固件的 `json_val` 同样能解析。**两边通吃。**

代价：9,872 帧 / 617 批，按每批 0.55 s 估 **约 6 分钟**。慢，但能过。

**验证（`test_fw_ota.py`，端到端仿真）** —— 用真实的 `_fw_ota_start` / `_fw_ota_send_batch` 打一个精确复刻 `fgets(buf,256)` 语义的假 C6：

| 用例 | 结果 |
|---|---|
| chunk=80 + 旧 strstr 解析（**真实情况**） | 20,480/20,480 字节，**逐字节一致**，0 碎片、0 丢帧 |
| chunk=448 + 旧 strstr 解析（**你遇到的**） | 48 次切碎、16 帧全丢、**写入 0 字节**、永久挂起 |
| chunk=80 + 新 json_val 解析 | 同样完整通过 |
| 实测行长度 | 80 B→202 字符 / 96 B→234 / 448 B→938 |

**这次全绿，而且不需要 esptool。** 之前建议的 esptool 直刷仍然可以留作后备（万一哪个更老的构建连 `fw_ota_data` 分支都没有），但目前不必做。

**操作顺序很重要**：

1. 同步 4 个文件（`serial_gateway.py` / `config.json` / `run.sh` / `Dockerfile`）→ **必须 Rebuild**。
   > ⚠️ 我上一轮说过"纯 Python 不必 Rebuild"，**这句是错的**。`Dockerfile` 里是
   > `COPY serial_gateway.py /serial_gateway.py` —— 文件**烧进镜像**，重启只是重启已有镜像，
   > 不会重新 COPY。所以改了 `.py` 就必须重建，否则跑的还是旧代码。
   > 证据格式：旧版日志是 `Coordinator-Update: … (Frame 448 B × 16 = 7168 B pro Stoß)` 一行，
   > 新版是 `Coordinator-Update: … size=…` + `Plan: 9872 Frames à 80 B …` 两行。
2. **验收新代码有没有真的跑起来** —— 启动日志必须出现这一行（v1.1.4 新增）：

   ```
   [GATEWAY] v1.1.6 · OTA-Frame 80 B × 16 = 1280 B/Stoß · kompaktes JSON
   ```

   没有这行 = 还是旧镜像。网页标题 `ESP32-C6 Zigbee Gateway v1.1.6` 和页脚 `OTA 帧: 80 B` 同样可以确认。
3. **给 C6 断电重上电**（或按复位）。上一次失败留下了 `s_active=true`，不重启的话新请求会立刻回 `error:"busy"`。
4. 重新上传 `firmware/zigbee-vital-sensor-devcmd-fix.bin`（**789,760 字节**那个）。
5. 日志应每 ~0.5 s 出现一行进度，6 分钟左右到 100 %，然后 `Update fertig – Neustart …`。

**如果第 5 步又停在 1280 字节**（本机实测就是这样）：改走 esptool 直刷 ——
网页「应急：esptool 直刷」→ 选同一个 `.bin` → **上传并用 esptool 直刷**。
十几秒完成，串口独占期间桥接自动暂停，结束后自动恢复，日志形如：

```
[FLASH] starte: -m esptool --chip esp32c6 --port /dev/serial/by-id/... --baud 460800 ... write_flash 0x20000 /data/fw_upload.bin
[FLASH] Chip is ESP32-C6
[FLASH] Wrote 789760 bytes ... Hash of data verified.
[FLASH] esptool beendet, rc=0
[FLASH] Serial wieder offen – Brücke läuft weiter
```

刷完 C6 会自动重启，串口会重新打印 boot 日志，`app_init: App version` 会变成新构建。

**顺带发现**：你上传的 bin 是 **789,804 字节**，仓库里的 `firmware/zigbee-vital-sensor-devcmd-fix.bin` 是 **789,760 字节**（差 44）。上传解析器已验证字节精确（789,764 → 789,764，零误差），所以是**你选的文件不是仓库里那个** —— 刷错镜像会在 `esp_ota_end` 校验时失败，请确认。

#### 7.0.1 实测：80 B + 紧凑 JSON 之后**仍然**卡住

v1.1.5 上线后的真实日志：

```
[GATEWAY] v1.1.6 · OTA-Frame 80 B × 16 = 1280 B/Stoß · kompaktes JSON
[OTA] Plan: 9873 Frames à 80 B, 16 pro Stoß → 618 Stöße, geschätzt 6.3 min
[OTA] coordinator: begin – Part ota_1, size=789804 – übertrage …
[OTA] coordinator 1280/789804 Bytes gesendet (0 %)
[OTA] WARNUNG: seit 20 s keine Quittung vom C6 (steht bei 1280/789804).
[OTA] Die Frames sind mit 80 B (~210 Zeichen) bereits klein genug für einen
      256-Byte-Puffer → der laufende Build kann fw_ota_data nicht parsen.
```

**1280 = 16 × 80，又是一整批就停。** 帧长和空格这两个因素都排除了，结论明确：
**这个固件里的 `fw_ota_data` 分支压根不工作**（要么没有，要么解析方式与我推测的都不同）。

从 boot 日志还能确认固件身份：

```
App version: 314b0e4-dirty     Compile time: Oct 16 2026 22:22:54
boot: Loaded app from partition at offset 0x20000      ← 跑的是 ota_0
boot: 3 ota_0  OTA app  00 10 00020000 001c0000
boot: 4 ota_1  OTA app  00 11 001e0000 001c0000
```

也就是：**要刷进去的新固件（含 `BUF_SIZE=1024` 的那版）恰好只能靠这个坏掉的通道传进去** —— 死锁。应用层 OTA 这条路到此为止。

#### 7.0.2 方案：esptool 直刷（v1.1.6 已实现）

绕过应用层，直接让 Bootloader 通过 USB-Serial-JTAG 写 flash。网页上多了一块
**「应急：esptool 直刷」**，上传 `.bin` 或填路径即可，无需拆机、无需拔线。

实现要点：

| 要点 | 做法 |
|---|---|
| 镜像偏移 | **`0x20000`（ota_0）** —— 依据 boot 日志，不是常见的 0x10000 |
| 串口独占 | 先把主循环停下（`_serial_pause` 事件），`ser.close()`，刷完重开并恢复 |
| 命令 | `python -m esptool --chip esp32c6 -p <port> -b 460800 --before default_reset --after hard_reset write_flash 0x20000 <bin>` |
| esptool 版本 | Dockerfile 里锁 `esptool>=4.7,<5` —— 5.x 把命令改名成 `write-flash`，锁住更稳 |
| 并发/回滚 | `_flash_mutex` 保证同时只有一个；退出路径一定清 pause 并重开串口 |
| 可观测 | esptool 每行输出实时进 add-on 日志和网页（保留最近 40 行）；状态 `idle/running/done/error` |

**回归测试**：`test_esptool_flash.py`（19 项，无需硬件）—— 校验命令行（chip/port/reset/offset）、
`flash_apply` 确实关了旧端口再重开、pause 被正确清除、失败路径不死锁、HTTP 路由与 Dockerfile 版本锁。

### 7.1 必须你动手：新设备配对（我做不到）
- 30 分钟配对窗口（05:10–05:40，多轮）内**没有任何新设备入网**，全网仍只有 `0x07da` 一个。
- 你之前按的配对键大概率已超时（Zigbee 配对模式通常 180 s）。
- **操作**：把 C6 插回 HA 机器 → add-on 起来后点"允许加入"（或 `mosquitto_pub -t gw/coordinator/cmd/permit_join -m 180`）→ 在 180 s 内逐个按设备配对键。
- 限制：`permit_join` 受 `uint8_t` 约束，**单次最长 180 s**（SDK `esp_zb_bdb_open_network` 签名如此），设备多就分几轮。

### 7.2 尚未实机验证的部分
| 项目 | 状态 | 原因 |
|---|---|---|
| **esptool 直刷** | **未实机验证** | 逻辑与命令行已用假 Popen 全测过（19 项），但真正的 USB-Serial-JTAG 复位只有插在 HA 上才能验。若 `--before default_reset` 失败，改成手动按住 BOOT 再点直刷（`--before no_reset` 兜底） |
| 从 HA 点开关控制真实灯具 | 未验证 | 需新固件刷进去后才有意义：`dev_cmd` 的跨任务修复在新固件里 |
| HA 里真实出现实体 | 部分验证 | 自动发现报文已用真实串口报文端到端测过；HA 端实体列表待确认 |

### 7.3 已知设计限制
- **协调器重启后，已入网设备要等它"说话"才出现在 HA**：应用层设备表在 RAM 里，只有收到 APS 帧或 Device Announce 才填充。市电开关需按一次键；电池传感器会自己定期上报。
  - 缓解：新增 `probe` 命令，可主动问型号 —— `mosquitto_pub -t gw/coordinator/cmd/probe -m "0x07da,16"`
- SDK 没有 Mgmt_Lqi_Req，**无法主动枚举网络内设备**。
- 设备若重新入网换了短地址，会以新实体出现（IEEE 已知时会用 IEEE 做 node_id 保持稳定）。

---

## 八、部署步骤

> 本节写于 v1.1.x 时期，步骤框架仍然适用，但**版本号与固件文件名是当时的**。
> 当前有效组合见 §一：add-on **v1.2.5**（重建容器生效，重启不够）+ 固件
> **`firmware/zigbee-vital-sensor-txpower.bin`**（792 880 B，ELF `f7e9fbcd2ab4dad0`）。
> 下文提到的 `devcmd-fix.bin` / `readslot.bin` 等旧镜像已从 `firmware/` 清掉，
> 只作为历史记录保留在 §十～§十七 里。

### 8.1 把 add-on 文件同步到 HA
把 `host/addon/esp32c6_gateway/` 下这 5 个文件覆盖到 HA 的本地 add-on 目录（通常在 `/addons/esp32c6_gateway/`，可用 Samba 访问）：

| 文件 | 说明 |
|---|---|
| `serial_gateway.py` | 自动发现 + 命令回路 + 上传解析 + 条件自动刷新 + 本地时区 |
| `device_db.json` | **新增**，3188 型号库（约 677 KiB） |
| `config.json` | 版本 1.1.9，新增 `"map": ["share"]`、`"timezone"`、`"fw_chunk": 80` |
| `run.sh` | 导出 `timezone` / `fw_chunk`；支持 `/share/serial_gateway.py` 覆盖（免 Rebuild） |
| `Dockerfile` | `apk add tzdata`；`pip install esptool>=4.7,<5`；COPY 后 `sed` 归一化 CRLF 并硬断言 shebang |

> **只要动了 `.py` 就必须 Rebuild** —— `Dockerfile` 是 `COPY serial_gateway.py /serial_gateway.py`，
> 文件烧进镜像，重启不会重新 COPY。（这条以前写错了，1.1.4 已更正。）

**不想每次都 Rebuild？** 1.1.4 的 `run.sh` 会优先用 `/share/serial_gateway.py`（如果存在且语法正确）：
把它放进 HA 的 `share` 文件夹（Samba 可见），**重启 add-on 即可生效**，无需重建。
日志会明确说 `[addon] CODE: /share/serial_gateway.py uebernommen (kein Rebuild noetig)`
或 `[addon] CODE: Image-Version (Aenderungen brauchen ein Rebuild)`。
注意：`run.sh` / `Dockerfile` / `config.json` 的改动仍然必须 Rebuild。

### 8.2 重新构建并重启
HA → 设置 → 加载项 → ESP32-C6 Zigbee Gateway → **重新构建 / Rebuild** → 启动。

配置页应多出 **`timezone`** 选项（默认 `Asia/Shanghai`），可直接改成本地时区。日志应出现：

```
[addon] timezone: Asia/Shanghai
[addon] ota frame: 80 B
[addon] CODE: Image-Version (Aenderungen brauchen ein Rebuild)
[10:38:32] [Log] Zeitzone Asia/Shanghai · UTC+0800 · jetzt 2026-10-17 10:38:32
[10:38:32] [GATEWAY] v1.1.9 · OTA-Frame 80 B × 16 = 1280 B/Stoß · kompaktes JSON
[10:38:32] [HA] Gerätedatenbank: 3188 Modelle geladen
```

三行自证，缺一不可：

| 行 | 说明 |
|---|---|
| `timezone` + `[Log] Zeitzone … UTC+0800` | 时区生效，不再是 UTC |
| `[GATEWAY] v1.1.9 … OTA-Frame 80 B` | **新代码真的在跑**（没有这行 = 旧镜像，OTA 一定还卡在 7168） |
| `[HA] Gerätedatenbank: 3188 Modelle` | 型号库进来了 |

`[addon] CODE:` 那行告诉你是用的镜像版本还是 `/share` 覆盖的版本。

### 8.3 升级协调器固件（含控制修复）
打开 `http://<HA-IP>:8082` → **协调器固件升级（串口 OTA）** → **选择文件** `zigbee-vital-sensor-devcmd-fix.bin` → 上传并升级。

> **1.1.1 起页面不再每 10 秒整页重载**，选中的文件会一直保留；页头会显示
> `已选择 zigbee-vital-sensor-devcmd-fix.bin（789760 字节），自动刷新已暂停`。
> 旧版本若出现"选完变回未选择任何文件"，就是这个自动刷新 bug，升级 add-on 即可。
>
> ⚠️ ~~但这一步目前会卡住~~ —— **已修好（见第十一章）**。卡住的原因是固件
> `fgets()` 在 USB-Serial-JTAG 上只拿到约 65 字节并当成行尾，长命令被切碎后静默丢弃。
> 现在 `rx_task` 会把碎片重组成整行，789,984 B 实测 341.6 s 传完、boot 分区成功切换。

> `/data` 是 add-on **容器内部**目录，在 HA 主机文件管理器里看不到
> （主机上对应 `/mnt/data/supervisor/addons/data/esp32c6_gateway/`，只有 SSH/终端能摸到）。
> 所以现在改成**浏览器选文件上传**，不再需要手工放文件。
> 如果仍想按路径升级：`/share` 已挂载进容器，把 bin 放到 HA 的 share 文件夹，路径填 `/share/xxx.bin` 即可。

预期日志（升级过程中）：

```
[OTA] Upload: 789760 Bytes → /data/fw_upload.bin
[OTA] Coordinator-Update: /data/fw_upload.bin size=789760
[OTA] coordinator: Update fertig – Neustart …
```

### 8.4 验收
1. 日志出现 `[HA] discovery switch/c6_0x07da/ep16_on_off → HGZB-043 上 开关`
2. HA 里出现设备 **HGZB-043**，3 个开关实体（上/中/下）
3. 点击开关 → 日志出现 `[→C6] {"cmd": "dev_cmd", "addr": "0x07da", "ep": 16, "sw": "on"}`
4. 灯动作，且状态回传
5. 新设备：`permit_join 180` 后按配对键

---

## 九、验证命令备忘

```bash
# 单元测试
python test_ha_discovery.py     # 11 组自动发现
python test_upload.py           # multipart 二进制 1:1 还原
python test_autoreload.py       # 19 项：页面条件刷新，不得清空 file input
python test_tz.py               # 6 组：日志时间戳必须是本地时区（本机需 pip install tzdata）
python test_fw_ota.py           # OTA 端到端仿真：80 B 帧传完 / 448 B 帧复现卡死
python test_packaging.py        # 打包体检：CRLF/BOM/shebang/版本一致性
python test_esptool_flash.py    # esptool 直刷：命令行/暂停/重开/失败路径

# 重新生成型号库（需先有 .zhc/ 克隆）
python gen_device_db.py

# 主动读取已在网设备的型号
mosquitto_pub -t gw/coordinator/cmd/probe -m "0x07da,16"

# 看自动发现报文
mosquitto_sub -h <broker> -t "homeassistant/#" -v
```


---

## 十、HA 侧根因定位与修复（v1.1.7 + 固件 repcfg 修复）

设备插在本机 COM9 上实测，把「HA 控制不了」拆成了**固件侧 1 个 + add-on 侧 4 个**独立缺陷。

### 10.1 实测证据（COM9，2026-10-17 11:3x）

| 动作 | 串口返回 | 结论 |
|---|---|---|
| `probe 0x07da ep16` | `{"t":"zigbee","addr":2010,"sub":"basic","p":{"mfr":"Feibit Inc co.  ","model":"FB56+ZSW1IKJ2.5"}}` | 设备**可达**、在多端点 Basic 扫描修复后能读到型号 |
| `dev_cmd toggle ep16` | `{"t":"dev_cmd",...,"tsn":1}` + `{"t":"aps","src":"0x07da","sep":16,"dep":1,"cl":"0x0006","sec":0,"len":5,"hex":"18010b0200"}` | 帧真的上了空口；`0b 02 00` = Default Response, cmd `0x02`, **status 0x00 成功** |
| 随后监听 20 s | **没有任何 `sub:"ep16/on_off"` 上报** | 设备**不回状态** ← HA 显示不动的直接原因 |
| 开机 5 s 内就发 `dev_cmd` | 连 Default Response 都没有 | 网络未稳定，命令进不去 → 必须等 ~15 s |
| 三个端点 `on`/`toggle` | ep16/17/18 全部 `status 0x00` | 跨任务调度器修复**实机验证通过** |

> 结论：发送通路已经通了（这是之前"点了没反应"的根因，靠 `esp_zb_scheduler_alarm()` 修好）；
> 但**状态回报通路仍然断着**，所以 HA 上的开关看起来"控制不了"。

### 10.2 add-on 侧 4 个缺陷（`serial_gateway.py`，v1.1.7）

| # | 缺陷 | 后果 | 修复 |
|---|---|---|---|
| H1 | 状态上报用 `mq.publish()` **不带 retain** | 每次 HA 重启，所有实体回到 `unknown`，像"没接上" | 新增 `_ha_state()`，统一 `retain=True, qos=1` |
| H2 | `_ha_prepublish()` 只发 discovery 配置，**从不写 state_topic** | 实体建出来就是一具空壳，首次上报前一直是 `unknown` | 同上，状态走 `_ha_state()` |
| H3 | HA 下发命令后**没有任何状态回声** | 设备不回上报 → 开关在 UI 上点完立刻弹回原位，用户看到的就是"点不动" | `_ha_handle_set()` 写串口后立刻回填乐观状态（`on/off` 按命令，`toggle` 按 `_ha_last` 翻转） |
| H4 | `_ha_node()` 依赖 IEEE 是否已知 | IEEE 晚到会让 node 改名 → 新 unique_id 造出**第二套实体**，旧 retained discovery 配置变孤儿 | per-addr **冻结** node（`_ha_nodes` 缓存） |

### 10.3 固件侧缺陷：Configure Reporting 记录格式违反 ZCL

`main/zb_gateway.c` 的 `iv_config_report()` 对所有属性都塞了 `reportable_change`：

```c
rec.reportable_change = &s_iv_change;      // 旧
```

按 **ZCL 2.5.7.1**，`reportable change` 字段**只属于模拟量**（Uint/Int/Float/Time）；
Bool、Bitmap、Enum 属于**离散类型，必须省略该字段**。多塞 2 字节 → 记录比设备预期长，
设备照样回 SUCCESS（所以一直没报错），但存下来的上报配置是错的，之后**再也不发状态变化**。

修复：按类型分流。

```c
static bool iv_type_is_analog(uint8_t t) {
    if (t >= ESP_ZB_ZCL_ATTR_TYPE_U8   && t <= ESP_ZB_ZCL_ATTR_TYPE_S64) return true;  /* 0x20–0x2f */
    if (t >= ESP_ZB_ZCL_ATTR_TYPE_SEMI && t <= ESP_ZB_ZCL_ATTR_TYPE_DOUBLE) return true; /* 0x38–0x3a */
    if (t >= ESP_ZB_ZCL_ATTR_TYPE_TIME_OF_DAY && t <= ESP_ZB_ZCL_ATTR_TYPE_UTC_TIME) return true;
    return false;      /* Bool/Bitmap/Enum/String/Array/Struct/Set → diskret */
}
...
rec.reportable_change = iv_type_is_analog(cfg->type) ? &s_iv_change : NULL;
```

温度/湿度/光照（模拟量）行为不变；开关（Bool）从此不带该字段。

**当前验证状态（必须读）：** 代码已改、已编译、已刷入 `0x20000`（`Hash of data verified`），
新固件启动正常、`probe` 与 `dev_cmd` 实测仍为 `status 0x00`。
但**新的上报配置还没有真正下发到开关**——它只在设备重新 announce（触发 interview）时才写一次，
而设备里存的仍是旧的、格式错误的配置。所以刷完立刻测，依然没有状态上报（已实测确认）。

激活方式（任选其一，需要你在设备侧动手）：
1. **给开关断电再上电**（拉一下空开/配电箱那一路）→ 设备重新入网 → announce → interview → 新的 Configure Reporting 下发；
2. 或者 `permit_join 180` 后按配对键让设备重新入网。

在此之前，HA 的状态靠 add-on 的**乐观回声**维持，界面不会弹回，但物理按键还不会同步到 HA。

### 10.4 新增测试 `test_ha_state.py`（28 项断言）

把「HA 下发 → 串口 → 状态回填」整条链路钉死：

- 设备上报 → state_topic，`retain=True`
- discovery 里的 `state_topic` / `command_topic` **必须**与收发两端一致（契约检查）
- `1/0/ON/OFF/true/false` 全部映射成 `sw:on|off`，串口上只出现一行
- 命令后必有状态回声：`on`→`{"v":1}`、`off`→`{"v":0}`、`toggle`→按上次值翻转
- node 冻结：IEEE 后到不会产生第二套 discovery
- `basic` 到达后 3 个端点立刻各有一个 switch 实体

### 10.5 部署

```bash
# 1) add-on（改的是 .py，必须 Rebuild 或用 /share 覆盖）
cp host/addon/esp32c6_gateway/serial_gateway.py  /addon/esp32c6_gateway/serial_gateway.py
#    启动横幅应显示 v1.1.9

# 2) 固件（已在本机编译并刷好；换机重刷见 8.3）
python -m esptool --chip esp32c6 --port COM9 --baud 460800 \
  --before default-reset --after hard-reset \
  write-flash 0x20000 firmware/zigbee-vital-sensor.bin
```

### 10.6 验收标准

1. add-on 启动日志有 `[HA] discovery switch/c6_0x07da/ep16_on_off → HGZB-043 上 开关` ×3
2. HA 里出现「HGZB-043 上/中/下 开关」三个实体，属于同一个设备
3. 在 HA 点开关 → add-on 日志出现 `[→C6] {"cmd":"dev_cmd",...,"sw":"on"}`
4. 串口出现 `{"t":"aps",...,"hex":"...0b0100"}`（Default Response status 0x00）
5. **开关状态不再弹回**（这一条靠 add-on v1.1.7 的乐观回声，不依赖设备上报）
6. 给开关断电重上电后，add-on 日志应重新出现
   `[interview] addr=0x07da state=start` → `ep16..18` → `act:config_report`；
   此后按物理按键，HA 里的状态应跟着变（这一条才是 repcfg 修复的最终验收）

> 如果第 6 条仍然不成立，说明该 Feibit 固件根本不做 on-change 上报，
> 下一步应改成「发完 dev_cmd 后主动 Read Attributes 读回 0x0006/0x0000」——
> 这需要在固件里加一个读回 handler，本轮未做。


### 10.7 端到端联调（本地，真进程 + 真设备）—— 12/12 通过

HA 主机不在同一网段（本机 `192.168.100.108`，HA `192.168.178.218`，端口全超时），
所以改在本地把**整条生产链路**搭起来跑：

```
amqtt Broker 127.0.0.1:18830
   ↕
serial_gateway.py 作为独立进程（v1.1.7）
   ↕  COM9
真实 ESP32-C6 + 真实 HGZB-043 开关
   ↕
Fake-HA 客户端（模拟 HA MQTT 集成：读 discovery、发 command_topic、读 state_topic）
```

运行：`PYTHONPATH="$PWD/.mqttenv" python e2e_local.py --port COM9 --addr 0x07da`

| 检查项 | 结果 |
|---|---|
| add-on 进程起来并 `{BASE}/status = online` | OK |
| MQTT 发 `probe` → 设备回 Basic → 发出 `homeassistant/switch/c6_0x07da/ep16_on_off/config` | OK |
| discovery 里 `state_topic` / `command_topic` / `payload_on·off` / `unique_id` | OK |
| 发 `set` `1` → `state_topic = {"v":1}` | OK |
| **retain 验证**：另起一个全新客户端订阅，立刻收到 `{"v":1}` | OK |
| 发 `set` `0` → `{"v":0}` | OK |
| 发 `toggle` → 从 0 翻到 `{"v":1}` | OK |
| **设备真的 ACK 了**：`0b0100`(on) / `0b0000`(off) / `0b0200`(toggle)，全部 status `0x00` | OK |
| 三个端点各拿到一个 switch 实体 | OK |

这组证据的意义：**add-on v1.1.7 不是"单元测试通过"，而是作为一个真实进程、
用真实串口、驱动真实设备，把 HA 会走的每一步都走通了。**
HA 那边唯一还没验证的只有「HA 的 MQTT 集成本身会不会消费这些 discovery 配置」——
这取决于 HA 侧 MQTT 是否启用、discovery prefix 是否为 `homeassistant`（默认一致）。

顺带说明：本机没有 mosquitto，broker 用 `amqtt` 装在项目内隔离目录 `.mqttenv`
（`pip install --target`，不污染任何现有 Python 环境）。`e2e_local.py` 需要它。


---

## 十一、OTA 卡死（1280 字节）根因定位与修复

### 11.1 症状

固件升级**每一次都停在 1280 字节（= 80 B × 16）**，没有任何报错，日志里只有
`coordinator 1280/789804 Bytes gesendet` 然后就没有下文。之前以为是
"帧太长 / JSON 带空格"，把帧缩到 80 B、改成紧凑 JSON，依然停在 1280。
最后只好改用 esptool 直刷绕过——**这个通道一直没修好，也没验证过。**

### 11.2 真正的根因：`fgets()` 只拿到 ~65 字节

C6 的 USB-Serial-JTAG 控制台，底层 `read()` 每次只返回 **约 64–66 字节**，
而 `fgets()` 把这种"短读"当成了行尾。于是**任何超过约 65 字符的命令都被切成碎片**，
每一片都被当成一条独立的（残缺的）命令去解析。

实测阈值（`fw_ota_data` 无会话时应回 `error not_active`）：

| data 字节 | 行长度 | 结果 |
|---|---|---|
| 1 | 41 | 有响应 |
| 12 | 63 | 有响应 |
| 13 | 65 | 有响应 |
| 14 | 67 | **静默** |
| 16 | 71 | 静默 |
| 80 | 199 | 静默 |

为什么之前没发现？因为其它命令"碰巧"都不受影响：

- `dev_cmd`（45 字符）整行都在 65 以内 → 正常；
- `dev_cmd` 加到 199 字符也正常，因为 `addr`/`ep`/`sw` 全在前 65 字符内就已完整；
- `probe` 后面补 150 个填充字符同样正常，同理；
- 但 `fw_ota_data` 的 `data` 值**很长，而且闭合引号在行尾**。
  被截断后 `strchr(pd, '"')` 返回 NULL，`ha_mqtt.c` 里那个
  `if (end) { ... s_cmd_cb(...) }` 直接跳过 → **静默丢弃，连错误都不报**。

这就是"永远停在第 16 帧"的全部原因：ACK 每 16 帧发一次，第 17 帧起全被丢弃，
主机永远等不到下一个 ACK。

### 11.3 修复（`main/ha_mqtt.c`，`rx_task`）

不再假设 `fgets()` 一次就给出整行，而是**按行末标记重组碎片**：

```c
size_t raw = strlen(buf);
int eol = (raw > 0 && (buf[raw-1] == '\n' || buf[raw-1] == '\r'));
...
if (!eol) {                     /* Teilzeile -> anpuffern */
    if (acc_len + n < sizeof(acc)) { memcpy(acc + acc_len, buf, n); acc_len += n; }
    else acc_len = 0;           /* Ueberlauf: verwerfen */
    continue;
}
if (acc_len) {                  /* Zeile endet, war aber gestueckelt */
    ... acc + buf zusammenfuehren, zurueck nach buf ...
}
```

这样无论底层按多少字节切块，解析器看到的永远是一条完整命令行。

### 11.4 验证（同一台设备，COM9）

1. 刷入新固件后，199 字符的 `fw_ota_data` 立刻回 `error not_active`（修复前是静默）。
2. 完整传输 789,984 B：
   `ota_test.py --chunk 80 --batch 16` → 一路 ACK 到 `789760 (99 %)`，
   剩余 224 B 属最后一个不满 16 帧的尾批（见 11.5）。
3. 补发 `fw_ota_end` → 设备重启，
   boot 日志：**`Loaded app from partition at offset 0x1e0000`**
   —— 已从 **ota_1** 启动，说明 `esp_ota_end()` 校验通过、boot 分区切换成功。
4. 重启后 199 字符命令仍正常 → 运行中的正是新镜像。
5. `dev_cmd on/off` 仍然 `status 0x00` → `zb_storage` 未被触碰，**配对数据完好**。

**顺带解决了上一章悬着的那条**：OTA 重启触发了设备重新 announce →
interview 重跑 → 新的（符合 ZCL 的）Configure Reporting 真正下发 →
串口立刻出现

```
{"t":"aps","src":"0x07da","sep":16,...,"hex":"082e0a00002001"}
{"t":"zigbee","addr":2010,"sub":"ep16/on_off","p":{"v":1}}
```

`0A` 就是 **Report Attributes**。也就是说**开关现在会主动上报状态了**，
repcfg 修复（§10.3）到此完成实机验证，HA 不再依赖乐观回声。

### 11.5 三个必须记住的坑

**① 最后一批不会再有 ACK。**
固件每 `FW_ACK_EVERY = 16` 帧才发一次 ACK。若总帧数不能被 16 整除，
最后那个尾批永远等不到 ACK。add-on 的 `_fw_ota_send_batch()` 里已有
`if (_fw_ota_off >= _fw_ota_size): ser.write(fw_ota_end)`，所以它不受影响；
但**外部脚本/手工测试必须自己补发 `fw_ota_end`**，否则会误判成"卡在 99%"。

**② 成功 OTA 之后，esptool 刷 `0x20000` 会失效。**
OTA 成功后 otadata 指向 ota_1，此后写 ota_0 根本不会被启动。
要恢复"从 ota_0 启动"必须连同 otadata 一起写：

```bash
python -m esptool --chip esp32c6 --port COM9 --baud 460800 \
  --before default-reset --after hard-reset \
  write-flash --flash-mode dio --flash-size 4MB --flash-freq 80m \
  0xf000 build/ota_data_initial.bin \
  0x20000 build/zigbee-vital-sensor.bin
```

**③ 复位别用 RTS 脉冲。**
对 C6 的 USB-JTAG 打 RTS 会让 USB 重新枚举，手里的句柄立刻失效
（`ClearCommError failed (PermissionError(13))`）。
需要复位就用 esptool 的 `--after hard-reset`，它会自己重新打开端口。

### 11.6 新增的调试工具

| 文件 | 用途 |
|---|---|
| `ota_test.py` | 完整 OTA 驱动（chunk/batch/delay 可调），带停滞检测 |
| `ota_thresh.py` | 二分测出 `data` 长度阈值（诊断截断问题） |
| `ota_probe2.py` | 一个会话内干净区分"长度问题"和"命令问题" |
| `c6_cli.py` | 收发单条 JSON |
| `dev_test.py` | 一次开端口跑完所有端点，避免反复复位 |
| `live_check.py` | 插回 HA 前的 5 项自检（boot / 设备表 / probe / dev_cmd / 上报） |
| `live_dump.py` | 每行带时间戳、命令前后分段的完整抓包 |
| `rep_final.py` | **决定性测试**：等 interview 完成后再 toggle，验证 0x0A 上报 |

### 11.7 上报闭环的最终验证（含一个必须知道的时序）

`rep_final.py` 的实测序列（同一台 HGZB-043，COM9）：

```
[  61.4] APS sep=16 cl=0x0006 cmd=0x0b 18010b0200      ← toggle 的 Default Response
[  61.4] IV  {"state":"start"}
[  61.8] IV  {"act":"config_report","ep":16,"cluster":"0x0006","attr":"0x0000"}
[  61.8] IV  {"state":"done","eps":[18,17,16]}
[  61.9] IV  {"act":"config_report_resp","ep":16,"status":"0x00","ok":1}
[  76.6] >>> REPORT cl=0x0006 sep=16 083e0a00002001     ← 0x0A Report Attributes
[  76.6] ZB  {"sub":"ep16/on_off","p":{"v":1}}
[  76.6] >>> REPORT cl=0x0006 sep=17 083f0a00002001
[  76.6] ZB  {"sub":"ep17/on_off","p":{"v":1}}
[  76.6] >>> REPORT cl=0x0006 sep=18 08400a00002000
[  76.6] ZB  {"sub":"ep18/on_off","p":{"v":0}}
```

3/3 端点的 Configure Reporting 都回 `SUCCESS`，**14.8 s 后三个端点各来一条 `0x0A`**。
repcfg 修复（§10.3）到此完全闭环：设备会自己报，HA 不再只靠乐观回声。

**★ 时序上的坑（调试时连续踩了三次）：**

> **每打开一次串口，C6 就复位一次**（USB-CDC 的 DTR），复位后 RAM 里的设备表
> 是空的（heartbeat 里 `"dev":[]`）。而 Interview 的触发条件是
> **收到该设备的第一帧 APS**（`aps_data_ind_cb` 里的 `!d->interviewed` 分支）。
>
> 所以"开机 → 干等"是永远不会上报的：没人说话 → 不触发 Interview →
> 不下发 Configure Reporting → 设备自然不会报。实测 340 s 静默，
> `0` 条上报，就是这个原因，**不是功能坏了**。
>
> 正确姿势：开机后先发一条 `dev_cmd`（或按一次物理按键），
> 等 `{"t":"interview","state":"done"}` 出现，再改变状态 → 上报立刻就来。
>
> 这条对 HA 同样成立：**add-on 启动 = C6 复位**，所以插回 HA 后要么从
> HA 点一次开关、要么按一次物理按键，上报通道才会重建。

---

## 十二、中继（Router）与信号覆盖

### 12.1 墙开关会不会当中继

**会。** 零火线（市电供电）的 Zigbee 墙开关在协议里是 **ZR（Zigbee Router）**，
入网后自动参与 mesh 转发，**不需要协调器做任何配置**。本固件
`esp_zb_cfg_t.nwk_cfg.zczr_cfg.max_children = MAX_DEVICES = 32`，
协调器最多直连/承载 32 个子设备。

### ★ 实测确认（2026-10-17 14:27，HGZB-043 / `0x07da`）

```
[→C6] {"cmd":"node_desc","addr":"0x07da"}
[C6] {"t":"aps","src":"0x07da","sep":0,"dep":0,"cl":"0x8002","len":17,
      "hex":"0c00da0701408e000050a0000000a00000"}
[C6] {"t":"node_desc","addr":"0x07da","cap":142,"role":"router",
      "power":"mains","rx_idle":true}
```

`cap = 0x8E = 1000 1110`：

| bit | 值 | 含义 |
|---|---|---|
| 0 | 0 | 非备用 PAN 协调器 |
| **1** | **1** | **FFD / Router —— 能转发他人流量** |
| **2** | **1** | **市电供电** |
| **3** | **1** | **空闲时接收机常开（ZR 必要条件）** |
| 7 | 1 | 分配地址（父节点能力） |

ZDO 帧里还能读出 `manufacturer_code=0x0000`、`max_buf=80`、
`max_incoming/outgoing_transfer=160 B`。

**结论：这台 HGZB-043 是标准 ZR，入网后自动当中继，无需任何配置。**

唯一的例外：市面上确实存在少量**市电供电但仍被实现为 ZED** 的廉价设备
（不休眠，但不路由、也不能当父节点）。判别方法就是上面这个 MAC Capability
Flags 的 **bit1（Device type：1 = FFD/Router）** 与 **bit2（Power source：1 = 市电）**。

> **已实现（固件 `zigbee-vital-sensor-node-desc.bin` / add-on v1.1.8+）：**
> 串口命令 `{"cmd":"node_desc","addr":"0x07da"}`，或 Web UI 设备详情页 →
> “命令”下拉框 → **node_desc（查角色：Router / End Device）**。
> 返回 `{"t":"node_desc","addr":"0x07da","cap":N,"role":"router|end_device",
> "power":"mains|battery","rx_idle":true|false}`；
> 设备详情页会多一行 **“角色”**，直接写“Router (ZR) – leitet Fremdverkehr weiter”。
>
> 代价：要烧一次新固件（OTA 现在可用，约 2 分钟）+ 重装 add-on。
> 没烧之前只能说“同类产品基本都是 ZR”，不能说实测。

### 12.2 但"能不能中继"和"中继有没有用"是两件事

中继的前提是**它自己一跳就能稳定连上协调器**。如果开关装在楼梯间、
C6 在机柜里，链路本身就很勉强，那它提供的是一条"弱链路中继"：
入网能用，但把它当骨干转发只会放大丢包和延迟。

### 12.3 这个场景里最该先动的是机柜，不是加中继

金属机柜对 2.4 GHz 相当于法拉第笼，实测衰减通常 **20–30 dB**，
C6 又是板载 PCB 天线（约 0–2 dBi），在柜内相当于把通信距离从
20–30 m 压到 3–5 m。**用一根 USB 延长线把 C6 甩到柜外 20–50 cm
（或贴到柜门内侧、开孔处），收益远大于加一个中继。**

参考损耗：砖墙 一堵 ≈ 10–15 dB，混凝土承重墙 ≈ 15–25 dB，金属柜 ≈ 20–30 dB。

### 12.4 怎么量化（插回 HA 后就能看）

| 位置 | 看什么 |
|---|---|
| add-on 日志 | `[HB] uptime=… ch=20 pan=0x… lqi=[0x07da=212]` |
| Web UI `:8082` 设备表 | 每台设备一列 LQI 进度条 + RSSI dBm |

判据：**LQI ≥ 150 良好 / 100–150 可用 / < 100 已经不稳**，需要挪位置或换通道。

### 12.5 通道

当前 `ch=20`（2450 MHz）。若楼道里的 WiFi 用 6 信道（2437 MHz），两者几乎完全重叠。
替代：**15（2425）或 25（2475）**。

> ⚠️ `set_channel` 会清掉 Zigbee FAT-Storage、**重建网络 —— 所有设备必须重新配对**。
> 要换就趁现在只有一台设备的时候换。

### 12.6 真要扩覆盖

加**专用 ZR**（USB 供电的中继插座/中继器）放在"机柜外 + 楼梯间中段"，
而不是指望一台墙开关恰好落在覆盖边缘。一个 ZR 的常规覆盖是
10–15 m / 穿一堵砖墙。

### 12.7 确认「它到底是不是 Router」——两步

**① 烧新固件**（含 `node_desc` 命令，790,992 B）
Web UI `http://<HA-IP>:8082` → 协调器固件升级 → 选 `firmware/zigbee-vital-sensor-node-desc.bin`
→ 上传升级（约 2 分钟，OTA 通道已修好）。设备会重启、重新 interview，**配对数据不丢**。

**② 重装 add-on 到 v1.1.8 以上（当前 1.1.9）**（`.py` 改过 → Rebuild；或把 `serial_gateway.py` 放 `/share` 重启）

**③ 查**：Web UI → 点进设备 `0x07da` → “命令”下拉框选
**node_desc（查角色：Router / End Device）** → 发送。几秒后表格里多出一行：

| 角色 | 显示 |
|---|---|
| Router | `Router (ZR) – leitet Fremdverkehr weiter`，并附 `cap / mains|battery / rx_idle` |
| End Device | `End Device (ZED) – leitet NICHT weiter` |

日志同时打印 `[ND] 0x07da: role=router power=mains rx_idle=true cap=15`。

---

## 十三、静默失败 #3：命令在解析器里没接线（`node_desc` 首测无响应）

### 13.1 症状

HA 端发 `node_desc`，日志有 `[→C6] {"cmd":"node_desc","addr":"0x07da"}`，
但 C6 **一条输出都没有**（既没有 `{"t":"node_desc"}`，也没有 error）。
连发两次（14:08:34、14:10:38），两次都是死寂。

### 13.2 根因

`ha_mqtt.c` 的 `rx_task` 不是通用 JSON 解析，而是一串 `strstr()` 分支：

```c
} else if (strstr(buf, "fw_ota_end")) { ...
} else if (strstr(buf, "dev_cmd"))  { ...
} else if (strstr(buf, "probe"))    { ...
```

**每加一条串口命令，必须在 `ha_mqtt.c` 里单独接一根线**，`main.c` 的
`on_uart_cmd` 只是下游。这次只在 `main.c` 加了分支，`ha_mqtt.c` 没加 →
命令在进入 `main.c` 之前就被丢掉了，**没有任何提示**。

这是同一类错误在本项目里的第三次：
| # | 现象 | 根因 |
|---|---|---|
| 1 | OTA 永远停在 1280 B | `fgets()` 短读 → 闭合引号丢失 → `if (end)` 整块跳过 |
| 2 | 早期长帧被丢弃 | `BUF_SIZE` 太小 |
| 3 | `node_desc` 无响应 | 解析链里缺分支 |

### 13.3 修复

1. 补上 `node_desc` 分支（`{"cmd":"node_desc","addr":"0x07da"}` → payload `"ADDR"`）。
2. **加兜底**：任何含 `"cmd"` 但没被任何分支接住的行，回一条

```json
{"t":"error","why":"unknown_cmd","cmd":"node_desc"}
```

以后再漏接线，会立刻在日志里喊出来，而不是人间蒸发。

> 教训：**这个固件的命令识别是"白名单 + 静默丢弃"**。
> 新增任何命令，`main.c` 和 `ha_mqtt.c` 两处都要改；只改一处 = 静默失败。

### 13.4 验证（同一台设备，14:27:49）

补上分支后 OTA 到 `zigbee-vital-sensor-node-desc2.bin`（791,360 B，
ELF `7041d628b…`），再发同一条命令：

```
[→C6] {"cmd":"node_desc","addr":"0x07da"}
[C6] {"t":"node_desc","addr":"0x07da","cap":142,"role":"router","power":"mains","rx_idle":true}
[ND] 0x07da: role=router power=mains rx_idle=True cap=142
```

从"零输出"到"2 秒内出结果"，只差一根线。`node_desc` 结果见 §12.1。

---

## 十四、物理按键上报：实测通过

14:09:48 / 14:09:50 的日志（两次都是人手按的中间键）：

```
{"t":"aps","src":"0x07da","sep":17,"dep":11,"cl":"0x0006","hex":"080c0a00002000"}
{"t":"zigbee","sub":"ep17/on_off","p":{"v":0}}
{"t":"aps","src":"0x07da","sep":17,"dep":11,"cl":"0x0006","hex":"080d0a00002001"}
{"t":"zigbee","sub":"ep17/on_off","p":{"v":1}}
```

两条间隔 2 s、值 0→1 → **本地按键会主动上报**，HA 能看到手动操作。

至此行为矩阵完整：

| 触发方式 | 设备行为 | HA 侧 |
|---|---|---|
| 远程 `dev_cmd` | 只回 Default Response，**不发 Report** | 靠 add-on 乐观回声立即更新 |
| 物理按键 | 发 `0x0A` Report Attributes | 真实上报，MQTT retained |
| 断电重启后 | 设备表清空，需一次交互触发 Interview | 期间不上报（见 §11.7） |


---

## 十五、两项收尾修复（add-on v1.2.0 + 固件 readback-q）

### 15.1 实体命名不再依赖 short address

**问题**：node id 直接由 `c6_` + 当前 short address 拼成（`c6_0x07da`）。设备 rejoin 换了
地址（断电、移位置、重新入网都会），就会得到 `c6_0x08a1` —— 同一台设备在 HA 里变成两套
实体，旧的 retained discovery 配置留在 broker 里成了孤儿。

**改法**：新增 `ha_nodes.json`（存在 add-on 的 `/data`，重启不丢），三张表：

| 表 | 内容 | 作用 |
|---|---|---|
| `addr_nodes` | `{addr: node}` | 冻结已发出的名字，**绝不迁移** |
| `ieee_nodes` | `{ieee: node}` | 设备换地址后靠 IEEE 找回旧名字 |
| `addr_ieee` | `{addr: ieee}` | 学习到的对应关系 |

`_ha_node()` 三级回退：地址已知 → 用旧名；地址未知但 IEEE 已知且已登记 → **复用旧 node id**
（日志打印 `[HA] 0x08a1 hat neue Kurzadresse → bestehende Entities c6_0x07da übernommen`）；
都没有 → 新建。

**一个关键取舍**：新建时仍用**地址**命名，不改用 IEEE。否则升级 add-on 的瞬间，所有
`c6_0x07da` 都会变成 `c6_<ieee>`，等于亲手造出它要消灭的那种重复实体。IEEE 表只负责
"认回旧名字"，不负责起名字。

`_ha_publish` 的去重键同时从 `(node, obj_id)` 改成 `(node, obj_id, addr)`：地址变了，
`state_topic` / `command_topic` 必须重新 publish。

**验证**（`test_nodes.py`，从真实源码切片后 exec，10/10 通过）：

```
1) Join 0x07da, IEEE gelernt, dann Node-ID bilden      PASS  c6_0x07da（与旧版一致）
2) Zweiter Aufruf: Name bleibt eingefroren              PASS
3) Datei wurde geschrieben                              PASS  addr_nodes/ieee_nodes/addr_ieee 各就位
4) Geraet kommt mit NEUER Adresse 0x08a1 zurueck        PASS  复用 c6_0x07da
5) Ein ZWEITES Geraet darf nicht kollidieren            PASS
6) Neustart simulieren                                  PASS
7) Neustart + andere Adresse                            PASS
```

### 15.2 远程命令后读回真状态（不再只靠乐观回声）

**问题**：HGZB-043 收到远程 On/Off 只回 Default Response（`0x0B`），**不发 Report Attributes**。
add-on 只能在发命令后自己"假设成功"并 publish 一个状态，设备若拒绝命令、或状态本来就不同步，
HA 会一直显示错值。

**改法**：命令发出 0.7 s 后，add-on 追一条 `read_onoff`，固件用 Read Attributes 读回
cluster `0x0006` / attr `0x0000`，用真实值覆盖乐观回声。

固件侧新增（`main/zb_gateway.c`）：

```c
void zb_gateway_read_onoff(uint16_t addr, uint8_t ep);   /* → esp_zb_zcl_read_attr_cmd_req */
```

响应在 `zb_action_handler` 的 `ESP_ZB_CORE_CMD_READ_ATTR_RESP_CB_ID` 里分流：cluster 是
`0x0006` 且属性 id 是 `0x0000` → publish `{"v":0|1}`；其余（Basic 的厂商/型号）走原有分支。

**为什么要队列而不是一个全局变量**：HA 场景一次关十台设备时，十条 `read_onoff` 会在几毫秒
内到达。单槽 `s_rd_addr` 会让后一条覆盖前一条 —— 读回发到错的设备上。改成 16 槽 SPSC
环（`s_rd_head` 只由 UART 任务写、`s_rd_tail` 只由 Zigbee 任务写，`uint8_t` 原子，无锁），
每 60 ms 抽一条，避免响应挤成一堆。

**验证**（`test_readback.py`，切真实函数 + 假串口，19/19 通过）：

```
[→C6] {"cmd":"dev_cmd","addr":"0x07da","ep":16,"sw":"off"}
[→C6] {"cmd":"read_onoff","addr":"0x07da","ep":16}      Abstand: 0.71 s
dev_cmd 52 Zeichen / read_onoff 44 Zeichen（限 65）
```

环队列边界单独验过：10 条并发送入 → 收 7 条（留一格判满）、顺序不变、24 次绕回正确。

### 15.3 顺手修掉的第 3 个 bug：端点 1 的回声发错 topic

固件发布时对**端点 1 不加前缀**（`ha_mqtt_publish_zigbee_ep()` 里 `endpoint > 1` 才拼
`ep%u/`），所以 ep1 设备的状态 topic 是 `{BASE}/zigbee/{addr}/on_off`。但 `_ha_handle_set`
的回声硬编码 `ep{n}/on_off`，对 ep1 设备写到了 `.../ep1/on_off` —— 和实体订阅的
`state_topic` 不是同一个 topic，**单端点设备点了照样弹回**；连带 `_ha_last` 的键也对不上，
`toggle` 永远当作"未知 → 开"。

HGZB-043 用的是 ep 16/17/18，所以这个问题一直没暴露。已统一为与固件相同的规则：

```python
sub = f"ep{ep}/on_off" if ep > 1 else "on_off"
```

### 15.4 部署

| 项 | 版本 / 文件 | 指纹 |
|---|---|---|
| add-on | v1.2.0 | `serial_gateway.py` 改 4 处（`_nodes_load`/`_nodes_save`/`_ha_learn_ieee`/`_ha_node`/`_ha_publish` key/`_ha_handle_set` 的 sub） |
| 固件 | `firmware/zigbee-vital-sensor-readback-q.bin` | 792,096 B，sha256 `acf7a19e66fc9e90`，ELF `cf4e7904a1c0d0d9` |

烧写后判定新固件生效：日志里 `ELF file SHA256: cf4e7904a1c0d0d9…`（**不要用字节数或
Compile time 判断**，增量编译不刷新 Compile time）。

验收：

```bash
# 1) 命名：设备换地址后应出现这行，且 HA 里不新增实体
[HA] 0x____ hat neue Kurzadresse → bestehende Entities c6_0x____ übernommen (gleiche IEEE)

# 2) 读回：每次远程操作后 0.7 s 应看到
[→C6] {"cmd":"read_onoff","addr":"0x07da","ep":16}
[C6] {"t":"zigbee","addr":..., "sub":"ep16/on_off","p":{"v":0}}

# 3) 持久化文件
cat /data/ha_nodes.json     # add-on 容器内
```


---

## 十六、线上诊断（2026-10-17 16:40，直连 HA + Broker）

用户报告「MQTT 里面不太对，反馈和控制」。这次没有靠猜：直连 HA REST/WebSocket 和
MQTT Broker，拿到了真实现场数据。

### 16.1 现场拓扑

| 设备 | IEEE | 短地址 | 端点 | LQI / RSSI |
|---|---|---|---|---|
| HGZB-043（三联） | `00:12:4b:00:07:6b:f1:70` | `0x07da` | 16/17/18 | **30 / −74 dBm** |
| FTB56+ZSN18ZY1.1（二联情景） | `00:12:4b:00:07:65:d4:df` | `0x747d` | 1/2 | **15 / −77 dBm** |

装到机柜旁时是 LQI 153 / RSSI −50。**装进墙里之后掉到 15–30，已经到临界值。**
这是本次所有"控制不太对"的物理根因。

### 16.2 查到的四个问题

**① 同一台设备在 Broker 上有两套 retained 数据（孤儿）**

```
gw/coordinator/zigbee/0xb2a2/{join,basic,on_off,ep1/on_off,ep2/on_off}   ← 旧地址
gw/coordinator/zigbee/0x747d/{join,basic,on_off,ep1/on_off,ep2/on_off}   ← 新地址
```

两个地址的 IEEE 完全相同（`00:12:4b:00:07:65:d4:df`）。设备 rejoin 换了短地址，
旧地址那一整套没人清理也没人订阅 —— 纯脏数据。已手动清空 5 条。

**② 端点 1 同时存在 `on_off` 和 `ep1/on_off`，且值相反**

```
0x747d/on_off       {"v": 0}   ← 实体真正订阅的 state_topic
0x747d/ep1/on_off   {"v": 1}   ← 1.1.8 版 _ha_handle_set 硬编码写下的残留
```

这正是 §15.3 那个 bug 的历史遗留。1.2.0 改了写入端，但**已经写进 broker 的旧值不会自己消失**。
已清空。

**③ 实体名重复**：`HGZB-043 HGZB-043 上 开关`

discovery 的 `name` 里已经写了设备名，HA 因为 `has_entity_name` 又拼了一次。

**④ 命令会丢，而乐观回声把它掩盖了**

16:31 的实测时间线（Broker 侧抓包 + HA 侧 0.1 s 采样）：

| 时刻 | topic | 值 | 说明 |
|---|---|---|---|
| 18.19 | ep17/on_off | 1 | 命令 turn_on → 回声 |
| 23.78 | ep17/on_off | 0 | 命令 turn_off → 回声 |
| **24.56** | ep17/on_off | **1** | **0.78 s 后读回：设备仍是开着的** |

也就是说：**HA 显示已关，灯其实没关。** 而 0.7 s 的读回把真相写了回来 —— 但紧接着
又会被下一次乐观回声盖住，用户看到的就是"点了有时灵有时不灵"。

同一次测试里 ep16、ep18、二联两个端点的读回请求**完全没有响应**；
再单独复测 ep17 两次 turn_off，也**一次读回都没回来**。结论：**读回请求本身也在丢**。

### 16.3 修复

**A. 命令确认 + 自动重试**（`_ha_handle_set` 重写）

```
发 dev_cmd → 0.7 s 读回 → 1.5 s 再读一次（防第一次丢）
                        → 2.6 s 还没任何回音，判定命令丢失 → 重发
读回值与目标不符 → 立刻重发
最多 3 次；3 次仍不生效 → 放弃，并把 HA 显示改回设备真实值
```

日志会写明发生了什么：

```
[HA] 0x07da ep17: Gerät bestätigt v=0                       ← 设备确认，正常
[HA] 0x07da ep17: Gerät meldet v=1, gewollt 0 → Befehl wird wiederholt (2/3)
[HA] 0x07da ep17: keine Bestätigung → Befehl wiederholt (2/3)
[HA] 0x07da ep17: nach 3 Versuchen nicht durchgesetzt (Gerät meldet v=1)
```

**B. `has_entity_name`**：discovery 的 `name` 只写端点+角色（`"上 开关"`），
由 HA 自己拼设备名 → 显示变成 `HGZB-043 上 开关`。

同时引入 `_DISC_VER`：**改了 discovery 内容必须 bump 版本**，否则 `_ha_sent` 会按
`(node, obj_id, addr)` 判定"已发过"而跳过，HA 永远拿不到新配置。本轮 `_DISC_VER = 2`。

**C. 地址迁移时自动清理**（`_purge_addr`）

`ha_nodes.json` 新增 `ieee_addr`（IEEE→当前地址）和 `addr_subs`（地址→发布过的 subs）。
`_ha_learn_ieee()` 发现同一 IEEE 换了地址时：

1. 先把旧 Node-ID 写进 `ieee_nodes`（名字不丢）
2. 把旧地址的状态 topic 和 discovery config 全部 `retain=""` 清空
3. 再让新地址复用旧名字

这样 ①② 那种孤儿不用再手工清。

### 16.4 验证

| 测试 | 覆盖 | 结果 |
|---|---|---|
| `test_retry.py` | 确认即时清除、反向值重发、3 次放弃并纠正显示、无响应超时重发、EP1 无前缀、双击覆盖 | **19/19** |
| `test_readback.py` | 0.7 s 读回时序、回声 topic、行长度 | **19/19** |
| `test_nodes.py` | IEEE 认回旧名、换地址复用、重启稳定 | **10/10** |

现场已清理：`0xb2a2` 5 条 + `0x747d/ep1/on_off` 1 条，retained topic 从 15 条降到 9 条。
原始数据留在 `mq_trace.txt`。

### 16.5 但根因在信号

软件重试只能把"丢了重发"补上，**补不了链路本身**。LQI 15 意味着相当比例的单播帧
收不到 —— 重试 3 次仍可能全丢。这个必须物理解决：

| 做法 | 说明 |
|---|---|
| **优先：把协调器从金属机柜里挪出来** | 机柜是法拉第笼，挪 30 cm 出柜门通常比加中继更有效 |
| 二联开关（LQI 15）先确认位置 | 15 已经接近掉线，若它和协调器之间隔承重墙/金属箱，几乎必然丢命令 |
| 加一个市电 Zigbee 路由器（插座/灯） | 放在两者中间的开放位置 |
| 换信道 | 当前 ch20（2450 MHz），WiFi 也挤在 1/6/11，可试 25（2475 MHz） |

装好之后看 `gw/coordinator/heartbeat`：

```json
{"t":"heartbeat","ch":20,"pan":"0x83e8","rssi":-77,
 "dev":[{"addr":"0x07da","lqi":30,"rssi":-74},{"addr":"0x747d","lqi":15,"rssi":-77}]}
```

`lqi` 稳定 > 100 才谈得上可靠；< 50 就会开始丢命令。

### 16.6 部署

- add-on **v1.2.1**（`serial_gateway.py` + `config.json`）
- 固件不用动：`zigbee-vital-sensor-readback-q.bin` 已在跑（读回功能确认工作）

装完第一次操作时注意日志：如果出现 `nicht durchgesetzt`，说明设备确实收不到命令，
那就是信号问题，不是软件问题。


---

## 十七、读回功能静默死亡：固件环形队列卡死（2026-10-17 17:10）

### 17.1 症状

把协调器从机柜挪出来后，17:00 那一轮读回**100% 成功**（每条命令 0.7 s + 1.5 s 两次确认）：

```
11.17 ep16 → 1   (回声)
11.97 ep16 → 1   (0.7 s 读回)
12.82 ep16 → 1   (1.5 s 二次确认)
```

两分钟后同样操作，读回**彻底归零**：

```
[ 8.90] 0x07da/ep16/on_off {"v": 0}   ← 只有回声
[13.94] 0x07da/ep16/on_off {"v": 1}   ← 只有回声
[18.99] 0x07da/ep16/on_off {"v": 0}   ← 只有回声
（订阅 gw/coordinator/zigbee/# 全量抓包，5 次操作 0 条读回）
```

而 C6 的 uptime 是连续的（545 → 605），**没有重启**。设备通信正常（回声之后状态确实跟着变）。
排除了两个猜测：

| 猜测 | 验证 | 结论 |
|---|---|---|
| 信号变差 | 同期报文正常收发、命令被执行 | 否 |
| `toggle` 命令特有 | 换回 `turn_on`/`turn_off` 同样无读回 | 否 |

### 17.2 根因：`s_rd_head` / `s_rd_tail` 失步后永久卡死

§15.2 里我用了 16 槽 SPSC 环形队列，理由是"场景一次关十台设备时命令会挤在一起"。
思路没错，**实现错了**：

```c
if (s_rd_tail != s_rd_head) {
    addr = s_rd_q[s_rd_tail].addr;        /* 判断非空 */
    ...
    s_rd_tail = (uint8_t)((s_rd_tail + 1) % RD_Q_LEN);   /* 写回 */
    more = (s_rd_tail != s_rd_head);
}
if (more) esp_zb_scheduler_alarm(read_onoff_send, 0, 60);  /* 靠回调自己续命 */
```

两个致命点：

1. **续命依赖回调自身**。队列里还有剩余项时，靠回调末尾再 arm 一次。只要这一次
   `esp_zb_scheduler_alarm()` 没生效，就再没有任何定时器来消费剩余项。
2. **两个游标分属两个任务却没有保护**。`s_rd_head` 由 UART 任务写、`s_rd_tail` 由
   Zigbee 任务写，一旦两个回调交错（或上面那次 arm 失败），两个游标就会永久错开。

结果是：**队列永远"非空"，但每次新请求 arm 的回调都去消费队首那批陈旧积压**，
新请求排在后面永远轮不到。表现出来就是读回请求发给了错的（或已失效的）地址 ——
设备当然不回，MQTT 上一条报文都不会有。

这个故障**没有任何日志**，从外面看就是"命令发出去了，设备也动了，但确认没了"。

### 17.3 修复：固定槽位，取消共享游标

```c
#define RD_SLOTS 8
static struct { uint16_t addr; uint8_t ep; volatile bool busy; } s_rd_slots[RD_SLOTS];

void zb_gateway_read_onoff(uint16_t addr, uint8_t ep) {
    for (uint8_t i = 0; i < RD_SLOTS; i++) {
        if (s_rd_slots[i].busy) continue;
        s_rd_slots[i].addr = addr;  s_rd_slots[i].ep = ep;  s_rd_slots[i].busy = true;
        esp_zb_scheduler_alarm(read_onoff_send, i, (uint32_t)(10 + i * 45));
        return;
    }
    ESP_LOGW(TAG, "read_onoff: alle %d Slots belegt – Anfrage verworfen", RD_SLOTS);
}

static void read_onoff_send(uint8_t slot) {
    uint16_t addr = s_rd_slots[slot].addr;   /* nur dieser Slot */
    uint8_t  ep   = s_rd_slots[slot].ep;
    s_rd_slots[slot].busy = false;           /* sofort freigeben */
    ... esp_zb_zcl_read_attr_cmd_req(...)
}
```

- **没有共享游标** → 不存在"失步"这种状态，也就不可能卡死
- 槽号作为 `param` 传进 alarm，每个回调只碰自己的槽
- 槽位错开 45 ms，避免响应挤成一堆
- 某个 alarm 丢了 → **只丢一次确认**，下一次照常工作
- 全满时打一条 `ESP_LOGW`，不再静默

### 17.4 固件

| 文件 | 大小 | sha256 | ELF |
|---|---|---|---|
| `firmware/zigbee-vital-sensor-readslot.bin` | 792,144 B | `07f0746d49ff7f54` | `38e13b24e219b2c8` |

烧写后判定生效：日志里 `ELF file SHA256: 38e13b24e219b2c8…`。

**临时缓解**：现在读回是死的，HA 显示的全是乐观回声。重启 add-on 会重新打开串口，
C6 随之复位、队列清空，读回会短暂恢复 —— 但旧固件还会再卡，所以那只是应急。

### 17.5 对 §16.5 结论的修正

§16.5 / §7 第 18 条把"控制不太对"归因于信号，**这个结论只对了一半**：

- **信号确实差**（LQI 30 → 挪位后二联 15 → 45），命令会丢，这部分成立
- **但读回失效不是信号造成的**，是上面这个固件 bug。它把"其实执行成功了"也报成了
  "没有确认"，反过来又触发 §16.3 的重试逻辑重复发命令

两件事都要修：固件修队列（本节），物理上继续改善信号（§16.5）。

### 17.6 教训

**凡是"回调里再 arm 下一次"的设计，都必须假设那次 arm 会失败。**
本项目的第三次静默失败（前两次见 §13），共同点是：**失败路径不产生任何日志**。
所以这次除了改结构，还在队列满时加了 `ESP_LOGW` —— 宁可日志吵，不要静默死。

---

## 十八、情景开关 vs 开关：怎么自动判别（add-on v1.2.2）

### 18.1 结论先行

**FTB56+ZSN18ZY1.1 没有执行机构，它是一块按键面板，不是开关。**

这和"远程控制没反应"完全对得上：它**收** On/Off 命令（协议要求回执），但**没有**
可切换的负载，所以按下去什么都不会发生；同时它**没有 OnOff 属性**，所以也永远
无法回读状态。把它暴露成 HA 的 `switch`，本身就是错的。

### 18.2 现场证据（2026-10-17 17:39，同一台协调器、同一时刻）

对两台设备各发一次 `on`，抓 `gw/coordinator/console` 的 ZCL 原帧：

| | 三联 `0x07da`（HGZB-043） | 二联 `0x747d`（FTB56+ZSN18ZY1.1） |
|---|---|---|
| 命令回执 | `18 6b 0b 01 00` → cmd `0x0b` Default-Response，命令 `0x01`(On)，**status `0x00` SUCCESS** | `18 4f 0b 01 00` → 完全一样，**status `0x00` SUCCESS** |
| 0.7 s 后读回 | `18 6c 01 00 00 00 20 01`<br>= Read-Attr-Resp，attr `0x0000`，**status `0x00`**，type `0x20`(uint8)，value `0x01` | `18 50 01 00 00 86`<br>= Read-Attr-Resp，attr `0x0000`，**status `0x86` UNSUPPORTED_ATTRIBUTE** |

解码方式：`fc | tsn | cmd | attrid(2) | status | [type | value]`。

- 三联那一帧后面还有 `00 20 01`（status 成功 → 有 type + value）
- 二联那一帧到 `86` 就结束了 —— **status 0x86 = UNSUPPORTED_ATTRIBUTE**，
  失败记录里不带 type/value，所以整帧只有 6 字节

**这一条就是判据**：能读回 OnOff 属性的，才有实体状态；读不回的，没有。

### 18.3 为什么不能靠设备库（`device_db.json`，3188 条）

用户的直觉是"库里应该标好了"。查过了，**不行**：

| 看起来能用的线索 | 实测反例 | 结论 |
|---|---|---|
| `caps` 含 `action`（31 条） | `MEAZON_BIZY_PLUG`（带计量插座）、`DinRail 1-phase meter`、`Power switch S1`、`LK FUGA wiser wireless double relay` —— 全都**有**继电器 | 会把插座判成面板 |
| `endpoints` 为空 | 219 个 `caps == ["switch"]` 的机型端点为空的，只是 Z2M 没给端点起名（HGZB-02S 自己也空） | 不能判 |
| `description` 含 "scene wall switch" | 描述字符串，可信度高但不是结构化字段 | 只能当提示 |
| 型号 `FTB56+ZSN18ZY1.1` | **3188 条里没有这个型号** | 库覆盖不到 |

所以库只作提示（`_ha_db_hint()`，日志里会写 "Hinweis … → Tastenpanel vermutet"），
**撤不撤开关由实测证据决定**。

#### 18.3.1 库字段的实测覆盖度（2026-10-17 18:00）

| 指标 | 实测值 |
|---|---|
| 字段总数 | **7**：`caps` / `components` / `description` / `endpoints` / `model` / `src` / `vendor`（**没有 `type`**） |
| `description` 非空 | **3188 / 3188（100%）**，但共 **2004 种**不同写法 —— 自由文本，只能关键词 |
| `components` 含 `switch` | **618** 个机型 |
| 其中 `description` 含 "scene" | **只有 7 个**（全部是 Nue/3A 的 scene wall switch + Eva scene selector） |
| `caps` 含 `action` 且 `components` 含 `switch` | 31 个，其中混着带继电器的插座/电表 |
| 本机这个型号 `FTB56+ZSN18ZY1.1` | **不在库里**（3188 条无此键） |

结论：`description` 能判的设备**只占需要判的那一小撮的 1%**，且恰好是 Z2M 已收录的老机型。
新机型、OEM 变种、同族不同配置永远会冒出来 —— 所以判据的主语必须是**设备自己说的话**
（报文），库只能做加速和交叉验证。

#### 18.3.2 但库还能这样用：同族继承（已实现）

库里的**字形前缀**可以救回没有收录的变种。实测这个例子很典型：

```
FTB56+SKT1BCW1.0    caps=[switch]          "Power plug"                  ← 有继电器
FTB56+ZSN15HG1.0    caps=[switch,action]   "Smart 1 key scene wall switch"
FTB56+ZSN16HG1.0    caps=[switch,action]   "Smart 2 key scene wall switch"
FTB56+ZSN18ZY1.1    ← 我们的设备，库里没有
```

- 前缀取到 `FTB56+`（6 字）会把**插座**和**面板**混在一起 → 必须拒绝
- 前缀取到 `FTB56+ZSN1`（11 字）→ 命中 2/2 全是 scene wall switch → **继承为 panel**

实现（`_ha_db_family()`）：从最长前缀往短试，**最短 9 字符**，且要求族内
**全部**指向 panel 才给 panel 级提示，混合则退回"无提示"。用真实库跑出来的结果：

| 型号 | 结论 | 依据 |
|---|---|---|
| `FTB56+ZSN18ZY1.1`（库里没有） | **panel** | 族 `FTB56+ZSN1*` → 两个 Szenen-Panel |
| `FTB56+SKT1BCW1.0` | switch | 精确命中 "Power plug"（**没被族规则带偏**） |
| `FB56+ZSW1IKJ2.5`（三联） | switch | 精确命中 "Smart light switch - 3 gang" |
| `FTB56+ZSN16HG1.0` | panel | 精确命中 "Smart 2 key scene wall switch" |

它只影响两件事：**日志提示**和**描述符缺失时是否仍去探测**。判定权仍在报文。

### 18.4 报文里真正可用的判别信息

Zigbee 设备在 ZDO 层会自报三样东西，这三样比任何厂家描述都可靠：

| 来源 | 内容 | 用途 |
|---|---|---|
| Node Descriptor (0x0002) | 设备类型（协调器/路由/终端）+ MAC 能力 | 判断是否路由、是否常供电 |
| **Simple Descriptor (0x0004)** | profile、**device_id**、**Input 簇（Server）**、**Output 簇（Client）** | **主判据** |
| Active Endpoints (0x0005) | 端点列表 | 枚举 |
| ZCL Read Attributes (0x00) | 能否读出 OnOff | 复核 + 自愈 |

**主判据是 0x0006 的方向**：

- 在 **Input/Server** 列表 → 设备持有 OnOff 状态 → 真开关
- 在 **Output/Client** 列表 → 设备只会**发** On/Off → 按键面板

第二判据是 **device_id**（HA profile 里 `…Switch` 那一类：0x0005/0x0008/0x0009/0x000A），
仅在簇列表给不出答案时使用。

### 18.5 自动判别流程（全自动，无人工介入）

```
设备入网
  ├─ interview 发来 Simple-Descriptor（每个端点一条）
  │    └─ _classify_from_desc()
  │         ├─ 0x0006 在 in  → 真开关（若曾被误判，撤销）
  │         └─ 0x0006/0x0005 只在 out，或 device_id 属开关类 → 标记「发送方」
  │
  ├─ interview done → _probe_start()
  │    └─ 只对「带 On/Off 簇」的端点各发一次 Read-Attributes
  │         ├─ 有值回来（_pending_ack → _probe_seen）→ 真开关
  │         └─ 4 s 无回答 → _noread_add()  → 标记「状态不可回读」
  │
  └─ 用户点一下（兜底，无需等入网）
       └─ 3 次都没确认 → _noread_add()  （同一套判定，只是来得晚一点）

判定结果 → _classify()
  ├─ 只是发送方 + 能回读   → switch 和 event 都留（如带继电器的双控面板）
  └─ 只是发送方 + 不能回读 → 撤掉 switch，保留 event（本设备的情形）
```

**判定是持久化的**（`/data/ha_nodes.json` 的 `noread` / `ctrl` 两个数组），
重启不重算；地址换了会跟着搬（`_ha_learn_ieee` 里做键迁移），不会被重新当成新设备。
**判定也是可自愈的**：任何时候只要读回一个真实值，`_noread_del()` 就撤销
"不可回读"，`_classify_from_desc()` 见到 Server 簇就撤销"发送方"。

### 18.6 行为变化

| 场景 | 之前（v1.2.1） | 现在（v1.2.2） |
|---|---|---|
| 点面板的开关 | 1 条命令 + 2 次读回 + 超时再补 2 条 = **3 条命令、6 次读回**，最后报"未生效" | **1 条命令 + 1 次读回**（那一次是自愈探测） |
| 面板的 HA 实体 | 2 个 `switch`（点了没反应） | 2 个 `event`（按下墙面按键 → HA 收到 on/off/toggle/scene） |
| 三联开关 | 正常 | 正常，不受影响（能读回 → 永不进 `noread`） |
| 温度传感器 | — | 不参与 OnOff 探测（描述符里没有 0x0006） |
| 兜底 | — | 想恢复被撤的开关：把 `"addr/ep"` 从 `ha_nodes.json` 的 `ctrl` 删掉，重启 add-on |

### 18.7 顺带修掉的一个真 bug

新测试暴露出：超时重发只链到第 2 次就停了 —— `_retry_if_silent` 只挂在
`attempt == 1` 上，第 2 次及其后续不再挂，于是"3 次重试"实际只有 2 次，
而且 `_pending` 永远留着。已改为每次尝试都挂（它自己会判断是否太早）。

改动文件：
- `host/addon/esp32c6_gateway/serial_gateway.py`（78.7 KB → 106 KB）
- `host/addon/esp32c6_gateway/config.json`（1.2.1 → 1.2.2）
- `test_panel.py`（新增，47 项：按键解码 / 判定 / 切换 / noread / 库提示 / 同族继承）
- `test_nodes.py` / `test_retry.py`（补新全局的桩）

---

## 十九、向 Zigbee2MQTT 学 MQTT（add-on v1.2.3）

用户提示："整个项目对于参考意义更大一些，特别是 MQTT 处理上面。"于是把
`Koenkk/zigbee2mqtt` 拉下来（`344b6e0`，2026-10-02）逐文件读了一遍。

先澄清一个事实，避免下次找错地方：

> **`zigbee-herdsman-converters` v26.120.0 里一行 MQTT 代码都没有。**
> `package.json` 的依赖只有 `iconv-lite` / `semver` / `zigbee-herdsman`，
> `src/` 下只有 `devices/`、`index.ts`、`indexer.ts`。所有 MQTT 逻辑都在
> **`Koenkk/zigbee2mqtt` 主仓库**的 `lib/` 下。

本项目的设备库取自前者，MQTT 层该学的是后者。

### 19.1 Z2M 的 MQTT 结构（读源码所得）

| 文件 | 行数 | 职责 |
|---|---|---|
| `lib/mqtt.ts` | 265 | 连接、LWT、birth、retained 登记、自发布过滤、topic 校验 |
| `lib/extension/homeassistant.ts` | 2502 | Discovery 生成、去重、清理、可用性注入 |
| `lib/extension/availability.ts` | 359 | 设备在线状态（主动 ping / 被动超时 / 退避） |
| `lib/controller.ts` | ~490 | `publishEntityState`：状态合并、cache、retain/qos 策略 |
| `lib/state.ts` | 133 | 状态缓存与落盘（`cache_state`） |
| `lib/extension/publish.ts` / `receive.ts` | 314 / 191 | 命令解析、乐观更新、防抖/节流 |

### 19.2 六条值得抄的做法

**① 内容比较代替版本号（最重要）**

```ts
// lib/extension/homeassistant.ts:2032
const discoveredMessage = discovered.messages[topic];
if (!discoveredMessage || discoveredMessage.payload !== payloadStr || !discoveredMessage.published)
```

Z2M 拿**成品 JSON 字符串**和上一次比。本项目原来是
`_ha_sent` 存 `(node, obj_id, addr, _DISC_VER)` 四元组，靠手工 bump
`_DISC_VER` 强制重发。这是个**静默失败源**：改了 config 内容忘了 bump，
新配置永远不发，HA 一直用旧的，日志里没有任何异常。

已改成 `_ha_sent[topic] = payload_json`，`_DISC_VER` 停用（保留常量作文档）。

**② 启动时的 retained 回读窗口**

```ts
// lib/extension/homeassistant.ts:583-608
const discoverWait = 5;
await this.discover(this.bridge, false);   // 只登记，不发布
await this.mqtt.subscribe(`${this.discoveryTopic}/#`);
setTimeout(async () => { await this.mqtt.unsubscribe(...); /* 只发缺的 */ }, 5000);
```

Z2M 先订 5 s 自己的 discovery 主题，把 broker 里已有的 retained config
读回来，然后只发"缺的或不一致的"。

本项目原来每次重启都无条件重发全部 config，HA 每次都要重新处理一遍所有实体。
已照做：`_disc_begin_window()` 开窗 → `_disc_flush()` 比较后只发差异。

**因为本项目的 discovery 是"来一条报文建一个实体"（事件驱动），不像 Z2M 能一次
算全量，所以还加了一个 `_disc_defer` 队列**：窗口期间的请求先排队，窗口关闭后
统一比对——否则这 5 s 内到达的设备会彻底丢失（它们的报文不会再来一次）。

**③ 状态清理**

```ts
// lib/extension/homeassistant.ts:2055-2060
for (const topic of lastDiscoveredTopics) {
  if (!newDiscoveredTopics.has(topic) && !isDeviceAutomation)
    await this.mqtt.publish(topic, "", {clientOptions: {retain: true, qos: 1}, ...});
}
```

发**空 retained 载荷** = 删除 broker 上的 retained 消息。本项目之前只在换地址
（`_purge_addr`）时清理，设备彻底离开网络后 config 会永远留着。已补
`_disc_purge_orphans()`。

**关键安全设计**：判断"这条 config 是不是我的"不能用节点名前缀（`c6_…`），
否则**同一个 broker 上的第二个网关实例**会把第一个的实体全删掉。Z2M 的做法是
看 config 自己的可用性 topic：

```ts
// lib/extension/homeassistant.ts:2077
if (!isDeviceAutomation && !message.availability?.[0].topic.startsWith(baseTopic)) return;
```

已照抄：解析 config 的 `availability[0].topic`，必须等于 `{BASE}/status`；
同时兼容旧格式 `availability_topic`（1.2.2 及以前生成的 config）。

再加一条保守规则：**`_addr_nodes` 和 `_ieee_nodes` 都为空时（`ha_nodes.json`
丢失 / 全新安装）整体跳过清理**——此时"未知设备"和"还没重新出现的设备"分不开，
误删一次用户就丢整套实体。

**④ retained 登记 + 重连后重发**

```ts
// lib/mqtt.ts:180-186
this.republishRetainedTimer = setTimeout(async () => {
  for (const msg of Object.values(this.retainedMessages))
    await this.publish(msg.topic, msg.payload, msg.options);
}, 2000);
```

理由写在注释里：`https://github.com/Koenkk/zigbee2mqtt/issues/9629` —— 有些
broker 不持久化 retained，重启后全丢。已加 `_retained` 登记表 + `_republish_retained()`，
MQTT 连上 2 s 后重放。

**⑤ 可用性用列表不用单字段**

```ts
// lib/extension/homeassistant.ts:1866-1888
payload.availability = [{topic: `${baseTopic}/bridge/state`}];
payload.availability_mode = "all";
payload.availability.push({topic: `${baseTopic}/availability`});
// 设备被禁用时：value_template = '{{ "offline" }}'（强制不可用）
```

本项目原来用老式 `availability_topic` 单字段。已改为列表 + `availability_mode: latest`
（每个条目自带 `payload_available` / `payload_not_available`，所以仍能用纯文本
`online`/`offline`，不必改成 JSON）。

`availability_mode: "all"` 需要**两个**可用性来源（网关 + 设备）。现在只有网关一个，
用 `"all"` 会让实体永远不可用，所以本项目用 `"latest"`（任一条 online 即在线）。
这是**有意偏离 Z2M**，等以后加了设备级可用性再改。

**⑥ HA 重启后重发状态**

```ts
// lib/extension/homeassistant.ts:2114-2124
} else if (data.topic === this.statusTopic && data.message.toLowerCase() === "online") {
  await this.mqtt.publish("bridge/state", stringify({state: "online"}), ...);
  for (const entity of ...) if (this.state.exists(entity))
    await this.publishEntityState(entity, this.state.get(entity), "publishCached");
}
```

HA 启动完成会往 `homeassistant/status` 发 `online`。Z2M 收到后重发桥状态 + 所有
缓存状态。本项目原来订阅里没有这个 topic，HA 重启且 broker 无持久化时实体全是
`unknown`。已订阅并实现 `_ha_republish_all()`。

### 19.3 另外两个小但值钱的守卫

**Wildcard 校验**（`lib/mqtt.ts:214`）：

```ts
if (topic.includes("+") || topic.includes("#")) { logger.error(...); return; }
```

publish 的 topic 里出现 `+`/`#`，broker 会当协议错误直接断连。本项目**已经踩过**：
`mqtt_probe.py` 用 `c6_#` 抓包，broker 立即断开（`#` 必须是最后一级且前面是 `/`）。
`mq_put()` 现在直接拦截，顺带防止某个设备名/型号里带 `#` 把网关连接打掉。

**自发消息过滤**（`lib/mqtt.ts:196`）：

```ts
if (!this.publishedTopics.has(topic)) this.eventBus.emitMQTTMessage(...);
```

Z2M 订阅 `base_topic/#`，会收到自己发的消息，必须过滤。本项目订阅的是
`{BASE}/cmd/+` 和 `{BASE}/set/#`，与自己发布的话题不重叠——但这是巧合，
不是设计。已加 `_self_pub`（1 s 内自发布过的 topic 不收），防止以后扩订阅时踩坑。

### 19.4 本项目没抄的两块（附理由）

**设备级可用性（`lib/extension/availability.ts`）**

Z2M 把设备分成 active（市电/路由）和 passive（电池），active 的用 PingReq 探活，
带 jitter 和指数退避，ping 队列串行执行（每次间隔 2 s），避免把 Zigbee 网络打爆。

值得抄，但**前置条件本项目还没有**：C6 固件没有"主动 Ping 设备"的命令。
自然替代是周期性 Read Attributes —— 而 `_probe` / `_read_onoff` 已经具备这个能力
（能区分"设备回 0x86"和"设备不回"）。留给下一轮，先把 `availability_mode` 从
`latest` 改成 `all` 才有意义。

**状态合并与 `cache_state`**

Z2M 发布的是**合并后的全量状态**（`{...旧状态, ...本次更新}`），因为 HA 的
`value_json.x` 模板要在同一个 JSON 里取值。本项目是**每属性一个 topic**
（`ep16/on_off`、`ep1/temperature` 各自独立），天然不需要合并。
`_ha_last` 只用于 toggle 方向判断和 HA 重启后重发。

### 19.5 改动与验证

代码：
- `host/addon/esp32c6_gateway/serial_gateway.py`（108 490 → 117 794 B，2741 行）
  - 新增 `mq_put()` / `_republish_retained()` / `_ha_republish_all()` / `_ha_avail()`
  - 新增 `_disc_send()` / `_disc_begin_window()` / `_disc_flush()` / `_disc_purge_orphans()`
  - `_ha_sent`：`set(tuple)` → `dict[topic, payload]`
  - 所有 `mq.publish(...)` 收拢到 `mq_put(...)`（原 20 处 → 1 处）
  - `on_message` 增加：HA 状态话题、启动窗口、自发消息过滤
- `host/addon/esp32c6_gateway/config.json`（1.2.2 → 1.2.3）
- `test_mqtt.py`（新增，35 项）

测试（全部通过，rc=0）：

| 文件 | 结果 |
|---|---|
| `test_nodes.py` | 10/10 |
| `test_readback.py` | 19/19 |
| `test_retry.py` | 19/19 |
| `test_panel.py` | 47/47 |
| `test_mqtt.py` | **35/35（新）** |
| `test_ha_discovery.py` | 全部通过 |
| `test_ha_state.py` | 全部通过 |

`test_ha_discovery.py` 与 `test_ha_state.py` 此前**早已失效**（从未加载命令模块，
断言还停留在 `has_entity_name` 改造之前，`_issue_cmd` / `_pending_ack` 直接
NameError）。这次一并修好：补上命令块的 exec、补 `threading`/`time`/`_noread`
等桩、把 4 处过期断言改成当前真实值。

### 19.6 设备库重新生成（v26.120.0）

上一轮的库有 **810 个脏键**（`','`、`'1'`、`'1 Mini'`…）。根因是 EcoSmart 用
**二进制串**当 `zigbeeModel`：

```ts
zigbeeModel: [
    '\u0000\u0002\u0000\u0004\u0003"…',   // 字符串里有一个字面量 "
```

正则 `['"]([^'"]+)['"]` 从 `'` 匹配到那个 `"` 就断了。已把 ID 提取换成真正的
**TS 字符串扫描器**（`ts_string` / `ts_string_array` / `_ts_unescape`），
`model` / `vendor` / `description` 也走同一个扫描器（`description: "16\" 灯"`
这类转义同样会切错）。

重新生成结果：

```
Dateien        : 400
Definitionen   : 4803
Einträge       : 4882  (Schlüssel inkl. Aliase: 5326)
ohne ModelID   : 1062
Binär-IDs weg  : 110  (Steuerzeichen im ModelID)
Ausgabe        : device_db.json  (1030 KiB)
```

- 脏键：**810 → 0**（唯一剩下的 `'1'` 是 Shelly 1 Mini Gen 4 的真实 modelID）
- 110 个二进制 modelID 被丢弃、`ohne ModelID` 从 1017 升到 1062：这些设备出厂
  Basic 0x0005 是二进制，按 UTF-8 本来就匹配不上，留着只会污染 `_ha_db`
- 关键条目核对无误：`FTB56+ZSN15HG1.0` / `FTB56+ZSN16HG1.0` → `panel` 族；
  `FB56+ZSW1IKJ2.5` → `{top:16, center:17, bottom:18}`；`FTB56+SKT1BCW1.0` 仍是开关

### 19.7 需要用户在 HA 里做的

1. **重新构建** add-on 到 **1.2.3**（`Dockerfile` 是 `COPY serial_gateway.py` +
   `COPY device_db.json`，仅重启不生效）。
2. 首次启动会看到一行 `[HA] Discovery-Abgleich: N neu/geändert, M unverändert`。
   `M` 应该接近实体总数——说明大部分 config 没被重发。如果全是 `N`，说明 broker
   上的 retained 已经丢了（正常，会自愈）。
3. 之后**改 config 内容不用再动 `_DISC_VER`**，改完重建 add-on 就会生效。

---

## 二十、情景面板按键读不出来：真因是「上报形态」不是「报文丢了」（add-on v1.2.4）

### 20.1 现场数据（2026-10-17 18:53，用户从网关面板截取）

用户指出 `/api/state` 里"看起来是报的 1"。实测：

```
0x747d  clusters:
  ep2/on_off   {"v": 1}   18:53:40
  on_off       {"v": 1}   18:53:42      ← ep1
  basic        {"mfr":"Feibit CO.,LTD  ","model":"FTB56+ZSN18ZY1.1"}  18:52:18
  lqi 61 / rssi -68
```

两条相隔 2 秒、分别落在 ep2 和 ep1。**这不是丢包：面板确实在上报，
是 add-on 没把它当按键。**

### 20.2 三个叠在一起的错

**① 上报形态判断错了（主因）**

上游转换器写的是：

```ts
// src/devices/nue_3a.ts:60
zigbeeModel: ["FTB56+ZSN15HG1.0"], model: "HGZB-1S",
fromZigbee: [fz.command_recall],
```

`fz.command_recall` 处理 **genScenes / commandRecall**：

```ts
export const command_recall: Fz.Converter<"genScenes", undefined, "commandRecall"> = {
    cluster: "genScenes",
    type: "commandRecall",
    convert: (model, msg, publish, options, meta) => {
        const payload = {action: postfixWithEndpointName(`recall_${msg.data.sceneid}`, msg, model, meta)};
        ...
```

但**这个具体型号（FTB56+ZSN18ZY1.1）实际发的不是 Scene recall**，
而是 **genOnOff 的属性上报**（Report Attributes）。

而 `_ha_aps_action` 为了不把"自己命令的回声"当按键，有两道全丢的闸门：

```python
if fc & 0x08:  return          # Direction = Server→Client
if (fc & 0x03) != 0x01: return # 必须是 cluster 专有命令
```

Report Attributes 恰好**两条都命中**：帧类型 0（通用）、方向 Server→Client。
于是每一次按键都在第一行被静默 return，连日志都没有。

**② 场景 ID 被压成常量 `"scene"`**

即使面板发的是 recall，旧代码也把它写成固定的 `"scene"`，
两片按键在 HA 里完全一样 —— 分不出按了哪个。上游是 `recall_<sceneid>`。

**③ 制造商专有帧头偏移错**

ZCL 帧头在 Frame-Control bit 2（0x04）置位时会多 2 字节：

```
fc(1) | mfr(2, LSB first) | tsn(1) | cmd(1) | payload…
```

旧代码恒取 `cmd = b[2]`。一旦设备发了 herstellerspezifisch 的帧，
`b[2]` 是厂家码的低字节，永远不等于 0x00/0x01/0x02/0x05 —— 同样静默丢弃。
（本次现场帧不是这种，但同族设备很可能有，属于必须先修掉的隐患。）

### 20.3 修法

**新增 `_ha_button_from_report()`**：在 `dispatch` 的 `t=="zigbee"` 分支里，
把已经解析好的 `sub`/`p` 直接转成按键事件，绕开裸帧解析：

```python
_erwartet = ... (addr, ep) in _pending      # 必须先于 _pending_ack 判断
_pending_ack(addr, sub, p)
_ha_button_from_report(addr, sub, p, _erwartet)
```

- 只对 `on_off` 类 sub 生效
- 只对**已被判定为发送端**的端点生效（`_ctrl`），真开关的 Report 仍只是状态变化
- `was_expected=True`（值来自我们自己的 Read）→ 不产生按键，避免自激
- 兜底：设备已有一个端点在 `_ctrl` 且模型族**一致判定**为 `panel` 时，
  第二片按键即使没单独进 `_ctrl` 也认（严格到 2/2 一致，防止把真继电器当按键）

**顺手修的两处**：

- `_zcl_head()`：正确解析制造商专有帧头，返回 `(fc, mfr, tsn, cmd, payload_start)`
- Scenes 改成 `recall_<sid>` / `store_<sid>`，并把 `event_types` 预声明
  `on/off/toggle/scene + recall_0..31 + store_0..31`

  ★ 这一条是硬性的：HA 会**丢弃**不在 `event_types` 里的值（日志
  "Invalid event type"）。所以允许值必须提前全列出来，不能等收到再补。

**诊断**：来自发送端端点、但仍无法归类的报文，现在会打一行（同样内容只打一次）：

```
[HA] 0x747d ep2: unerkannte Tasten-Nachricht cl=0x0006 type=0 mfr=- hex=...
```

下次再有型号不按预期上报，看一行日志就能定位，不用再猜。

### 20.4 验证

`test_panel.py` 47 → **55 项**，新增：制造商专有帧、`recall_2`、
report→按键（v=1→`on` / v=0→`off`）、自己 Read 的回声不产生按键、
真开关的 Report 不产生按键、第二片按键的族兜底。

全量回归（rc=0）：`10/10`、`19/19`、`19/19`、**`55/55`**、`35/35`，另两套全过。

改动：
- `host/addon/esp32c6_gateway/serial_gateway.py`（117 794 → 123 494 B）
- `host/addon/esp32c6_gateway/config.json`（1.2.3 → **1.2.4**）
- `test_panel.py`（47 → 55 项）

### 20.5 已知局限（说清楚，不掩盖）

- 如果面板**只在值变化时上报**（attribute reporting 的常规行为），
  同一片按键连按两次、中间没有回到 0，设备不会发第二次报告，
  也就不会有第二个事件。这是设备侧行为，网关无法凭空造出来。
- 若想彻底拿到每一次按下，正确做法是**关掉该端点的 on/off 上报配置**，
  改为监听命令帧；但本面板实测发的是 Report，不是命令，关掉就什么都没有。
  因此保持现状，并以 `lqi`/`last_seen` 作为在线判据。

## 二十一、发射功率：陶瓷天线 + 20 dBm 该不该降（add-on v1.2.5 + 固件 txpower）

### 21.1 问题与现状

板载陶瓷天线，固件里写死 `esp_zb_set_tx_power(20)`（`main/zb_gateway.c:1000`，
在 `esp_zb_init()` 之后、端点构建之前）。用户提出：会不会功率过大反而自干扰，
像 ESP32-C3 mini 那样，不如降一点试试。

先把事实摆清：**代码里没有任何 LQI 阈值**。LQI 只被原样保存
（`zb_gateway.c:1201`）和画成 0–255 的进度条（`serial_gateway.py`）。
"≥150 良好 / 100–150 可用 / <100 不稳" 只是 §12 里写的**文档判据**，
不是代码行为。这一点上一轮已经确认过。

### 21.2 先纠正我自己的一个错判：LQI/RSSI 是上行，不是双向

上一轮我给你的说法是"降功率会让所有设备的 LQI/RSSI 一起变差"。
**这句是错的**，方向搞反了。

`aps_data_ind_cb`（`zb_gateway.c:1196`）里的两个值：

```c
int8_t rssi = esp_ieee802154_get_recent_rssi();   /* 收到的这一帧有多强 */
d->lqi  = (uint8_t)ind.lqi;                       /* 这一帧的相关性质量 */
```

两个都是**在 C6 侧、对收到的帧**测出来的，也就是 **设备 → 网关（上行）**。
`esp_zb_set_tx_power()` 改的是 **网关 → 设备（下行）**。
两者是两条独立的路径，改下行功率**不会**让上行的 LQI/RSSI 变化。

所以：

| 想改的东西 | 改网关发射功率有效吗 |
|---|---|
| 面板按键/传感器上报能不能收到（**上行**） | **无效**。这是链路条件和设备侧天线决定的 |
| 网关开关命令能不能送到设备（**下行**） | 有效。这是唯一能靠功率改善的方向 |

这条区别直接决定了测量方法：**不能拿 LQI 前后对比来评判功率档**，
必须看下行命令的成功率（见 §21.5）。

### 21.3 现有实测数据算得出什么

现场数据（2026-10-17，用户从 `/api/state` 与网页面板读出）：

| 设备 | 位置 | LQI | RSSI | 备注 |
|---|---|---|---|---|
| 0x747d 情景面板 | 装墙内 | 61 | −68 dBm | 距离网关 1–2 m |
| 0x07da HGZB-043 | 装墙内 | 15–30 | −74…−77 dBm | 同一面墙 |
| 0x07da | 挪到墙外 | 153 | −50 dBm | 只换了位置 |

自由空间损耗（f = 2475 MHz）：

```
FSPL(1 m) = 20·log10(1) + 20·log10(2.475e9) − 147.55 = 40.3 dB
FSPL(2 m) = 46.3 dB
```

面板的发射功率未知（这类模块常见 0…+8 dBm，取 0 与 +3 两档都算一遍）：

```
总损耗 = P_tx(设备) − RSSI
  P_tx = 0 dBm → 0 − (−68) = 68 dB
  P_tx = +3 dBm → 3 − (−68) = 71 dB

扣掉自由空间 40.3 dB → 还有 28…31 dB 无法用距离解释
```

28–31 dB 的额外损耗，只能来自：**面板自身的小天线效率、墙内金属暗盒、
极化失配、人体吸收**。这些**全部在上行路径上**，网关再怎么调功率都碰不到它们。

对照 0x07da 只挪出墙外就从 LQI 15–30 / −77 变成 153 / −50
（差 27 dB），这个量级正好和上面算出的"无法解释"部分吻合 ——
**主导因素是位置和遮挡，不是发射功率。**

结论：现场处于**上行链路本身偏弱**的状态，而这是网关功率管不着的部分。

### 21.4 用户的怀疑不是空穴来风，但在这个现场不成立

"功率太大会自干扰"在物理上确有依据，不是外行话：

- 陶瓷天线的实际带宽窄、效率低（常见 −2…−6 dBi 有效增益），
  配上 +20 dBm 的 PA，驻波比升高 → 反射功率回来 → PA 工作点被推向压缩区。
- PA 压缩的后果是 **EVM 变差**：信号更强，但星座点更糊，
  误帧率反而上升。这就是"C3 mini 那种问题"的机理。
- 近距离还会让对端接收机前端饱和（AGC 拉不下来），表现为"离得越近越不灵"。

但在这个现场，这几条都缺少证据：

1. RSSI −68 dBm 距离"+20 dBm 直射"差 28–31 dB，说明**信号根本没到饱和量级**；
   真正的过驱现场会看到 RSSI 掉到 −20…−35 dBm 这种"太强了"的读数。
2. C6 的 `esp_zb_set_tx_power()` 档位是芯片厂标定过的，
   datasheet 允许 20 dBm 输出到 50 Ω 负载。板上真正容易出问题的地方是
   **天线匹配网络（π 网络元件值/焊接）和天线净空区**，不是功率档位本身。
3. 如果是 PA 压缩，症状会随距离单调恶化（越远越差、越近也差），
   而现状是**只跟墙有关**：挪出墙就好 27 dB。这不像压缩，像遮挡。

所以：**降功率大概率会让情况更糟（直接削减下行链路预算），
但"大概率"不能代替测量。** 因此本轮的工作不是替你选一个值，
而是把这件事做成可以量出来的实验。

### 21.5 把它做成可测：三处改动

**(1) 固件：功率可在运行时改，并且回读实际值**

```c
void zb_gateway_set_tx_power(int8_t dbm) {
    esp_zb_set_tx_power(dbm);
    int8_t eff = esp_ieee802154_get_txpower();
    /* {"t":"txpower","want":N,"eff":M} */
}
```

`want` 是我们要的，`eff` 是 PA 真正在跑的。只有 `eff` 算数 ——
`esp_zb_set_tx_power()` 会把超范围的值悄悄夹到 PHY 能给的区间，
不看回读就会拿"愿望"当"实测"。

**(2) 固件：LQI 快照改为按需取**

原来 LQI/RSSI 只在 60 s 心跳里出现（`HEARTBEAT_INTERVAL_MS`），
做 5 档对比就要等 15 分钟，而且拿到的是"最后一帧"的值、可能已经过期。
新增 `{"cmd":"lqi"}` → `zb_gateway_report_lqi()`，立刻回一份带 `ms` 时间戳的快照。

**(3) add-on：三个入口 + 一条上行**

| 入口 | 用法 |
|---|---|
| 网页 `:8082` | 「发射功率」按钮组 20 / 14 / 8 / 2 / 0，加「立即取一次 LQI/RSSI」 |
| MQTT | `mosquitto_pub -t gw/coordinator/cmd/set_tx_power -m 8` |
| 上行 | `{BASE}/tx_power`（retained，含 want/eff）、`{BASE}/lqi`（retained） |

数值范围钳在 −24…+20 dBm（与 IDF `esp_ieee802154` 允许区间一致），
非数字直接拒绝并打日志，不做静默兜底。

### 21.6 顺带修掉的一个真 bug：网页按钮的命令被自己吃掉了

`/api/permit_join`（网页上的"开启 180 秒"）是通过
`mq_put(f"{BASE}/cmd/permit_join")` 走 MQTT 回环下发的。
但 `mq_put()` 会把该 topic 记进 `_self_pub`，而 `on_message` 开头就是：

```python
if _self_pub.get(msg.topic, 0) > time.time():
    return          # ← 自己发的命令被当成回声丢掉
```

`{BASE}/cmd/+` 又正好是我们自己订阅的。所以**网页上的"开启 180 秒"一直发不出去**，
日志里什么都看不到，面板也不会进入配对模式。原先的注释写着
"我们订阅的 topic 不是我们自己发布的"—— 这行注释被它下面三行的代码直接推翻了。

修法：把 `{BASE}/cmd/*` 从回声保护里显式豁免（`{BASE}/set/#` 不能豁免，
那是 HA 的命令入口，必须靠 echo 保护区分乐观回声）。

```python
if not msg.topic.startswith(f"{BASE}/cmd/") and _self_pub.get(msg.topic, 0) > time.time():
    return
```

### 21.7 测量脚本：`txpower_ab.py`

纯标准库（复用 `mqtt_probe.py` 的 MQTT 3.1.1 客户端），不依赖 paho：

```
python txpower_ab.py                                  # 20/14/8/2 dBm，各 10 次
python txpower_ab.py --addr 0x747d --ep 2 --n 20      # 指定设备/端点
python txpower_ab.py --dry-run                        # 只打印要发什么，不连网
```

每档的流程：

1. 下发 `set_tx_power` → 等 `{BASE}/tx_power` → **核对 `eff`**（不等就报警）
2. 静置 6 s（让重传队列清空、路由代价表更新）
3. 连发 N 次 `read_onoff`（Read Attributes on 0x0006/0x0000），
   每次等最多 4 s 看 `{BASE}/zigbee/<addr>/epN/on_off` 有没有回音
4. 取一次 LQI 快照（作为**对照**，用来确认链路本身没变）

输出：

```
Wunsch    wirksam  Antwort   LQI   RSSI
--------------------------------------------
+20 dBm   +20      10/10     61    -68 dBm
+14 dBm   +14      10/10     61    -68 dBm
+8 dBm    +8        9/10     60    -68 dBm
+2 dBm    +2        4/10     61    -68 dBm
Kleinste Stufe ohne einen einzigen Fehlversuch: +14 dBm
```

（上表是**格式示意**，不是实测结果 —— 真实数据要你在现场跑。）

**为什么用"设备有没有回话"当判据**：HGZB-043 这类设备对远程命令只回
Default Response、不发 Report（§五）。所以"命令发出去了"不代表"设备执行了"，
唯一可靠的证据是 0.7 s 后它对我们 Read 的应答是否回来。
这一条在 §五、§十七 已经踩过两次坑，不再重复。

### 21.8 判读规则（先定规则再看数据，避免事后找理由）

| 实测结果 | 结论 |
|---|---|
| 8–14 dBm 与 20 dBm 成功率相同 | 余量充足。**建议默认降到 14 dBm**：同频干扰更小、PA 温升更低、近端设备被强信号压制的概率更低。20 dBm 那 6 dB 是浪费 |
| 只有 20 dBm 全中，降档开始丢 | 20 dBm 是必需的，**不要降**。问题在别处（位置/遮挡/天线匹配） |
| 各档都丢（含 20 dBm） | 功率不是瓶颈。按 §21.3 的结论处理：挪位置、加中继、查天线净空 |
| LQI 随功率变化 | 不该发生（§21.2）。出现了就说明测量期间有别的变量在动（有人走过、设备换路由），本次数据作废重测 |

无论测出什么，**上行 LQI 61 的问题都只能靠位置/中继解决**（§21.3）。
这也是我不建议把"降功率"当成改善按键上报的手段的原因。

### 21.9 验证

`test_ha_state.py` 28 → **43 项**，新增 15 项把这次的东西钉住：

- 网页/MQTT 命令 → 串口线路上确实是 `{"cmd":"set_tx_power","dbm":14}`
- `dBm` 是数字不是字符串
- **自己发的 `cmd/` 不被回声保护吞掉**（就是 §21.6 那个 bug 的回归测试）
- 999 → 钳到 20；`abc` → 不转发；`{"dbm":2}` → 也认
- `{"t":"txpower","want":14,"eff":14}` → 存进 `_state["tx_power"]` 并 retained 发布
- `want` 与 `eff` 不一致时，状态里**保留差异**（不许抹平）
- `{"cmd":"lqi"}` → 上线路；`{"t":"lqi"}` → 两台设备的 LQI/RSSI 都落进 `_state`

另外 `test_packaging.py` 原先一直红着：`config.json` 的版本已经到 1.2.4，
而 `GW_VERSION` 还停在 1.2.0。这次一并对齐到 **1.2.5**，并把
`config.json` / `device_db.json` 的 CRLF 转成 LF（这两个文件是要 COPY 进
Linux 容器的，测试里有"不许有 CR"这条硬约束）。

全量回归（12 个测试文件、299 项断言，全部 exit=0）：

```
test_mqtt 35/35 · test_panel 55/55 · test_ha_discovery 38 · test_ha_state 43/43
test_readback 19 · test_nodes 10 · test_retry 19 · test_autoreload 20
test_upload 3 · test_fw_ota 13 · test_esptool_flash 24 · test_packaging 20
```

`test_tz.py` 在本机 Windows 上仍是红的，原因与代码无关：
这台机器的 Python 没装 `tzdata`，`ZoneInfo("Asia/Shanghai")` 直接
`ZoneInfoNotFoundError`。容器里（Linux + tzdata）不受影响。

### 21.10 改动清单

| 文件 | 改动 |
|---|---|
| `main/zb_gateway.c` | `#include "esp_timer.h"`；`zb_gateway_set_tx_power()` / `zb_gateway_get_tx_power()` / `zb_gateway_report_lqi()` |
| `main/zb_gateway.h` | 三个函数声明（含"只看回读值"的说明） |
| `main/main.c` | `on_uart_cmd` 新增 `set_tx_power`（钳位 −24…20）、`lqi` 两个分支 |
| `main/ha_mqtt.c` | `rx_task` 新增 `"dbm":` 取值与 `"lqi"` 分支 |
| `host/addon/.../serial_gateway.py` | 回声保护豁免 `cmd/`；`on_message` 加 `set_tx_power`/`lqi`/`read_onoff`；`dispatch` 加 `txpower`/`lqi`；web 入口与状态行；`GW_VERSION` 1.2.0 → 1.2.5 |
| `host/addon/.../config.json` | 1.2.4 → 1.2.5，CRLF → LF |
| `host/addon/.../device_db.json` | CRLF → LF（内容不变） |
| `test_ha_state.py` | 28 → 43 项 |
| `txpower_ab.py` | **新增**，A/B 实测脚本 |
| `firmware/zigbee-vital-sensor-txpower.bin` | **新增**，792 752 B，ELF `478b96c2de3ec6ca` |

### 21.11 局限（说清楚）

- **一次只改一个变量。** 测的时候别同时动网关位置、别让人在中间走动、
  别同时配对设备。否则 LQI 的抖动会和功率的影响混在一起，数据作废。
- **样本量小。** 每档 10 次只能分辨"差很多"和"差不多"，
  分辨不了 5% 的差异。要做统计结论得每档 30 次以上、并跑两轮。
- **设备侧功率不知道。** §21.3 里设备 P_tx 是按 0/+3 dBm 两档假设算的，
  真值要读设备自己的 Basic/ Power Configuration 簇才知道，
  这两类模块很多不实现。
- **功率不影响上行。** 这是本节最重要的结论，也是最容易被"调功率试试"
  这个直觉绕过的一条。改功率之前先想清楚要修的是哪个方向。

### 21.12 现场崩溃：`{"cmd":"lqi"}` 把 2 KB 的任务栈打爆了

新固件（ELF `478b96c2de3ec6ca`）烧上去后，用户收到一份崩溃转储
（`Stack memory:` + `ELF file SHA256: 478b96c2d…`）。这是我引入的 bug。

**根因**：`zb_gateway_report_lqi()` 里写了

```c
static char buf[2048 + 64];
char line[2048 + 128];      /* ← 2176 B auf dem Stack */
```

而它跑在 `uart_rx` 任务里，那个任务的栈是

```c
xTaskCreate(rx_task, "uart_rx", 2048, NULL, 4, NULL);   // ha_mqtt.c:271
```

**一个数组就比整个栈还大**（2176 > 2048）。只要按一次「立即取一次 LQI/RSSI」
就必然溢出。转储里那片 `0xa5a5a5a5` 是 FreeRTOS 的栈填充字节 —— 看到它就该往
"栈"这个方向想，不是往"信号"方向想。

对照：`heartbeat_task` 也用 `lqi_buf[2048] + line[2240]`（4.3 KB），但它有
8192 B 栈（`main.c:172`），所以从来没出事。

**修法**：两个缓冲区都改成 `static`（挪进 `.bss`），并按真实上限重新定尺寸：

```c
#define LQI_JSON_MAX  1536     /* 32 Geräte à max. 41 B */
#define LQI_LINE_MAX  (LQI_JSON_MAX + 64)
void zb_gateway_report_lqi(void) {
    static char buf[LQI_JSON_MAX];
    static char line[LQI_LINE_MAX];
    ...
}
```

顺带加了一行 `ESP_LOGI` 面包屑，下次再崩能看出是崩在构造 JSON 之前还是之后。

**这条要记住的规矩**：`uart_rx` 只有 2048 B 栈。凡是能从串口命令走到
（`on_uart_cmd` → `zb_gateway_*`）的函数，**不许放超过几百字节的局部数组**，
大缓冲区一律 `static`。全项目扫了一遍，其余局部数组都在各自任务的安全范围内
（`scan_task` 4096 栈配 600 B，OK）。

新镜像：`firmware/zigbee-vital-sensor-txpower.bin`，792 880 B，
ELF `f7e9fbcd2ab4dad0`，BIN `09d773e47c18e557`。。


## 二十二、事件实体永远"未知"：payload 格式错了（add-on v1.2.6）

### 22.1 现象

按键报文早就到了 broker（MQTT 里看得清清楚楚）：

```
gw/coordinator/zigbee/0x747d/action      on @20:41:30（×2）
gw/coordinator/zigbee/0x747d/ep2/action  off @20:41:37（×2）…
```

但 HA 的事件卡片一直是 **"未知"**，日志簿里只有 `不可用/未知` 的状态翻转，
没有任何"触发"记录（截图时间 20:47，最新条目还停在 19:58）。

### 22.2 根因：event 实体要的是 JSON，我们发的是裸字符串

HA 官方文档（MQTT Event）对 `state_topic` 的定义：

> "The MQTT topic subscribed to receive **JSON event payloads. The JSON payload
> should contain the `event_type` element.**"

我们发的是裸 `on` / `off`。`on` 不是合法 JSON → HA 解不出 `event_type` →
**静默丢弃**（连日志簿都不留）。实体状态语义也印证了截图：
event 实体的状态是"最后一次触发的时间戳"，**"未知" = 自创建以来一次都没触发过**。
所以这不是显示问题，是从未成功过。

### 22.3 修法（一行的事）

```python
def _action_payload(act):
    return json.dumps({"event_type": act})   # 替换裸 act
```

`_ha_aps_action`（命令路径，§18）与 `_ha_button_from_report`（上报路径，§20）
两个发布点都换掉。**payload 改了就行，实体不用重建** —— event_types 早已
声明了 on/off/recall/store，卡住的一直只是 payload 格式。

### 22.4 双报不是 bug，不修

抓包里每个动作出现两次，最初我当成了"两条路径重复发布"加了去重；
用户当场纠正：**这就是设备行为** —— 极短触摸不会翻转继电器，但会上报，
所以两次 identical 的事件都是真实事件。去重已撤销，两次都上报。
（这个决定写进了代码注释，防止后人再"好心"加上。）

### 22.5 期望值：修好之后卡片长什么样

- 平时：事件卡片状态是**最后一次触发的时间戳**（不再是"未知"）。
- 它**不会**像开关一样停在 on/off —— 事件是瞬时的，没有持续状态。
- 想要"面板此刻 on/off"的可视化，正确做法是另建 binary_sensor
  （数据源 `epN/on_off`，面板本来就在报）—— 需要的话是下一个功能。

### 22.6 验证与部署

`test_panel.py` 断言全部改为 JSON payload（55/55 仍过）；13 个测试文件全 exit=0。
部署：**只需重建 add-on 到 v1.2.6**（纯 add-on 改动，固件没动）。
重建后按一下按键 → 卡片状态应变成时间戳、日志簿出现触发记录。

---

## 二十三、容量与配对保持：对照 tostmann/esp-coordinator 重构到 SDK 1.6（2026-10-22）

### 23.1 上游到底靠什么做到 200 节点

源码出处：`tostmann/esp-coordinator` `master`（浅克隆在 `.tostmann/`）。
关键只有三处，全部在 `main/zb_ncp.cpp`：

```cpp
// zb_ncp.h
static constexpr uint16_t OVERALL_NETWORK_SIZE = 200;

// init_int()
zb_config_overall_network_size(OVERALL_NETWORK_SIZE); // 必须先于 ZB_INIT 扩表
ZB_INIT();
zb_set_max_children(OVERALL_NETWORK_SIZE);
zb_set_nvram_erase_at_start(0);   // ★ 永不清 NVRAM
```

`zb_config_overall_network_size()` 在 `ZB_INIT()` **之前**把 ZBOSS 的邻居表 /
路由表 / 地址表一起放大——只改 `max_children` 是没用的，表本身还是 32 条，
超了会撞 `zb_address.c:395` 的表耗尽断言。上游在 `SET_MAX_CHILDREN` 命令里
也做了同样防护：host 传的值大于 `OVERALL_NETWORK_SIZE` 直接返回
`GENERIC_INVALID_PARAMETER_1`。

### 23.2 配对信息保持的三层机制（上游）

1. **NVRAM 不清**：`zb_set_nvram_erase_at_start(0)`。网络密钥、PAN、ExtPAN、
   信道、帧计数器、地址表都在 `zb_storage` 分区里，启动时整包载入 → 设备不需要重配。
2. **冷启动时序**：`NCP_RESET` 的应答**不能**在 `app::start_int()` 里立刻发。
   ZBOSS 的 NVRAM 载入是在专用任务里跑的，早发的应答会让 host 读到默认值
   `0xFFFF/0xFF`，host 以为"配置与设备网络不一致" → 调 `formNetwork()` →
   **把已配对设备全部抹掉**（上游 issue #5 / #19、z2m#26152）。修法是等
   `ZB_ZDO_SIGNAL_SKIP_STARTUP`（此刻数据集保证已载入）再发 boot-ready 帧。
3. **结构化备份**：`GET/RESTORE_STRUCTURED_BACKUP`（0x009B/0x009C），
   `backup_structured.h` 定义 `'ZBSB'` TLV：PAN、ExtPAN、Channel、UpdateID、
   Coord IEEE、NWK Key + Seq、帧计数器、设备表（16 B/设备：IEEE+短址+关系+
   类型+深度+LQI）。换芯片时整包搬到新板子，设备无需重配。

另外还有一个独立组件：`boot_guard.h` —— RTC 面包屑数"启动后没走到 SKIP_STARTUP"
的次数，30 s 死线强转重启，连续 3 次进 safe mode（栈不启动，只应答少数命令），
避免"哑砖"只能返厂重刷。

### 23.3 我们原来的差距（诊断）

| 项 | 上游 | 我们（改前） |
|---|---|---|
| 容量 | 200，表随之扩 | **32 写死**；SDK 1.0.9 的 `zb_config.h` 里 `ZB_NEIGHBOR_TABLE_SIZE 32U`，且 ZBOSS 是**预编译 .a**，改宏只会造成 ABI 不一致 |
| 扩容 API | `zb_config_overall_network_size()` | 1.0.9 库里**没有这个符号**（`nm` 验证过），`esp_zb_overall_network_size_set()` 到 1.2.2 才进 SDK |
| 配对保持 | NVRAM + 冷启动时序 + 备份 | 默认不清 NVRAM（能保持），但**没有显式声明**，也没有备份/恢复手段 |
| 自己的设备表 | 结构化备份含设备表 | `s_devices[]` **纯 RAM**：重启后要等设备发 Device-Announce；睡眠端设备可能几小时都不出现 → LQI 快照/Keep-Alive 空窗 |

### 23.4 本次改法

**① 升级运行时库**（`main/idf_component.yml`）
`esp-zigbee-lib 1.0.9 → 1.6.8`、`esp-zboss-lib 1.0.9 → 1.6.4`。
版本组合不是随便挑的：`esp-zboss-lib` **最高只有 1.6.4**（1.6.8 会版本求解失败）；
1.6.8 基于 IDF **v5.5.1**，与本机一致（2.0.x 要 5.5.4，用不了）。

**② API 迁移（编译期暴露的全部变更点，4 处）**

| 变更 | 版本 | 处理 |
|---|---|---|
| `esp_zb_ep_list_add_ep()` 改结构体参数 | 1.2.0 | 两处调用改 `esp_zb_endpoint_config_t` |
| `RADIO_MODE_NATIVE` / `HOST_CONNECTION_MODE_NONE` 加 `ZB_` 前缀 | 1.3.0 | 改 `ZB_RADIO_MODE_NATIVE` / `ZB_HOST_CONNECTION_MODE_NONE` |
| `esp_zb_zdo_ieee_addr_req` 回调改 `(status, resp*, ctx)` | 1.6.0 | `ieee_resolved_cb` 改用 `resp->ieee_addr` |
| `esp_zb_main_loop_iteration()` 弃用 | 1.5.0 | 改 `esp_zb_stack_main_loop()` |

**③ 容量：单点定义 + 表随之扩**（`zb_gateway.h` / `zb_gateway.c`）

```c
#define ZB_GATEWAY_MAX_DEVICES 64          // 头文件，唯一真源
...
esp_zb_overall_network_size_set(MAX_DEVICES);  // 必须在 esp_zb_init() 之前
esp_zb_cfg_t cfg = { .nwk_cfg.zczr_cfg = { .max_children = MAX_DEVICES } };
```

所有缓冲从该宏派生，避免"改一个忘另一个"：
`LQI_JSON_MAX = MAX_DEVICES*48 + 128`、heartbeat 的 `lqi_buf/line` 同源，
heartbeat 任务栈 8192 → 12288（64 设备时两个缓冲合计约 6.6 KB）。

> 为什么选 64 而不是 200：64 是 SDK 缺省的网规尺寸，我们现场设备数只有个位数；
> 200 要多付约 12 KB .bss + 每 60 s 一条 ~10 KB 的 LQI 报文。
> 真需要 200，只改 `ZB_GATEWAY_MAX_DEVICES` 一个数（缓冲会自动跟上），
> 但 heartbeat 栈要再核一遍。

**④ 配对保持：显式 NVRAM 语义 + 设备表落 NVS**

```c
esp_zb_nvram_erase_at_start(false);  // 显式写出来，不再是"碰巧默认如此"
devtab_load();                        // 从 NVS 取回 {短址, IEEE}
```

`devtab`（namespace `zb_gw`，key `devtab`，`DEVTAB_MAGIC` + count + 64×10 B
≈ 645 B blob）：

- **存**：Device-Announce 新增/迁移地址后、IEEE 解析成功后
- **清**：收到 `LEAVE_INDICATION` 且 `rejoin=0`（`rejoin=1` 是换地址回来，保留）
- **载**：启动时载入，`interviewed` 一律置 false —— 不假装已面试，
  等设备真的应答再走 Bind/Reporting，否则只会发空请求

这样重启后 LQI 快照与 Keep-Alive 立刻能看到已知设备，不必等它们播报；
设备换了短地址时，ANNCE 的 IEEE 迁移逻辑照旧生效。

**⑤ 明确对齐、但本次不做**（列出来避免以后重复讨论）

- 结构化备份/恢复（换芯片免重配）：需要在 JSON 串口协议里新增两条命令，
  且 add-on 侧要有存储入口，改动面比本次大，单独做
- boot_guard 类保护：我们不是 NCP，启动时序简单，暂不需要
- 200 节点：见上，一个宏的事

### 23.5 验证

```text
idf.py build → EXIT=0，无 warning（main/ 五个源文件全部干净）
镜像 652 080 B（旧版 792 880 B；新栈更小，app 分区 1.75 MB 仍充裕）
DRAM 109 454 / 452 112 B 空闲（24.2%），.bss 36 264 B
test_mqtt 35/35、test_panel 55/55、test_ha_state、test_packaging 全过
```

### 23.6 ⚠️ 部署风险（刷机前必读）

**ZBOSS 运行时库跨大版本（1.0.9 → 1.6.4），NVRAM 数据集格式不保证兼容。**
若新版不认旧数据集，表现是启动后 `esp_zb_bdb_is_factory_new() == true` →
固件按原逻辑重建网络 → **所有设备丢失配对，需要重新入网**。

因此刷机前请：
1. 先确认 add-on 的 `/data/ha_nodes.json` 有备份（设备名/属性还在，重配后能对上号）
2. 选一个方便重新配对的时间窗（面板开关要逐个按键入网）
3. 刷完后看串口/日志第一条：出现 `Neustart – Netzwerk wiederhergestellt`
   = 数据集兼容，设备不用动；出现 `Erster Start – Netzwerk wird gebildet`
   = 已重建网络，必须重新配对

回退路径：`git revert` 本次提交 + `idf.py build`，旧的 `firmware/*.bin` 仍在仓库里。

#### 23.6.1 实测结论（2026-10-22，已上机）

- **OTA 成功**：应用层串口 OTA（`fw_ota_start/data/end`）写入 652 080 B，`done` 只在
  `esp_ota_end()` + `esp_ota_set_boot_partition()` 均返回 ESP_OK 后才上报，随后自重启。
- **NVRAM 不兼容，已证实**：设备重启后走的是 `Erster Start – Netzwerk wird gebildet`，
  开关与情景开关**全部重新配对**才恢复 —— 即上面第 3 条的第二种结果。
  ⇒ 跨大版本（1.0.9 → 1.6.x）升级的代价就是一次全网重配，升级前必须留出时间窗。
- **升级后不会再重配**：重建之后的 NVRAM 已是 1.6 格式，后续同大版本内的 OTA 不受影响。
- ⚠️ **上面第 3 条判据在实践中读不到**：`main/ha_mqtt.c:269` 在 `ha_mqtt_init()` 里
  `esp_log_set_vprintf(null_vprintf)` 把整个 ESP_LOG 静音，且 `sdkconfig` 用
  `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y` / `CONFIG_ESP_CONSOLE_UART_NUM=-1`，
  所以 `Netzgröße…` / `Neustart – Netzwerk wiederhergestellt` 这类日志**既不到 UART 也不到 USB**。
  UART/USB 上只有 `emit()` 的纯 JSON（`{"t":"boot"}` / `heartbeat` / `zigbee` / …）。
- ⇒ 实际可用的判据只有两条：
  1. `[C6] boot` 出现 + 心跳 `uptime` 归零 → 设备重启成功、新镜像在跑；
  2. PAN ID 是否变化、设备表是否清空 → 判断 NVRAM 兼容性（重建则 PAN 变、表清空）。
  心跳里没有版本字段，因此无法从 host 侧直接读出"跑的是哪个固件版本"。
  （改进项：在 `ha_mqtt.c:275` 的 boot 消息里加 `"fw":"sdk1.6.8","cap":64`。）

## 二十四、中继器无线升级 + 运行时可调功率（2026-10-25）

### 24.1 三个提出来才知道的问题

1. 中继器能不能走 Zigbee OTA？
2. Web 管理端设备页只能填「容器内路径」，**不能自己选固件上传** —— 而不同设备装的是
   不同固件，选错类型等于白推。
3. 改功率要重新编译刷机，太麻烦。

### 24.2 链路核对：断点只在最末端

Zigbee OTA 的四段链路，前三段早就写好并上过机：

| 环节 | 位置 | 状态 |
|---|---|---|
| Web 触发 | `serial_gateway.py` `POST /api/ota` | 已有 |
| host 喂数据 | `_ota_start()` / `_ota_handle_req()`（应答 `ota_req`） | 已有 |
| 网关 OTA 服务端 | `main/zb_ota_server.c`（`MFR 0x131B`、`notify_on=1`） | 已有 |
| **中继端点** | `router/main/repeater.c` 原先只有 Basic + Identify | **缺 OTA client** |

没有 OTA client 簇 → 中继不会发 Query Next Image Request → 网关的 Image Notify 广播
无人应答，超时后什么都不会发生。所以中继原先只能 USB 刷。

★ 另一条容易忽略的事实：add-on 里的**版本号取镜像文件的 mtime**
（`_ota_start()` 里 `int(os.path.getmtime(path)) & 0xFFFFFFFF`），不是手工版本号。
所以每次重新编译出来的镜像自动就算「更新」，不需要维护版本表。

### 24.3 ★ 关键设计约束：image type 必须分家

原来 `ota_start` 的 payload 只有 `SIZE,VERSION`，服务端硬编码 `OTA_IMAGE_TYPE 0x0001`，
终端节点（`client/`）的 OTA client 也是 `0x0001`。

如果中继沿用 `0x0001`，网关一次广播会让**两类设备同时来抢同一份镜像**，
镜像内容不匹配的那个照样往自己的 ota 分区写 —— 这不是理论风险。

所以本次把 image type 提升为一等参数：

| image type | 设备 | 目录 |
|---|---|---|
| `0x0001` | 终端节点（switch2wifi） | `client/` |
| `0x0002` | 中继器（纯 Router） | `router/` |

改动分布（全链路透传）：

- `main/zb_ota_server.c`：`zb_ota_server_start(size, ver, image_type)`，`type == 0` 时
  回落 `0x0001`（老 host 端不受影响）；`ota_status` 行里多一个 `"type":"0x…"`
- `main/main.c`：`ota_start` payload 变 `"SIZE,VERSION[,IMAGETYPE]"`（第三段可选）
- `main/ha_mqtt.c`：解析 JSON 的 `"type"` 字段（数字或带引号的 `"0x0002"` 都吃）
- host `_ota_start(bin_path, image_type)`：`ota_start` 行里带 `"type"`；
  上传落盘按类型分文件（`/data/ota_0x0001.bin` / `/data/ota_0x0002.bin`），
  **不会互相覆盖**

### 24.4 中继固件新增的东西

1. **OTA client**（`router/main/zb_ota_client.c`，从 `client/` 移植）
   - image type `0x0002`
   - 去掉全部 `ha_mqtt_publish_ota_status()`（中继没有 MQTT），进度只走 `ESP_LOGI`
   - **版本号存 NVS**（`rep/ota_ver`）：否则客户端永远自称 version=1，
     网关每次广播都会让它把同一版重下一遍
2. **厂商专有簇 `0xFC00` / 属性 `0x0001`**（S8，dBm，READ_WRITE）
   - 写入 → `esp_zb_set_tx_power()` 立刻生效 + 写 NVS（`rep/txp`，存 `dbm + 24`）
   - 掉电重启后从 NVS 恢复，不再是编译期常量
3. **按键分级**（同一个 BOOT 键，`btn_task()`）
   - 短按 <1 s → 循环 8 / 14 / 20 dBm（网络不通时的兜底）
   - 长按 1~5 s → rejoin
   - 长按 ≥5 s → 恢复出厂

### 24.5 网关侧新增写属性能力

`zb_gateway.c` 里新增 `zb_gateway_write_tx_power(addr, ep, dbm)`：
组 `esp_zb_zcl_write_attr_cmd_t`（cluster `0xFC00`、attr `0x0001`、`S8`）→
`esp_zb_zcl_write_attr_cmd_req()`。

★ 沿用 `dev_cmd_send()` 那条老规矩：`esp_zb_zcl_*_cmd_req()` **只能在 ZBOSS 任务里调**，
UART-RX 任务里直接调会被静默丢弃。所以走 `esp_zb_scheduler_alarm()` 排队。

链路：Web 设备页按钮 → `POST /api/device/<addr>/tx_power` →
MQTT `{BASE}/cmd/set_dev_power`（JSON `{"addr","ep","dbm"}`）→ add-on `on_message`
翻成 UART `{"cmd":"set_dev_power",…}` → 网关 `zb_gateway_write_tx_power()`。
网关回一行 `{"t":"wr_power","addr":…,"dbm":…,"tsn":…}`，Web 页显示「已下发」。
**属性写入不会自动回读**，中继是否真的改了，以它自己的串口日志为准。

### 24.6 Web 管理端

- 仪表盘 + 设备页的 OTA 区块都改成「**类型下拉 + 文件上传**」，另留「按容器内路径」的兜底表单
- 设备页会按 `model` 自动预选类型（含 `Repeater` → `0x0002`，否则 `0x0001`），可手动改
- 新增「中继发射功率」区块（20 / 14 / 8 / 2 / 0），显示上次下发记录
- 新增 `_mp_field()`：multipart 表单里 `parse_qs` 用不了（会把二进制搅碎），
  单独从 body 里取 `type` 字段；`_save_upload()` 增加 `dst` 参数

### 24.7 编译出来的坑（都是编译器先发现的）

1. `esp_zb_set_tx_power()` 在 1.6.8 里返回 **void**，不能 `esp_err_t err = …`
2. `zb_ota_client.c` 需要 `esp_ota_ops.h` → `router/main/CMakeLists.txt`
   的 `REQUIRES` 必须加 **`app_update`**（`esp_partition` 也补上）
3. ★ `"\x10ESP32-C6-Repeater"` 是**错的**：编译器把 `\x10E` 当成一个十六进制转义
   （`E` 也是十六进制数字）→ 报 `hex escape sequence out of range`，ZCL 字符串的
   长度字节就废了。必须拆开写：`"\x10" "ESP32-C6-Repeater"`。
   —— 这一条在**旧固件里就存在**（只是没人读 model），本次顺手修掉。
