/*
 * zb_device.c – Zigbee Router (verbindet sich mit Coordinator)
 *
 * Cluster: On/Off (server-side – steuerbar vom Coordinator)
 * Meldet Attributwechsel via ha_mqtt_publish_attr()
 */
#include <string.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_zigbee_core.h"
#include "ha/esp_zigbee_ha_standard.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_ieee802154.h"
#include "esp_partition.h"
#include "esp_system.h"
#include "esp_zigbee_cluster.h"
#include "esp_zigbee_attribute.h"
#include "esp_zigbee_endpoint.h"
#include "zb_device.h"
#include "zb_ota_client.h"
#include "ha_mqtt.h"
#include "wifi_switch.h"

#define ZB_NVS_NAMESPACE "zb_cfg"
#define ZB_NVS_KEY_CH    "channel"

#define TAG            "zb_dev"
#define ZB_EP          1
#define JOIN_RETRY_MS  30000   /* 未入网时每 30 s 试一次 */
#define DEFAULT_CH     20

static bool      s_joined      = false;
static uint16_t  s_pan_id      = 0;
static uint8_t   s_channel     = 0;
static volatile int16_t s_temp_pending = (int16_t)0x8000;
static volatile uint32_t s_temp_report_ms = 605000UL;  /* 605 s default */

/* 前向声明 */
static void temp_report_periodic(uint8_t param);   /* 65 s —— 紧跟 60 s 心跳之后 */

/* ── ZCL Attribute Handler ──────────────────────────────────────────────── */
static esp_err_t zb_action_handler(esp_zb_core_action_callback_id_t id,
                                    const void *msg) {
    if (id == ESP_ZB_CORE_OTA_UPGRADE_VALUE_CB_ID)
        return zb_ota_client_handle(msg);

    if (id == ESP_ZB_CORE_CMD_CUSTOM_CLUSTER_REQ_CB_ID) {
        const esp_zb_zcl_custom_cluster_command_message_t *m = msg;
        if (m && m->info.cluster == 0xFF01) {
            if (m->info.command.id == 0x01) {
                zb_device_switch2wifi_request();
            } else if (m->info.command.id == 0x02 && m->data.size >= 4) {
                uint32_t secs = *(uint32_t *)m->data.value;
                zb_device_set_sleep(secs);
            }
        }
        return ESP_OK;
    }
    if (id != ESP_ZB_CORE_SET_ATTR_VALUE_CB_ID) return ESP_OK;

    const esp_zb_zcl_set_attr_value_message_t *m = msg;
    if (!m) return ESP_ERR_INVALID_ARG;

    if (m->info.cluster == ESP_ZB_ZCL_CLUSTER_ID_ON_OFF &&
        m->attribute.id == ESP_ZB_ZCL_ATTR_ON_OFF_ON_OFF_ID) {
        uint8_t val = *(uint8_t *)m->attribute.data.value;
        char jval[4];
        snprintf(jval, sizeof(jval), "%u", (unsigned)val);
        ha_mqtt_publish_attr("on_off", jval);
    }
    return ESP_OK;
}

/* ── 周期性的入网看门狗（未入网时每 30 s 一次） ─────────────────────────── */
static void join_retry_alarm(uint8_t param) {
    (void)param;
    if (!s_joined) {
        ESP_LOGI(TAG, "Join-Retry ch=%d", DEFAULT_CH);
        esp_zb_set_primary_network_channel_set(1U << DEFAULT_CH);
        esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING);
    }
    esp_zb_scheduler_alarm(join_retry_alarm, 0, JOIN_RETRY_MS);
}

