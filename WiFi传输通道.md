# Wi-Fi 传输通道（不插 USB 也能当 Zigbee 网关）

## 这个功能解决什么

原来的部署方式是：C6 用一根 USB 线插在 HA 主机上，`serial_gateway.py` 打开
`/dev/ttyACM0` 收它的 JSON，再转成 MQTT。线一拔，网关就没了。

现在 C6 可以不插在主机上：**接一个 5V 电源头，通过家里 Wi-Fi 把同样的 JSON
发给主机**。主机这边不用做任何额外配置，add-on 只是从"监听串口"改成"监听一个
TCP 端口"。

## 为什么只需要换传输层

这个项目的协议边界是「一行一条 JSON」。序列化和反序列化分别落在两处：

| 方向 | 代码位置 |
|---|---|
| C6 → 主机 | `emit()` → 队列 → `tx_task` → 写出 |
| 主机 → C6 | 读入 → 攒成一行 → `ha_mqtt_handle_line()` → `on_uart_cmd()` |

两处都不关心字节从哪个 fd 来。于是：

- **固件**：把 `tx_task` 的写出目标从 `stdout` 换成 socket，把读入从
  `fgets(stdin)` 换成 `recv()`。`on_uart_cmd` 和整个 `zb_gateway.c`（2000 行）
  **一行都没改**。
- **Add-on**：`CoordinatorSerial` 旁边加一个 `CoordinatorTcp`，接口完全一样
  （`read/write/close/in_waiting/can_reset`），主循环**一行都没改**。

```
        ┌────────────────────────┐
        │ ESP32-C6               │
        │  Zigbee 协调器         │
        │  Wi-Fi STA + TCP 客户端│
        └───────────┬────────────┘
                    │  2.4 GHz（与 802.15.4 软件时分复用）
              ┌─────┴─────┐
              │  家用 AP   │
              └─────┬─────┘
                    │
        ┌───────────┴────────────┐
        │ HA 主机                │
        │  add-on 监听 :12345    │
        │  同一份 JSON 解析      │
        └────────────────────────┘
```

## 为什么默认仍然是串口

传输通道存在 NVS（命名空间 `netlink`，键 `tr`）里，**默认 0 = 串口**。

这是刻意的：Wi-Fi 配置写错、路由器换了、AP 加密方式变了、密码打错一个字……
任何一种情况下，板子插回 USB 就还能用一条命令把配置改回来。
如果默认走 Wi-Fi，一次写错就得重新 USB 刷机。

## 配置步骤

### 第 1 步：USB 插着，把 Wi-Fi 参数写进 C6

板子先用 USB 插在主机上（或者任意能发串口命令的电脑上），发：

```json
{"cmd":"net_cfg","ssid":"你家WiFi","pass":"密码","host":"192.168.101.5","port":12345,"tr":1}
```

- `host` 填 **HA 主机的 IP**，不要填 `homeassistant.local`
  （容器里解析不了这个名字，详见项目记忆里的踩坑记录）。
- `tr`：`0` = 串口，`1` = Wi-Fi。
- 任何字段留空表示「这一项不改」，方便只换 host 或只翻通道。
- 字段之间在固件内部用 `\x1f` 分隔（`ha_mqtt.c` 负责拆分 JSON → payload），
  所以 SSID 或密码里有逗号也不会被截断。

回执是一条 `{"t":"net_status",...}`，能看到当前配置和前一次的状态。

### 第 2 步：add-on 改成监听模式

配置页把 `link` 从 `uart` 改成 `tcp`，`tcp_port` 保持 `12345`（要和第 1 步
里的 `port` 一致）。因为 add-on 是 `host_network: true`，不需要做端口映射。

改完启动，日志里应该出现：

```
[Link] 传输通道 = Wi-Fi/TCP，监听 0.0.0.0:12345
[Link] 已监听 0.0.0.0:12345 —— 等 C6 通过 Wi-Fi 连上来
[Link] C6 已连接：192.168.101.88:54321
[NET] 通道已连通 ip=192.168.101.88 rssi=-47 → 192.168.101.5:12345
```

