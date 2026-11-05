#pragma once
#include <stdint.h>
#include "esp_err.h"
#include "esp_zigbee_core.h"

/*
 * zb_ota_server – Zigbee OTA Upgrade Server (Coordinator-Seite)
 *
 * Protokoll Pi→C6: {"cmd":"ota_start","size":<N>,"ver":<V>,"type":<T>}
 *                  {"cmd":"ota_data","off":<O>,"data":"<HEX>"}
 * Protokoll C6→Pi: {"t":"ota_req","off":<O>,"sz":<S>}
 *                  {"t":"ota_status","status":"started"|"error","size":<N>}
 *                  {"t":"ota_srv_status","addr":"0x<A>","status":"started"|"aborted"|"done"}
 *
 * ★ image type 必须跟目标设备的 OTA client 一致，否则设备根本不会来问：
 *     0x0001 = 终端节点（client/，switch2wifi）
 *     0x0002 = 中继器（router/，纯 Router）
 *   不带 type 时默认 0x0001（向后兼容老的 host 端）。
 */

/* 已定义的 image type */
#define OTA_IMAGE_TYPE_ENDDEVICE  0x0001
#define OTA_IMAGE_TYPE_REPEATER   0x0002

/* 创建 OTA 服务端的属性簇（SERVER_ROLE） */
esp_zb_attribute_list_t *zb_ota_server_cluster_create(void);

/* 收到树莓派的 ota_start 命令后开始一个 OTA 会话。
 * image_type 决定这次给哪一类设备推固件（见上面的常量）。 */
void zb_ota_server_start(uint32_t size, uint32_t version, uint16_t image_type);

/* 送入一块数据：payload = "OFF,HEX"。
 * ★ 带 OFF 是为了把这块放到窗口里正确的位置（以前是无脑追加 ⇒ 重传时整体错位）。
 * ★ 参数用 uint16_t 而不是 uint8_t：一次 ota_req 要取 OTA_FETCH=1024 字节
 *   （= 2048 个十六进制字符），用 uint8_t 到 255 就断了。 */
void zb_ota_server_feed_data(const char *payload, uint16_t len);

/* 结束会话（客户端完成或中止），并清空窗口。 */
void zb_ota_server_stop(void);

/* ★ Der OTA-Callback der SDK liefert KEINEN Datei-Offset (`index` = 镜像表
 *   序号，恒 0)，只有客户端自己发的 Image Block Request 里有。
 *   所以由 aps_data_ind_cb 把客户端报的 fileOffset 送进来，服务端据此
 *   校正自己的读指针 —— 否则协调器在传输中途重启一次，两边就永久错位，
 *   客户端把错位数据写进 flash，整包作废（2026-10-26 实测踩过）。 */
void zb_ota_server_note_offset(uint32_t off);
