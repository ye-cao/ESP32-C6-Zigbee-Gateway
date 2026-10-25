/*
 * repeater.c – 纯 Zigbee 中继（Zigbee Router）
 *
 * 用途：常电 ESP32-C6 加入现有网络后转发报文，扩展覆盖。
 * 刻意只保留 Basic + Identify 两个簇，且不挂任何业务端点：
 *   → 网关/HA 里能看到它在线，但不会多出虚假的开关/温度实体。
 *
 * 与 client/ 的区别：client 是"能上报数据的路由器"（On/Off + 温度 +
 * switch2wifi + OTA client），本文件是"只转发"的裁剪版。
 *
 * 操作：
 *   短按 BOOT(GPIO9)  → 主动再试一次入网（协调器需先开 permit join）
 *   长按 BOOT 5 秒    → 恢复出厂（退网 + 清除配对信息），用于换网络
 *
 * 注意：路由器必须常电。一旦进入休眠省电就退化成终端设备，不再转发。
 */
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_system.h"
#include "nvs_flash.h"
#include "ha/esp_zigbee_ha_standard.h"
#include "esp_zigbee_core.h"
#include "esp_zigbee_cluster.h"
#include "esp_zigbee_attribute.h"
#include "esp_zigbee_endpoint.h"

#define TAG           "REPEATER"
#define ENDPOINT      1
#define MAX_CHILDREN  20      /* 本中继下面最多挂多少个终端/休眠设备 */
#define BTN_GPIO      GPIO_NUM_9
#define BTN_LONG_MS   5000

/* 发射功率 (dBm)。20 = 芯片上限，但峰值电流也最高：
 * 供电偏弱（长 USB 线 / 弱 USB 口 / 无稳压）时会触发 Brownout 复位，
 * 表现是启动后刚开射频就重启、日志里出现 "E BOD: Brownout detector was triggered"。
 * 遇到这种情况先把这里降到 8 验证，或直接把板子换到独立 5V 供电。 */
#define TX_POWER_DBM  8

/* 主动（重新）入网。协调器必须已开 permit join，否则会失败并每秒重试。 */
static void rejoin(void) {
    ESP_LOGI(TAG, "开始寻找网络（协调器需处于允许入网状态）…");
    esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING);
}

/* 恢复出厂：退出网络并清掉 zb_storage 里的网络密钥/PAN/地址表，然后重启。 */
static void factory_reset(void) {
    ESP_LOGW(TAG, "长按 5 秒 – 恢复出厂，退网并清除配对信息");
    esp_zb_bdb_reset_via_local_action();
    vTaskDelay(pdMS_TO_TICKS(1500));
    esp_restart();
}

/* ── 按键：短按重入网，长按恢复出厂 ─────────────────────────────────────── */
static void btn_task(void *arg) {
    gpio_config_t io = {
        .pin_bit_mask = 1ULL << BTN_GPIO,
        .mode         = GPIO_MODE_INPUT,
        .pull_up_en   = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type    = GPIO_INTR_DISABLE,
    };
    gpio_config(&io);

    int  held = 0;
    bool long_fired = false;

    while (1) {
        if (gpio_get_level(BTN_GPIO) == 0) {          /* BOOT 键低有效 */
            held += 100;
            if (!long_fired && held >= BTN_LONG_MS) {
                long_fired = true;
                factory_reset();
            }
        } else {
            if (held > 0 && held < BTN_LONG_MS) rejoin();   /* 短按 */
            held = 0;
            long_fired = false;
        }
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

/* ── Zigbee 网络信号 ────────────────────────────────────────────────────── */
void esp_zb_app_signal_handler(esp_zb_app_signal_t *signal_struct) {
    uint32_t *sg_p = signal_struct->p_app_signal;
    esp_err_t err  = signal_struct->esp_err_status;
    esp_zb_app_signal_type_t sig = *sg_p;

    switch (sig) {
    case ESP_ZB_ZDO_SIGNAL_SKIP_STARTUP:
        esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_INITIALIZATION);
        break;

    case ESP_ZB_BDB_SIGNAL_DEVICE_FIRST_START:
    case ESP_ZB_BDB_SIGNAL_DEVICE_REBOOT:
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "Zigbee 初始化失败: %s", esp_err_to_name(err));
            break;
        }
        if (esp_zb_bdb_is_factory_new()) {
            ESP_LOGI(TAG, "首次启动 – 尚未入网，开始寻找网络");
            rejoin();
        } else {
            /* NVRAM 里的网络信息还在，ZBOSS 会自行 rejoin */
            ESP_LOGI(TAG, "重启 – 尝试恢复原网络");
        }
        break;

    case ESP_ZB_BDB_SIGNAL_STEERING:
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "已入网 – PAN 0x%04hx 信道 %d 短地址 0x%04hx",
                     esp_zb_get_pan_id(), esp_zb_get_current_channel(),
                     esp_zb_get_short_address());
        } else {
            ESP_LOGW(TAG, "入网失败 (%s)，1 秒后重试", esp_err_to_name(err));
            esp_zb_scheduler_alarm(
                (esp_zb_callback_t)(void *)esp_zb_bdb_start_top_level_commissioning,
                ESP_ZB_BDB_MODE_NETWORK_STEERING, 1000);
        }
        break;

    case ESP_ZB_ZDO_SIGNAL_LEAVE: {
        esp_zb_zdo_signal_leave_params_t *p = esp_zb_app_signal_get_params(sg_p);
        if (p && p->leave_type == ESP_ZB_NWK_LEAVE_TYPE_RESET) {
            /* 被移出网络（不带 rejoin）→ 重新找网络 */
            ESP_LOGW(TAG, "已被移出网络 – 重新入网");
            esp_zb_scheduler_alarm(
                (esp_zb_callback_t)(void *)esp_zb_bdb_start_top_level_commissioning,
                ESP_ZB_BDB_MODE_NETWORK_STEERING, 3000);
        } else {
            ESP_LOGI(TAG, "暂时离网 – 等待重新接入");
        }
        break;
    }

    default:
        ESP_LOGD(TAG, "ZB 信号: %s err=%s",
                 esp_zb_zdo_signal_to_string(sig), esp_err_to_name(err));
        break;
    }
}

