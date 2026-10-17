/*
 * ha_mqtt.c  –  JSON via USB-JTAG/CDC → ttyACM1
 *
 * CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y leitet printf()/stdin auf den
 * nativen USB-CDC-Port (ttyACM1). Kein UART-Treiber nötig.
 * IDF-Log wird per null_vprintf stummgeschaltet.
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

#define BUF_SIZE    1024

static mqtt_cmd_cb_t     s_cmd_cb   = NULL;
static SemaphoreHandle_t s_tx_mutex = NULL;

static int null_vprintf(const char *fmt, va_list args) {
    (void)fmt; (void)args;
    return 0;
}

static void emit(const char *line) {
    xSemaphoreTake(s_tx_mutex, portMAX_DELAY);
    puts(line);
    fflush(stdout);
    xSemaphoreGive(s_tx_mutex);
}

/* Liefert einen Zeiger auf den Wert hinter "key" – tolerant gegenüber
 * Leerzeichen nach dem Doppelpunkt. Notwendig, weil json.dumps() (Python)
 * {"addr": "0x07da"} erzeugt: ein starres strstr(buf, "\"addr\":\"") findet
 * dann NICHTS, und der Befehl scheitert stillschweigend. */
static const char *json_val(const char *buf, const char *key) {
    const char *k = strstr(buf, key);
    if (!k) return NULL;
    k += strlen(key);
    while (*k == ' ' || *k == '\t') k++;
    if (*k != ':') return NULL;
    k++;
    while (*k == ' ' || *k == '\t') k++;
    return k;
}

