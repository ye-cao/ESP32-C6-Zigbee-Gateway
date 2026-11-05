#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_check.h"
#include "driver/uart.h"
#include "mr60bha1.h"

#define TAG "mr60bha1"

/* ── Frame-Konstanten ───────────────────────────────────────────────────── */
#define SOF1 0x53
#define SOF2 0x59
#define EOF1 0x54
#define EOF2 0x43

#define T_STATUS 0x05
#define T_BREATH 0x80
#define T_HEART  0x81
#define CMD_INFO 0x01
#define CMD_WAVE 0x02
#define CMD_RATE 0x05

/* ── Parser-Zustandsmaschine ────────────────────────────────────────────── */
typedef enum {
    S_SOF1, S_SOF2, S_TYPE, S_CMD,
    S_LENL, S_LENH, S_DATA, S_CKSUM, S_EOF1, S_EOF2
} parse_state_t;

/* ── Modulzustand ───────────────────────────────────────────────────────── */
static uart_port_t     s_port;
static mr60_callback_t s_cb;
static TaskHandle_t    s_task   = NULL;
static SemaphoreHandle_t s_mutex = NULL;
static mr60_data_t     s_data   = { .bpm = -1, .rpm = -1, .status = 0 };

/* Parser */
static parse_state_t s_state = S_SOF1;
static uint8_t       s_type, s_cmd;
static uint16_t      s_len;
static uint8_t       s_data_buf[64];
static uint16_t      s_data_idx;
static uint8_t       s_cksum_acc;

/* ── Frame-Dispatch ─────────────────────────────────────────────────────── */
static void dispatch(uint8_t t, uint8_t cmd, const uint8_t *data, uint16_t len) {
    mr60_data_t upd;
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    upd = s_data;
    xSemaphoreGive(s_mutex);

    bool changed = false;

    if (t == T_STATUS && cmd == 0x01 && len >= 1) {
        if (upd.status != data[0]) { upd.status = data[0]; changed = true; }
    } else if (t == T_BREATH) {
        if (cmd == CMD_INFO && len >= 1 && upd.rpm_category != data[0]) {
            upd.rpm_category = data[0]; changed = true;
        } else if (cmd == CMD_RATE && len >= 1 && upd.rpm != data[0]) {
            upd.rpm = data[0]; changed = true;
        } else if (cmd == CMD_WAVE && len >= 4) {
            float v; memcpy(&v, data, 4);
            if (v != upd.rpm_wave) { upd.rpm_wave = v; changed = true; }
        }
    } else if (t == T_HEART) {
        if (cmd == CMD_INFO && len >= 1 && upd.bpm_category != data[0]) {
            upd.bpm_category = data[0]; changed = true;
        } else if (cmd == CMD_RATE && len >= 1 && upd.bpm != data[0]) {
            upd.bpm = data[0]; changed = true;
        } else if (cmd == CMD_WAVE && len >= 4) {
            float v; memcpy(&v, data, 4);
            if (v != upd.bpm_wave) { upd.bpm_wave = v; changed = true; }
        }
    }

    if (changed) {
        xSemaphoreTake(s_mutex, portMAX_DELAY);
        s_data = upd;
        s_data.frames_ok++;
        xSemaphoreGive(s_mutex);
        if (s_cb) s_cb(&upd);
    } else {
        xSemaphoreTake(s_mutex, portMAX_DELAY);
        s_data.frames_ok++;
        xSemaphoreGive(s_mutex);
    }
}

/* ── 字节流解析 ─────────────────────────────────────────────────────────── */
static void parse_byte(uint8_t b) {
    switch (s_state) {
    case S_SOF1:
        if (b == SOF1) s_state = S_SOF2;
        break;
    case S_SOF2:
        s_state = (b == SOF2) ? S_TYPE : S_SOF1;
        break;
    case S_TYPE:
        s_type = b; s_cksum_acc = b; s_state = S_CMD;
        break;
    case S_CMD:
        s_cmd = b; s_cksum_acc += b; s_state = S_LENL;
        break;
    case S_LENL:
        s_len = b; s_cksum_acc += b; s_state = S_LENH;
        break;
    case S_LENH:
        s_len |= (uint16_t)b << 8; s_cksum_acc += b;
        s_data_idx = 0;
        if (s_len == 0)       s_state = S_CKSUM;
        else if (s_len > 60)  { s_state = S_SOF1; xSemaphoreTake(s_mutex, portMAX_DELAY); s_data.frames_err++; xSemaphoreGive(s_mutex); }
        else                   s_state = S_DATA;
        break;
    case S_DATA:
        s_data_buf[s_data_idx++] = b;
        s_cksum_acc += b;
        if (s_data_idx == s_len) s_state = S_CKSUM;
        break;
    case S_CKSUM:
        if (b == (s_cksum_acc & 0xFF)) s_state = S_EOF1;
        else { xSemaphoreTake(s_mutex, portMAX_DELAY); s_data.frames_err++; xSemaphoreGive(s_mutex); s_state = S_SOF1; }
        break;
    case S_EOF1:
        s_state = (b == EOF1) ? S_EOF2 : S_SOF1;
        break;
    case S_EOF2:
        s_state = S_SOF1;
        if (b == EOF2) dispatch(s_type, s_cmd, s_data_buf, s_len);
        else { xSemaphoreTake(s_mutex, portMAX_DELAY); s_data.frames_err++; xSemaphoreGive(s_mutex); }
        break;
    }
}

/* ── UART-Lesetask ──────────────────────────────────────────────────────── */
static void uart_task(void *arg) {
    uint8_t buf[128];
    while (1) {
        int n = uart_read_bytes(s_port, buf, sizeof(buf), pdMS_TO_TICKS(20));
        for (int i = 0; i < n; i++) parse_byte(buf[i]);
    }
}

/* ── 对外接口 ───────────────────────────────────────────────────────────── */
esp_err_t mr60_init(uart_port_t port, int tx_pin, int rx_pin,
                    mr60_callback_t cb) {
    s_port = port;
    s_cb   = cb;
    s_mutex = xSemaphoreCreateMutex();

    uart_config_t cfg = {
        .baud_rate  = 115200,
        .data_bits  = UART_DATA_8_BITS,
        .parity     = UART_PARITY_DISABLE,
        .stop_bits  = UART_STOP_BITS_1,
        .flow_ctrl  = UART_HW_FLOWCTRL_DISABLE,
    };
    ESP_RETURN_ON_ERROR(uart_driver_install(port, 512, 0, 0, NULL, 0), TAG, "uart install");
    ESP_RETURN_ON_ERROR(uart_param_config(port, &cfg), TAG, "uart config");
    ESP_RETURN_ON_ERROR(uart_set_pin(port, tx_pin, rx_pin,
                        UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE), TAG, "uart pins");

    xTaskCreate(uart_task, "mr60_uart", 2048, NULL, 5, &s_task);
    ESP_LOGI(TAG, "MR60BHA1 init ok (UART%d TX=%d RX=%d)", port, tx_pin, rx_pin);
    return ESP_OK;
}

void mr60_deinit(void) {
    if (s_task) { vTaskDelete(s_task); s_task = NULL; }
    uart_driver_delete(s_port);
}

void mr60_get(mr60_data_t *out) {
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    *out = s_data;
    xSemaphoreGive(s_mutex);
}

bool mr60_ready(void) {
    xSemaphoreTake(s_mutex, portMAX_DELAY);
    bool r = (s_data.status == MR60_STATUS_MEAS &&
              s_data.bpm > 0 && s_data.rpm > 0);
    xSemaphoreGive(s_mutex);
    return r;
}
