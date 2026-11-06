# 变更日志

本文件记录本项目所有值得说明的改动。
格式参考 *Keep a Changelog*，版本号沿用 add-on 的版本号
（`host/addon/esp32c6_gateway/config.json`），因为那是用户在界面上唯一能看到的号。

> 语言约定：说明、注释、文档一律中文（专有名词与 API 名保留原文）。

## [未发布]

### 修复 – 固件（协调器 `main/`）

- **★★★ Wi-Fi 扫不到 AP / 连上就狂掉包的根因：从未注册 Wi‑Fi ↔ 802.15.4 共存仲裁。**
  `CONFIG_ESP_COEX_SW_COEXIST_ENABLE=y` 只是把 `esp_coex` 组件编进固件，并不等于
  把 802.15.4 注册进仲裁器。对照 ESPHome 官方 `zigbee_esp32.cpp` 发现它在
  `esp_zigbee_init()` 之后显式调用 `esp_coex_wifi_i154_enable()`，并在
  `zigbee_esp32.py` 里 `include_builtin_idf_component("esp_coex")`；本项目
  `main/CMakeLists.txt` 的 `REQUIRES` 里没有 `esp_coex`，链接图里该符号出现 0 次。
  本次补上：`REQUIRES` 增加 `esp_coex`，`wifi_begin()` 在 `esp_wifi_init()` 之前调用
  `esp_coex_wifi_i154_enable()`，`net_status` 新增 `"coex"` 回执（`0`=已启用、
  `0x7FFFFFFF`=尚未调用，便于实测确认）。
  **实测（同一块板、同一 AP、未做物理 RST）**：刷写前 `EARLYJOIN ok:0 / why=209`、
  Wi-Fi 完全连不上；刷写后 `wifi_join ok:1 ip=192.168.100.139 rssi=-59 ch=1 why=0`、
  `coex=0`；连续 `ping -n 40` **0% 丢包、平均 85 ms**（此前记录为丢 70% / RTT 3122 ms）。
  ARP 复核 MAC `98-88-e0-71-b8-54` 确认目标就是 C6。
- **`wifi_scan` 会把已关联的 STA 打断且不恢复。** 原实现无条件
  `esp_wifi_disconnect()` 后做 13 信道主动扫描，注释假设「net_task 下一轮会重连」，
  但串口模式 `tr=0` 下 `net_task` 根本没启动 ⇒ 实测 `why=8`（ASSOC_LEAVE）、
  ARP 消失、网关离线。现改为：已关联时只扫当前 AP 信道（短驻留、不断开），
  未关联时保持全信道扫描；回执新增 `"attached"` 说明扫的是哪一种。
- **OTA 后可能回滚。** `serial_ota.c` 用标准 `esp_ota_*`，
  `esp_ota_set_boot_partition()` 会把新镜像标成 `PENDING_VERIFY`；此前未开启
  `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE`，也没有首启确认逻辑。现在开启该配置，
  并在 `app_main()` 末尾（Zigbee 已启动、任务已建）确认健康后调用
  `esp_ota_mark_app_valid_cancel_rollback()`，避免「OTA 成功但重启回旧固件」。

### 变更 – 构建（**IDF 5.5.1 → 5.5.2，根因的最后一块**）

- `esp_coex_wifi_i154_enable()` 在 IDF **5.5.1 里是半成品**：实现只有
  `coex_enable(); coex_schm_status_bit_set(1, 1);`（源码注释还挂着
  `TODO: Add a scheme for wifi and 154 coex`），`libcoexist.a` 里**没有**
  `esp_coex_ieee802154_status_enable` 这个符号，`coexist.c` 也没 include
  `esp_coex_i154.h`。5.5.2 起才改成
  `coex_enable(); esp_coex_ieee802154_status_enable();`。
  6.0.2 与 5.5.2 在共存策略上一致，只差底层时钟/GPIO 初始化写法。
- 这解释了此前「补了调用却仍时好时坏」：5.5.1 上只是设了一个无语义的 scheme
  状态位，仲裁器并不知道要给 802.15.4 留窗口。
