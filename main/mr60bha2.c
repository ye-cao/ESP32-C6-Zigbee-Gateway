#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_check.h"
#include "driver/uart.h"
#include "mr60bha2.h"

#define TAG "mr60bha2"

/*
 * MR60BHA2 frame format (host-receive direction):
 *   [0x01][ID_H][ID_L][LEN_H][LEN_L][TYPE_H][TYPE_L][HDR_CRC][DATA…][DATA_CRC]
 *   LEN      = number of DATA bytes (big-endian)
 *   HDR_CRC  = ~(XOR of bytes 0..6)
 *   DATA_CRC = ~(XOR of DATA bytes)
 *
 * Frame types (16-bit):
 *   0x0A14  Breath rate   data[0..3] = float LE
 *   0x0F09  People exist  data[0..1] = uint16 LE (0 or 1)
 *   0x0A15  Heart rate    data[0..3] = float LE
 *   0x0A16  Distance      data[4..7] = float LE (only if data[0] != 0)
 *   0x0A04  Num targets   data[0..3] = uint32 LE
 *
 * Field mapping in mr60_data_t:
 *   bpm          <- heart rate (float -> int)
 *   rpm          <- breath rate (float -> int)
 *   bpm_wave     <- heart rate raw float
 *   rpm_wave     <- distance float (repurposed; BHA2 has no wave output)
 *   status       <- has_target (0 = no person, 1 = person detected)
 *   bpm_category <- num_targets (truncated to uint8)
 *   rpm_category <- unused (always 0)
 */

#define SOF                  0x01
#define FRAME_TYPE_BREATH    0x0A14
#define FRAME_TYPE_HEART     0x0A15
#define FRAME_TYPE_DISTANCE  0x0A16
#define FRAME_TYPE_PEOPLE    0x0F09
#define FRAME_TYPE_TARGETS   0x0A04

/* ── Parser states ───────────────────────────────────────────────────────── */
typedef enum {
    S_SOF, S_ID_H, S_ID_L, S_LEN_H, S_LEN_L,
    S_TYPE_H, S_TYPE_L, S_HDR_CRC, S_DATA, S_DATA_CRC
} ps_t;

/* ── Module state ────────────────────────────────────────────────────────── */
static uart_port_t       s_port;
static mr60_callback_t   s_cb;
static TaskHandle_t      s_task  = NULL;
static SemaphoreHandle_t s_mutex = NULL;
static mr60_data_t       s_data  = { .bpm = -1, .rpm = -1, .status = 0 };

static ps_t     s_state = S_SOF;
static uint8_t  s_hdr[7];       /* bytes 0..6 collected for header checksum */
static uint8_t  s_hdr_idx;
static uint16_t s_frame_type;
static uint16_t s_len;          /* remaining data bytes to receive */
static uint16_t s_total_len;
static uint8_t  s_buf[64];
static uint16_t s_idx;
static uint8_t  s_crc_acc;      /* running XOR accumulator for data CRC */

/* ── Checksum helpers ────────────────────────────────────────────────────── */
static uint8_t hdr_checksum(const uint8_t *hdr7)
{
    uint8_t c = 0;
    for (int i = 0; i < 7; i++) c ^= hdr7[i];
    return ~c;
}

/* ── Frame dispatch ──────────────────────────────────────────────────────── */
static void dispatch(uint16_t frame_type, const uint8_t *data, uint16_t dlen)
{
    mr60_data_t upd;
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    upd = s_data;
    xSemaphoreGive(s_mutex);

    bool changed = false;

    switch (frame_type) {
    case FRAME_TYPE_BREATH:
        if (dlen >= 4) {
            float v; memcpy(&v, data, 4);
            if (v != 0.0f && (int)v != upd.rpm) {
                upd.rpm = (int)v; changed = true;
            }
        }
        break;

    case FRAME_TYPE_HEART:
        if (dlen >= 4) {
            float v; memcpy(&v, data, 4);
            if (v != 0.0f) {
                if ((int)v != upd.bpm) { upd.bpm = (int)v; changed = true; }
                if (v != upd.bpm_wave) { upd.bpm_wave = v; changed = true; }
            }
        }
        break;

    case FRAME_TYPE_DISTANCE:
        if (data != NULL && data[0] != 0 && dlen >= 8) {
            float v; memcpy(&v, data + 4, 4);
            if (v != upd.rpm_wave) { upd.rpm_wave = v; changed = true; }
        }
        break;

    case FRAME_TYPE_PEOPLE:
        if (dlen >= 2) {
            uint16_t ht = (uint16_t)((data[1] << 8) | data[0]);
            if (upd.status != (uint8_t)ht) {
                upd.status = (uint8_t)ht; changed = true;
                if (ht == 0) {
                    upd.bpm = 0; upd.rpm = 0;
                    upd.bpm_wave = 0.0f; upd.rpm_wave = 0.0f;
                }
            }
        }
        break;

    case FRAME_TYPE_TARGETS:
        if (dlen >= 4) {
            uint32_t n; memcpy(&n, data, 4);
            if (upd.bpm_category != (uint8_t)n) {
                upd.bpm_category = (uint8_t)n; changed = true;
            }
        }
        break;

    default:
        break;
    }

    xSemaphoreTake(s_mutex, portMAX_DELAY);
    if (changed) s_data = upd;
    s_data.frames_ok++;
    xSemaphoreGive(s_mutex);
    if (changed && s_cb) s_cb(&upd);
}

