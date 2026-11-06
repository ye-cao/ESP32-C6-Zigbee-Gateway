#pragma once
/*
 * net_link.h —— 可选的 Wi-Fi/TCP 传输通道
 *
 * 目的：让 C6 不必插在 HA 主机的 USB 口上。协议本身一个字都不动 ——
 * 依旧是「一行一条 JSON」，只是这根「线」从 UART/USB-CDC 换成了一条 TCP 连接。
 *
 * 两条通道**同时存在**，由 NVS 里的一项决定当前走哪条：
 *   0 = 串口（默认，和没这功能时完全一样）
 *   1 = Wi-Fi/TCP
 * 默认给 0，所以刷了新固件但没配置过 Wi-Fi 的板子行为不变，
 * 万一配置写错了也还能靠 USB 把配置改回来。
 */
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 传输通道取值 */
#define NET_LINK_UART   0   /* USB-Serial-JTAG / UART（原来的行为） */
#define NET_LINK_TCP    1   /* Wi-Fi STA + TCP 客户端 */

/* 读取 NVS 配置；若通道为 TCP 则拉起 Wi-Fi 与 TCP 任务。
 * ★ 不会因为配置缺失/初始化失败而 abort —— 一律降级回串口并记一条日志。
 *   拔掉 USB 之后 abort 一次就是一块砖（复位也回不来）。 */
esp_err_t net_link_start(void);

/* TCP 是否已经连上（可以往里写） */
bool net_link_ready(void);

/* 当前通道（NET_LINK_UART / NET_LINK_TCP） */
uint8_t net_link_transport(void);

/* 写一行（自动补 '\n'）。未连接时返回 ESP_ERR_INVALID_STATE，绝不阻塞等待。 */
esp_err_t net_link_send_line(const char *line);

/* 写配置：存 NVS 并立即生效（Wi-Fi 已启动时先断开再按新参数重连）。
 * pass / host 允许传 NULL 表示「不改这一项」。 */
esp_err_t net_link_configure(const char *ssid, const char *pass,
                             const char *host, int port, int transport);

/* 把当前状态写成一行 JSON（含 ssid/host/port/ip/rssi/是否连上），
 * 供 {"cmd":"net_status"} 回执与故障排查用。 */
void net_link_status_json(char *out, size_t out_len);

/* 让 C6 自己扫一遍 2.4 GHz，把看见的 AP 列表拼成一行 JSON。
 * 诊断用：连不上 Wi-Fi 时，这个才是"以固件的眼睛看世界"的事实。
 * 会短暂占用射频，别放进轮询。 */
esp_err_t net_link_scan_json(char *out, size_t out_len);

/* 真正做一次「连上现有 SSID」的尝试（最长等 20 s），回 {"t":"wifi_join",…}。
 * 诊断用：判 Wi-Fi 到底通不通，必须用"连"而不是"扫" —— 扫描每个信道只停
 * 120 ms，边际链路上会全灭，而 connect 会守在目标信道上反复重试。 */
esp_err_t net_link_join_json(char *out, size_t out_len);

/* 开机早期扫频：必须在 esp_zb_start() 之前调用。
 * 用来把"Wi-Fi 能不能收到信号"和"Zigbee 是不是占住了射频"分开判断。
 * 回执类型是 {"t":"wifi_bootscan",…}，字段与 wifi_scan 相同。 */
esp_err_t net_link_scan_early(char *out, size_t out_len);

/* 主动踢掉现有 TCP 连接，让 net_task 尽快重连（改配置后用）。 */
void net_link_kick(void);

#ifdef __cplusplus
}
#endif