- 版本背景：5.5.1 发布于 2025‑08‑30，5.5.6 是 2026‑10‑16，落后 5 个 bugfix
  版本、14 个月；`libphy`/`libnet80211`/`libpp` 三个射频库逐版本都不同
  （`libphy` 182180 → 184020 → 185912 B）。ESPHome 官方 Zigbee 组件要求
  IDF ≥ 5.5.4。
- 选型：升 **5.5.2**（本机已装），不升 6.0（共存能力等价、破坏性变更多、
  ZBOSS 1.6.8 在 6.0 上未验证），不换 `esp-zigbee-lib 2.0.5`（那是换协议栈）。
  `esp-zigbee-lib 1.6.8` 只要求 `idf >= 5.0`，升 5.5.2 在支持范围内。
- **实测（5.5.2 固件，未做物理 RST）**：`coex` 由 `2147483647` 变 **0**；
  `wifi_join ok:1 ip=192.168.100.139 rssi=-57 ch=6 why=0`；`ping -n 40`
  **0% 丢包**（RTT 平均 157 ms）；已关联状态下 `wifi_scan` 返回
  `attached:1 n:1` 且**不再掉线**（此前必 `why=8`）；切 `tr=1` 后 add-on 日志
  出现 `状态：通道=wifi state=tcp_up` 与 `[HB] uptime=… ch=20 pan=0xd13e`，
  TCP 通道成立。
- 换 IDF 版本后 **bootloader 需一起刷**（`0x0`）。本次因沙箱锁导致 bootloader
  子项目配置失败，实际刷的是 5.5.1 bootloader + 5.5.2 app，同 minor 可正常启动
  （`Loaded app from partition at offset 0x20000`，无 panic）。

### 修复 – 构建配置符号

- `sdkconfig.defaults` 里 `CONFIG_ZB_COORDINATOR_ROLE=y` 是**不存在的符号**
  （`main/Kconfig.projbuild` 实际定义 `ZB_ZCZR`），每次构建都报
  `unknown kconfig symbol`，只靠 `default ZB_ZCZR` 侥幸生效。改为 `CONFIG_ZB_ZCZR=y`。
- 删除已废弃的 `CONFIG_ESP_WIFI_SW_COEXIST_ENABLE`（IDF 会提示
  "was replaced with CONFIG_ESP_COEX_SW_COEXIST_ENABLE"，5.5.2 起不存在）。

### 新增 – 构建脚本（并行）

- `build.ps1`（Windows）与重写的 `build.sh`：用
  `cmake --build <dir> --parallel N -- -k 0` 替代无并行参数的 `idf.py build`；
  默认并行 `min(逻辑核数, 8)`，本机 16 核 → 8；并行失败自动 `-j 1` 串行收尾；
  构建后再跑一次要求 no-op，用来验证依赖图完整、没有重复编译。
- 支持 `-IdfPath` / `-BuildDir` 指定 IDF 版本与独立构建目录（本次 5.5.2 用
  `C:/Temp/b552x`，不污染原有 `build/`）。
- 说明：本机实测真正拖慢编译的**不是并行度**，而是三件事：`.ninja_log` 丢失导致
  ninja 判定全部目标重编、多个 ninja 实例抢 `.ninja_lock`、以及 `deps = gcc`
  让 ninja 编译后删除 depfile 时被文件监控拦住（`Access is denied`）。
  全量 1066 目标最终耗时约 86 分钟。

### 变更 – 固件

- `net_status` 回执新增 `"coex":<int>`；`wifi_scan` 回执新增 `"attached":0|1`。
  两者都是新增字段，add-on 侧忽略未知键，无兼容影响。
- 运行日志里残留的德语/英语叙述共 **47 处**改为中文（专有名词、API 名、
  字段名与格式占位符保持原样；`zb_gateway.c` 里作为设备状态值的
  `"offen"/"geschlossen"` 未改动）。门禁 `RESULT: ALL OK (5 Checks)`。

### 新增 – 固件（协调器 `main/`）

- **新命令 `{"cmd":"wifi_join"}`**：真正做一次 `esp_wifi_connect()`（最长等 20 s），
  回 `{"t":"wifi_join","ok":0|1,"conn":"…","ip":"…","rssi":…,"ch":…,"why":…}`。
  由头是 2026-11-06 的教训：**判 Wi-Fi 通不能用"扫"当判据** —— 扫描是
  13 信道各停 120 ms 的主动探测，链路余量最差；连接是单信道长驻留重试。
  边际链路上"扫不到"不等于"连不上"，两个要各测一遍互为印证。
  ★ 照规矩两处登记（`main.c` 分发链 + `ha_mqtt.c` 白名单）。

