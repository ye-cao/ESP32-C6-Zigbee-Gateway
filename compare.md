# Matter 与 Zigbee 对比

围绕本项目的 ESP32-C6 Vital Gateway 展开。

## Matter（esp32c6_matter_vital_gateway）

**优点**
- 跨厂商标准（Apple、Google、Amazon、Samsung）
- 原生集成 iOS 家庭、Google Home、Alexa，无需桥接
- 基于 IP（WiFi / Thread）→ 不是私有协议
- OTA 升级走 Matter 协议，标准化
- TLS 端到端加密
- 由 CSA 联盟持续推动，长期方向明确

**缺点**
- 固件很大（约 1.9 MB）——Flash 余量极小（仅剩 1%）
- BLE 配网流程复杂，首次启动必须有 App/控制器
- WiFi 工作模式功耗高（活动期间无法休眠）
- 延迟高于 Zigbee（TCP/IP 协议栈开销）
- Matter over WiFi 在路由器重启后容易掉线，且没有重连逻辑
- 不支持直接 MQTT——要接其他系统得靠 Home Assistant / Apple Home 做桥

---

## Zigbee（zigbee-vital-sensor）

**优点**
- 延迟极低（< 15 ms）
- 功耗低——适合电池供电
- Mesh 网络：设备自己路由，覆盖范围大
- 通过网关直接接入 MQTT，不需要额外控制器
- 配对简单（Permit Join），不需要 BLE
- 固件小（约 1.5 MB），Flash 余 25%
- 久经实战（海量设备，Philips Hue、IKEA 等）

**缺点**
- 需要专用协调器硬件（就是本网关）
- 不借助额外桥（如 Hue Hub）无法原生进入 Apple Home / Google Home
- Zigbee 协调器是全网的单点故障
- ZBOSS 协议栈需要特定 Flash 分区（子类型 `fat`，官方没文档）
- 没有跨厂商统一的 OTA 机制
- Zigbee 3.0 与老档（ZHA、ZLL）并不总是兼容

---

## 结论建议

| 维度 | 更优者 |
|-----------|--------|
| 生态集成 | Matter |
| 功耗 | Zigbee |
| 延迟 | Zigbee |
| 上手难度 | Zigbee |
| 长期前景 | Matter |
| MQTT / Home Assistant | Zigbee |
| 安全性 | Matter |
| 固件体积 / Flash | Zigbee |

**结论：** 对本项目（基于 MQTT 的智能家居 + Home Assistant）而言，Zigbee 是更合适的选择。
等到确实需要 Apple Home / Google Home 直连、不想再经 HA 桥接时，再考虑 Matter。
