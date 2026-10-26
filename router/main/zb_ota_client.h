#pragma once
#include "esp_err.h"
#include "esp_zigbee_core.h"

/*
 * zb_ota_client – Zigbee OTA Upgrade Client (Router / Repeater)
 *
 * 从 client/ 移植过来的裁剪版：中继没有 MQTT，所以去掉了所有
 * ha_mqtt_publish_ota_status() 调用，进度只走 ESP_LOGI。
 *
 * ★ image type 用 0x0002，与终端节点（0x0001）严格分开。
 *   同一个网络里两个 image type 混用会让网关的 Image Notify 广播
 *   被两类设备同时应答，镜像内容不匹配的那个照样往自己分区写。
 *
 * 版本号存在 NVS（key "ota_ver"）：
 *   OTA 完成并 esp_ota_set_boot_partition() 成功后写入新版本号，
 *   下次启动时作为 ota_upgrade_file_version 上报。否则客户端永远
 *   自称 version=1，网关每次广播都会让它重下一遍。
 */

#define OTA_IMAGE_TYPE_REPEATER  0x0002

/* 已应用的固件版本（NVS 里没有就是 1） */
uint32_t zb_ota_client_local_version(void);

/* 创建 OTA-Client-Cluster（含 client 变量属性） */
esp_zb_attribute_list_t *zb_ota_client_cluster_create(void);

/* OTA 升级回调 —— 在 core action handler 里转进来 */
esp_err_t zb_ota_client_handle(const void *message);

/* 是否正在下载镜像。正在 OTA 时不要重启/重新入网，会把下载打断。 */
bool zb_ota_client_busy(void);