## [1.5.5] – 2026-11-06

重点：**不插 USB 也能当网关**。C6 可以用 5V 电源头供电，通过家里 Wi-Fi 把同样的
JSON 发给主机。协议一个字没改，只换了"那根线"。

### 新增 – 固件（协调器 `main/`）

- **`main/net_link.c` / `net_link.h`**：Wi-Fi STA + TCP 客户端。内含
  非阻塞 connect（5 s 超时）、1 s 粒度的 `select` 收包循环、指数退避重连
  （2 s → 30 s 封顶）、`SO_SNDTIMEO=200 ms`（`tx_task` 绝不允许被写操作卡住）。
- **`ha_mqtt_handle_line()`**：把原来 `rx_task` 里那一长串 if-else 抽成独立函数，
  串口与 TCP 两个入口共用。这是「两处只改一处」老毛病的结构性预防 ——
  `node_desc` / `fw_ota_data` / `dev_remove` 三次静默死亡都源于此。
- `tx_task` 发送改为 **TCP 优先、串口回退**；回退前必须过
  `usb_serial_jtag_is_connected()`。原因：USB-Serial-JTAG 的 VFS 写路径在
  无主机时是 `timeout = non_blocking ? 0 : portMAX_DELAY`（永久阻塞），
  拔掉 USB 后无脑 `fputs()` 会让整条上报链路静默死掉。
- 新命令 `{"cmd":"net_cfg",…}`（写 NVS 并立即生效）与 `{"cmd":"net_status"}`。
  ★ 按既定规矩**两处都登记**：`main.c::on_uart_cmd` + `ha_mqtt.c` 的
  JSON→payload 分支。二者都列入了免锁白名单（只写 NVS / 只读自有结构体，
  持 ZBOSS 锁做 NVS 写毫无必要）。
  字段约定：空 = 不改，`"-"` = 清空（`net_fld()`）。
- 通道存在 NVS 命名空间 `netlink`，**默认 `tr=0` 即串口** —— 配错了插回 USB
  就能改，而不是变砖。`net_link_start()` 全程不 `ESP_ERROR_CHECK`，
  任何失败都降级回串口并记一条日志。
- 新增主动上报 `{"t":"net","up":…,"ip":…,"rssi":…,"host":…,"port":…}`。
- `sdkconfig.defaults` 增加 Wi-Fi / 共存配置项（`CONFIG_ESP_COEX_SW_COEXIST_ENABLE`
  等），并写明信道规划：Zigbee 25 + Wi-Fi 1/6，**不要用 Zigbee 20**。

### 新增 – Add-on（`host/addon/esp32c6_gateway/`）

- **`CoordinatorTcp`**：TCP 服务端，等 C6 连上来。接口与 `CoordinatorSerial`
  完全一致（`read/write/close/in_waiting/can_reset`），主循环一行未改。
  `in_waiting` 恒返回 0 ⇒ 主循环走 `read(1)` 阻塞路径；`read()` 内部至少
  `recv(4096)`，避免 4 KB 拓扑 dump 被切成 4000 次系统调用。
- **`_link_open` / `_link_reopen` / `_link_kick_reset`**：通道分发。主循环只调
  这三个，所以加通道不再需要动主循环。
- `can_reset`：TCP 通道为 `False`。主循环那条「45 s 没数据就复位芯片」的看门狗
  据此跳过，否则会每 90 s 打一条"强制复位"却什么也没发生，误导排查。
- 新参数 `--link {uart,tcp}`、`--tcp-port`、`--tcp-bind`；`config.json` 同步加
  `link` / `tcp_port` 选项并把 `serial_port` 的 schema 从
  `device(subsystem=tty)` 改成 `str` —— 原选择器在设备不存在时会**直接挡住
  add-on 启动**，拔掉 USB 就打不开，正好废掉本功能的全部意义。
- `{"t":"net"}` / `{"t":"net_status"}` 的 dispatch 分支：打 `[NET] …` 日志并写进
  `_state["net"]`（`/api/state` 可见）。
