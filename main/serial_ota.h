/*
 * serial_ota.h – Coordinator-Firmware-Update über USB-Serial
 *
 * Protokoll Pi→C6 (JSON-Lines, analog zum Zigbee-OTA):
 *   {"cmd":"fw_ota_start","size":<N>}              → esp_ota_begin()
 *   {"cmd":"fw_ota_data","off":<O>,"data":"<HEX>"} → esp_ota_write()
 *   {"cmd":"fw_ota_end"}                           → esp_ota_end()+Boot+Restart
 * Antworten C6→Pi: {"t":"fw_ota","st":"begin|write|done|error",...}
 */
#pragma once
#include <stdint.h>

/* Startet eine OTA-Session (image_size für die End-Prüfung). */
void serial_ota_start(uint32_t size);

/* Schreibt einen Block; off = Dateioffset (muss zum geschriebenen Stand passen),
 * hex = Hex-String der Nutzdaten, hex_len = dessen Länge in Zeichen. */
void serial_ota_data(uint32_t off, const char *hex, int hex_len);

/* Beendet die Session, setzt die Boot-Partition und startet neu. */
void serial_ota_end(void);

/* Bricht die Session ab (Handle freigeben, ohne Boot-Wechsel). */
void serial_ota_abort(void);
