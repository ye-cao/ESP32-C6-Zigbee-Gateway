/*
 * serial_ota.c – Coordinator-Firmware-Update über USB-Serial
 *
 * Die Pi-Seite (serial_gateway.py) hält die Binary und schiebt sie in
 * Blöcken über UART; der C6 schreibt sie via esp_ota in die nächste
 * OTA-Partition (ota_1, wenn aus ota_0 gebootet wurde) und wechselt
 * danach die Boot-Partition. Kein WiFi, kein HTTPS – nur USB-Serial.
 */
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "ha_mqtt.h"
#include "serial_ota.h"

#define TAG          "serial_ota"
#define FW_MAX_CHUNK 1024          /* dekodierte Bytes pro Frame */
#define FW_ACK_EVERY 16            /* Statusmeldung alle N Blöcke (Flow-Control) */

static esp_ota_handle_t   s_handle;
static const esp_partition_t *s_part;
static bool               s_active;
static uint32_t           s_size;
static uint32_t           s_written;
static uint32_t           s_chunks;

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
        fw_emit("error", "\"why\":\"busy\"");
        return;
    }
    if (size < 0x10000) {           /* kleiner als 64 KB ist kein App-Image */
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

    char extra[96];
    snprintf(extra, sizeof(extra),
             "\"part\":\"%s\",\"size\":%lu",
             s_part->label, (unsigned long)size);
    fw_emit("begin", extra);
    ESP_LOGI(TAG, "OTA begin → %s size=%lu", s_part->label, (unsigned long)size);
}

void serial_ota_data(uint32_t off, const char *hex, int hex_len)
{
    if (!s_active) {
        fw_emit("error", "\"why\":\"not_active\"");
        return;
    }
    if (off != s_written) {
        /* nur sequenzielles Schreiben – vereinfacht die Fehlerbehandlung */
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
    ESP_LOGI(TAG, "OTA done – %lu bytes, Neustart …", (unsigned long)s_written);

    /* Zeile noch rauslassen, dann neu starten */
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