- `on_message` 新增两条命令：`{BASE}/cmd/net_status` 与 `{BASE}/cmd/net_cfg`。
  后者把 JSON 对象**整包转发**给固件、不在 add-on 侧拆字段 —— 拆了就又变成
  「两处解析」，而固件那边已经在拆。有了这两条，配 Wi-Fi 不需要接串口、
  也不用写终端，在 HA 的「服务 → MQTT: Publish」里发就行。
- `flash_apply()` 在 TCP 模式下**明确拒绝**：esptool 只能走 USB，硬闯只会半途失败；
  提示改用 App 级 OTA。
- 新增回归检查 **`tools/checks/check_link_tcp.py`**（已加入 `run_all.py`）：
  验证通道选择、两个通道的接口一致性、`can_reset` 语义，以及用**真 socket**
  走一遍 accept → 读 → 写 → 断开 → 重新 accept。

### 修正

- **`net_cfg` / `net_status` 一开始只登记在 `main.c`，没登记 `ha_mqtt.c`** ——
  提交后自审才抓到。这正是项目里已经死过三次的那个毛病（`node_desc`、
  `fw_ota_data`、`dev_remove`），老固件会回 `{"t":"error","why":"unknown_cmd"}`。
  已补上，并把两个分支**放在解析链最前面**：链中大量 `strstr()` 没有引号
  （`"topo"`、`"probe"`、`"lqi"`），密码里若含这些字母组合就会被前面那条劫持。
- **`net_task` 可能被创建两次** —— `net_link_start()` 建任务时没置标志，
  `net_link_configure()` 里的 `task_started` 是**函数级** static，两者不共享。
  「开机 tr=1 后再发一条 net_cfg」会造出第二个 net_task，两个任务同时
  `esp_wifi_connect` / `tcp_connect`、抢同一个 socket。改成文件级
  `s_task_started` + 统一的 `net_task_spawn()`。
- **Wi-Fi 参数一次都没下发过** —— `applied` 与 `s_cfg_gen` 初值同为 0，
  「配置变了」判定恒为假 ⇒ `wifi_apply_cfg()` 被跳过 ⇒ `esp_wifi_connect()`
  拿空 SSID 去连，日志只报 AUTH_EXPIRE，看起来像密码错。改法：`applied`
  初值 `0xFFFFFFFF`，并把 apply 挪到 `esp_wifi_start()` **之前**。
- **add-on 在 `link=tcp` 时收不到 `{"t":"boot"}`** —— `ha_mqtt_init()` 里那条
  boot 是在 TCP 还没连上时发的，只能落到串口回退；而 TCP 模式下 add-on
  只读 socket。后果是网关状态不上线、发射功率不会被重新下发（固件不持久化
  它）。现在每次 TCP 连上都会补发一条 boot（add-on 侧处理是幂等的）。
- `announce()` / `net_status` 的日志措辞：串口模式下 `state=uart`、`ip=0.0.0.0`
  却说「通道已连通」，会误导排查。改为照实打印。
- `wifi_begin()` 中途失败重试时不会重复创建 STA netif（单独加了 `s_netif_created`）。
- 清掉一批上一轮漏译的德文（运行日志、注释、测试描述）：`verbinde`、
  `Beendet`、`Abfrage fehlgeschlagen`、`Lese-Fehler`、`gestartet`、
  `abgemeldet`、`FEHLER`、`angefordert` 等，以及 `run.sh`、`build.sh`、
  `host/install.sh`、`test_packaging.py` 里的德文说明。
- `serial_gateway.py` 局部变量 `gehoert_uns` → `owns_it`。
- `config.json` / `sdkconfig.defaults` 行尾统一为 LF（打包检查会拦 CRLF），
  并新增 `.gitattributes` 钉死 add-on 打包文件的行尾。

### 实机验证（2026-11-06）

直刷上机后确认：设备正常启动、Zigbee 网络完整（`ch 20 / pan 0xa311` 不变，
无需重新配网）、`cmd/net_status` 有正确回执、`\x1f` 分字段在密码含逗号时
不串位、NVS 落盘跨重启有效、`"-"` 清空语义有效。
新固件的确凿证据：启动横幅
`I (306) main:   JSON : 串口 USB-Serial-JTAG（默认）/ Wi-Fi+TCP（NVS 里 tr=1）`。