static void rx_task(void *arg) {
    char *buf = malloc(BUF_SIZE);
    configASSERT(buf);

    /* Der USB-Serial-JTAG-CDC liefert pro fgets() nur ~64 Byte und fgets()
     * behandelt das als Zeilenende. Laengere Befehle kamen deshalb nur als
     * Fragment an: bei fw_ota_data fehlte das schliessende '"', strchr()
     * fand kein Ende, und der Befehl wurde STILL verworfen. Genau daran ist
     * das Firmware-OTA reproduzierbar bei 1280 Byte (16 Frames) stehen
     * geblieben – ohne Fehlermeldung. Kurze Befehle (dev_cmd, 45 Zeichen)
     * funktionierten, weil alle Felder vor dem Abbruch lagen.
     * Deshalb: Fragmente sammeln, bis ein Zeilenende ankommt. */
    static char acc[BUF_SIZE];
    size_t acc_len = 0;

    while (1) {
        if (!fgets(buf, BUF_SIZE, stdin)) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        size_t raw = strlen(buf);
        int eol = (raw > 0 && (buf[raw-1] == '\n' || buf[raw-1] == '\r'));

        /* Zeilenende entfernen */
        size_t n = raw;
        while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) buf[--n] = '\0';

        if (!eol) {                      /* Teilzeile -> anpuffern */
            if (acc_len + n < sizeof(acc)) {
                memcpy(acc + acc_len, buf, n);
                acc_len += n;
            } else {
                acc_len = 0;             /* Ueberlauf: verwerfen */
            }
            continue;
        }
        if (acc_len) {                   /* Zeile endet, war aber gestueckelt */
            if (acc_len + n >= sizeof(acc)) {
                acc_len = 0;
                continue;
            }
            memcpy(acc + acc_len, buf, n);
            acc_len += n;
            memcpy(buf, acc, acc_len);
            buf[acc_len] = '\0';
            n = acc_len;
            acc_len = 0;
        }
        if (n == 0 || !s_cmd_cb) continue;

        if (strstr(buf, "permit_join")) {
            char *p = strstr(buf, "\"sec\":");
            char secs_str[8] = "180";
            if (p) snprintf(secs_str, sizeof(secs_str), "%d", atoi(p + 6));
            s_cmd_cb("permit_join", secs_str, strlen(secs_str));
        } else if (strstr(buf, "fw_ota_start")) {
            /* Coordinator-Firmware-Update – muss VOR ota_start stehen,
             * weil "fw_ota_start" den Substring "ota_start" enthält */
            char *ps = strstr(buf, "\"size\":");
            char payload[16] = "";
            if (ps) snprintf(payload, sizeof(payload), "%ld", atol(ps + 7));
            s_cmd_cb("fw_ota_start", payload, strlen(payload));
        } else if (strstr(buf, "fw_ota_data")) {
            /* payload = "OFF,HEX" (Hex kann ~900 Zeichen haben → static) */
            const char *po = json_val(buf, "\"off\"");
            char *pd = (char *)json_val(buf, "\"data\"");
            if (po && pd) {
                if (*pd == '"') pd++;
                char *end = strchr(pd, '"');
                if (end) {
                    *end = '\0';
                    static char payload[BUF_SIZE];
                    snprintf(payload, sizeof(payload), "%ld,", atol(po));
                    size_t pre = strlen(payload);
                    size_t hexlen = (size_t)(end - pd);
                    if (pre + hexlen < sizeof(payload)) {
                        memcpy(payload + pre, pd, hexlen);
                        payload[pre + hexlen] = '\0';
                        s_cmd_cb("fw_ota_data", payload, (int)(pre + hexlen));
                    }
                }
            }
        } else if (strstr(buf, "fw_ota_end")) {
            s_cmd_cb("fw_ota_end", "", 0);
        } else if (strstr(buf, "dev_cmd")) {
            /* Gerätesteuerung: {"cmd":"dev_cmd","addr":"0x07da","ep":16,"sw":"on"}
             * payload = "ADDR,EP,CMD"   CMD: 0=off 1=on 2=toggle
             * ACHTUNG: Aktion heißt "sw" NICHT "cmd" – sonst findet
             * strstr() zuerst das führende {"cmd":"dev_cmd",…} und jede
             * Aktion würde zu "on". */
            const char *pa = json_val(buf, "\"addr\"");
            const char *pe = json_val(buf, "\"ep\"");
            const char *pc = json_val(buf, "\"sw\"");
            if (pa && pe && pc) {
                unsigned long addr = strtoul((*pa == '"') ? pa + 1 : pa, NULL, 16);
                const char *sv = (*pc == '"') ? pc + 1 : pc;
                char payload[32];
                int ep  = atoi(pe);
                int cid = 1;                                  /* default: on */
                if (strncmp(sv, "off", 3) == 0)         cid = 0;
                else if (strncmp(sv, "toggle", 6) == 0) cid = 2;
                snprintf(payload, sizeof(payload), "%lu,%d,%d", addr, ep, cid);
                s_cmd_cb("dev_cmd", payload, strlen(payload));
            }
        } else if (strstr(buf, "probe")) {
            /* Modellabfrage: {"cmd":"probe","addr":"0x07da","ep":1}
             * payload = "ADDR,EP" */
            const char *pa = json_val(buf, "\"addr\"");
            const char *pe = json_val(buf, "\"ep\"");
            if (pa && pe) {
                unsigned long addr = strtoul((*pa == '"') ? pa + 1 : pa, NULL, 16);
                char payload[24];
                snprintf(payload, sizeof(payload), "%lu,%d", addr, atoi(pe));
                s_cmd_cb("probe", payload, strlen(payload));
            }
        } else if (strstr(buf, "node_desc")) {
            /* Node-Descriptor: {"cmd":"node_desc","addr":"0x07da"}
             * payload = "ADDR" – klärt, ob das Gerät als Router (ZR) arbeitet. */
            const char *pa = json_val(buf, "\"addr\"");
            if (pa) {
                unsigned long addr = strtoul((*pa == '"') ? pa + 1 : pa, NULL, 16);
                char payload[16];
                snprintf(payload, sizeof(payload), "%lu", addr);
                s_cmd_cb("node_desc", payload, strlen(payload));
            }
        } else if (strstr(buf, "read_onoff")) {
            /* Zustands-Rücklesen: {"cmd":"read_onoff","addr":"0x07da","ep":16}
             * payload = "ADDR,EP" (ADRESS-Modus wie probe)
             * Nötig, weil das Endgerät auf Fernbefehle keinen Report schickt. */
            const char *pa = json_val(buf, "\"addr\"");
            const char *pe = json_val(buf, "\"ep\"");
            if (pa && pe) {
                unsigned long addr = strtoul((*pa == '"') ? pa + 1 : pa, NULL, 16);
                char payload[24];
                snprintf(payload, sizeof(payload), "%lu,%d", addr, atoi(pe));
                s_cmd_cb("read_onoff", payload, strlen(payload));
            }
        } else if (strstr(buf, "set_channel")) {
            char *p = strstr(buf, "\"ch\":");
            char ch_str[4] = "20";
            if (p) snprintf(ch_str, sizeof(ch_str), "%d", atoi(p + 5));
            s_cmd_cb("set_channel", ch_str, strlen(ch_str));
        } else if (strstr(buf, "scan_chan")) {
            s_cmd_cb("scan_chan", "", 0);
        } else if (strstr(buf, "switch2wifi")) {
            s_cmd_cb("switch2wifi", "", 0);
        } else if (strstr(buf, "set_sleep")) {
            char *p = strstr(buf, "\"s\":");
            char val[16] = "600";
            if (p) snprintf(val, sizeof(val), "%d", atoi(p + 4));
            s_cmd_cb("set_sleep", val, strlen(val));
        } else if (strstr(buf, "set_tx_power")) {
            /* Sendeleistung zur Laufzeit: {"cmd":"set_tx_power","dbm":14}
             * payload = "14"  (dBm). Erlaubt, die Leistung ohne Neuflashen
             * zu variieren und den wirksamen Wert zurückzumelden. */
            char *p = strstr(buf, "\"dbm\":");
            char val[8] = "20";
            if (p) snprintf(val, sizeof(val), "%d", atoi(p + 6));
            s_cmd_cb("set_tx_power", val, strlen(val));
        } else if (strstr(buf, "\"lqi\"")) {
            /* Momentaufnahme LQI/RSSI: {"cmd":"lqi"}
             * Nur auf Anfrage – im Heartbeat steht dieselbe Liste, aber der
             * Wert gilt für den letzten empfangenen Frame und ist nach 60 s
             * unter Umständen längst überholt. */
            s_cmd_cb("lqi", "", 0);
        } else if (strstr(buf, "ota_start")) {
            char *ps = strstr(buf, "\"size\":");
            char *pv = strstr(buf, "\"ver\":");
            char payload[32] = "";
            if (ps) {
                long sz  = atol(ps + 7);
                long ver = pv ? atol(pv + 6) : 1;
                snprintf(payload, sizeof(payload), "%ld,%ld", sz, ver);
            }
            s_cmd_cb("ota_start", payload, strlen(payload));
        } else if (strstr(buf, "ota_data")) {
            char *pd = strstr(buf, "\"data\":\"");
            if (pd) {
                pd += 8;
                char *end = strchr(pd, '"');
                if (end) {
                    *end = '\0';
                    uint8_t byte_count = (uint8_t)((end - pd) / 2);
                    s_cmd_cb("ota_data", pd, byte_count);
                }
            }
        } else if (strstr(buf, "\"cmd\"")) {
            /* Unbekanntes Kommando NICHT stillschweigend verwerfen.
             * Genau daran sind schon zwei Fehler stumm gestorben:
             * fw_ota_data (Kapitel 11) und node_desc (Befehl war in main.c
             * vorhanden, hier aber nicht verdrahtet → gar keine Ausgabe). */
            const char *pc = strstr(buf, "\"cmd\":\"");
            char name[24] = "?";
            if (pc) {
                pc += 7;
                size_t i = 0;
                while (i < sizeof(name) - 1 && pc[i] && pc[i] != '"') {
                    name[i] = pc[i];
                    i++;
                }
                name[i] = '\0';
            }
            char line[96];
            snprintf(line, sizeof(line),
                     "{\"t\":\"error\",\"why\":\"unknown_cmd\",\"cmd\":\"%s\"}", name);
            emit(line);
        }
    }
}

