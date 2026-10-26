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
#include "zb_topo.h"
#include "zb_ota_server.h"
#include "serial_ota.h"

/* Permit-Join-Taste (Boot-Taste = GPIO9) */
#define BTN_PERMIT_JOIN  9
#define PERMIT_JOIN_SECS 180

#define TAG "main"

/* ── ZBOSS "big lock" ──────────────────────────────────────────────────────
 *
 * ★★★ 2026-10-26: DAS war die eigentliche Reboot-Ursache, nicht der Stack
 *     und nicht der blockierende serielle Schreibpfad.
 *
 * Befund (Add-on-Log, 0,6 s-Mitschnitt mitten im read_onoff-Sturm):
 *   assert failed: vPortExitCritical port.c:618
 *                   (port_uxCriticalNesting[0] > 0)
 * Rückverfolgung über die Stack-Adressen:
 *   zb_sched_loop_iteration → zb_multimac_mac_logic_iteration_proxy
 *   → zb_mac_logic_iteration → zb_esp_clr_event
 *   → zb_osif_enable_all_interrupts   (= vPortExitCritical())
 *
 * Der ZBOSS-Kern ist NICHT thread-safe. Seine Interrupt-Klammer ist ein
 * globales Byte mit vPortEnter/ExitCritical-Paar, und port_uxCriticalNesting
 * ist ein globaler Zähler pro Kern – nicht pro Task. Jeder zb_*-Aufruf aus
 * einem FREMDEN Task (uart_rx, heartbeat, btn, chan_scan) läuft in genau
 * dieses Fenster und verstellt dem ZBOSS-Task den Zähler ⇒ Unterlauf ⇒
 * assert ⇒ rst:0xc (SW_CPU).
 *
 * Die offizielle API verlangt die Lock (esp_zigbee_core.h, esp_zb_lock_acquire):
 *   "It's mandatory to acquire the lock before calling any Zigbee SDK APIs,
 *    except that the call site is in Zigbee callbacks."
 * Bis hierher wurde sie im ganzen Projekt NIRGENDS genommen.
 *
 * Gleiches Muster wie tostmann/esp-coordinator (.tostmann/main/zb_ncp.cpp,
 * zboss_lock_guard) – dort mit exakt derselben Assert-Meldung reproduziert.
 * Die Lock ist rekursiv (zb_esp_osif_lock_release → xQueueGiveMutexRecursive),
 * Verschachtelung mit Aufrufen, die selbst esp_zb_lock_acquire nehmen, ist
 * also harmlos.
 *
 * ⚠️ Vor esp_zb_start() existiert s_zb_mutex noch nicht (tostmann boot_guard
 *    aus demselben Grund) ⇒ immer erst zb_gateway_is_ready() prüfen.
 */
static inline bool zb_lock(void) {
    if (!zb_gateway_is_ready()) return false;
    return esp_zb_lock_acquire(portMAX_DELAY);
}
static inline void zb_unlock(bool held) {
    if (held) esp_zb_lock_release();
}