**真正的 Wi-Fi/TCP 数据通路尚未验证** —— 需要真实 SSID 与密码。

### 追加修正（2026-11-06 下午，真实凭据实测后）

拿到真实 SSID/密码后做端到端验证，过程中又抓出一批问题，全部已修：

固件（`main/`）：

- **纯 WPA3 的 AP 连不上**。原来只要有密码就写
  `threshold.authmode = WIFI_AUTH_WPA2_PSK`，遇上 WPA3-个人（SAE）的 AP 不会走
  SAE，表现只是反复 `STA_DISCONNECTED`，没有任何提示。改为
  `WIFI_AUTH_WPA2_WPA3_PSK` + `sae_pwe_h2e = WPA3_SAE_PWE_BOTH`（该 AP 明确
  不支持 H2E）。
- **`sdkconfig.defaults` 补上 WPA3/SAE 三项配置**。`sdkconfig` 是
  `.gitignore` 掉的，只在本地有效 —— 换台机器干净构建就会丢掉 WPA3 支持，
  届时连纯 WPA3 路由器会静默失败。
- **新增 `{"cmd":"wifi_scan"}`**：让 C6 用它自己的射频扫一遍并回
  `{"t":"wifi_scan","n":…,"got":…,"aps":[{"s","r","c","a"}]}`。
  排查"连不上"时主机扫描结果没有参考价值，这个才是事实来源。
- **`net_status` 增加 `why`**（`wifi_err_reason_t` 断开原因码）。没有它，
  密码错(15)、找不到 AP(201)、认证方式不匹配(也是 15) 在回执里长得一模一样。
- **新增 `{"cmd":"log_on"}` / `{"cmd":"log_off"}`**：临时把 `null_vprintf` 换回
  真 `vprintf`，用于看 Wi-Fi 驱动的内部日志。只在 TCP 通道下用（否则日志会
  混进串口 JSON 流）。
- **修 assert 崩溃**：串口模式（`tr=0`）下 `net_task` 不会创建，`s_ev` 是 NULL；
  此时懒加载拉起 Wi-Fi（例如发 `wifi_scan`），`STA_START` 回调里
  `xEventGroupSetBits(NULL,…)` 直接 `assert failed: event_groups.c:561` 并重启 ——
  表现成"命令没有回执"。改为 `net_sync_ensure()` 统一建事件组/锁，回调内也加了
  空指针保护。
- **`state` 字段跟 `transport` 走**：原来只看 `s_wifi_started`，串口模式的设备
  也会回 `state="wifi_only"`，误导排查。
- 诊断命令不再静默失败：`net_link_scan_json()` 任何一步出错都会写一条
  `{"t":"wifi_scan","err":"…"}`，而不是空回执。
- `wifi_begin()` 改为真正幂等：netif 用 `s_netif_created` 单独保证，
  不再因为 `s_wifi_inited` 早退而漏建 STA netif。

Add-on（`serial_gateway.py`）：

- **`[→C6] …` 不再撒谎**。原来这条回显在 `ser.write()` 之前打印，
  通道死掉时照样打出来，看起来"命令已下发"。现在写失败会紧跟一句
  「上面那行 `[→C6]` 实际上没有发出去」。`_NullSerial.write()` 也计入失败。
- **串口重开加候选兜底**：前 3 次试配置端口，全失败后扫 `/dev/ttyACM*`、
  `/dev/ttyUSB*`，打通会提示把 `serial_port` 改成实际路径。
  （`/dev/serial/by-id/…` 是 udev 符号链接，设备重新枚举后会失效甚至变成断链。）
- **重开节流 10 秒**：端口彻底不在时，看门狗每 6 秒就刷三段失败日志，把有用的行
  全淹掉。
- 「端口被刷机占用」措辞改为「另一个重开/刷机流程正占着端口」—— 那把锁也会被
  另一个正在退避的重开流程占着，原文案会把排查方向带偏到 esptool。
- 新增 `/api/wifi_scan` 与 MQTT `cmd/wifi_scan`，回执打印成 `[SCAN]` 并进
  `/api/state`。
