/*
 * zb_ota_client.c – Zigbee OTA Upgrade Client（中继器版本）
 *
 * 与 client/main/zb_ota_client.c 的差别：
 *   1. image type = 0x0002（终端节点用 0x0001，必须分开）
 *   2. 没有 MQTT：进度只走 ESP_LOGI，不调用 ha_mqtt_publish_ota_status()
 *   3. 版本号写 NVS：否则重启后又自称 version=1，网关每次广播都会重下一遍
 *
 * 中继是常电 Router，不涉及休眠设备那套 fast-poll / turbo-poll 处理。
 */
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "nvs.h"
#include "esp_zigbee_core.h"
#include "esp_zigbee_ota.h"
#include "esp_zigbee_attribute.h"
#include "esp_zigbee_cluster.h"
#include "zcl/esp_zigbee_zcl_ota.h"
#include "zb_ota_client.h"

#define TAG            "ota_cli"
#define OTA_MFR_CODE   0x131B
#define OTA_MAX_DATA   223   /* ★ 客户端最大块尺寸。ESP 官方例程就用 223：
                              * 块数 9546 → 2740，直接省 3.5 倍时间。
                              * 82 B 以上靠 APS 分片，ZBOSS 支持。*/
#define NVS_NS         "rep"
#define NVS_KEY_VER    "ota_ver"

static esp_ota_handle_t       s_ota_handle    = 0;
static const esp_partition_t *s_ota_partition = NULL;
static uint32_t               s_written       = 0;
static uint32_t               s_total         = 0;
static uint32_t               s_new_version   = 0;
static uint32_t               s_local_version = 0;
static bool                   s_ver_loaded    = false;
/* ★ 正在下载的权威标志。
 *   原来 zb_ota_client_busy() 靠 `s_total > 0 && s_written < s_total` 判断，
 *   但实测 `msg->ota_header.image_size` 在 START 事件里是 **0**（包装层没填），
 *   于是 busy() 永远返回 false。后果很严重：OTA 期间只要冒出一次
 *   ESP_ZB_NWK_SIGNAL_NO_ACTIVE_LINKS_LEFT，`repeater.c` 里那道
 *   `!zb_ota_client_busy()` 的闸门就形同虚设，直接重入网把传输打断
 *   （2026-10-26 实测日志 `OTA 接收中 4181 / 0 B (0%)`）。
 *   改成显式标志：START 置位，APPLY/FINISH/ABORT 清除，与长度是否已知无关。 */
static bool                   s_busy          = false;

/* ── 版本号读写（NVS） ──────────────────────────────────────────────────── */
static void ver_load(void) {
    if (s_ver_loaded) return;
    s_ver_loaded = true;
    s_local_version = 1;
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) == ESP_OK) {
        uint32_t v = 1;
        if (nvs_get_u32(h, NVS_KEY_VER, &v) == ESP_OK && v > 1) {
            s_local_version = v;
        }
        nvs_close(h);
    }
}

static void ver_store(uint32_t v) {
    if (v <= 1) return;
    s_local_version = v;
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_u32(h, NVS_KEY_VER, v);
        nvs_commit(h);
        nvs_close(h);
        ESP_LOGI(TAG, "版本号写入 NVS: 0x%08lx", (unsigned long)v);
    }
}

uint32_t zb_ota_client_local_version(void) {
    ver_load();
    return s_local_version;
}

/* 正在下载（START 之后、APPLY/FINISH/ABORT 之前）*/
bool zb_ota_client_busy(void) {
    return s_busy;
}

