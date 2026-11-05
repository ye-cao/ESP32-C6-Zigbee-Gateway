# 开发踩坑记录 —— ESP32-C6 Zigbee 生命体征网关

## 硬件连接

### 协调器（树莓派侧）

- **ttyACM0** = CH340 USB-UART（1a86:55d3）→ 接到协调器 ESP32-C6 的 GPIO16/17
- **ttyACM1** = Espressif USB-JTAG/CDC（303a:1001）→ 协调器 ESP32-C6 的原生 USB
- udev 符号链接：`zigbee-coordinator` → ttyACM0，`zigbee-coordinator-jtag` → ttyACM1
- JSON 输出走 **ttyACM1**（USB-JTAG），靠 `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`

### 客户端（笔记本侧）

- **ttyACM0** = Espressif USB-JTAG/CDC（303a:1001）→ 客户端 ESP32-C6 的原生 USB
- 客户端 MAC（IEEE 802.15.4）：`98:a3:16:ff:fe:97`（EUI-64：`98:a3:16:ff:fe:97:c4:10`）
- 客户端 Zigbee 短地址：`0xb4c9`（重新入网会变，要稳定标识请用 IEEE）
- DS18B20：GPIO6（4.7 kΩ 上拉），用 `espressif/onewire_bus` 的 RMT 组件

---

## 关键结论与教训

### 1. DTR/RTS 复位陷阱

手动组合 DTR=True + RTS=False 会把 ESP32-C6 打进下载模式（`boot:0x4 DOWNLOAD`）。

**解法：** 一律用 `esptool --before default_reset`，不要手工拉 DTR/RTS 做复位。

---

### 2. USB 口方向（ttyACM0 与 ttyACM1）

- ttyACM0 = CH340 = UART0
- ttyACM1 = Espressif 原生 USB-JTAG = 开了 `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y` 后的 stdout
- **JSON 走 ttyACM1**；树莓派上的 `serial_gateway.py` 从 `/dev/zigbee-coordinator-jtag` 读

---

### 3. 心跳任务栈溢出

`Guru Meditation Error: Stack protection fault in task "heartbeat"` —— 该任务需要超过
2048 字节来放 `char lqi_buf[256]` + `char line[400]` 以及嵌套调用。

**解法：** 心跳任务栈 2048 → **4096 字节**。

---

### 4. `esp_ieee802154_get_recent_rssi()` 调用过早

在 Zigbee 初始化之前调用 → 第一次心跳就崩。

**解法（协调器）：** 加一个 `static volatile int8_t s_last_rssi`，在 `aps_data_ind_cb`
里更新；把直接调用替换成 `zb_gateway_get_last_rssi()`。

---

### 5. ZCL API 的线程安全 —— 从 FreeRTOS 任务调 `esp_zb_zcl_report_attr_cmd_req()` 会崩

任何 `esp_zb_zcl_*` API **必须**在 Zigbee 任务上下文（`esp_zb_main_loop_iteration()`
所在线程）里跑。ESP-IDF Zigbee v1.0.9 / ESP-IDF v5.2.5 里**没有** `esp_zb_lock_acquire()`。

**解法：** 从 Zigbee 上下文内部（`esp_zb_app_signal_handler` 收到 STEERING 入网信号时）
用 `esp_zb_scheduler_alarm()` 起一个周期回调，回调每次执行完再给自己续 65 s。
FreeRTOS 心跳任务只写一个 `volatile int16_t`（RISC-V 上原子）。

```c
// 在 STEERING 信号处理里（Zigbee 上下文）：
esp_zb_scheduler_alarm(temp_report_periodic, 0, TEMP_REPORT_MS);

// 周期回调（Zigbee 上下文）：
static void temp_report_periodic(uint8_t param) {
    int16_t t = s_temp_pending;          // 读 volatile
    esp_zb_zcl_report_attr_cmd_req(&cmd);
    esp_zb_scheduler_alarm(temp_report_periodic, 0, TEMP_REPORT_MS);
}

// 心跳任务里（任意上下文）：
void zb_device_report_temp(int16_t temp_100) {
    s_temp_pending = temp_100;           // int16 原子写
}
```

