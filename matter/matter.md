# Matter over Thread —— ESP32-C6 生命体征网关

同一块 ESP32-C6 DevKit 的第二种固件方案：改用 **Matter over Thread** 协议上报
MR60BHA2 雷达生命体征，而不是 MQTT/Zigbee。

```
MR60BHA2 ──UART──► ESP32-C6 ──Thread (802.15.4)──► Border Router ──IP──► Matter Controller
                                                                            (Apple Home / Google / Chip-tool)
```

---

## 为什么选 Matter over Thread

| | Zigbee 固件 | Matter/Thread 固件 |
|---|---|---|
| 传输 | WiFi + MQTT | Thread（802.15.4）Mesh |
| 协议 | MQTT + HA 自动发现 | Matter（CHIP）数据模型 |
| 配网 | 固件里写死凭据 | BLE GATT → 二维码 / NFC |
| 控制器 | 仅 Home Assistant | Apple Home、Google Home、Chip-tool… |
| OTA | 手工刷机 | Matter OTA 簇 |
| 传感数据通道 | `vital-gw-XXXX/mr60bha1` MQTT | Matter 属性上报 |

Thread 需要一台 **OpenThread Border Router**（如树莓派 + `ot-br-posix`）把
Thread ↔ IP 打通，Matter 控制器才能找到设备。

---

## 硬件（与 Zigbee 方案相同）

| 部件 | 说明 |
|---|---|
| ESP32-C6 DevKit | 内置 802.15.4 射频（Thread 与 Zigbee 共用同一个射频） |
| Seeed MR60BHA2 | 60 GHz 毫米波雷达，UART 115200 8N1，3.3 V 电平 |
| Border Router | 装 `ot-br-posix` 的树莓派，或商用 Thread BR |

### 接线（MR60BHA2 → ESP32-C6）

| MR60BHA2 引脚 | ESP32-C6 引脚 | 说明 |
|---|---|---|
| TX | GPIO 5（RX1） | 传感器 → ESP |
| RX | GPIO 4（TX1） | ESP → 传感器 |
| GND | GND | |
| 5V | 5V / VIN | 传感器要 5 V 供电 |

---

## MR60BHA2 协议

MR60BHA2 的二进制帧格式与 MR60BHA1 不同：

```
[0x55][LEN_H][LEN_L][TYPE][CMD][DATA...][CRC]
```

| 字段 | 长度 | 说明 |
|---|---|---|
| SOF | 1 字节 | 固定 `0x55` |
| LEN | 2 字节 | 大端，统计 TYPE+CMD+DATA 的字节数 |
| TYPE | 1 字节 | 功能类型（见下表） |
| CMD | 1 字节 | 该类型下的命令 |
| DATA | 0–60 字节 | 负载 |
| CRC | 1 字节 | 从 TYPE 到最后一个 DATA 字节的异或 |

### 功能类型

| TYPE | 含义 | CMD | 负载 |
|---|---|---|---|
| `0x05` | 工作状态 | `0x01` | `0x00`=初始化 `0x01`=校准中 `0x02`=测量中 |
| `0x80` | 呼吸 | `0x05` | uint8 呼吸率（rpm） |
| `0x80` | 呼吸 | `0x04` | uint8 分类（0=无 1=正常 2=偏快 3=偏慢） |
| `0x80` | 呼吸 | `0x06` | float32 小端波形 |
| `0x81` | 心率 | `0x05` | uint8 心率（bpm） |
| `0x81` | 心率 | `0x04` | uint8 分类（0=无 1=正常 2=偏快 3=偏慢） |
| `0x81` | 心率 | `0x06` | float32 小端波形 |

### 与 MR60BHA1 的差异

| | MR60BHA1 | MR60BHA2 |
|---|---|---|
| SOF | `0x53 0x59`（2 字节） | `0x55`（1 字节） |
| EOF | `0x54 0x43`（2 字节） | 无 |
| LEN 字节序 | 小端 | 大端 |
| CRC 算法 | 累加取模 256 | 异或 |