/* ── Byte parser ─────────────────────────────────────────────────────────── */
static void parse_byte(uint8_t b)
{
    switch (s_state) {
    case S_SOF:
        if (b == SOF) { s_hdr[0] = b; s_hdr_idx = 1; s_state = S_ID_H; }
        break;
    case S_ID_H:
        s_hdr[s_hdr_idx++] = b;
        s_state = S_ID_L;
        break;
    case S_ID_L:
        s_hdr[s_hdr_idx++] = b;
        s_state = S_LEN_H;
        break;
    case S_LEN_H:
        s_hdr[s_hdr_idx++] = b;
        s_total_len = (uint16_t)b << 8;
        s_state = S_LEN_L;
        break;
    case S_LEN_L:
        s_hdr[s_hdr_idx++] = b;
        s_total_len |= b;
        s_state = S_TYPE_H;
        break;
    case S_TYPE_H:
        s_hdr[s_hdr_idx++] = b;
        s_frame_type = (uint16_t)b << 8;
        s_state = S_TYPE_L;
        break;
    case S_TYPE_L:
        s_hdr[s_hdr_idx++] = b;
        s_frame_type |= b;
        if (s_frame_type != FRAME_TYPE_BREATH   &&
            s_frame_type != FRAME_TYPE_HEART     &&
            s_frame_type != FRAME_TYPE_DISTANCE  &&
            s_frame_type != FRAME_TYPE_PEOPLE    &&
            s_frame_type != FRAME_TYPE_TARGETS) {
            ESP_LOGD(TAG, "unknown frame type 0x%04x", s_frame_type);
            s_state = S_SOF;
            xSemaphoreTake(s_mutex, portMAX_DELAY); s_data.frames_err++; xSemaphoreGive(s_mutex);
        } else {
            s_state = S_HDR_CRC;
        }
        break;
    case S_HDR_CRC:
        if (b != hdr_checksum(s_hdr)) {
            ESP_LOGE(TAG, "HDR_CRC error: got 0x%02x exp 0x%02x", b, hdr_checksum(s_hdr));
            s_state = S_SOF;
            xSemaphoreTake(s_mutex, portMAX_DELAY); s_data.frames_err++; xSemaphoreGive(s_mutex);
        } else if (s_total_len == 0) {
            dispatch(s_frame_type, NULL, 0);
            s_state = S_SOF;
        } else if (s_total_len > sizeof(s_buf)) {
            ESP_LOGE(TAG, "帧过长：%u", s_total_len);
            s_state = S_SOF;
            xSemaphoreTake(s_mutex, portMAX_DELAY); s_data.frames_err++; xSemaphoreGive(s_mutex);
        } else {
            s_len = s_total_len;
            s_idx = 0;
            s_crc_acc = 0;
            s_state = S_DATA;
        }
        break;
    case S_DATA:
        s_buf[s_idx++] = b;
        s_crc_acc ^= b;
        if (--s_len == 0) s_state = S_DATA_CRC;
        break;
    case S_DATA_CRC:
        s_state = S_SOF;
        if (b == (uint8_t)(~s_crc_acc)) {
            dispatch(s_frame_type, s_buf, s_idx);
        } else {
            ESP_LOGE(TAG, "DATA_CRC error: got 0x%02x exp 0x%02x", b, (uint8_t)(~s_crc_acc));
            xSemaphoreTake(s_mutex, portMAX_DELAY); s_data.frames_err++; xSemaphoreGive(s_mutex);
        }
        break;
    }
}

/* ── UART read task ──────────────────────────────────────────────────────── */
static void uart_task(void *arg)
{
    uint8_t buf[128];
    while (1) {
        int n = uart_read_bytes(s_port, buf, sizeof(buf), pdMS_TO_TICKS(20));
        for (int i = 0; i < n; i++) parse_byte(buf[i]);
    }
}

/* ── Public API ──────────────────────────────────────────────────────────── */
esp_err_t mr60bha2_init(uart_port_t port, int tx_pin, int rx_pin,
                         mr60_callback_t cb)
{
    s_port  = port;
    s_cb    = cb;
    s_mutex = xSemaphoreCreateMutex();

    uart_config_t cfg = {
        .baud_rate  = 115200,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
    };
    ESP_RETURN_ON_ERROR(uart_driver_install(port, 512, 0, 0, NULL, 0), TAG, "uart install");
    ESP_RETURN_ON_ERROR(uart_param_config(port, &cfg),                 TAG, "uart config");
    ESP_RETURN_ON_ERROR(uart_set_pin(port, tx_pin, rx_pin,
                        UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE),       TAG, "uart pins");

    xTaskCreate(uart_task, "mr60bha2", 2048, NULL, 5, &s_task);
    ESP_LOGI(TAG, "MR60BHA2 init ok (UART%d TX=%d RX=%d)", port, tx_pin, rx_pin);
    return ESP_OK;
}

void mr60bha2_deinit(void)
{
    if (s_task) { vTaskDelete(s_task); s_task = NULL; }
    uart_driver_delete(s_port);
}

void mr60bha2_get(mr60_data_t *out)
{
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    *out = s_data;
    xSemaphoreGive(s_mutex);
}

bool mr60bha2_ready(void)
{
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    bool r = (s_data.status != 0 && s_data.bpm > 0 && s_data.rpm > 0);
    xSemaphoreGive(s_mutex);
    return r;
}
