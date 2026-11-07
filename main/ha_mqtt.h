#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

/*
 * ha_mqtt – JSON-Lines 收发层（串口 / Wi-Fi 共用）
 *
 * C6 → 主机：{"t":"boot"}
 *            {"t":"zigbee","addr":<uint>,"sub":"<name>","p":{...}}
 *            {"t":"permit_join","p":{"open":true/false,"seconds":<n>}}
 *            {"t":"heartbeat","uptime":<s>,"ch":<n>,"pan":"0x<hex>"}
 *            {"t":"net","up":<bool>,"ip":"<ip>","rssi":<dbm>,...}
 * 主机 → C6：{"cmd":"permit_join","sec":<n>}
 *            {"cmd":"set_channel","ch":<11-26>}
 *            {"cmd":"net_cfg","ssid":"…","pass":"…","host":"<ip>","port":<n>,"tr":1}
 */

typedef void (*mqtt_cmd_cb_t)(const char *cmd, const char *payload, int len);

esp_err_t ha_mqtt_init(const char *broker_uri,
                       const char *username,
                       const char *password);

void ha_mqtt_set_cmd_cb(mqtt_cmd_cb_t cb);

/* 处理**一整行**已去掉换行的 JSON（就地改写 buf）。
 * ★ 串口（rx_task）和 Wi-Fi（net_link.c 收包循环）都调这里 ——
 *   命令解析只允许存在一份，否则「两处只改一处」的老毛病会立刻回来。 */
void ha_mqtt_handle_line(char *buf);

/* ── 通道跟随（2026-11-07）───────────────────────────────────────────────
 * 两条通道同时在线，但**只往 add-on 实际挂着的那条发**：
 * 哪个通道最近收到过命令，就往哪个发；两边都没动静（或开机还没收到过）
 * 才退回双发。这样 add-on 用 --link serial 还是 tcp 都不需要改 C6 任何配置，
 * 切换后最多一个心跳周期（5 s）就自动跟上。
 *
 * 每个接收点解析出一整行之前调一次 note_link，标注这行是从哪来的：
 *   · net_link.c 的 TCP 收包循环 → NET_LINK_TCP
 *   · ha_mqtt.c 的 rx_task（USB-Serial-JTAG）→ NET_LINK_UART
 * 注意这些值要和 net_link.h 的 NET_LINK_* 一致（0/1）。 */
void ha_mqtt_note_link(uint8_t link);

void ha_mqtt_publish_zigbee(uint16_t addr,
                             const char *subtopic,
                             const char *payload);
/* 同上，但可指定来源端点（多通道设备 / 多联开关）。
 * endpoint <= 1：Topic 保持 "<subtopic>" 不变
 * endpoint  > 1：Topic 变成 "<epN>/<subtopic>"（例如 "ep2/on_off"） */
void ha_mqtt_publish_zigbee_ep(uint16_t addr,
                               uint8_t endpoint,
                               const char *subtopic,
                               const char *payload);
void ha_mqtt_publish_permit_join(bool open, uint8_t seconds);
void ha_mqtt_publish_heartbeat(uint32_t uptime_s, uint8_t channel, uint16_t pan_id);

bool        ha_mqtt_connected(void);
const char *ha_mqtt_base_topic(void);
void        ha_mqtt_logf(const char *tag, const char *fmt, ...)
    __attribute__((format(printf, 2, 3)));

/* 直接吐出一行原始 JSON（用于扫描结果） */
void ha_mqtt_emit_raw(const char *line);

/* 临时打开/关闭 IDF 自身的日志输出。
 * 默认是关闭的（见 ha_mqtt.c 的 null_vprintf）—— 关掉才能保证串口上只有
 * JSON 行。排查 Wi-Fi/ZBOSS 驱动内部问题时用 log_on 打开，害处是日志会
 * 混进串口数据流，所以只在 TCP 通道下用，用完记得 log_off。 */
void ha_mqtt_log_verbose(bool on);