/* ── Zigbee 主任务 ─────────────────────────────────────────────────────── */
static void zb_task(void *arg) {
    esp_zb_cfg_t cfg = {
        .esp_zb_role         = ESP_ZB_DEVICE_TYPE_ROUTER,
        .install_code_policy = false,
        .nwk_cfg.zczr_cfg    = { .max_children = MAX_CHILDREN },
    };
    esp_zb_init(&cfg);
    esp_zb_set_tx_power(TX_POWER_DBM);

    /* NVRAM 不能每次启动就清：里面存着网络密钥、PAN、信道和地址表。
     * 清掉就等于每断电一次都要重新入网。 */
    esp_zb_nvram_erase_at_start(false);

    /* 端点只留 Basic + Identify —— 没有任何可控/可上报的簇 */
    esp_zb_cluster_list_t *cl = esp_zb_zcl_cluster_list_create();

    esp_zb_basic_cluster_cfg_t basic_cfg = { .zcl_version = 3, .power_source = 0x01 };
    esp_zb_attribute_list_t *basic_attr = esp_zb_basic_cluster_create(&basic_cfg);
    /* ZCL 字符串：首字节是长度 */
    static const char mfr_str[]   = "\x09Espressif";        /* len=9  */
    static const char model_str[] = "\x10ESP32-C6-Repeater"; /* len=16 */
    esp_zb_basic_cluster_add_attr(basic_attr, ESP_ZB_ZCL_ATTR_BASIC_MANUFACTURER_NAME_ID, (void *)mfr_str);
    esp_zb_basic_cluster_add_attr(basic_attr, ESP_ZB_ZCL_ATTR_BASIC_MODEL_IDENTIFIER_ID,  (void *)model_str);
    esp_zb_cluster_list_add_basic_cluster(cl, basic_attr, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);

    esp_zb_identify_cluster_cfg_t id_cfg = { .identify_time = 0 };
    esp_zb_cluster_list_add_identify_cluster(cl,
        esp_zb_identify_cluster_create(&id_cfg), ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);

    esp_zb_ep_list_t *ep = esp_zb_ep_list_create();
    esp_zb_endpoint_config_t ep_cfg = {
        .endpoint           = ENDPOINT,
        .app_profile_id     = ESP_ZB_AF_HA_PROFILE_ID,
        .app_device_id      = ESP_ZB_HA_RANGE_EXTENDER_DEVICE_ID,
        .app_device_version = 0,
    };
    esp_zb_ep_list_add_ep(ep, cl, ep_cfg);
    esp_zb_device_register(ep);

    ESP_ERROR_CHECK(esp_zb_start(false));
    esp_zb_stack_main_loop();
}

/* ── 入口 ──────────────────────────────────────────────────────────────── */
void app_main(void) {
    ESP_LOGI(TAG, "=== ESP32-C6 Zigbee 中继（纯 Router）===");

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    esp_zb_platform_config_t pcfg = {
        .radio_config = { .radio_mode = ZB_RADIO_MODE_NATIVE },
        .host_config  = { .host_connection_mode = ZB_HOST_CONNECTION_MODE_NONE },
    };
    ESP_ERROR_CHECK(esp_zb_platform_config(&pcfg));

    xTaskCreate(zb_task, "zb_main", 8192, NULL, 5, NULL);
    xTaskCreate(btn_task, "btn", 2048, NULL, 3, NULL);
}
