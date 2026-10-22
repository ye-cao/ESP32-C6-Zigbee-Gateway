#include <string.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "driver/gpio.h"
#include "esp_zigbee_core.h"

#include "ha_mqtt.h"
#include "zb_gateway.h"
#include "zb_ota_server.h"
#include "serial_ota.h"

/* Permit-Join-Taste (Boot-Taste = GPIO9) */
#define BTN_PERMIT_JOIN  9
#define PERMIT_JOIN_SECS 180

#define TAG "main"

/* ── Kommando-Handler: Befehle vom Pi via UART1 ─────────────────────────── */
static void on_uart_cmd(const char *cmd, const char *payload, int len) {
    char buf[8] = {0};
    int n = len < (int)(sizeof(buf) - 1) ? len : (int)(sizeof(buf) - 1);
    memcpy(buf, payload, n);

    if (strcmp(cmd, "dev_cmd") == 0) {
        /* payload: "ADDR,EP,CMD"  CMD: 0=off 1=on 2=toggle */
        unsigned long a = strtoul(payload, NULL, 0);
        const char *c1 = strchr(payload, ',');
        uint8_t ep  = c1 ? (uint8_t)atoi(c1 + 1) : 1;
        const char *c2 = c1 ? strchr(c1 + 1, ',') : NULL;
        uint8_t cid = c2 ? (uint8_t)atoi(c2 + 1) : 1;
        ESP_LOGI(TAG, "dev_cmd 0x%04x ep%u cmd=%u", (uint16_t)a, (unsigned)ep, (unsigned)cid);
        zb_gateway_dev_cmd((uint16_t)a, ep, cid);
    } else if (strcmp(cmd, "probe") == 0) {
        /* payload: "ADDR,EP" – Basic (Hersteller/Modell) gezielt abfragen */
        unsigned long a = strtoul(payload, NULL, 0);
        const char *c1 = strchr(payload, ',');
        uint8_t ep = c1 ? (uint8_t)atoi(c1 + 1) : 1;
        ESP_LOGI(TAG, "probe 0x%04x ep%u", (uint16_t)a, (unsigned)ep);
        zb_gateway_probe_basic((uint16_t)a, ep);
    } else if (strcmp(cmd, "node_desc") == 0) {
        /* payload: "ADDR" – Node-Descriptor lesen: Router oder End Device? */
        unsigned long a = strtoul(payload, NULL, 0);
        ESP_LOGI(TAG, "node_desc 0x%04x", (uint16_t)a);
        zb_gateway_node_desc((uint16_t)a);
    } else if (strcmp(cmd, "read_onoff") == 0) {
        /* payload: "ADDR,EP" – On/Off-Attribut aktiv lesen (Zustands-Bestätigung) */
        unsigned long a = strtoul(payload, NULL, 0);
        const char *c1 = strchr(payload, ',');
        uint8_t ep = c1 ? (uint8_t)atoi(c1 + 1) : 1;
        ESP_LOGI(TAG, "read_onoff 0x%04x ep%u", (uint16_t)a, (unsigned)ep);
        zb_gateway_read_onoff((uint16_t)a, ep);
    } else if (strcmp(cmd, "permit_join") == 0) {
        uint8_t secs = (uint8_t)atoi(buf);
        ESP_LOGI(TAG, "permit_join %u s", secs);
        zb_gateway_permit_join(secs);
    } else if (strcmp(cmd, "scan_chan") == 0) {
        zb_gateway_scan_channels();
    } else if (strcmp(cmd, "set_channel") == 0) {
        uint8_t ch = (uint8_t)atoi(buf);
        ESP_LOGI(TAG, "set_channel %u", ch);
        zb_gateway_set_channel(ch);
    } else if (strcmp(cmd, "ota_start") == 0) {
        /* payload: "SIZE,VERSION" */
        uint32_t size = (uint32_t)atol(payload);
        const char *comma = strchr(payload, ',');
        uint32_t ver = comma ? (uint32_t)atol(comma + 1) : 1;
        zb_ota_server_start(size, ver);
    } else if (strcmp(cmd, "fw_ota_start") == 0) {
        /* Coordinator-Firmware über Serial flashen */
        serial_ota_start((uint32_t)atol(payload));
    } else if (strcmp(cmd, "fw_ota_data") == 0) {
        /* payload = "OFF,HEX" – Hex-Länge = len - Offset-Teil */
        long off = atol(payload);
        const char *hex = strchr(payload, ',');
        if (hex) {
            hex++;
            serial_ota_data((uint32_t)off, hex, len - (int)(hex - payload));
        }
    } else if (strcmp(cmd, "fw_ota_end") == 0) {
        serial_ota_end();
    } else if (strcmp(cmd, "ota_data") == 0) {
        /* payload = hex string, len = byte count */
        zb_ota_server_feed_data(payload, (uint8_t)len);
    } else if (strcmp(cmd, "switch2wifi") == 0) {
        ESP_LOGI(TAG, "switch2wifi -> ZCL an alle Devices");
        zb_gateway_send_switch2wifi_all();
    } else if (strcmp(cmd, "set_tx_power") == 0) {
        /* payload = "-24" … "20" (dBm). Laufzeit-Umstellung, damit sich die
         * Sendeleistung ohne Neuflashen vergleichen lässt: hohe Leistung kann
         * bei Modulen mit Keramikantenne den PA an die Kompressionsgrenze
         * bringen, niedrige kostet direkt Link-Budget. Welcher Wert besser
         * ist, entscheidet die Messung, nicht die Vermutung. */
        int dbm = atoi(buf);
        if (dbm >  20) dbm =  20;
        if (dbm < -24) dbm = -24;
        ESP_LOGI(TAG, "set_tx_power %d dBm", dbm);
        zb_gateway_set_tx_power((int8_t)dbm);
    } else if (strcmp(cmd, "lqi") == 0) {
        /* Momentaufnahme LQI/RSSI aller Geräte, ohne auf den Heartbeat zu warten */
        zb_gateway_report_lqi();
    } else if (strcmp(cmd, "set_sleep") == 0) {
        uint32_t secs = (uint32_t)atol(buf);
        if (secs < 10) secs = 10;
        ESP_LOGI(TAG, "set_sleep %lu s", (unsigned long)secs);
        zb_gateway_send_set_sleep(secs);
    }
}