- `config.json` 增加 **`"uart": true`** —— `serial_port` 从 `device(subsystem=tty)`
  选择器改成普通字符串后，Supervisor 不再自动注入串口设备，容器里
  `/dev/ttyACM*` 和 `/dev/serial/by-id/*` 全都不存在。`uart: true` 无条件映射，
  且设备不在时也不阻塞启动。
- 三处德语残留（`lese vorhandene`、`LQI-Momentaufnahme angefordert`、
  `[FLASH] angefordert`）改中文。

新增诊断工具：

- `tools/c6_serial_cmd.py` —— 经 USB 串口给 C6 下发一条 JSON 并打印回执。
- `tools/c6_flash_and_capture.py` —— 刷写后**立刻**接管串口抓开机日志。
  板子的 USB 是 C6 自带 USB-Serial-JTAG，RTS 没接 EN，"打开串口拉 RTS 复位"
  无效；唯一可靠的复位点是 esptool 结束时那次 hard_reset，所以刷写与抓取
  必须在同一个进程里完成。
- `tools/tcp_probe.py` —— 本机当假网关：12345 等 C6 连入，12346 当遥控口。

**结论：本次未通过 —— 阻塞点是硬件。** 同一块板子上跑 IDF 官方
`examples/wifi/scan` 也是 `Total APs scanned = 0`（2.5 s 真扫、无 Zigbee），
说明该板 Wi-Fi 接收不工作（Zigbee 一切正常）。详见
`Wi-Fi通道-测试报告-2026-11-06.md`。

### 暂未做（下一步）

- **`CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE` 仍是关闭的。** 没有 USB 时，一份连不上
  Wi-Fi 的固件就是砖。上生产前必须打开它并实现首启自检（Wi-Fi + TCP 都通了才
  `mark_valid`）。本次**有意**不动：这个开关一旦打开，应用侧不同步实现
  `mark_valid` 会导致每次 OTA 后都回滚。
- 日志通道：拔掉 USB 后 `ESP_LOGI` 无人接收。需要时可把 console 挪到
  UART0（GPIO16/17）接 USB-TTL。

详见 `WiFi传输通道.md`。

## [1.5.4] – 2026-11-05

重点：**让设备真的能被删掉**。已经搬到别的 Zigbee 网络的节点，以前会一直作为
僵尸残留在网页、MQTT（retained）和 Home Assistant 里。

### 新增 – 固件（协调器 `main/`）

- **`zb_gateway_remove_device()`**（`zb_gateway.c`）+ 串口命令
  `{"cmd":"dev_remove","addr":"0x…"}`：从设备表**和** NVS 副本（`devtab_save()`）
  里删掉该条目，随后另起任务补发一次 ZDO `Mgmt_Leave_req`（`rejoin = 0`，仅在
  IEEE 已知时发送）。回执：
  `{"t":"dev_removed","addr":…,"ieee":…,"found":…,"leave":…}`。
- 该命令必须登记在**两处**——`main.c`（命令分发）和 `ha_mqtt.c`（JSON→payload）。
  少一处，协调器就回 `{"t":"error","why":"unknown_cmd"}`；本次第一次测试正是如此，
  尽管函数早已编译进固件。
- 边界（有意如此并写明）：ZBOSS 自己的邻居/路由表不会因此清理——
  `esp-zigbee-lib` 没有对应 API；死掉的邻居会由 link-status 老化自行淘汰。

### 新增 – Add-on（`host/addon/esp32c6_gateway/`）

- **`_device_remove()`**：一个“设备彻底不在了”的出口。删除 retained 状态话题、
  HA Discovery 配置（session 内发布过的 `_sent_addr` **和** 启动时从 broker 读到的
  镜像 `_disc_remote` 两处都查）、地址/IEEE 映射、`_noread`/`_ctrl`、可用性记分板
  和 retained 的 `dev/<addr>/status`；然后 `_nodes_save()`，最后才向协调器发
  `dev_remove`。
- `POST /api/device/<addr>/delete` 改用这条路径（以前只做
  `_state["devices"].pop()` + 删 DB 行——60 秒后设备就回来了，因为心跳会把整份
  设备表重新发下来，而 `t=="lqi"` 分支用 `setdefault` 把缺的地址又建了回来）。
- 新增消息 `{"t":"dev_removed"}`：确认删除，并在本地仍有残留时补清理。
- 设备页按钮改名为「从网关彻底移除（含 HA 实体）」，确认框里写明四项影响；
  删除了下拉里那个 `leave` 选项（固件从来就不认识它，纯属摆设）。

