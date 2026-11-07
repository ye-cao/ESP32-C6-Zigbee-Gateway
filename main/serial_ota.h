/*
 * serial_ota.h —— App 级在线升级（esp_ota），走当前 JSON 通道（串口或 Wi-Fi/TCP）
 *
 * 协议 主机→C6（JSON-Lines，与 Zigbee OTA 同一思路）：
 *   {"cmd":"fw_ota_start","size":<N>}              → esp_ota_begin()
 *   {"cmd":"fw_ota_data","off":<O>,"data":"<HEX>"} → esp_ota_write()
 *   {"cmd":"fw_ota_end"}                           → esp_ota_end()+切启动分区+重启
 * C6→主机 回执：{"t":"fw_ota","st":"begin|write|done|error",...}
 *
 * ★ 这条路不经 esptool，所以 Wi-Fi 通道下也能用；与之相对的
 *   bootloader 直刷（host 侧 flash_apply）必须插 USB。
 * ★ 会话断流超过 120 s 后，新的 fw_ota_start 会直接接管旧会话，
 *   不需要重启 C6。
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
