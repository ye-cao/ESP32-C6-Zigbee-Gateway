#pragma once
#include "esp_err.h"
#include "esp_zigbee_core.h"

/*
 * zb_ota_client – Zigbee OTA Upgrade Client (Router-Seite)
 *
 * 通过 Zigbee OTA 簇从协调器接收固件更新。
 * 数据写入第二个 OTA 分区，完成后重启。
 *
 * C6→Pi (via ha_mqtt_publish_ota_status):
 *   {"t":"ota_progress","status":"started"|"receiving"|"applying"|"done"|"error",
 *    "off":<bytes_written>,"total":<total_bytes>}
 */

/* 创建 OTA 客户端簇属性，供端点组装时使用 */
esp_zb_attribute_list_t *zb_ota_client_cluster_create(void);

/* OTA-Upgrade-Callback – in zb_action_handler aufrufen */
esp_err_t zb_ota_client_handle(const void *message);