## [1.5.3] – 2026-10-26

第一个打标签的版本。重点：**让网络拓扑可见、让 OTA 下载可靠、让串口链路扛得住
一个已经死掉的 USB-CDC 文件描述符**。

技术栈：ESP-IDF 5.5.1 · `esp-zigbee-lib` 1.6.8 · `esp-zboss-lib` 1.6.4
设备容量：64 个节点 · OTA image type `0x0001`（终端节点）/ `0x0002`（中继），
厂商代码 `0x131B`

### 新增 – 固件（协调器 `main/`）

- **`zb_topo.c` / `zb_topo.h`**：通过逐节点的 ZDO `Mgmt_Lqi_Req`（`0x0031`）取真实
  Mesh 拓扑，含分页续取和 15 s 看门狗。输出 `{"t":"topo_mesh",…}`。要点：参数
  **必须**用 `ZB_BUF_GET_PARAM()` 放进缓冲区——`zb_buf_begin()` 不增加缓冲长度，
  ZBOSS 于是把请求发给自己（`0x0000`），每台设备都会回报“协调器自己的邻居”。
- **`zb_ota_server.c`**：OTA 服务端重写。四个让下载彻底不可用的错误已修：
  1. `ota_data` 改用 `json_val()` 解析（JSON 冒号后有空格；写死的
     `strstr("\"data\":\"")` 会**静默丢弃每一块**）。
  2. 从 Image Block Request（ASDU）里读出**客户端的文件偏移**并告知回调。SDK 回调
     不给 offset；盲目自增的服务端游标在协调器重启后会把整份镜像整体错位——这就是
     以前“跑到 99.4% 却版本不生效”的原因。
  3. `notify_req` 之前**预填 2 KB 窗口**。第一个块请求落空会让 SDK 直接中断会话
     （不重试）。
  4. 统计（`hit / miss / to / bad / served / sync`）外加 `ota_stop`。
- **`zb_gateway.c`**：LQI/RSSI 仅在**有效值**时才覆盖（`ind.lqi != 0`）；新增字段
  `lqia`（距上次有效测量的秒数，`0xFFFFFFFF` = 从未）。以前只要来一帧没带 LQI 的
  报文，所有设备的值就被清零。
- **`main.c`**：对来自外来任务的**每一次** SDK 调用都加 `zb_lock()` / `zb_unlock()`
  （`uart_rx`、`btn`、`heartbeat`、`scan`）。`port_uxCriticalNesting` 是每核全局的，
  不是每任务的——未加锁的 `esp_zb_*` 调用会导致
  `assert failed: vPortExitCritical … (port_uxCriticalNesting[0] > 0)` 和 `rst:0xc`。
  只有纯 Flash 路径（`fw_ota_*`、`ota_data`）例外。
  `heartbeat_task` 里的缓冲区改为 `static`（4 KB 任务栈放不下）。
- **`ha_mqtt.c`**：`emit()` 不再在 ZBOSS 任务里阻塞写串口。整行 → 堆块 → 16 深度
  队列 → 独立 `tx_task`。以前一次 4 KB 的拓扑 dump 会阻塞约 1.4 s，把 MAC 层拖进
  `Guru Meditation (Load access fault)` 并随后重启。
- 设备发射功率可写（`set_dev_power`，Basic `0x0016`）。

### 新增 – 中继器（`router/`）

- **`zb_ota_client.c` / `zb_ota_client.h`**：OTA 客户端，image type `0x0002`，
  `max_data_size = 223`，版本存 NVS，`esp_ota_set_boot_partition()` + 重启。
- **厂商簇 `0xFC00`**：`0x0001` = 发射功率（S8，dBm，**NVS 持久化**），
  `0x0002` = 自报的父节点。
- **BOOT 键短按（<1 s）**循环切换功率 8 → 14 → 20 → 8 dBm 并保存。

### 新增 – Add-on（`host/addon/esp32c6_gateway/`，v1.5.3）

