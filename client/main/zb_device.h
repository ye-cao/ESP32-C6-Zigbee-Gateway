#pragma once
#include <stdint.h>
#include <stdbool.h>

/* Zigbee Router starten – verbindet sich mit bestehendem Netz */
void zb_device_start(void);

/* 设置并上报 On/Off 属性 */
void zb_device_set_onoff(bool on);

/* 开 N 秒的配网窗口（路由器也能收设备） */
void zb_device_permit_join(uint8_t seconds);

/* Netzwerk verlassen */
void zb_device_leave(void);

/* 设置信道（存 NVS + 重启，与协调器一致） */
void zb_device_set_channel(uint8_t ch);

/* Aktuellen Netzwerkstatus */
bool     zb_device_joined(void);
uint16_t zb_device_pan_id(void);
uint8_t  zb_device_channel(void);
int8_t   zb_device_rssi(void);
void     zb_device_report_temp(int16_t temp_100);
void     zb_device_switch2wifi_request(void);
void     zb_device_set_sleep(uint32_t seconds);
