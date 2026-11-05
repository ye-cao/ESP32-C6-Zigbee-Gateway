#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "driver/uart.h"

/* Status-Codes */
#define MR60_STATUS_INIT   0
#define MR60_STATUS_CAL    1
#define MR60_STATUS_MEAS   2

/* Kategorie-Codes */
#define MR60_CAT_NONE   0
#define MR60_CAT_NORMAL 1
#define MR60_CAT_FAST   2
#define MR60_CAT_SLOW   3

typedef struct {
    int     bpm;           /* 心率（次/分），-1 = 未知 */
    int     rpm;           /* 呼吸率（次/分），-1 = 未知 */
    uint8_t bpm_category;  /* MR60_CAT_* */
    uint8_t rpm_category;  /* MR60_CAT_* */
    uint8_t status;        /* MR60_STATUS_* */
    float   bpm_wave;      /* Herzschlag-Rohwellenform */
    float   rpm_wave;      /* Atem-Rohwellenform */
    uint32_t frames_ok;
    uint32_t frames_err;
} mr60_data_t;

typedef void (*mr60_callback_t)(const mr60_data_t *data);

esp_err_t mr60_init(uart_port_t port, int tx_pin, int rx_pin,
                    mr60_callback_t cb);
void      mr60_deinit(void);
void      mr60_get(mr60_data_t *out);  /* 取最近一次测量值（线程安全） */
bool      mr60_ready(void);            /* 状态为 MEAS 且已有数值时返回 true */