驱动 `mr60bha2.c` 与 BHA1 驱动共用同一个 `mr60_data_t` 结构和 `mr60_callback_t`
回调类型，所以上层代码（端点、MQTT 发布）对具体传感器无感。

---

## Matter 数据模型

设备上注册了三个端点：

| 端点 | 类型 | 用途 |
|---|---|---|
| 0 | Root Node | Matter 根节点（必需） |
| 1 | OccupancySensing | 雷达存在检测 → `Occupancy` 属性（1 = 测量中） |
| 2 | 自定义 | 心率 —— 簇 `0xFFF10001` |
| 3 | 自定义 | 呼吸率 —— 簇 `0xFFF10002` |

### 厂商自定义簇

Matter 把 `0xFC00–0xFFFE` 保留给厂商自定义用途。

#### 簇 0xFFF10001 —— 心率

| 属性 ID | 名称 | 类型 | 说明 |
|---|---|---|---|
| `0x0000` | value | uint16 | 心率（BPM） |
| `0x0001` | category | uint8 | 0=无 1=正常 2=偏快 3=偏慢 |
| `0x0002` | radar_status | uint8 | 0=初始化 1=校准 2=测量中 |

#### 簇 0xFFF10002 —— 呼吸率

| 属性 ID | 名称 | 类型 | 说明 |
|---|---|---|---|
| `0x0000` | value | uint16 | 呼吸率（次/分） |
| `0x0001` | category | uint8 | 0=无 1=正常 2=偏快 3=偏慢 |
| `0x0002` | radar_status | uint8 | 同心率簇 |

属性使用 `ATTRIBUTE_FLAG_EXTERNAL_STORAGE`，这样 Matter 协议栈按需通过
`attribute::update()` 读取当前值，而不是缓存过期数据。

---

## 软件结构

```
app_main()
│
├── nvs_flash_init()          Matter 配网数据需要 NVS
├── esp_netif_init()
├── esp_event_loop_create_default()
│
├── matter_endpoints_init()   注册端点与簇，启动 Matter 协议栈
│   ├── node::create()
│   ├── endpoint::occupancy_sensor::create()   ep 1
│   ├── endpoint::create()                     ep 2（BPM）
│   ├── endpoint::create()                     ep 3（RPM）
│   ├── vital_clusters_create()
│   │   ├── cluster::create(0xFFF10001)        心率
│   │   └── cluster::create(0xFFF10002)        呼吸率
│   └── esp_matter::start()                   启动 BLE 配网 + Thread
│
└── mr60bha2_init()           UART1，TX=GPIO4，RX=GPIO5，115200
    └── uart_task (FreeRTOS)  → 帧解析 → mr60_callback()
                                                │
                                                └── matter_update_vitals()
                                                    ├── attribute::update ep1 Occupancy
                                                    └── vital_clusters_update()
                                                        ├── attribute::update ep2 BPM/分类/状态
                                                        └── attribute::update ep3 RPM/分类
```

### 源文件

| 文件 | 语言 | 说明 |
|---|---|---|
| `main/main.c` | C | `app_main`：初始化顺序、MR60 回调 |
| `main/mr60bha2.c` | C | MR60BHA2 UART 驱动、帧解析、FreeRTOS 任务 |
| `main/mr60bha2.h` | C | 与 Zigbee 固件共用（符号链接 → `../../main/`） |
| `main/mr60bha1.c` | C | BHA1 驱动（符号链接 → `../../main/`，留作参考） |
| `main/matter_endpoint.cpp` | C++ | esp_matter 端点与簇注册、属性更新 |
| `main/matter_endpoint.h` | C | `extern "C"` 接口，供 `main.c` 调用 |
| `main/vital_cluster.cpp` | C++ | 创建与更新厂商自定义簇 0xFFF10001/2 |
| `main/vital_cluster.h` | C | 簇/属性 ID 定义、`extern "C"` 声明 |
| `main/CMakeLists.txt` | CMake | IDF 组件：C + C++ 源文件 |
| `partitions.csv` | CSV | 4 MB flash 的 NVS + OTA 分区表 |
| `sdkconfig.defaults` | Kconfig | 编译选项（见下） |
| `../CMakeLists.txt` | CMake | 顶层工程，为 esp-matter 设置 `EXTRA_COMPONENT_DIRS` |

