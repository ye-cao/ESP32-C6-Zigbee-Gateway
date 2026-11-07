/*
 * serial_ota.c —— App 级在线升级（esp_ota），走当前 JSON 通道
 *
 * 主机侧（serial_gateway.py）持有镜像文件，按块经当前通道推下来；C6 用
 * esp_ota 写进下一个 OTA 分区（从 ota_0 启动时即 ota_1），写完后切换
 * 启动分区。通道可以是 USB 串口，也可以是 Wi-Fi/TCP —— 这条路不需要
 * esptool，所以不需要物理接触。
 *
 * ★ 与 flash_apply()（bootloader 级直刷）的区别：那条路必须插 USB，
 *   TCP 通道下会被 add-on 直接拒绝。
 */
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_timer.h"
#include "ha_mqtt.h"
#include "serial_ota.h"

#define TAG          "serial_ota"
#define FW_MAX_CHUNK 1024          /* 每帧解码后的字节数 */
#define FW_ACK_EVERY 16            /* 每 N 块回报一次状态（流控） */
/* ★ 断流判定：主机若中途消失（TCP 掉线、add-on 重启），s_active 会永远
 *   挂着，之后所有 fw_ota_start 都被回 busy —— 只能重启 C6 才能再刷。
 *   这里不看定时器，只在「新请求到来」时顺手检查：超过这么久没收到数据，
 *   就当作上一个会话已死，直接接管。 */
#define FW_STALE_US  (120LL * 1000 * 1000)

static esp_ota_handle_t   s_handle;
static const esp_partition_t *s_part;
static bool               s_active;
static uint32_t           s_size;
static uint32_t           s_written;
static uint32_t           s_chunks;
static int64_t            s_last_us;   /* 最后一次成功写入的时刻 */

static void fw_emit(const char *st, const char *extra)
{
    char line[160];
    if (extra) {
        snprintf(line, sizeof(line),
                 "{\"t\":\"fw_ota\",\"st\":\"%s\",%s}", st, extra);
    } else {
        snprintf(line, sizeof(line),
                 "{\"t\":\"fw_ota\",\"st\":\"%s\"}", st);
    }
    ha_mqtt_emit_raw(line);
}

void serial_ota_start(uint32_t size)
{
    if (s_active) {
        int64_t idle_us = esp_timer_get_time() - s_last_us;
        if (idle_us <= FW_STALE_US) {
            fw_emit("error", "\"why\":\"busy\"");
            return;
        }
        /* 上一个会话已经断流（TCP 掉线 / 主机重启），直接接管 */
        ESP_LOGW(TAG, "上一会话已断流 %lld s，丢弃并重新开始",
                 (long long)(idle_us / 1000000));
        esp_ota_abort(s_handle);
        s_active = false;
    }
    if (size < 0x10000) {           /* 小于 64 KB 不可能是 App 镜像 */
        fw_emit("error", "\"why\":\"size\"");
        return;
    }

    s_part = esp_ota_get_next_update_partition(NULL);
    if (!s_part) {
        fw_emit("error", "\"why\":\"no_partition\"");
        return;
    }

    esp_err_t err = esp_ota_begin(s_part, size, &s_handle);
    if (err != ESP_OK) {
        fw_emit("error", "\"why\":\"begin\"");
        return;
    }

    s_active = true;
    s_size   = size;
    s_written = 0;
    s_chunks = 0;
    s_last_us = esp_timer_get_time();

    char extra[96];
    snprintf(extra, sizeof(extra),
             "\"part\":\"%s\",\"size\":%lu",
             s_part->label, (unsigned long)size);
    fw_emit("begin", extra);
    ESP_LOGI(TAG, "开始 OTA → %s size=%lu", s_part->label, (unsigned long)size);
}

void serial_ota_data(uint32_t off, const char *hex, int hex_len)
{
    if (!s_active) {
        fw_emit("error", "\"why\":\"not_active\"");
        return;
    }
    if (off != s_written) {
        /* 只允许顺序写 —— 这样错误处理最简单 */
        fw_emit("error", "\"why\":\"offset\"");
        return;
    }

    static uint8_t buf[FW_MAX_CHUNK];
    size_t n = (size_t)hex_len / 2;
    if (n > FW_MAX_CHUNK) n = FW_MAX_CHUNK;

    for (size_t i = 0; i < n; i++) {
        char h[3] = { hex[i * 2], hex[i * 2 + 1], '\0' };
        buf[i] = (uint8_t)strtol(h, NULL, 16);
    }

    esp_err_t err = esp_ota_write(s_handle, buf, n);
    if (err != ESP_OK) {
        serial_ota_abort();
        fw_emit("error", "\"why\":\"write\"");
        return;
    }

    s_written += n;
    s_chunks++;
    s_last_us = esp_timer_get_time();

    if ((s_chunks % FW_ACK_EVERY) == 0) {
        char extra[80];
        uint32_t pct = s_size ? (s_written * 100 / s_size) : 0;
        snprintf(extra, sizeof(extra),
                 "\"off\":%lu,\"pct\":%lu",
                 (unsigned long)s_written, (unsigned long)pct);
        fw_emit("write", extra);
    }
}

void serial_ota_end(void)
{
    if (!s_active) {
        fw_emit("error", "\"why\":\"not_active\"");
        return;
    }

    esp_err_t err = esp_ota_end(s_handle);
    if (err != ESP_OK) {
        serial_ota_abort();
        fw_emit("error", "\"why\":\"end\"");
        return;
    }

    err = esp_ota_set_boot_partition(s_part);
    s_active = false;
    if (err != ESP_OK) {
        fw_emit("error", "\"why\":\"set_boot\"");
        return;
    }

    char extra[64];
    snprintf(extra, sizeof(extra),
             "\"written\":%lu", (unsigned long)s_written);
    fw_emit("done", extra);
    ESP_LOGI(TAG, "OTA 完成 – %lu 字节，即将重启 …", (unsigned long)s_written);

    /* 先让这一行发出去，再重启 */
    vTaskDelay(pdMS_TO_TICKS(300));
    esp_restart();
}

void serial_ota_abort(void)
{
    if (s_active) {
        esp_ota_abort(s_handle);
    }
    s_active = false;
}