---

### 6. ZCL 属性上报被静默丢弃 —— 簇没注册

客户端把 Temperature Measurement（0x0402）的 Report Attributes 发给协调器 EP1，
而协调器 EP1 上根本没注册温度簇 → ZCL 层静默丢掉整帧，`ESP_ZB_CORE_REPORT_ATTR_CB_ID`
永远不触发。

**解法：** 在协调器 EP1 上把温度簇注册成 **CLIENT 角色**：

```c
esp_zb_cluster_list_add_temperature_meas_cluster(cl,
    esp_zb_temperature_meas_cluster_create(&temp_cfg),
    ESP_ZB_ZCL_CLUSTER_CLIENT_ROLE);
```

---

### 7. 设备的永久身份：IEEE MAC 与 Zigbee 短地址

Zigbee 短地址每次重新入网都会变。要拿永久 6 字节 MAC，用
`esp_read_mac(mac, ESP_MAC_IEEE802154)`，格式 `%02x:%02x:%02x:%02x:%02x:%02x`
→ `98:a3:16:ff:fe:97`。

---

### 8. 温度 0x8000 = ZCL 里的“无效/未知”

ZCL 温度测量：`MeasuredValue = 0x8000`（int16_t = -32768）表示没有有效读数。

**`serial_gateway.py` 里的归一化：**

```python
def _extract_value(cluster, raw):
    if cluster == "temperature":
        r = raw.get("raw")
        return None if (r is None or r == -32768) else r / 100.0
```

存簇负载之前，也要把 `p["raw"] = None` 一起归一化。

---

### 9. 服务架构

- **树莓派（192.168.178.218）：** `zigbee-mqtt-gateway.service` 读协调器 ttyACM1，
  Web 界面 8082 端口
- **笔记本：** 不跑服务——只用来通过 ttyACM0 刷客户端 ESP32-C6
- 温度数据链路：客户端 DS18B20 → Zigbee ZCL 属性上报（0x0402）→ 协调器 →
  serial_gateway.py → MariaDB + Web 界面

---

### 10. esptool 语法差异

- **树莓派（旧 esptool 4.x）：** 下划线写法 `--before default_reset`、`write_flash`、`--flash_mode`
- **笔记本（新 esptool 5.x）：** 连字符写法 `--before default-reset`、`write-flash`、`--flash-mode`

---

## 当时的心跳 JSON 格式

### 客户端（USB ttyACM0 → 笔记本）

```json
{"t":"heartbeat","mac":"98:a3:16:ff:fe:97","uptime":61,"pan":"0x43cb","ch":20,"rssi":-85,"temp":null}
```

### 客户端（Zigbee ZCL 温度簇 → 协调器 → serial_gateway.py）

- 没接传感器：`{"raw": null}`（由 -32768 归一化而来）
- 接了传感器：`{"raw": 2250}` = 22.50 °C

### 协调器心跳（ttyACM1 → 树莓派）

```json
{"t":"heartbeat","uptime":6005,"ch":20,"pan":"0x43cb","rssi":-34,"dev":[{"addr":"0xb4c9","lqi":0,"rssi":-88},{"addr":"0x0684","lqi":234,"rssi":-34}]}
```

---

## 编译与刷写流程

```bash
# 客户端固件（在树莓派上编译，在笔记本上刷）：
ssh pi 'cd ~/python/esp32c6-zigbee-vital-gateway/client && source ~/esp-idf/export.sh && idf.py build'
scp pi:~/python/esp32c6-zigbee-vital-gateway/client/build/zigbee-client.bin /tmp/
esptool --chip esp32c6 -p /dev/ttyACM0 -b 460800 \
  --before default-reset --after hard-reset \
  write-flash --flash-mode dio --flash-size 4MB --flash-freq 80m \
  0x20000 /tmp/zigbee-client.bin

# 协调器固件（在树莓派上编译并刷写）：
ssh pi 'cd ~/python/esp32c6-zigbee-vital-gateway && source ~/esp-idf/export.sh && idf.py build'
ssh pi 'sudo systemctl stop zigbee-mqtt-gateway'
ssh pi 'python -m esptool --chip esp32c6 -p /dev/ttyACM1 -b 460800 \
  --before default_reset --after hard_reset \
  write_flash --flash_mode dio --flash_size 4MB --flash_freq 80m \
  0x20000 build/zigbee-vital-sensor.bin'
ssh pi 'sudo systemctl start zigbee-mqtt-gateway'
```

