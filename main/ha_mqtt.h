#pragma once
#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

/*
 * ha_mqtt – USB Serial JTAG JSON-Lines Bridge
 *
 * C6 → Pi: {"t":"boot"}
 *           {"t":"zigbee","addr":<uint>,"sub":"<name>","p":{...}}
 *           {"t":"permit_join","p":{"open":true/false,"seconds":<n>}}
 *           {"t":"heartbeat","uptime":<s>,"ch":<n>,"pan":"0x<hex>"}
 * Pi → C6: {"cmd":"permit_join","sec":<n>}
 *           {"cmd":"set_channel","ch":<11-26>}
 */

typedef void (*mqtt_cmd_cb_t)(const char *cmd, const char *payload, int len);

esp_err_t ha_mqtt_init(const char *broker_uri,
                       const char *username,
                       const char *password);

void ha_mqtt_set_cmd_cb(mqtt_cmd_cb_t cb);

void ha_mqtt_publish_zigbee(uint16_t addr,
                             const char *subtopic,
                             const char *payload);
/* Wie oben, aber mit Quell-Endpoint (Mehrfach-Geräte / Mehrkanal-Schalter).
 * endpoint <= 1: Topic unverändert "<subtopic>"
 * endpoint  > 1: Topic wird "<epN>/<subtopic>" (z.B. "ep2/on_off") */
void ha_mqtt_publish_zigbee_ep(uint16_t addr,
                               uint8_t endpoint,
                               const char *subtopic,
                               const char *payload);
void ha_mqtt_publish_permit_join(bool open, uint8_t seconds);
void ha_mqtt_publish_heartbeat(uint32_t uptime_s, uint8_t channel, uint16_t pan_id);

bool        ha_mqtt_connected(void);
const char *ha_mqtt_base_topic(void);
void        ha_mqtt_logf(const char *tag, const char *fmt, ...)
    __attribute__((format(printf, 2, 3)));

/* Rohe JSON-Zeile direkt ausgeben (für scan-Ergebnisse) */
void ha_mqtt_emit_raw(const char *line);
