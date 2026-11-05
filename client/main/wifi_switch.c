/*
 * wifi_switch.c —— 进入 WiFi 模式并保持 WIFI_SWITCH_TIMEOUT_S，然后 esp_restart()
 *
 * 流程：
 *   1. 经 Zigbee 收到 switch2wifi 命令 → wifi_switch_trigger()
 *   2. 置 NVS 标志 + esp_restart()
 *   3. 下次启动：app_main 检查标志 → wifi_switch_run()
 *   4. 扫 WiFi → 挑信号最好的开放 AP → 连上 → 等一会儿 → esp_restart()
 *   5. 再下次启动：回到正常 Zigbee 模式
 */
#include "wifi_switch.h"
#include "ha_mqtt.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_wifi.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_system.h"
#include "nvs.h"
#include <string.h>
#include "lwip/sockets.h"
#include "esp_netif.h"
#include "esp_netif_ip_addr.h"

#define TAG      "wifi_sw"
#define NVS_NS   "wifi_sw"
#define NVS_KEY  "pending"
#define SCAN_MAX 20

/* ── NVS-Flag ────────────────────────────────────────────────────────────── */
bool wifi_switch_is_pending(void) {
    nvs_handle_t h;
    uint8_t v = 0;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        nvs_get_u8(h, NVS_KEY, &v);
        nvs_close(h);
    }
    return v == 1;
}

static void set_pending(uint8_t val) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_u8(h, NVS_KEY, val);
        nvs_commit(h);
        nvs_close(h);
    }
}

/* 从 Zigbee 回调里调用：置标志 + 立即重启 */
void wifi_switch_trigger(void) {
    set_pending(1);
    ha_mqtt_emit_raw("{\"t\":\"switch2wifi\",\"state\":\"rebooting\"}");
    /* 稍等一下，让 emit() 把数据吐完，再重启 */
    vTaskDelay(pdMS_TO_TICKS(150));
    esp_restart();
}

/* ── 用 TCP socket 发最小化的 MQTT PUBLISH ──────────────────────────────── */
static void mqtt_publish_ip(const char *broker_ip, const char *topic,
                            const char *payload) {
    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port   = htons(1883),
    };
    inet_pton(AF_INET, broker_ip, &addr.sin_addr);

    int sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) return;
    struct timeval tv = { .tv_sec = 3 };
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    if (connect(sock, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        close(sock); return;
    }

    /* MQTT CONNECT packet (clientId="esp32c6") */
    uint8_t connect_pkt[] = {
        0x10, 0x12,                          /* CONNECT, remaining=18 */
        0x00, 0x04, 'M','Q','T','T',       /* protocol name */
        0x04,                                /* protocol level 3.1.1 */
        0x02,                                /* connect flags: clean session */
        0x00, 0x3c,                          /* keepalive 60s */
        0x00, 0x08,                          /* clientId length */
        'e','s','p','3','2','c','6','x'    /* clientId "esp32c6x" */
    };
    write(sock, connect_pkt, sizeof(connect_pkt));
    uint8_t connack[4]; read(sock, connack, 4); /* wait CONNACK */

    /* MQTT PUBLISH packet */
    uint16_t tlen = (uint16_t)strlen(topic);
    uint16_t plen = (uint16_t)strlen(payload);
    uint32_t remaining = 2 + tlen + plen;  /* no QoS packet id */
    uint8_t pub_hdr[2] = { 0x30, (uint8_t)remaining };
    uint8_t tlen_be[2] = { (uint8_t)(tlen >> 8), (uint8_t)(tlen & 0xff) };
    write(sock, pub_hdr, 2);
    write(sock, tlen_be, 2);
    write(sock, topic, tlen);
    write(sock, payload, plen);

    vTaskDelay(pdMS_TO_TICKS(200));
    close(sock);
}