/* ── Netzwerk-Signale ───────────────────────────────────────────────────── */
void esp_zb_app_signal_handler(esp_zb_app_signal_t *signal_struct) {
    uint32_t *sg_p = signal_struct->p_app_signal;
    esp_err_t  err = signal_struct->esp_err_status;
    esp_zb_app_signal_type_t sig = *sg_p;

    switch (sig) {
    case ESP_ZB_ZDO_SIGNAL_SKIP_STARTUP:
        esp_zb_bdb_start_top_level_commissioning(
            ESP_ZB_BDB_MODE_INITIALIZATION);
        break;

    case ESP_ZB_BDB_SIGNAL_DEVICE_FIRST_START:
    case ESP_ZB_BDB_SIGNAL_DEVICE_REBOOT:
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "%s – suche Netzwerk ch=%d",
                     esp_zb_bdb_is_factory_new() ? "Erster Start" : "Neustart",
                     DEFAULT_CH);
            esp_zb_set_primary_network_channel_set(1U << DEFAULT_CH);
            esp_zb_bdb_start_top_level_commissioning(
                ESP_ZB_BDB_MODE_NETWORK_STEERING);
            /* 启动看门狗：未入网时每 30 s 试一次入网 */
            esp_zb_scheduler_alarm(join_retry_alarm, 0, JOIN_RETRY_MS);
        }
        break;

    case ESP_ZB_BDB_SIGNAL_STEERING:
        if (err == ESP_OK) {
            s_joined  = true;
            s_pan_id  = esp_zb_get_pan_id();
            s_channel = esp_zb_get_current_channel();
            ESP_LOGI(TAG, "Verbunden – PAN 0x%04hx Kanal %d", s_pan_id, s_channel);
            ha_mqtt_publish_joined(s_pan_id, s_channel);
            /* Periodischen Temp-Report im Zigbee-Kontext starten */
            esp_zb_scheduler_alarm_cancel(temp_report_periodic, 0);
            esp_zb_scheduler_alarm(temp_report_periodic, 0, s_temp_report_ms);
        } else {
            ESP_LOGW(TAG, "Steering fehlgeschlagen – Watchdog übernimmt Retry");
        }
        break;

    case ESP_ZB_ZDO_SIGNAL_LEAVE:
        s_joined = false;
        ESP_LOGI(TAG, "Netzwerk verlassen");
        ha_mqtt_publish_left();
        /* automatisch neu verbinden */
        esp_zb_scheduler_alarm(
            (esp_zb_callback_t)(void *)esp_zb_bdb_start_top_level_commissioning,
            ESP_ZB_BDB_MODE_NETWORK_STEERING, 3000);
        break;

    default:
        ESP_LOGD(TAG, "ZB-Signal: %s err=%s",
                 esp_zb_zdo_signal_to_string(sig), esp_err_to_name(err));
        break;
    }
}