/* ── Heartbeat-Task: sofort beim Boot, dann alle 60 s ───────────────────── */
#define HEARTBEAT_INTERVAL_MS 60000

static void heartbeat_task(void *arg) {
    /* Größe folgt ZB_GATEWAY_MAX_DEVICES (48 B je Gerät + Umschlag) – sonst
     * wird die LQI-Liste ab ~32 Geräten abgeschnitten. */
    char lqi_buf[ZB_GATEWAY_MAX_DEVICES * 48 + 128];
    char line[sizeof(lqi_buf) + 192]; /* Heartbeat-JSON inkl. lqi_buf + Overhead */
    vTaskDelay(pdMS_TO_TICKS(5000));  /* Zigbee Startup abwarten */
    while (1) {
        uint32_t uptime_s = (uint32_t)(esp_timer_get_time() / 1000000ULL);
        uint8_t  ch       = esp_zb_get_current_channel();
        uint16_t pan      = esp_zb_get_pan_id();
        int8_t   rssi     = zb_gateway_get_last_rssi();
        zb_gateway_lqi_json(lqi_buf, sizeof(lqi_buf));
        snprintf(line, sizeof(line),
            "{\"t\":\"heartbeat\",\"uptime\":%lu,\"ch\":%u,\"pan\":\"0x%04x\",\"rssi\":%d,\"dev\":%s}",
            (unsigned long)uptime_s, (unsigned)ch, (unsigned)pan, (int)rssi, lqi_buf);
        ha_mqtt_emit_raw(line);
        vTaskDelay(pdMS_TO_TICKS(HEARTBEAT_INTERVAL_MS));
    }
}

/* ── Permit-Join-Taste ──────────────────────────────────────────────────── */
static void btn_task(void *arg) {
    gpio_set_direction(BTN_PERMIT_JOIN, GPIO_MODE_INPUT);
    gpio_set_pull_mode(BTN_PERMIT_JOIN, GPIO_PULLUP_ONLY);

    bool last = true;

    while (1) {
        bool cur = gpio_get_level(BTN_PERMIT_JOIN);
        if (last && !cur) {
            ESP_LOGI(TAG, "Permit Join %d s (Boot-Taste)", PERMIT_JOIN_SECS);
            zb_gateway_permit_join(PERMIT_JOIN_SECS);
        }
        last = cur;
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

/* ── app_main ───────────────────────────────────────────────────────────── */
void app_main(void) {
    ESP_LOGI(TAG, "=== ESP32-C6 Zigbee Gateway ===");
    ESP_LOGI(TAG, "");
    ESP_LOGI(TAG, "  JSON : UART1 TX=GPIO16 RX=GPIO17 → Pi /dev/ttyACM0");

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ha_mqtt_set_cmd_cb(on_uart_cmd);
    ESP_ERROR_CHECK(ha_mqtt_init(NULL, NULL, NULL));

    zb_gateway_start();

    xTaskCreate(btn_task,       "btn",       2048, NULL, 3, NULL);
    /* Stack: lqi_buf + line ≈ 6,6 KB bei 64 Geräten, plus snprintf/MQTT-Puffer */
    xTaskCreate(heartbeat_task, "heartbeat", 12288, NULL, 2, NULL);
}