/* ── Kommando-Handler: Befehle vom Pi via UART1 ─────────────────────────── */
static void on_uart_cmd(const char *cmd, const char *payload, int len) {
    char buf[8] = {0};
    int n = len < (int)(sizeof(buf) - 1) ? len : (int)(sizeof(buf) - 1);
    memcpy(buf, payload, n);

    /* ★★ 2026-10-25: Reboot-Schleife hier abfangen.
     * ha_mqtt_init() startet den uart_rx-Task VOR zigbee; parallel läuft
     * zb_task erst durch esp_zb_start(). Kommt in diesem Fenster ein Befehl
     * (das Add-on schickt direkt nach dem Boot read_onoff für jedes Gerät),
     * dann dereferenziert esp_zb_scheduler_alarm() eine noch nicht
     * existierende Warteschlange:
     *   Guru Meditation Error: Core 0 panic'ed (Load access fault)
     *   MEPC 0x42022f9a (zb_schedule_alarm) / MTVAL 0x0000000d
     * → alle ~20 s ein Reset, von außen sah es nach "Gateway startet
     *   ständig neu" aus. Der Befehl wird deshalb verworfen, nicht gestaut. */
    if (!zb_gateway_is_ready()) {
        ESP_LOGW(TAG, "Befehl '%s' verworfen – Zigbee-Stack startet noch", cmd);
        return;
    }

    /* ★ Ab hier geht JEDER Zweig in die Zigbee-SDK. Ausnahmen:
     *  - fw_ota_*: schreibt nur in den Flash (serial_ota.c)
     *  - ota_data:  schreibt nur in den OTA-Puffer des Servers
     *               (zb_ota_server.c) und gibt ein Semaphor – kein SDK-Aufruf.
     *    ★★ Warum ota_data die Lock NICHT nehmen darf (2026-10-26):
     *    ota_next_data_cb läuft im ZBOSS-Task. Braucht er Daten, wartet er
     *    auf genau dieses Semaphor. Hielte rx_task hier die Lock, käme
     *    feed_data nie an → 10 s Stillstand pro Block → OTA steht.
     *    （实测：OTA 卡在 8 / 610976 Bytes，之后客户端直接 abort。） */
    bool held = (strncmp(cmd, "fw_ota", 6) == 0 ||
                 strcmp(cmd, "ota_data") == 0) ? false : zb_lock();

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
    } else if (strcmp(cmd, "topo") == 0) {
        /* payload: 可选 "DELAY_MS"。给了就先等 DELAY_MS 再 dump（add-on 会
         * 在等待期间逐台发 node_desc 触发路由发现）；不给就立刻 dump 现有表。 */
        int delay = payload ? atoi(payload) : 0;
        ESP_LOGI(TAG, "topo dump (delay %d ms)", delay);
        if (delay > 0) zb_topo_request_full((uint32_t)delay);
        else           zb_topo_request();
    } else if (strcmp(cmd, "scan_chan") == 0) {
        zb_gateway_scan_channels();
    } else if (strcmp(cmd, "set_channel") == 0) {
        uint8_t ch = (uint8_t)atoi(buf);
        ESP_LOGI(TAG, "set_channel %u", ch);
        zb_gateway_set_channel(ch);
    } else if (strcmp(cmd, "ota_start") == 0) {
        /* payload: "SIZE,VERSION[,IMAGETYPE]"
         * IMAGETYPE: 0x0001 = 终端节点, 0x0002 = 中继（省略则默认 0x0001） */
        uint32_t size = (uint32_t)atol(payload);
        const char *comma = strchr(payload, ',');
        uint32_t ver = comma ? (uint32_t)atol(comma + 1) : 1;
        uint16_t type = 0;
        const char *comma2 = comma ? strchr(comma + 1, ',') : NULL;
        if (comma2) type = (uint16_t)strtoul(comma2 + 1, NULL, 0);
        zb_ota_server_start(size, ver, type);
    } else if (strcmp(cmd, "set_dev_power") == 0) {
        /* payload: "ADDR,EP,DBM" – 给中继写厂商专有簇 0xFC00/0x0001 */
        unsigned long a = strtoul(payload, NULL, 0);
        const char *c1 = strchr(payload, ',');
        uint8_t ep = c1 ? (uint8_t)atoi(c1 + 1) : 1;
        const char *c2 = c1 ? strchr(c1 + 1, ',') : NULL;
        int dbm = c2 ? atoi(c2 + 1) : 8;
        if (dbm > 20)  dbm = 20;
        if (dbm < -24) dbm = -24;
        ESP_LOGI(TAG, "set_dev_power 0x%04x ep%u → %d dBm", (uint16_t)a, (unsigned)ep, dbm);
        zb_gateway_write_tx_power((uint16_t)a, ep, (int8_t)dbm);
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
        /* payload = "OFF,HEX" (bis 1024 B = 2048 Hex-Zeichen) */
        zb_ota_server_feed_data(payload, (uint16_t)len);
    } else if (strcmp(cmd, "ota_stop") == 0) {
        /* OTA-Session abbrechen (auch vom Host aus, damit ein versehentlich
         * gestarteter Transfer nicht 80 Minuten Funkzeit frisst). */
        zb_ota_server_stop();
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

    zb_unlock(held);
}

/* ── Heartbeat-Task: sofort beim Boot, dann alle 60 s ───────────────────── */
#define HEARTBEAT_INTERVAL_MS 60000

static void heartbeat_task(void *arg) {
    /* Größe folgt ZB_GATEWAY_MAX_DEVICES (64 B je Gerät + Umschlag) – sonst
     * wird die LQI-Liste ab ~32 Geräten abgeschnitten.
     * ★ static: 这两个缓冲区加起来 ≈ 8,6 KB bei 64 Geräten. Als Locals
     *   belegen sie 70% des 12 KB-Stacks; static 版本占 .bss 而不是栈
     *   （本任务只有一个实例，静态是安全的）。 */
    static char lqi_buf[ZB_GATEWAY_MAX_DEVICES * 64 + 128];
    static char line[sizeof(lqi_buf) + 192]; /* Heartbeat-JSON inkl. lqi_buf + Overhead */
    vTaskDelay(pdMS_TO_TICKS(5000));  /* Zigbee Startup abwarten */
    while (1) {
        uint32_t uptime_s = (uint32_t)(esp_timer_get_time() / 1000000ULL);
        /* Nur die beiden SDK-Getter brauchen die ZBOSS-Lock; lqi_json und
         * get_last_rssi arbeiten auf eigenen Tabellen (s_dev_mutex). */
        bool held = zb_lock();
        uint8_t  ch   = esp_zb_get_current_channel();
        uint16_t pan  = esp_zb_get_pan_id();
        zb_unlock(held);
        int8_t   rssi = zb_gateway_get_last_rssi();
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
            bool held = zb_lock();
            zb_gateway_permit_join(PERMIT_JOIN_SECS);
            zb_unlock(held);
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