---

### 11. ZCL switch-case：IAS Zone 与 Power Config 的处理分支缺失

那两个 `patch_multi_cluster.py` 脚本都报了成功，但因为转义序列不一致
（`\"` 与 `\\"`），锚点字符串实际上没匹配上。于是 IAS Zone（0x0500）和
Power Config（0x0001）的上报掉进 `default` 分支，只被记成
`{"cluster":"0x0500","attr":"0x0002"}`——**没有 raw 值**。

**解法：** 直接用转义正确的 C 字符串把分支补上：

```c
case ESP_ZB_ZCL_CLUSTER_ID_IAS_ZONE: {
    uint16_t zs = *(uint16_t *)msg->attribute.data.value;
    snprintf(payload, sizeof(payload), "{\"zone_status\":%u,\"motion\":%u}", zs, (uint8_t)(zs & 1));
    ha_mqtt_publish_zigbee(addr, "ias_zone", payload);
    break;
}
case ESP_ZB_ZCL_CLUSTER_ID_POWER_CONFIG: {
    uint16_t attr_id = msg->attribute.id;
    if (attr_id == 0x0021) {                          // BatteryPercentageRemaining
        uint8_t raw = *(uint8_t *)msg->attribute.data.value;
        snprintf(payload, sizeof(payload), "{\"pct\":%u,\"raw\":%u}", raw / 2, raw);
    } else if (attr_id == 0x0020) {                   // BatteryVoltage（单位 100 mV）
        uint8_t raw = *(uint8_t *)msg->attribute.data.value;
        snprintf(payload, sizeof(payload), "{\"voltage_mv\":%u,\"raw\":%u}", (unsigned)(raw * 100U), raw);
    } else {
        uint8_t raw = *(uint8_t *)msg->attribute.data.value;
        snprintf(payload, sizeof(payload), "{\"attr\":\"0x%04x\",\"raw\":%u}", attr_id, raw);
    }
    ha_mqtt_publish_zigbee(addr, "battery", payload);
    break;
}
```

**涂鸦电池的取值：**

- `attr 0x0021`：`raw=170` → `pct=85`（%）—— `raw / 2 = %`
- `attr 0x0020`：`raw=29`  → `voltage_mv=2900`（mV）—— `raw × 100 mV`
- CR2032 满电：raw≈32（3200 mV），耗尽：raw≈27（2700 mV）

---

### 12. 可配置的休眠间隔（ZCL 自定义簇 0xFF01 命令 0x02）

默认 600 s 的心跳间隔现在可以通过 MQTT/网页修改。

- **客户端：** `volatile uint32_t s_sleep_ms`（默认 600 000 ms），NVS 键 `client_cfg/sleep_s`
- **ZCL 命令：** 簇 0xFF01，命令 0x02，负载 uint32（秒）
- **`zb_device_set_sleep(secs)`：** 设置 `s_sleep_ms`、`s_temp_report_ms = (secs+5)*1000`，写 NVS
- 最小值 10 s；重启后由 NVS 恢复

```bash
# 通过 MQTT：
mosquitto_pub -h 192.168.178.218 -t "gw/coordinator/cmd/set_sleep" -m 300

# 通过网页：
# 设备详情页 → 休眠间隔表单
```

---

### 13. 网页去掉了「最后上报」列

设备列表里的 `last_seen` 时间戳并不可靠（那是树莓派收到消息时的系统时间，不是设备时间）。
**权威时间戳**在每个簇的负载里自带。因此删掉该列，
表格现在是 6 列：地址、名称、IEEE、负载、LQI、RSSI。