> `mr60bha2.c/h` 的正本在 `main/`；
> `matter/main/mr60bha2.c/h` 是指向它的符号链接，两种固件共用一份驱动。

---

## 编译环境

| 工具 | 版本 |
|---|---|
| ESP-IDF | v5.2.5 |
| esp-matter | v1.4 |
| connectedhomeip | 随 esp-matter v1.4 附带 |
| cmake | 3.30.2（经 `idf_tools.py` 安装） |
| 目标 | ESP32-C6 |

### 编译脚本

```bash
bash ~/build_matter.sh
```

`build_matter.sh` 做四件事：
1. source `esp-idf/export.sh` 和 `esp-matter/export.sh`
2. 设 `TMPDIR=$HOME/tmp_build`（避免写满 96 MB 的 `/tmp` tmpfs）
3. 跑 `idf.py set-target esp32c6 && idf.py build`
4. 成功后 `git add matter/ && git commit && git push`

### 手工编译

```bash
export IDF_PATH=~/esp-idf
export ESP_MATTER_PATH=~/esp-matter
export PATH=~/.espressif/tools/cmake/3.30.2/cmake-3.30.2-linux-x86_64/bin:$PATH
export TMPDIR=~/tmp_build

. $IDF_PATH/export.sh
. $ESP_MATTER_PATH/export.sh

cd esp32c6-repo/matter
idf.py set-target esp32c6
idf.py build
```

### 烧写

```bash
idf.py -p /dev/ttyUSB0 flash monitor
```

---

## sdkconfig.defaults

关键选项及为什么这么设：

| 选项 | 值 | 原因 |
|---|---|---|
| `CONFIG_OPENTHREAD_ENABLED` | `y` | Thread 网络协议栈 |
| `CONFIG_OPENTHREAD_FTD` | `y` | 完整 Thread 设备（可承担路由） |
| `CONFIG_OPENTHREAD_RADIO_NATIVE` | `y` | 使用 ESP32-C6 内置 802.15.4 射频 |
| `CONFIG_BT_ENABLED` | `y` | Matter 配网要 BLE |
| `CONFIG_BT_NIMBLE_ENABLED` | `y` | NimBLE（轻量 BLE 协议栈） |
| `CONFIG_ESP_MATTER_ENABLE_DATA_MODEL` | `y` | Matter 数据模型 |
| `CONFIG_ESP_MATTER_NVS_ENCRYPTION` | `n` | 简化密钥存储（开发阶段） |
| `CONFIG_PARTITION_TABLE_CUSTOM` | `y` | 使用 `partitions.csv` |
| `CONFIG_ESPTOOLPY_FLASHSIZE_4MB` | `y` | ESP32-C6 DevKit 是 4 MB flash |
| `CONFIG_CHIP_WIFI_NETWORK_ENDPOINT_ID` | `1` | WiFi 与 Thread 端点 ID 必须不同（CHIP 断言要求） |
| `CONFIG_ENABLE_WIFI_STATION` | `n` | 纯 Thread 设备，不要 WiFi |
| `CONFIG_ENABLE_WIFI_AP` | `n` | 同上，不要 WiFi AP |
| `CONFIG_MBEDTLS_HKDF_C` | `y` | Matter 加密需要 HKDF（CHIPCryptoPALmbedTLS） |
| `CONFIG_ESP_TASK_WDT_TIMEOUT_S` | `30` | Matter 启动要 5 s 以上 |

---

## 配网

1. 烧写固件。
2. 打开 Matter 控制器 App（Apple Home、Google Home、chip-tool）。
3. 设备启动后立刻通过 **BLE**（NimBLE）广播。
4. 扫二维码（串口会打印）或输入配对码。
5. 控制器经 BLE 下发 Thread 凭据。
6. 配网完成后设备加入 Thread 网络，BLE 关闭。

### chip-tool 示例

