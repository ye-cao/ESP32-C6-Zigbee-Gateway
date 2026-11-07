#pragma once
#include <stdint.h>
#include <stdbool.h>

/* 最大设备数 = 网络规模。这个值同时决定 ZBOSS 的表大小
 * （esp_zb_overall_network_size_set）和我们自己的设备库。
 * main.c 里的缓冲区按它算尺寸——不要单独调大。 */
#define ZB_GATEWAY_MAX_DEVICES 64

/* 启动 Zigbee 协调器 */
void zb_gateway_start(void);

/* ★ 2026-10-25：esp_zb_start() 走完才为 true。在此之前任何 ZBOSS 调用
 * （esp_zb_scheduler_alarm、zcl_*_req、bdb_*）都是空指针解引用——而
 * uart_rx 任务此时已经跑起来了。详见 main.c/on_uart_cmd 里的注释。 */
bool zb_gateway_is_ready(void);

/* 打开 N 秒的入网窗口（0 = 立即关闭） */
void zb_gateway_permit_join(uint8_t seconds);

/* 把设备表写进日志（调试用） */
void zb_gateway_list_devices(void);

/* 把设备表写成 JSON 数组放进 buf（线程安全） */
void zb_gateway_devices_json(char *buf, size_t len);

/* 设备彻底移除：从 RAM 表 + NVS 副本里删掉，并向设备补发一次 ZDO
 * Mgmt_Leave（IEEE 已知时才发，失败不影响删除）。
 * 结果：{"t":"dev_removed","addr":"0x…","ieee":"…","found":bool,"leave":bool}
 *
 * 必须在固件侧删，不能只在 add-on 删：每 60 s 的心跳会把本表整份发给
 * add-on，add-on 的 `t=="lqi"` 处理会用 setdefault 把缺的补回来 ——
 * 只删 add-on 的话，网页上删掉的设备 60 秒后自己回来。 */
void zb_gateway_remove_device(uint16_t addr);

/* 设置 Zigbee 信道（11-26），存入 NVS 并重启。
 * 默认 20，失败回退 25（下次重启生效）。 */
void zb_gateway_set_channel(uint8_t ch);

/* 扫全部信道 11-26（每信道约 4 秒），结果以 JSON 输出 */
void zb_gateway_scan_channels(void);


/* 把已知设备的 LQI 写成 JSON 数组放进 buf */
void zb_gateway_lqi_json(char *buf, size_t len);
int8_t zb_gateway_get_last_rssi(void);

/* 把所有已知短地址（不含 0x0000）拷进 out[]，最多 max 个。
 * 返回个数。用于拓扑采集（对每台设备发 Mgmt_Lqi）——
 * 只有真正通信过的设备才会应答。 */
uint8_t zb_gateway_known_addrs(uint16_t *out, uint8_t max);

/* 设备控制：向某个端点发 On/Off 命令
 * on_off_cmd: 0=关 1=开 2=切换 */
void zb_gateway_dev_cmd(uint16_t addr, uint8_t ep, uint8_t on_off_cmd);

/* 中继发射功率：写厂商专有簇 0xFC00 的属性 0x0001（S8，dBm）。
 * 中继固件里这个属性是「写入即生效 + 落 NVS」，所以调功率不用重刷固件。
 * 结果回一行 {"t":"wr_power","addr":"0x…","ep":N,"dbm":V,"tsn":T}。 */
void zb_gateway_write_tx_power(uint16_t addr, uint8_t ep, int8_t dbm);

/* 读设备的 Basic 簇（厂商/型号）——针对已入网但从不吭声、因而从未被
 * 面试过的设备。结果以 {"t":"zigbee","sub":"basic"} 回传。 */
void zb_gateway_probe_basic(uint16_t addr, uint8_t ep);

/* 读节点描述符（ZDO 0x0002）：判断设备是作为 Router（ZR）工作
 * ——只有 Router 会转发别人的流量。结果：
 * {"t":"node_desc","addr":"0x…","cap":N,"role":"router|end_device",
 *  "power":"mains|battery","rx_idle":true|false} */
void zb_gateway_node_desc(uint16_t addr);

/* 手动重跑一次设备采访（Active-EP → Simple-Desc → Bind → 配置上报）。
 * 平时只在入网时自动跑一次；设备出问题后想复核它到底注册了哪些簇时用这个。
 * 典型用途：确认某台设备是不是 mfr=Espressif / model=ESP32-C6-Repeater 的中继器。 */
void zb_gateway_interview(uint16_t addr);

/* 主动回读 On/Off 状态（对 0x0006/0x0000 发 Read Attributes）。
 * 有些设备（如 HGZB-043）对远程命令只回 Default Response、不发 Report，
 * 需要这样回读，HA 才能确认真实状态而不是只拿到网关的乐观假设。
 * 结果：{"t":"zigbee","sub":"epN/on_off","p":{"v":0|1}} */
void zb_gateway_read_onoff(uint16_t addr, uint8_t ep);

/* 向所有已配对设备发 switch2wifi 命令 */
void zb_gateway_send_switch2wifi_all(void);
void zb_gateway_send_set_sleep(uint32_t seconds);

/* 运行期设置发射功率，并回读**实际生效**的值。
 * 结果：{"t":"txpower","want":N,"eff":M}
 *
 * 为什么只认回读值：esp_zb_set_tx_power() 接收愿望值，但会按 PHY 支持的
 * 范围内部截断。只有 esp_ieee802154_get_txpower() 才说明 PA 真正跑在多少。 */
void   zb_gateway_set_tx_power(int8_t dbm);
int8_t zb_gateway_get_tx_power(void);

/* 立刻输出所有已知设备的 LQI/RSSI（不必等 60 s 心跳）。
 * 结果：{"t":"lqi","ms":<uptime_ms>,"dev":[{"addr":"0x0684","lqi":220,"rssi":-58},…]}
 *
 * 这个值对应的是该设备**最后收到的那一帧**——不是滑动平均。要比两种发射
 * 功率，必须先触发一次读取再取快照，否则比的是几分钟前的数据。 */
void zb_gateway_report_lqi(void);