### 第 3 步：拔掉 USB

C6 换成 5V 电源头供电。它自己会重新连 Wi-Fi → 连主机 → 继续发 JSON。
设备列表、HA 实体、自动化都不受影响（Zigbee 的配对凭据在
`zb_storage@0x3A0000`，跟 USB 无关）。

## 命令参考

| 命令 | 作用 |
|---|---|
| `{"cmd":"net_status"}` | 查询当前通道 / SSID / IP / RSSI / AP 信道 |
| `{"cmd":"net_cfg","ssid":…,"pass":…,"host":…,"port":…,"tr":…}` | 写配置并立即生效 |

字段约定（三个字段都一样）：

- **缺省或空串** = 这一项不改（方便只换 host、只翻通道）
- **`"-"`** = 清空这一项（用来抹掉写错的 SSID / 密码）
- 字段之间在固件内部用 `\x1f` 分隔，所以 SSID 或密码里有逗号也不会被截断

三条下发途径，效果完全一样：

1. **MQTT**（推荐，不用接串口）：
   ```
   gw/coordinator/cmd/net_status        载荷留空
   gw/coordinator/cmd/net_cfg           载荷 = 上面那个 JSON 对象
   ```
   HA 里可以直接用「开发者工具 → 服务 → MQTT: Publish」发。
2. **网页 / 其它工具**：add-on 的 `{BASE}/cmd/*` 通道与上面是同一入口。
3. **串口**：USB 插着时可以直接把一行 JSON 写进 `/dev/ttyACM0`。

固件在以下时机**主动**上报 `{"t":"net",…}`：TCP 连上、TCP 断开、配置变更回执。
add-on 会把它打成 `[NET] …` 一行日志，并放进 `/api/state` 的 `net` 键里。

## 实机验证记录（1.5.5）

在真机上跑过、确认无误的：

| 项 | 判据 |
|---|---|
| 新固件确实在跑 | 启动横幅 `I (306) main:   JSON : 串口 USB-Serial-JTAG（默认）/ Wi-Fi+TCP（NVS 里 tr=1）`；`net_status` 能应答（老固件只回 `unknown_cmd`） |
| 命令两处登记生效 | `cmd/net_status` → `{"t":"net_status",…}` |
| `\x1f` 分字段 | 密码给 `"p,a,s s"`，host 解析出来仍是完整 IPv4（说明没被逗号截断） |
| NVS 落盘 | 写入后重启，`net_status` 里 ssid/host 仍在 |
| 清空语义 | `ssid/host` 给 `"-"` → 回执变 `""` |
| 直刷不丢配对 | 刷完 `ch:20 pan:0xa311` 不变，设备自动回来，无需重新配网 |
| `why` 原因码 | `net_status` 回 `"why":201`（= NO_AP_FOUND），把"连不上"收敛到具体原因 |
| 扫频诊断 | `{"cmd":"wifi_scan"}` → `{"t":"wifi_scan","n":0,"got":0,"aps":[]}`，耗时 2850 ms |
| 日志开关 | `{"cmd":"log_on"}` 后能看到 Wi-Fi 驱动的内部日志 |

**尚未验证：真正的 Wi-Fi/TCP 数据通路。**

⚠ **实测阻塞：本块 C6 的 Wi-Fi 接收不工作。** 同一块板子上跑 IDF 官方
`examples/wifi/scan` 也是 `Total APs scanned = 0`（2.5 s 真扫、无 Zigbee 在跑），
而 Zigbee 一切正常。所以问题不在固件、也不在共存，在板子本身。

排查过程、已修的问题清单与后续建议，见
[`Wi-Fi通道-测试报告-2026-11-06.md`](Wi-Fi通道-测试报告-2026-11-06.md)。