/* ── OTA 回调 ───────────────────────────────────────────────────────────── */
esp_err_t zb_ota_client_handle(const void *message) {
    const esp_zb_zcl_ota_upgrade_value_message_t *msg = message;
    if (!msg || msg->info.status != ESP_ZB_ZCL_STATUS_SUCCESS) return ESP_OK;

    esp_err_t ret = ESP_OK;

    switch (msg->upgrade_status) {

    case ESP_ZB_ZCL_OTA_UPGRADE_STATUS_START:
        s_total       = msg->ota_header.image_size;
        s_new_version = msg->ota_header.file_version;
        s_written     = 0;
        s_busy        = true;
        s_ota_partition = esp_ota_get_next_update_partition(NULL);
        if (!s_ota_partition) {
            ESP_LOGE(TAG, "找不到 OTA 分区");
            return ESP_FAIL;
        }
        ret = esp_ota_begin(s_ota_partition, OTA_WITH_SEQUENTIAL_WRITES, &s_ota_handle);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "esp_ota_begin: %s", esp_err_to_name(ret));
            return ret;
        }
        ESP_LOGI(TAG, "OTA 开始 – 共 %lu B → 分区 %s（新版本 0x%08lx）%s",
                 (unsigned long)s_total, s_ota_partition->label,
                 (unsigned long)s_new_version,
                 s_total ? "" : "  [!] 头里的 image_size 为 0，进度百分比不可用");
        break;

    case ESP_ZB_ZCL_OTA_UPGRADE_STATUS_RECEIVE:
        if (msg->payload && msg->payload_size > 0) {
            ret = esp_ota_write(s_ota_handle, msg->payload, msg->payload_size);
            if (ret != ESP_OK) {
                ESP_LOGE(TAG, "esp_ota_write: %s", esp_err_to_name(ret));
                return ret;
            }
            s_written += msg->payload_size;
            /* 每 4 KB 报一次，避免刷屏 */
            if ((s_written & 0x0FFF) < (uint32_t)msg->payload_size) {
                if (s_total) {
                    ESP_LOGI(TAG, "OTA 接收中 %lu / %lu B (%lu%%)",
                             (unsigned long)s_written, (unsigned long)s_total,
                             (unsigned long)((uint64_t)s_written * 100 / s_total));
                } else {
                    ESP_LOGI(TAG, "OTA 接收中 %lu B（总长度未知）",
                             (unsigned long)s_written);
                }
            }
        }
        break;

    case ESP_ZB_ZCL_OTA_UPGRADE_STATUS_APPLY:
        s_busy = false;                /* 下载结束，先放开闸门再做收尾 */
        ret = esp_ota_end(s_ota_handle);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "esp_ota_end: %s", esp_err_to_name(ret));
            return ret;
        }
        /* 先记版本号再切分区：切完就重启，来不及写 NVS */
        ver_store(s_new_version);
        ret = esp_ota_set_boot_partition(s_ota_partition);
        if (ret != ESP_OK) {
            ESP_LOGE(TAG, "esp_ota_set_boot: %s", esp_err_to_name(ret));
            return ret;
        }
        ESP_LOGW(TAG, "OTA 完成 – 500 ms 后重启进入新固件");
        vTaskDelay(pdMS_TO_TICKS(500));
        esp_restart();
        break;

    case ESP_ZB_ZCL_OTA_UPGRADE_STATUS_FINISH:
        s_busy = false;
        ESP_LOGI(TAG, "OTA finish – ver=0x%08lx size=%lu B",
                 (unsigned long)msg->ota_header.file_version,
                 (unsigned long)msg->ota_header.image_size);
        break;

    case ESP_ZB_ZCL_OTA_UPGRADE_STATUS_ABORT:
        s_busy = false;
        ESP_LOGW(TAG, "OTA 被中止（已写 %lu B）", (unsigned long)s_written);
        break;

    default:
        break;
    }
    return ret;
}

/* ── Cluster-Attribute ──────────────────────────────────────────────────── */
esp_zb_attribute_list_t *zb_ota_client_cluster_create(void) {
    esp_zb_ota_cluster_cfg_t cfg = {
        .ota_upgrade_file_version        = zb_ota_client_local_version(),
        .ota_upgrade_manufacturer        = OTA_MFR_CODE,
        .ota_upgrade_image_type          = OTA_IMAGE_TYPE_REPEATER,
        .ota_min_block_reque             = 0,
        .ota_upgrade_file_offset         = 0xFFFFFFFF,
        .ota_upgrade_downloaded_file_ver = 0xFFFFFFFF,
        .ota_image_upgrade_status        = 0x00,
    };
    memset(cfg.ota_upgrade_server_id, 0xFF, sizeof(cfg.ota_upgrade_server_id));

    esp_zb_attribute_list_t *attr = esp_zb_ota_cluster_create(&cfg);

    esp_zb_zcl_ota_upgrade_client_variable_t var = {
        .timer_query   = 5,   /* 每 5 分钟问一次有没有新固件 */
        .hw_version    = 0x0001,
        .max_data_size = OTA_MAX_DATA,
    };
    esp_zb_ota_cluster_add_attr(attr,
        ESP_ZB_ZCL_ATTR_OTA_UPGRADE_CLIENT_DATA_ID, &var);
    return attr;
}