/* ── WiFi 模式（在 app_main 里、Zigbee 启动之前跑） ─────────────────────── */
void wifi_switch_run(void) {
    set_pending(0);   /* 立刻清掉标志 */

    ha_mqtt_emit_raw("{\"t\":\"switch2wifi\",\"state\":\"starting\"}");

    /* 初始化事件循环 + netif（只做一次，因为这个模式不起 Zigbee） */
    esp_err_t err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "event_loop_create: %s", esp_err_to_name(err));
    }
    ESP_ERROR_CHECK(esp_netif_init());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());

    /* Scan starten (blocking) */
    wifi_scan_config_t scan_cfg = {
        .ssid = NULL, .bssid = NULL, .channel = 0, .show_hidden = false,
    };
    esp_wifi_scan_start(&scan_cfg, true);

    uint16_t         ap_count = SCAN_MAX;
    wifi_ap_record_t recs[SCAN_MAX];
    memset(recs, 0, sizeof(recs));
    esp_wifi_scan_get_ap_records(&ap_count, recs);

    /* 挑最好的开放 AP（信号最强、不需要密码） */
    int best = -1, best_rssi = -128;
    for (int i = 0; i < ap_count; i++) {
        if (recs[i].authmode == WIFI_AUTH_OPEN && recs[i].rssi > best_rssi) {
            best_rssi = recs[i].rssi;
            best      = i;
        }
    }

    if (best < 0) {
        char line[80];
        snprintf(line, sizeof(line),
            "{\"t\":\"switch2wifi\",\"state\":\"no_open_ap\",\"scanned\":%d}",
            ap_count);
        ha_mqtt_emit_raw(line);
        vTaskDelay(pdMS_TO_TICKS(500));
        esp_restart();
        return;
    }

    char ssid[33] = {0};
    memcpy(ssid, recs[best].ssid, 32);

    char line[160];
    snprintf(line, sizeof(line),
        "{\"t\":\"switch2wifi\",\"state\":\"connecting\",\"ssid\":\"%s\",\"rssi\":%d}",
        ssid, best_rssi);
    ha_mqtt_emit_raw(line);

    /* Verbinden */
    wifi_config_t wifi_cfg = {};
    memcpy(wifi_cfg.sta.ssid, recs[best].ssid, 32);
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    esp_wifi_connect();

    /* Verbindungsaufbau abwarten */
    vTaskDelay(pdMS_TO_TICKS(6000));

    snprintf(line, sizeof(line),
        "{\"t\":\"switch2wifi\",\"state\":\"connected\",\"ssid\":\"%s\",\"timeout_s\":%d}",
        ssid, WIFI_SWITCH_TIMEOUT_S);
    ha_mqtt_emit_raw(line);

    /* 取 IP，直接用 MQTT 发给 broker */
    esp_netif_t *netif = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
    if (netif) {
        esp_netif_ip_info_t ip_info;
        if (esp_netif_get_ip_info(netif, &ip_info) == ESP_OK) {
            char ip_str[16];
            esp_ip4addr_ntoa(&ip_info.ip, ip_str, sizeof(ip_str));
            char mqtt_payload[100];
            snprintf(mqtt_payload, sizeof(mqtt_payload),
                     "{\"t\":\"wifi_ip\",\"ip\":\"%s\",\"ssid\":\"%s\"}",
                     ip_str, ssid);
            mqtt_publish_ip("192.168.178.218",
                            "gw/coordinator/zigbee/client/wifi_ip",
                            mqtt_payload);
            ha_mqtt_emit_raw(mqtt_payload);  /* auch per UART */
        }
    }

    /* Timeout laufen lassen */
    vTaskDelay(pdMS_TO_TICKS((uint32_t)WIFI_SWITCH_TIMEOUT_S * 1000));

    ha_mqtt_emit_raw("{\"t\":\"switch2wifi\",\"state\":\"timeout_reboot\"}");
    vTaskDelay(pdMS_TO_TICKS(200));
    esp_restart();
}
