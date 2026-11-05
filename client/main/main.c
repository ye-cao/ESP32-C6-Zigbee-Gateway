#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "driver/gpio.h"
#include "esp_zigbee_core.h"
#include "ha_mqtt.h"
#include "zb_device.h"
#include "wifi_switch.h"
#include "onewire_bus.h"
#include "onewire_cmd.h"
#include "onewire_device.h"

#define TAG              "main"
#define BTN_GPIO         9       /* Boot-Taste GPIO9 */
#define DS18B20_GPIO     6       /* 1-Wire Datenleitung */
#define HEARTBEAT_MS_DEFAULT  600000U  /* 600 s */
volatile uint32_t s_sleep_ms = HEARTBEAT_MS_DEFAULT;

/* DS18B20 Befehle */
#define DS18B20_CMD_CONVERT_T       0x44
#define DS18B20_CMD_READ_SCRATCHPAD 0xBE
#define DS18B20_NO_TEMP             -127.0f

static onewire_bus_handle_t s_ow_bus      = NULL;
static bool                 s_sensor_found = false;

static void temp_sensor_init(void) {
    onewire_bus_config_t     bus_cfg = { .bus_gpio_num = DS18B20_GPIO };
    onewire_bus_rmt_config_t rmt_cfg = { .max_rx_bytes = 10 };
    if (onewire_new_bus_rmt(&bus_cfg, &rmt_cfg, &s_ow_bus) != ESP_OK) {
        ESP_LOGE(TAG, "1-Wire Bus init fehlgeschlagen (GPIO%d)", DS18B20_GPIO);
        return;
    }
    onewire_device_iter_handle_t iter;
    onewire_new_device_iter(s_ow_bus, &iter);
    onewire_device_t dev;
    if (onewire_device_iter_get_next(iter, &dev) == ESP_OK) {
        s_sensor_found = true;
        ESP_LOGI(TAG, "DS18B20 gefunden an GPIO%d (addr: %016llx)",
                 DS18B20_GPIO, dev.address);
    } else {
        ESP_LOGW(TAG, "Kein DS18B20 an GPIO%d – temp=null", DS18B20_GPIO);
    }
    onewire_del_device_iter(iter);
}

static float temp_read(void) {
    if (!s_sensor_found || !s_ow_bus) return DS18B20_NO_TEMP;

    if (onewire_bus_reset(s_ow_bus) != ESP_OK) return DS18B20_NO_TEMP;
    uint8_t cmd_convert[] = { ONEWIRE_CMD_SKIP_ROM, DS18B20_CMD_CONVERT_T };
    onewire_bus_write_bytes(s_ow_bus, cmd_convert, sizeof(cmd_convert));
    vTaskDelay(pdMS_TO_TICKS(800));  /* 12-bit Konversion ~750 ms */

    if (onewire_bus_reset(s_ow_bus) != ESP_OK) return DS18B20_NO_TEMP;
    uint8_t cmd_read[] = { ONEWIRE_CMD_SKIP_ROM, DS18B20_CMD_READ_SCRATCHPAD };
    onewire_bus_write_bytes(s_ow_bus, cmd_read, sizeof(cmd_read));
    uint8_t scratchpad[9] = {0};
    onewire_bus_read_bytes(s_ow_bus, scratchpad, sizeof(scratchpad));

    int16_t raw = (int16_t)((scratchpad[1] << 8) | scratchpad[0]);
    return raw * 0.0625f;
}

/* ── 命令处理：来自树莓派的命令 ─────────────────────────────────────────── */
static void on_cmd(const char *cmd, const char *payload, int len) {
    (void)len;
    if (strcmp(cmd, "leave") == 0) {
        zb_device_leave();
    } else if (strcmp(cmd, "set_onoff") == 0) {
        zb_device_set_onoff(atoi(payload) != 0);
    } else if (strcmp(cmd, "permit_join") == 0) {
        zb_device_permit_join((uint8_t)atoi(payload));
    } else if (strcmp(cmd, "set_channel") == 0) {
        zb_device_set_channel((uint8_t)atoi(payload));
    }
}

/* ── Heartbeat-Task ─────────────────────────────────────────────────────── */
static void heartbeat_task(void *arg) {
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(s_sleep_ms));
        uint32_t up   = (uint32_t)(esp_timer_get_time() / 1000000ULL);
        float    temp = temp_read();
        ha_mqtt_publish_heartbeat(up, zb_device_pan_id(), zb_device_channel(),
                                  zb_device_rssi(), temp);
        /* Temperatur auch via Zigbee melden (Temperature Cluster) */
        if (temp > -126.0f) {
            zb_device_report_temp((int16_t)(temp * 100.0f));
        } else {
            /* 没装传感器：上报 invalid 值 */
            zb_device_report_temp((int16_t)0x8000);
        }
    }
}

/* ── Permit-Join-Taste (Boot-Taste) ─────────────────────────────────────── */
static void btn_task(void *arg) {
    gpio_set_direction(BTN_GPIO, GPIO_MODE_INPUT);
    gpio_set_pull_mode(BTN_GPIO, GPIO_PULLUP_ONLY);
    bool last = true;
    while (1) {
        bool cur = gpio_get_level(BTN_GPIO);
        if (last && !cur) {
            if (zb_device_joined()) {
                zb_device_permit_join(60);
            } else {
                esp_zb_bdb_start_top_level_commissioning(
                    ESP_ZB_BDB_MODE_NETWORK_STEERING);
            }
        }
        last = cur;
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

/* ── app_main ───────────────────────────────────────────────────────────── */
void app_main(void) {
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    /* Sleep-Intervall aus NVS laden */
    { nvs_handle_t _h; uint32_t _s = 600;
      if (nvs_open("client_cfg",NVS_READONLY,&_h)==ESP_OK){
        nvs_get_u32(_h,"sleep_s",&_s); nvs_close(_h);}
      s_sleep_ms = _s * 1000UL; }
    ha_mqtt_set_cmd_cb(on_cmd);
    ESP_ERROR_CHECK(ha_mqtt_init());

    ha_mqtt_publish_boot();

    temp_sensor_init();

    /* 若 NVS 标志已置则进 WiFi 模式（协调器发来的命令） */
    if (wifi_switch_is_pending()) {
        wifi_switch_run();  /* blockiert bis esp_restart() */
    }
    zb_device_start();

    xTaskCreate(btn_task,       "btn",       2048, NULL, 3, NULL);
    xTaskCreate(heartbeat_task, "heartbeat", 2048, NULL, 2, NULL);
}