- **串口自愈**：容器重启后旧的 USB-CDC 文件描述符会永久返回 `[Errno 5] I/O error`，
  在同一个 fd 上重试**永远不会**恢复。新增 `_NullSerial`、带重试的 `_serial_open()`、
  带退避的 `_serial_reopen()`、`_serial_kick_reset()`（只拉 RTS，**绝不**碰 DTR——
  DTR 是 strapping 引脚）、吞掉写错误后的补写，以及 45 s 看门狗。
  另加 `POST /api/serial_reopen`。
- **设备级可用性**：每台设备有自己的 `{base}/dev/<addr>/status` 话题，Discovery 用
  `availability_mode: "all"`。以前掉线的开关显示“关”，因为只看网关状态。判定用记分
  板（APS 帧、成功的 `node_desc` 探测、邻居表条目）；一台设备只有在“历史上有实体、
  本次启动后毫无音讯、**且**不在新鲜的邻居表里”时才算消失（启动后 5 分钟宽限）。
- **Discovery 迁移**：`_disc_upgrade_avail()` 在启动时给已存在的 retained 配置补上
  第二个可用性来源。否则升级后再没发过消息的设备会永远保留旧配置。
- **拓扑自动采集**（`topo_interval`，默认 60 s）和拓扑页 **15 秒自动刷新**；
  轮次计数、“本轮采集完成于”时间戳，以及稳定的 Mesh 数据（设备不再因为应答晚到而
  短暂消失）。
- **设备列表来自邻居表和裸 APS 帧**——纯按键面板（从不会发 ZCL Report）现在才第一次
  出现在界面上。
- **发射功率会被记住**（`/data/gw_txpower.json`），并在每次协调器重启后重新下发，
  直到回读值确认。固件**不**把它写进 NVS；没有这一步，断电后网关会静默地以默认功率
  运行（从 `lqi`/`rssi` 看不出来，因为它们反映的是反方向）。
- 设备页：每台设备的“最后上报”，LQI 带年龄标注。
- `node_desc` 丢弃广播地址（`0xfffc`–`0xffff`）；以前 `0xffff` 会生成一条永久的
  “设备离线”错误记录。
- `config.json` / `run.sh` 新增选项 `topo_interval`。

### 新增 – 测试与工具

- `tools/checks/run_all.py` —— 一条命令跑全部检查：设备可用性/Discovery/发射功率、
  拓扑轮次、串口自愈、Web UI 渲染 + i18n。
- `tools/ota_watch.py` —— Zigbee OTA 的速率/停顿监视。
- `tools/mq_dump.py` —— 导出 retained 的 Discovery 配置和可用性话题。

### 变更

- 预编译镜像 `firmware/zigbee-vital-sensor-sdk16.bin`（663 616 B）和
  `firmware/zigbee-repeater.bin`（613 088 B）现在与源码状态完全一致（已与构建产物
  逐字节比对）。
- `README.md` 和 `DEVICE_INTEGRATION_REPORT.md` 补充了拓扑、OTA 与功率测量；
  `OTA-调试记录-2026-10-26.md` 详细记录了那四个 OTA 服务端错误。

### 修复 – 现场实测

- 中继在其**安装位置**做 OTA（LQI 40–51、RSSI −71 dBm）：613 088 B / **772 s**，
  ~920 B/s，**0 次停顿**，服务端 `hit=2751 miss=0 to=0 bad=0`。据此可以断定：以前
  掉到 115 B/s 不是信号问题，而是那四个 OTA 服务端错误。升级**不需要**把中继搬到
  协调器旁边。
- 吞吐上限是结构性的：每块 223 B 超过 802.15.4 单帧的 APS 净荷（约 82 B），即拆成
  3 个分片各自 ACK ⇒ 约 4.4 块/秒 ≈ 950 B/s，与链路质量无关。

### 已知限制

- 对于不回应 `Mgmt_Lqi_Req` 的设备（如 Feibit 开关，回 `0x84 NOT_SUPPORTED`），以及
  ZBOSS 一律把 `relationship` 填 2、`depth` 填 0 的 ESP32-C6 中继：父子关系只能由
  “协调器邻居表 + 设备自报”构成的**无向边图**通过 BFS 推导。
- 想再提速只能把块调小（82/160 B）；这需要 USB 刷中继，收益未经验证。
- `dev_cmd`/`set_dev_power` 只对提供了可写 Basic `0x0016` 的设备有效；中继把自己的
  功率存在自己的 NVS 里。
