/*
 * zb_ota_client.c —— Zigbee OTA 升级客户端
 *
 * 通过 ZCL OTA Upgrade 簇接收固件分块，顺序写入空闲的 OTA 分区。
 * 全部收完后：激活该分区并重启。
 */
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_zigbee_core.h"
#include "esp_zigbee_ota.h"
#include "esp_zigbee_attribute.h"
#include "zcl/esp_zigbee_zcl_ota.h"
#include "ha_mqtt.h"
#include "zb_ota_client.h"

#define TAG            "ota_cli"
#define OTA_MFR_CODE   0x131B
#define OTA_IMAGE_TYPE 0x0001
#define OTA_MAX_DATA   64

static esp_ota_handle_t            s_ota_handle     = 0;
static const esp_partition_t      *s_ota_partition  = NULL;
static uint32_t                    s_written        = 0;
static uint32_t                    s_total          = 0;

/* ── OTA-Upgrade-Callback ────────────────────────────────────────────────── */
esp_err_t zb_ota_client_handle(const void *message)
{
    const esp_zb_zcl_ota_upgrade_value_message_t *msg = message;
    if (!msg || msg->info.status != ESP_ZB_ZCL_STATUS_SUCCESS) return ESP_OK;

    esp_err_t ret = ESP_OK;

    switch (msg->upgrade_status) {

    case ESP_ZB_ZCL_OTA_UPGRADE_STATUS_START:
        s_total    = msg->ota_header.image_size;
        s_written  = 0;
        s_ota_partition = esp_ota_get_next_update_partition(NULL);
        if (!s_ota_partition) {
            ESP_LOGE(TAG, "Keine OTA-Partition gefunden");
            ha_mqtt_publish_ota_status("error", 0, 0);
            return ESP_FAIL;
        }
        ret = esp_ota_begin(s_ota_partition, OTA_WITH_SEQUENTIAL_WRITES, &s_ota_handle);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "esp_ota_begin: %s", esp_err_to_name(ret));
            ha_mqtt_publish_ota_status("error", 0, s_total);
            return ret;
        }
        ESP_LOGI(TAG, "OTA start – total %lu B → %s",
                 (unsigned long)s_total, s_ota_partition->label);
        ha_mqtt_publish_ota_status("started", 0, s_total);
        break;

    case ESP_ZB_ZCL_OTA_UPGRADE_STATUS_RECEIVE:
        if (msg->payload && msg->payload_size > 0) {
            ret = esp_ota_write(s_ota_handle, msg->payload, msg->payload_size);
            if (ret != ESP_OK) {
                ESP_LOGE(TAG, "esp_ota_write: %s", esp_err_to_name(ret));
                ha_mqtt_publish_ota_status("error", s_written, s_total);
                return ret;
            }
            s_written += msg->payload_size;
            /* Progress alle 4 KB melden */
            if ((s_written & 0xFFF) < (uint32_t)msg->payload_size) {
                ha_mqtt_publish_ota_status("receiving", s_written, s_total);
            }
        }
        break;

    case ESP_ZB_ZCL_OTA_UPGRADE_STATUS_APPLY:
        ret = esp_ota_end(s_ota_handle);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "esp_ota_end: %s", esp_err_to_name(ret));
            ha_mqtt_publish_ota_status("error", s_written, s_total);
            return ret;
        }
        ret = esp_ota_set_boot_partition(s_ota_partition);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "esp_ota_set_boot: %s", esp_err_to_name(ret));
            ha_mqtt_publish_ota_status("error", s_written, s_total);
            return ret;
        }
        ESP_LOGI(TAG, "OTA apply – Neustart in 500 ms");
        ha_mqtt_publish_ota_status("applying", s_written, s_total);
        vTaskDelay(pdMS_TO_TICKS(500));
        esp_restart();
        break;

    case ESP_ZB_ZCL_OTA_UPGRADE_STATUS_FINISH:
        ESP_LOGI(TAG, "OTA finish – ver=0x%lx size=%lu B",
                 (unsigned long)msg->ota_header.file_version,
                 (unsigned long)msg->ota_header.image_size);
        ha_mqtt_publish_ota_status("done", s_written, s_total);
        break;

    case ESP_ZB_ZCL_OTA_UPGRADE_STATUS_ABORT:
        ESP_LOGW(TAG, "OTA aborted");
        ha_mqtt_publish_ota_status("aborted", s_written, s_total);
        break;

    default:
        break;
    }
    return ret;
}

/* ── 客户端端点的簇属性 ──────────────────────────────────────────────────── */
esp_zb_attribute_list_t *zb_ota_client_cluster_create(void)
{
    /* 把当前固件版本当作「正在运行」的版本号 */
    esp_zb_ota_cluster_cfg_t cfg = {
        .ota_upgrade_file_version      = 0x00000001,
        .ota_upgrade_manufacturer      = OTA_MFR_CODE,
        .ota_upgrade_image_type        = OTA_IMAGE_TYPE,
        .ota_min_block_reque           = 0,
        .ota_upgrade_file_offset       = 0xFFFFFFFF,
        .ota_upgrade_downloaded_file_ver = 0xFFFFFFFF,
        .ota_image_upgrade_status      = 0x00,   /* Normal */
    };
    memset(cfg.ota_upgrade_server_id, 0xFF, sizeof(cfg.ota_upgrade_server_id));

    esp_zb_attribute_list_t *attr = esp_zb_ota_cluster_create(&cfg);

    esp_zb_zcl_ota_upgrade_client_variable_t var = {
        .timer_query  = 5,    /* 每 5 分钟问一次有没有新固件 */
        .hw_version   = 0x0001,
        .max_data_size = OTA_MAX_DATA,
    };
    esp_zb_ota_cluster_add_attr(attr,
        ESP_ZB_ZCL_ATTR_OTA_UPGRADE_CLIENT_DATA_ID, &var);
    return attr;
}