/* ── Zigbee Haupt-Task ──────────────────────────────────────────────────── */
static void zb_task(void *arg) {
    esp_zb_cfg_t cfg = {
        .esp_zb_role        = ESP_ZB_DEVICE_TYPE_ROUTER,
        .install_code_policy = false,
        .nwk_cfg.zczr_cfg   = { .max_children = 10 },
    };
    esp_zb_init(&cfg);
    esp_zb_set_tx_power(20);   /* max TX-Power: 20 dBm */

    /* Endpoint manuell aufbauen: Basic + Identify + On/Off-Server + OTA-Client */
    esp_zb_cluster_list_t *cl = esp_zb_zcl_cluster_list_create();

    esp_zb_basic_cluster_cfg_t basic_cfg = { .zcl_version = 3, .power_source = 0x03 };
    esp_zb_attribute_list_t *basic_attr = esp_zb_basic_cluster_create(&basic_cfg);
    /* ZCL 字符串：第一个字节 = 长度 */
    static const char mfr_str[]   = "\x09Espressif";   /* len=9 */
    static const char model_str[] = "\x0cESP32-C6-ZB";  /* len=12 */
    esp_zb_basic_cluster_add_attr(basic_attr, ESP_ZB_ZCL_ATTR_BASIC_MANUFACTURER_NAME_ID, (void *)mfr_str);
    esp_zb_basic_cluster_add_attr(basic_attr, ESP_ZB_ZCL_ATTR_BASIC_MODEL_IDENTIFIER_ID,  (void *)model_str);
    /* DateCode attr 0x0006 – Compile-Datum "YYYYMMDD" als ZCL-String */
    static char date_str[10];  /* \x08YYYYMMDD */
    {
        static const char *months = "JanFebMarAprMayJunJulAugSepOctNovDec";
        const char *d = __DATE__;
        int mo = 1; for (; mo <= 12; mo++) if (strncmp(d, months+(mo-1)*3, 3)==0) break;
        snprintf(date_str, sizeof(date_str), "\x08%04d%02d%02d", atoi(d+7), mo, atoi(d+4));
    }
    esp_zb_basic_cluster_add_attr(basic_attr, ESP_ZB_ZCL_ATTR_BASIC_DATE_CODE_ID, (void *)date_str);
    esp_zb_cluster_list_add_basic_cluster(cl, basic_attr, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);

    esp_zb_identify_cluster_cfg_t id_cfg = { .identify_time = 0 };
    esp_zb_cluster_list_add_identify_cluster(cl,
        esp_zb_identify_cluster_create(&id_cfg), ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);

    esp_zb_on_off_cluster_cfg_t on_off_cfg = { .on_off = false };
    esp_zb_cluster_list_add_on_off_cluster(cl,
        esp_zb_on_off_cluster_create(&on_off_cfg), ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);

    esp_zb_temperature_meas_cluster_cfg_t temp_cfg = {
        .measured_value = 0x8000,   /* 无效值 */
        .min_value      = -5000,    /* -50°C */
        .max_value      =  8500,    /* +85°C */
    };
    esp_zb_cluster_list_add_temperature_meas_cluster(cl,
        esp_zb_temperature_meas_cluster_create(&temp_cfg), ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);

    /* Custom cluster 0xFF01: switch2wifi command (SERVER) */
    esp_zb_attribute_list_t *sw_attr = esp_zb_zcl_attr_list_create(0xFF01);
    esp_zb_cluster_list_add_custom_cluster(cl, sw_attr, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);

    esp_zb_cluster_list_add_ota_cluster(cl,
        zb_ota_client_cluster_create(), ESP_ZB_ZCL_CLUSTER_CLIENT_ROLE);

    esp_zb_ep_list_t *ep = esp_zb_ep_list_create();
    esp_zb_ep_list_add_ep(ep, cl, ZB_EP,
        ESP_ZB_AF_HA_PROFILE_ID, ESP_ZB_HA_ON_OFF_LIGHT_DEVICE_ID);
    esp_zb_device_register(ep);

    esp_zb_core_action_handler_register(zb_action_handler);

    /* 从 NVS 读信道，默认 20 */
    uint8_t ch = 20;
    nvs_handle_t nvs_h;
    if (nvs_open(ZB_NVS_NAMESPACE, NVS_READONLY, &nvs_h) == ESP_OK) {
        nvs_get_u8(nvs_h, ZB_NVS_KEY_CH, &ch);
        nvs_close(nvs_h);
    }
    ESP_LOGI(TAG, "Primärer Scan-Kanal: %d", ch);
    esp_zb_set_primary_network_channel_set(1U << ch);

    ESP_ERROR_CHECK(esp_zb_start(false));
    esp_zb_main_loop_iteration();
}

/* ── 对外接口 ───────────────────────────────────────────────────────────── */
void zb_device_start(void) {
    esp_zb_platform_config_t pcfg = {
        .radio_config = { .radio_mode = RADIO_MODE_NATIVE },
        .host_config  = { .host_connection_mode = HOST_CONNECTION_MODE_NONE },
    };
    ESP_ERROR_CHECK(esp_zb_platform_config(&pcfg));
    xTaskCreate(zb_task, "zb_main", 8192, NULL, 5, NULL);
}

void zb_device_set_onoff(bool on) {
    /* Lokales Attribut setzen + reporten */
    uint8_t val = on ? 1 : 0;
    esp_zb_zcl_set_attribute_val(ZB_EP,
        ESP_ZB_ZCL_CLUSTER_ID_ON_OFF,
        ESP_ZB_ZCL_CLUSTER_SERVER_ROLE,
        ESP_ZB_ZCL_ATTR_ON_OFF_ON_OFF_ID,
        &val, false);
    char jval[4];
    snprintf(jval, sizeof(jval), "%u", (unsigned)val);
    ha_mqtt_publish_attr("on_off", jval);
}

void zb_device_permit_join(uint8_t seconds) {
    esp_zb_bdb_open_network(seconds);
}