顺带确认一件事：`ESP_LOGI` 虽然在 `ha_mqtt_init()` 里被 `null_vprintf` 静音，
但那之前（开机前 ~300 ms）的日志仍然会从串口漏出来 —— 上面那条横幅就是这么
看到的。这也意味着 panic 前的几行不一定全丢。需要看运行期日志时发
`{"cmd":"log_on"}`（仅建议在 TCP 通道下用，否则日志会混进串口 JSON 流）。

## 信道规划（重要）

ESP32-C6 只有一个 2.4 GHz 射频，Wi-Fi 和 802.15.4 靠软件时分复用
（`CONFIG_ESP_COEX_SW_COEXIST_ENABLE=y`）。两个协议必须错开频道：

- **推荐**：Zigbee 25（2475 MHz） + Wi-Fi 1 或 6
- **不要用**：Zigbee 20 —— 它压在 Wi-Fi 11 上，两边都掉包
- 换 Zigbee 信道：`{"cmd":"set_channel","ch":25}`

共存开着的代价是上行吞吐会降一些（一次只能一个协议说话）。对本项目够用：
正常心跳 60 s 一条，LQI 快照几 KB。

## 已知代价，接受之前先看一眼

1. **`/api/fw_flash` 用不了了。** 那条路径靠 esptool 走 USB-Serial-JTAG 把芯片
   拽进下载模式，本质上是物理接触。固件升级改用 App 级 OTA（`/api/fw_ota`
   → `fw_ota_*`），它只写 Flash，和传输层无关。add-on 里已经做了拦截：TCP
   模式下点刷机会直接说明原因，不会半途失败。
2. **日志看不见了。** `CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y`，拔掉 USB 之后
   `ESP_LOGI` 的输出没人收。排查只能靠 add-on 侧的 `[NET]` / `[C6]` 行。
   如果以后需要，可以把 console 改到 UART0（GPIO16/17）接 USB-TTL。
3. **回滚还没开。** `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` 目前是关的。
   没有 USB 的情况下，一份连不上 Wi-Fi 的固件就是一块砖。**上生产前必须**
   打开它并在首启自检里 `mark_valid`（Wi-Fi + TCP 都通了才算成功），
   否则别把 USB 拔太久。
4. **`serial_port` 的 schema 从 `device(subsystem=tty)` 改成了 `str`。**
   原来那个选择器在设备不存在时会挡住 add-on 启动 —— 拔掉 USB 就打不开，
   正好废掉这个功能的全部意义。

## 故障排查

| 现象 | 先看这里 |
|---|---|
| add-on 一直没有 `[Link] C6 已连接` | C6 那边 `host`/`port` 填对了没？`net_status` 回执里 `ip` 是不是 `0.0.0.0` |
| `ip` 一直是 `0.0.0.0` | Wi-Fi 没连上。查 SSID/密码；`rssi` 若为 0 说明根本没关联 |
| 连上了但设备全掉线 | Zigbee 与 Wi-Fi 撞信道了，换 Zigbee 25 / Wi-Fi 1 或 6 |
| 每分钟断一次又连上 | AP 侧的空闲踢线，或 C6 供电不足（换 5V/2A 电源头试） |
| 网页上一个设备都没有，但 C6 日志正常 | 往 `stdout` 写被挡住了：`usb_serial_jtag_is_connected()` 为假时不再写；确认 `net_link_ready()` 为真 |

## 相关代码

| 文件 | 作用 |
|---|---|
| `main/net_link.c` / `.h` | Wi-Fi STA、TCP 客户端、断线重连、NVS 配置 |
| `main/ha_mqtt.c` | `emit()` 选通道；`ha_mqtt_handle_line()` 是两条通道的唯一解析入口 |
| `main/main.c` | `net_cfg` / `net_status` 命令分发（★ 与 `ha_mqtt.c` 必须两处都登记） |
| `host/addon/.../serial_gateway.py` | `CoordinatorTcp`、`_link_open/_link_reopen/_link_kick_reset` |
| `tools/checks/check_link_tcp.py` | 回归检查：接口一致、断开重连、`can_reset` |