```bash
# 配网（Thread dataset 从你的 Border Router 取）
chip-tool pairing ble-thread 1 hex:<thread-dataset> 20202021 3840

# 读心率属性
chip-tool any read-by-id 0xFFF10001 0x0000 1 2

# 读呼吸率属性
chip-tool any read-by-id 0xFFF10002 0x0000 1 3

# 订阅心率（上报间隔 0–30 s）
chip-tool any subscribe-by-id 0xFFF10001 0x0000 0 30 1 2
```

默认配对码：`20202021`
默认 discriminator：`3840`

---

## C / C++ 混编与链接

`main.c` 按 **C** 编译（它本身没有 `extern "C"`）。
`matter_endpoint.cpp` 和 `vital_cluster.cpp` 按 **C++** 编译，因为要调用
`esp_matter::` 命名空间的 API。

为了让 `main.c` 能调用 C++ 函数：

- `matter_endpoint.h` 里用 `#ifdef __cplusplus / extern "C"` 包住声明；
- `matter_endpoint.cpp` 里的定义显式标注 `extern "C"`，让 C++ 编译器生成
  未修饰（unmangled）的符号名。

```c
// matter_endpoint.h
#ifdef __cplusplus
extern "C" {
#endif
esp_err_t matter_endpoints_init(void);
esp_err_t matter_update_vitals(...);
#ifdef __cplusplus
}
#endif
```

```cpp
// matter_endpoint.cpp
extern "C" esp_err_t matter_endpoints_init(void) { ... }
extern "C" esp_err_t matter_update_vitals(...)   { ... }
```

---

## 分区表

```
# partitions.csv
nvs,         data, nvs,      0x9000,  0x6000   # 运行时 NVS
phy_init,    data, phy,      0xf000,  0x1000
nvs_keys,    data, nvs_keys, 0x10000, 0x1000
nvs_factory, data, nvs,      0x11000, 0x6000   # Matter 配网数据
otadata,     data, ota,      0x17000, 0x2000
ota_0,       app,  ota_0,    0x20000, 0x1C0000 # 1792 KB
ota_1,       app,  ota_1,    0x1E0000,0x1C0000 # 1792 KB
```

总计占用 3968 KB，ESP32-C6 DevKit 可用 4096 KB。

---

## 已知编译问题与解法

| 报错 | 根因 | 解法 |
|---|---|---|
| `static_assert(WIFI_ENDPOINT_ID != THREAD_ENDPOINT_ID)` | 两个 ID 默认都是 0 | `CONFIG_CHIP_WIFI_NETWORK_ENDPOINT_ID=1` + 关闭 WiFi |
| `ESP_RETURN_ON_ERROR` 未声明 | 缺 `#include "esp_check.h"` | 在 `mr60bha1.c` 补上 |
| 头文件里 `esp_err_t` 未声明 | 缺 `#include "esp_err.h"` | 在 `matter_endpoint.h` 补上 |
| `config_t` 不是 `esp_matter::endpoint` 的成员 | v1.4 没有通用的 `endpoint::config_t` | 直接 `endpoint::create(node, flags, NULL)` |
| `Id` 不是 `esp_matter::cluster::occupancy_sensing` 的成员 | 命名空间路径不对 | 用 ZAP 头里的 `chip::app::Clusters::OccupancySensing::Id` |
| `CLUSTER_FLAG_SERVER` 未声明 | 枚举在 `esp_matter` 命名空间里 | 在 .cpp 里 `using namespace esp_matter` |
| C 编译时报 `optional: No such file` | `.c` 文件间接包含了 C++ 头 | 把 `matter_endpoint.c` → `.cpp`、`vital_cluster.c` → `.cpp` |
| `undefined reference to matter_update_vitals` | C++ 名字修饰 | 声明和定义都加 `extern "C"` |
| `undefined reference to mbedtls_hkdf` | mbedTLS 里 HKDF 未开 | `sdkconfig.defaults` 加 `CONFIG_MBEDTLS_HKDF_C=y` |
| `/tmp` 太小（96 MB tmpfs） | 编译 Matter 时 GCC 临时文件写满 `/tmp` | `export TMPDIR=$HOME/tmp_build` |
