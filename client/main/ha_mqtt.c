/*
 * ha_mqtt.c  –  USB Serial JTAG (/dev/ttyACM0) JSON-Lines Bridge
 *               做法跟协调器一样 —— printf + null_vprintf
 *
 * C6 → Pi: {"t":"boot"}  {"t":"joined",...}  {"t":"attr",...}  {"t":"heartbeat",...}
 * Pi → C6: {"cmd":"leave"}  {"cmd":"set_onoff","v":1}  {"cmd":"permit_join","sec":60}
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdarg.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "ha_mqtt.h"

/* 把编译期日期 __DATE__（"Mar 25 2026"）转成 "2026-03-25" */
static const char *_build_date(void) {
    static char s[11];
    if (s[0]) return s;
    static const char *months = "JanFebMarAprMayJunJulAugSepOctNovDec";
    const char *d = __DATE__;  /* "Mar 25 2026" */
    int mo = 1; for (; mo <= 12; mo++) if (strncmp(d, months+(mo-1)*3, 3)==0) break;
    int day = atoi(d + 4);
    int year = atoi(d + 7);
    snprintf(s, sizeof(s), "%04d-%02d-%02d", year, mo, day);
    return s;
}
#include "esp_mac.h"

#define BUF_SIZE 512

static mqtt_cmd_cb_t     s_cmd_cb  = NULL;
static SemaphoreHandle_t s_tx_mtx  = NULL;
static char s_mac_str[18] = "";  /* "AA:BB:CC:DD:EE:FF" */

/* ── Logs stumm ─────────────────────────────────────────────────────────────── */
static int null_vprintf(const char *fmt, va_list args) {
    (void)fmt; (void)args; return 0;
}

/* ── JSON-Zeile senden ───────────────────────────────────────────────────────── */
static void emit(const char *line) {
    xSemaphoreTake(s_tx_mtx, portMAX_DELAY);
    fputs(line, stdout);
    fputc('\n', stdout);
    fflush(stdout);
    xSemaphoreGive(s_tx_mtx);
}

/* ── RX-Task: Kommandos vom Pi ───────────────────────────────────────────────── */
static void rx_task(void *arg) {
    char *buf = malloc(BUF_SIZE);
    configASSERT(buf);
    while (1) {
        if (fgets(buf, BUF_SIZE, stdin) == NULL) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        buf[strcspn(buf, "\r\n")] = '\0';
        if (strlen(buf) < 3 || !s_cmd_cb) continue;

        if (strstr(buf, "\"leave\"")) {
            s_cmd_cb("leave", "", 0);
        } else if (strstr(buf, "set_onoff")) {
            char *p = strstr(buf, "\"v\":");
            char val[4] = "0";
            if (p) snprintf(val, sizeof(val), "%d", atoi(p + 4));
            s_cmd_cb("set_onoff", val, strlen(val));
        } else if (strstr(buf, "permit_join")) {
            char *p = strstr(buf, "\"sec\":");
            char sec[8] = "60";
            if (p) snprintf(sec, sizeof(sec), "%d", atoi(p + 6));
            s_cmd_cb("permit_join", sec, strlen(sec));
        }
    }
}

/* ── Init ────────────────────────────────────────────────────────────────────── */
esp_err_t ha_mqtt_init(void) {
    s_tx_mtx = xSemaphoreCreateMutex();
    configASSERT(s_tx_mtx);
    /* MAC einmalig lesen */
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_IEEE802154);
    snprintf(s_mac_str, sizeof(s_mac_str), "%02x:%02x:%02x:%02x:%02x:%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    esp_log_set_vprintf(null_vprintf);
    setvbuf(stdout, NULL, _IONBF, 0);
    xTaskCreate(rx_task, "usb_rx", 2048, NULL, 4, NULL);
    return ESP_OK;
}

void ha_mqtt_set_cmd_cb(mqtt_cmd_cb_t cb) { s_cmd_cb = cb; }

void ha_mqtt_publish_boot(void) {
    emit("{\"t\":\"boot\"}");
}

void ha_mqtt_publish_joined(uint16_t pan_id, uint8_t channel) {
    char line[64];
    snprintf(line, sizeof(line),
        "{\"t\":\"joined\",\"pan\":\"0x%04x\",\"ch\":%u}",
        (unsigned)pan_id, (unsigned)channel);
    emit(line);
}

void ha_mqtt_publish_left(void) {
    emit("{\"t\":\"left\"}");
}

void ha_mqtt_publish_attr(const char *cluster, const char *json_val) {
    char line[128];
    snprintf(line, sizeof(line),
        "{\"t\":\"attr\",\"cluster\":\"%s\",\"v\":%s}",
        cluster, json_val);
    emit(line);
}

void ha_mqtt_publish_ota_status(const char *status, uint32_t offset, uint32_t total) {
    char line[128];
    snprintf(line, sizeof(line),
        "{\"t\":\"ota_progress\",\"status\":\"%s\",\"off\":%lu,\"total\":%lu}",
        status, (unsigned long)offset, (unsigned long)total);
    emit(line);
}

void ha_mqtt_publish_heartbeat(uint32_t uptime_s, uint16_t pan_id, uint8_t channel, int8_t rssi, float temp_c) {
    char line[220];
    if (temp_c <= -126.0f) {
        snprintf(line, sizeof(line),
            "{\"t\":\"heartbeat\",\"mac\":\"%s\",\"uptime\":%lu,\"pan\":\"0x%04x\",\"ch\":%u,\"rssi\":%d,\"temp\":null,\"build\":\"%s\"}",
            s_mac_str, (unsigned long)uptime_s, (unsigned)pan_id, (unsigned)channel, (int)rssi, _build_date());
    } else {
        snprintf(line, sizeof(line),
            "{\"t\":\"heartbeat\",\"mac\":\"%s\",\"uptime\":%lu,\"pan\":\"0x%04x\",\"ch\":%u,\"rssi\":%d,\"temp\":%.2f,\"build\":\"%s\"}",
            s_mac_str, (unsigned long)uptime_s, (unsigned)pan_id, (unsigned)channel, (int)rssi, (double)temp_c, _build_date());
    }
    emit(line);
}

void ha_mqtt_emit_raw(const char *line) {
    xSemaphoreTake(s_tx_mtx, portMAX_DELAY);
    fputs(line, stdout);
    fputc('\n', stdout);
    fflush(stdout);
    xSemaphoreGive(s_tx_mtx);
}
