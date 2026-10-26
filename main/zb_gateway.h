#pragma once
#include <stdint.h>
#include <stdbool.h>

/* Maximale Gerätezahl = Netzgröße. Dieser Wert dimensioniert die ZBOSS-
 * Tabellen (esp_zb_overall_network_size_set) und die eigene Geräte-DB.
 * Puffer in main.c leiten ihre Größe davon ab – nicht unabhängig hochsetzen. */
#define ZB_GATEWAY_MAX_DEVICES 64

/* Zigbee-Koordinator starten */
void zb_gateway_start(void);

/* ★ 2026-10-25: true, sobald esp_zb_start() durch ist. Vorher ist JEDER
 * ZBOSS-Aufruf (esp_zb_scheduler_alarm, zcl_*_req, bdb_*) ein NULL-Deref –
 * der uart_rx-Task läuft aber schon. Siehe Kommentar in main.c/on_uart_cmd. */
bool zb_gateway_is_ready(void);

/* Permit Join für N Sekunden öffnen (0 = sofort schließen) */
void zb_gateway_permit_join(uint8_t seconds);

/* Geräteliste ins Log schreiben (Debug) */
void zb_gateway_list_devices(void);

/* Geräteliste als JSON-Array in buf schreiben (thread-safe) */
void zb_gateway_devices_json(char *buf, size_t len);

/* Zigbee-Kanal setzen (11-26), speichert in NVS und startet neu.
 * Default: 20, Fallback: 25 (beim nächsten Neustart wirksam). */
void zb_gateway_set_channel(uint8_t ch);

/* Alle Kanäle 11-26 scannen (~4s/Kanal), Ergebnisse als JSON ausgeben */
void zb_gateway_scan_channels(void);


/* LQI aller bekannten Devices als JSON-Array in buf schreiben */
void zb_gateway_lqi_json(char *buf, size_t len);
int8_t zb_gateway_get_last_rssi(void);

/* Alle bekannten Kurzadressen (ohne 0x0000) nach out[] kopieren, max. max Stück.
 * Rückgabe = Anzahl. Für die Topologie-Erfassung (Mgmt_Lqi an jedes Gerät) –
 * nur Geräte, mit denen schon gesprochen wurde, antworten überhaupt. */
uint8_t zb_gateway_known_addrs(uint16_t *out, uint8_t max);

/* Gerätesteuerung: On/Off-Befehl an einen Endpoint senden
 * on_off_cmd: 0=off 1=on 2=toggle */
void zb_gateway_dev_cmd(uint16_t addr, uint8_t ep, uint8_t on_off_cmd);

/* 中继发射功率：写厂商专有簇 0xFC00 的属性 0x0001（S8，dBm）。
 * 中继固件里这个属性是「写入即生效 + 落 NVS」，所以调功率不用重刷固件。
 * 结果回一行 {"t":"wr_power","addr":"0x…","ep":N,"dbm":V,"tsn":T}。 */
void zb_gateway_write_tx_power(uint16_t addr, uint8_t ep, int8_t dbm);

/* Basic-Cluster (Hersteller/Modell) eines Geräts lesen – für Geräte, die schon
 * im Netz sind, aber nichts von sich hören lassen und deshalb nie interviewt
 * wurden. Ergebnis kommt als {"t":"zigbee","sub":"basic"} zurück. */
void zb_gateway_probe_basic(uint16_t addr, uint8_t ep);

/* Node-Descriptor (ZDO 0x0002) lesen: klärt, ob ein Gerät als Router (ZR)
 * arbeitet – nur Router leiten Fremdverkehr weiter. Ergebnis:
 * {"t":"node_desc","addr":"0x…","cap":N,"role":"router|end_device",
 *  "power":"mains|battery","rx_idle":true|false} */
void zb_gateway_node_desc(uint16_t addr);

/* On/Off-Zustand aktiv zurücklesen (Read Attributes auf 0x0006/0x0000).
 * Nötig für Geräte, die auf Fernbefehle nur mit Default-Response antworten
 * und KEINEN Report schicken (z. B. HGZB-043) – damit HA den echten Zustand
 * bestätigt bekommt statt nur der optimistischen Annahme des Gateways.
 * Ergebnis: {"t":"zigbee","sub":"epN/on_off","p":{"v":0|1}} */
void zb_gateway_read_onoff(uint16_t addr, uint8_t ep);

/* switch2wifi-Befehl via Zigbee an alle gepairten Devices senden */
void zb_gateway_send_switch2wifi_all(void);
void zb_gateway_send_set_sleep(uint32_t seconds);

/* Sendeleistung zur Laufzeit setzen und den WIRKSAMEN Wert zurückmelden.
 * Ergebnis: {"t":"txpower","want":N,"eff":M}
 *
 * Warum der Rücklesewert zählt: esp_zb_set_tx_power() nimmt den Wunschwert
 * entgegen, begrenzt ihn aber intern auf den vom PHY unterstützten Bereich.
 * Nur esp_ieee802154_get_txpower() sagt, was die PA wirklich fährt. */
void   zb_gateway_set_tx_power(int8_t dbm);
int8_t zb_gateway_get_tx_power(void);

/* LQI/RSSI aller bekannten Geräte sofort ausgeben (statt bis zum nächsten
 * 60-s-Heartbeat zu warten).
 * Ergebnis: {"t":"lqi","ms":<uptime_ms>,"dev":[{"addr":"0x0684","lqi":220,"rssi":-58},…]}
 *
 * Der Wert gilt für den *zuletzt* empfangenen Frame des jeweiligen Geräts –
 * er ist kein laufender Durchschnitt. Wer zwei Sendeleistungen vergleicht,
 * sollte deshalb erst einen Read auslösen und dann diese Momentaufnahme
 * abholen, sonst vergleicht er Messwerte von vor Minuten. */
void zb_gateway_report_lqi(void);