esp_err_t ha_mqtt_init(const char *broker_uri,
                       const char *username,
                       const char *password) {
    (void)broker_uri; (void)username; (void)password;

    s_tx_mutex = xSemaphoreCreateMutex();
    configASSERT(s_tx_mutex);

    /* IDF-Log stummschalten – nur unser JSON auf ttyACM1 */
    esp_log_set_vprintf(null_vprintf);

    xTaskCreate(rx_task, "uart_rx", 2048, NULL, 4, NULL);

    /* Kurz warten bis USB-CDC bereit */
    vTaskDelay(pdMS_TO_TICKS(500));
    emit("{\"t\":\"boot\"}");
    return ESP_OK;
}

void ha_mqtt_set_cmd_cb(mqtt_cmd_cb_t cb) { s_cmd_cb = cb; }

void ha_mqtt_publish_zigbee(uint16_t addr, const char *subtopic, const char *payload) {
    ha_mqtt_publish_zigbee_ep(addr, 1, subtopic, payload);
}

void ha_mqtt_publish_zigbee_ep(uint16_t addr, uint8_t endpoint, const char *subtopic, const char *payload) {
    char sub[64];
    char line[224];
    if (endpoint > 1) {
        snprintf(sub, sizeof(sub), "ep%u/%s", (unsigned)endpoint, subtopic);
    } else {
        snprintf(sub, sizeof(sub), "%s", subtopic);
    }
    snprintf(line, sizeof(line),
        "{\"t\":\"zigbee\",\"addr\":%u,\"sub\":\"%s\",\"p\":%s}",
        (unsigned)addr, sub, payload);
    emit(line);
}

void ha_mqtt_publish_heartbeat(uint32_t uptime_s, uint8_t channel, uint16_t pan_id) {
    char line[96];
    snprintf(line, sizeof(line),
        "{\"t\":\"heartbeat\",\"uptime\":%lu,\"ch\":%u,\"pan\":\"0x%04x\"}",
        (unsigned long)uptime_s, (unsigned)channel, (unsigned)pan_id);
    emit(line);
}

void ha_mqtt_publish_permit_join(bool open, uint8_t seconds) {
    char line[80];
    snprintf(line, sizeof(line),
        "{\"t\":\"permit_join\",\"p\":{\"open\":%s,\"seconds\":%u}}",
        open ? "true" : "false", (unsigned)seconds);
    emit(line);
}

bool        ha_mqtt_connected(void)          { return true; }
const char *ha_mqtt_base_topic(void)         { return "c6/uart"; }
void        ha_mqtt_logf(const char *t, const char *f, ...) { (void)t; (void)f; }
void        ha_mqtt_emit_raw(const char *line) { emit(line); }
