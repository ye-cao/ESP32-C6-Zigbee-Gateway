# ESP32-C6 Matter over Thread —— 生命体征网关

同一套 ESP32-C6 + MR60BHA1 硬件的第二种固件方案：不用 Zigbee 3.0 + MQTT，
改用 **Matter over Thread**。

## 架构

```
MR60BHA1 ──UART──► ESP32-C6 [ Matter End Device ]
                        │
                   Thread-Mesh (IEEE 802.15.4)
                        │
               Thread Border Router (RPi / GL.iNet)
                        │
                       IPv6
                        │
              Home Assistant (Matter Controller)
```

## 与 Zigbee 方案的差别

| | Zigbee 固件（`main/`） | Matter 固件（`matter/`） |
|---|---|---|
| 协议栈 | esp-zigbee-sdk | esp-matter + OpenThread |
| 传输 | Zigbee Mesh | Thread Mesh（IPv6） |
| 上位机软件 | gateway_service.py + MQTT | 不需要（原生 Matter） |
| 配网 | Permit Join | BLE GATT（二维码） |
| HA 集成 | MQTT 自动发现 | 原生 Matter |

## Matter 数据模型

| 端点 | 簇 | 内容 |
|---|---|---|
| 0 | Basic Information | 设备信息、OTA |
| 1 | Occupancy Sensing | 雷达存在检测（radar_status == 2） |
| 2 | （自定义 0xFFF10001） | 心率 BPM + 分类 |
| 3 | （自定义 0xFFF10002） | 呼吸率 RPM + 分类 |

## 前提条件

```bash
# ESP-IDF >= 5.1
. $IDF_PATH/export.sh

# ESP-Matter SDK
git clone --recursive https://github.com/espressif/esp-matter $ESP_MATTER_PATH
. $ESP_MATTER_PATH/export.sh
```

## 编译与烧写

```bash
cd matter/
idf.py set-target esp32c6
idf.py build
idf.py -p /dev/ttyUSB0 flash monitor
```

## Matter 配网

烧写之后，用 Apple 家庭、Google Home 或 Home Assistant：

1. 从 `idf.py monitor` 里读出二维码
2. 在控制器里点「添加设备」→ 扫描二维码
3. 设备直接以「Vital Sensor」出现，不需要任何额外网关

## 状态

- [x] 工程骨架
- [x] MR60BHA1 UART 驱动（复用）
- [x] Matter Occupancy 端点
- [ ] 自定义 BPM/RPM 簇
- [ ] 通过 Matter 做 OTA
- [ ] Thread Border Router 配置说明