void zb_device_leave(void) {
    esp_zb_zdo_device_leave_req(NULL, true, true);
}

/* 周期性温度上报 —— 只在 Zigbee 任务上下文里跑 */
static void temp_report_periodic(uint8_t param) {
    (void)param;
    if (s_joined) {
        int16_t t = s_temp_pending;
        esp_zb_zcl_set_attribute_val(ZB_EP,
            ESP_ZB_ZCL_CLUSTER_ID_TEMP_MEASUREMENT,
            ESP_ZB_ZCL_CLUSTER_SERVER_ROLE,
            ESP_ZB_ZCL_ATTR_TEMP_MEASUREMENT_VALUE_ID,
            &t, false);
        esp_zb_zcl_report_attr_cmd_t cmd = {
            .zcl_basic_cmd = {
                .dst_addr_u  = { .addr_short = 0x0000 },
                .dst_endpoint = 1,
                .src_endpoint = ZB_EP,
            },
            .address_mode = ESP_ZB_APS_ADDR_MODE_16_ENDP_PRESENT,
            .clusterID    = ESP_ZB_ZCL_CLUSTER_ID_TEMP_MEASUREMENT,
            .cluster_role = ESP_ZB_ZCL_CLUSTER_SERVER_ROLE,
            .attributeID  = ESP_ZB_ZCL_ATTR_TEMP_MEASUREMENT_VALUE_ID,
        };
        esp_zb_zcl_report_attr_cmd_req(&cmd);
    }
    /* 自己重新排下一次 */
    esp_zb_scheduler_alarm(temp_report_periodic, 0, s_temp_report_ms);
}

/* 由 heartbeat 任务调用：只写值（int16 写入是原子的） */
void zb_device_report_temp(int16_t temp_100) {
    s_temp_pending = temp_100;
}

bool     zb_device_joined(void)  { return s_joined; }
uint16_t zb_device_pan_id(void)  { return s_pan_id; }
uint8_t  zb_device_channel(void) { return s_channel; }
int8_t   zb_device_rssi(void)    { return esp_ieee802154_get_recent_rssi(); }

void zb_device_set_channel(uint8_t ch) {
    if (ch < 11 || ch > 26) {
        ESP_LOGE(TAG, "Ungültiger Kanal: %d (erlaubt: 11-26)", ch);
        return;
    }
    nvs_handle_t h;
    if (nvs_open(ZB_NVS_NAMESPACE, NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_u8(h, ZB_NVS_KEY_CH, ch);
        nvs_commit(h);
        nvs_close(h);
        ESP_LOGI(TAG, "Kanal %d in NVS gespeichert", ch);
    } else {
        ESP_LOGE(TAG, "NVS-Schreibfehler");
        return;
    }
    const esp_partition_t *part = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, "zb_storage");
    if (part) {
        esp_err_t err = esp_partition_erase_range(part, 0, part->size);
        ESP_LOGI(TAG, "zb_storage gelöscht: %s", esp_err_to_name(err));
    }
    ESP_LOGI(TAG, "Neustart für Kanalwechsel auf %d...", ch);
    vTaskDelay(pdMS_TO_TICKS(200));
    esp_restart();
}

/* switch2wifi：置 NVS 标志 + 重启（从 Zigbee action handler 里调） */
void zb_device_switch2wifi_request(void) {
    wifi_switch_trigger();
}

/* set_sleep：设置间隔 + 存进 NVS（从 ZCL 回调里调） */
void zb_device_set_sleep(uint32_t seconds) {
    if (seconds < 10) seconds = 10;   /* minimum 10s */
    extern volatile uint32_t s_sleep_ms;
    s_sleep_ms        = seconds * 1000UL;
    s_temp_report_ms  = (seconds + 5) * 1000UL;
    nvs_handle_t h;
    if (nvs_open("client_cfg", NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_u32(h, "sleep_s", seconds);
        nvs_commit(h);
        nvs_close(h);
    }
    char line[64];
    snprintf(line, sizeof(line),
             "{\"t\":\"sleep_set\",\"s\":%lu}", (unsigned long)seconds);
    ha_mqtt_emit_raw(line);
}
