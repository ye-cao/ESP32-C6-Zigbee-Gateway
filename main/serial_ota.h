/*
 * serial_ota.h —— 通过 USB 串口给协调器刷固件
 *
 * 协议 Pi→C6（JSON-Lines，与 Zigbee OTA 同一思路）：
 *   {"cmd":"fw_ota_start","size":<N>}              → esp_ota_begin()
 *   {"cmd":"fw_ota_data","off":<O>,"data":"<HEX>"} → esp_ota_write()
 *   {"cmd":"fw_ota_end"}                           → esp_ota_end()+切启动分区+重启
 * C6→Pi 回执：{"t":"fw_ota","st":"begin|write|done|error",...}
 */
#pragma once
#include <stdint.h>

/* 开始一次 OTA 会话（image_size 用于收尾时的完整性校验）。 */
void serial_ota_start(uint32_t size);

/* 写入一个数据块；off = 文件偏移（必须与已写进度一致），
 * hex = 有效载荷的十六进制字符串，hex_len = 该字符串的字符数。 */
void serial_ota_data(uint32_t off, const char *hex, int hex_len);

/* 结束会话：校验、切换启动分区并重启。 */
void serial_ota_end(void);

/* 中止会话（释放句柄，不切换启动分区）。 */
void serial_ota_abort(void);
