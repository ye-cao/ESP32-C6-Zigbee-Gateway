#include <string.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "driver/gpio.h"
#include "esp_zigbee_core.h"

#include "ha_mqtt.h"
#include "zb_gateway.h"
#include "zb_topo.h"
#include "zb_ota_server.h"
#include "serial_ota.h"

/* 入网按键（Boot 键 = GPIO9） */
#define BTN_PERMIT_JOIN  9
#define PERMIT_JOIN_SECS 180

#define TAG "main"

/* ── ZBOSS「大锁」─────────────────────────────────────────────────────────
 *
 * ★★★ 2026-10-26：这才是反复重启的真凶，不是栈溢出，也不是阻塞式串口写。
 *
 * 现场证据（add-on 日志，0.6 s 轮询在 read_onoff 风暴中间抓到的）：
 *   assert failed: vPortExitCritical port.c:618
 *                   (port_uxCriticalNesting[0] > 0)
 * 顺着栈地址回溯：
 *   zb_sched_loop_iteration → zb_multimac_mac_logic_iteration_proxy
 *   → zb_mac_logic_iteration → zb_esp_clr_event
 *   → zb_osif_enable_all_interrupts   (= vPortExitCritical())
 *
 * ZBOSS 内核**不是**线程安全的。它那对中断开关括号用的是 vPortEnter/
 * ExitCritical，而 port_uxCriticalNesting 是**每核的全局计数**，不是每任务。
 * 任何来自**外来任务**（uart_rx、heartbeat、btn、chan_scan）的 zb_* 调用都
 * 会挤进同一个窗口，把 ZBOSS 任务那边的计数搞乱 ⇒ 下溢 ⇒ assert ⇒
 * rst:0xc (SW_CPU)。
 *
 * 官方 API 明确要求加锁（esp_zigbee_core.h，esp_zb_lock_acquire）：
 *   "It's mandatory to acquire the lock before calling any Zigbee SDK APIs,
 *    except that the call site is in Zigbee callbacks."
 * 而在这之前，整个项目里**一处都没加**。
 *
 * 与 tostmann/esp-coordinator（.tostmann/main/zb_ncp.cpp 的 zboss_lock_guard）
 * 是同一个坑——那边用完全相同的 assert 复现过。
 * 这把锁是**递归锁**（zb_esp_osif_lock_release → xQueueGiveMutexRecursive），
 * 所以嵌套调用（被调函数自己再取一次锁）无害。
 *
 * ⚠️ esp_zb_start() 之前 s_zb_mutex 还不存在（tostmann 的 boot_guard 同理）
 *    ⇒ 一律先判 zb_gateway_is_ready()。
 */
static inline bool zb_lock(void) {
    if (!zb_gateway_is_ready()) return false;
    return esp_zb_lock_acquire(portMAX_DELAY);
}
static inline void zb_unlock(bool held) {
    if (held) esp_zb_lock_release();
}

/* ── 命令处理：来自树莓派的 UART1 命令 ────────────────────────────────── */
static void on_uart_cmd(const char *cmd, const char *payload, int len) {
    char buf[8] = {0};
    int n = len < (int)(sizeof(buf) - 1) ? len : (int)(sizeof(buf) - 1);
    memcpy(buf, payload, n);

    /* ★★ 2026-10-25：在这里挡住重启循环。
     * ha_mqtt_init() 先起 uart_rx 任务，zigbee 那边才刚跑到 esp_zb_start()。
     * 这个窗口期如果来了一条命令（add-on 开机后会立刻为每台设备发
     * read_onoff），esp_zb_scheduler_alarm() 就会解引用一个还不存在的队列：
     *   Guru Meditation Error: Core 0 panic'ed (Load access fault)
     *   MEPC 0x42022f9a (zb_schedule_alarm) / MTVAL 0x0000000d
     * → 每 ~20 s 复位一次，从外面看就是「网关一直在重启」。
     * 所以这里直接丢弃命令，不排队。 */
    if (!zb_gateway_is_ready()) {
        ESP_LOGW(TAG, "Befehl '%s' verworfen – Zigbee-Stack startet noch", cmd);
        return;
    }

    /* ★ 从这里往下**每一个**分支都要进 Zigbee SDK。例外只有两个：
     *  - fw_ota_*：只写 Flash（serial_ota.c）
     *  - ota_data： 只写服务端的 OTA 缓冲（zb_ota_server.c）并给一个信号量，
     *               不碰任何 SDK API。
     *    ★★ 为什么 ota_data 绝不能加锁（2026-10-26 实测）：
     *    ota_next_data_cb 跑在 ZBOSS 任务里，要数据时就在等这把信号量。
     *    如果 rx_task 在这里持锁，feed_data 永远送不进去 → 每块卡 10 s
     *    → OTA 直接停住。
     *    （实测：OTA 卡在 8 / 610976 字节，之后客户端直接 abort。） */
    bool held = (strncmp(cmd, "fw_ota", 6) == 0 ||
                 strcmp(cmd, "ota_data") == 0) ? false : zb_lock();

    if (strcmp(cmd, "dev_cmd") == 0) {
        /* payload: "ADDR,EP,CMD"  CMD: 0=关 1=开 2=切换 */
        unsigned long a = strtoul(payload, NULL, 0);
        const char *c1 = strchr(payload, ',');
        uint8_t ep  = c1 ? (uint8_t)atoi(c1 + 1) : 1;
        const char *c2 = c1 ? strchr(c1 + 1, ',') : NULL;
        uint8_t cid = c2 ? (uint8_t)atoi(c2 + 1) : 1;
        ESP_LOGI(TAG, "dev_cmd 0x%04x ep%u cmd=%u", (uint16_t)a, (unsigned)ep, (unsigned)cid);
        zb_gateway_dev_cmd((uint16_t)a, ep, cid);
    } else if (strcmp(cmd, "probe") == 0) {
        /* payload: "ADDR,EP" —— 定向读 Basic 簇（厂商/型号） */
        unsigned long a = strtoul(payload, NULL, 0);
        const char *c1 = strchr(payload, ',');
        uint8_t ep = c1 ? (uint8_t)atoi(c1 + 1) : 1;
        ESP_LOGI(TAG, "probe 0x%04x ep%u", (uint16_t)a, (unsigned)ep);
        zb_gateway_probe_basic((uint16_t)a, ep);
    } else if (strcmp(cmd, "node_desc") == 0) {
        /* payload: "ADDR" —— 读节点描述符：是 Router 还是 End Device？ */
        unsigned long a = strtoul(payload, NULL, 0);
        ESP_LOGI(TAG, "node_desc 0x%04x", (uint16_t)a);
        zb_gateway_node_desc((uint16_t)a);
    } else if (strcmp(cmd, "dev_remove") == 0) {
        /* payload: "ADDR" —— 把设备彻底从列表（和 NVS 副本）里删掉 */
        unsigned long a = strtoul(payload, NULL, 0);
        ESP_LOGI(TAG, "dev_remove 0x%04x", (uint16_t)a);
        zb_gateway_remove_device((uint16_t)a);
    } else if (strcmp(cmd, "read_onoff") == 0) {
        /* payload: "ADDR,EP" —— 主动回读 On/Off 属性（确认真实状态） */
        unsigned long a = strtoul(payload, NULL, 0);
        const char *c1 = strchr(payload, ',');
        uint8_t ep = c1 ? (uint8_t)atoi(c1 + 1) : 1;
        ESP_LOGI(TAG, "read_onoff 0x%04x ep%u", (uint16_t)a, (unsigned)ep);
        zb_gateway_read_onoff((uint16_t)a, ep);
    } else if (strcmp(cmd, "permit_join") == 0) {
        uint8_t secs = (uint8_t)atoi(buf);
        ESP_LOGI(TAG, "permit_join %u s", secs);
        zb_gateway_permit_join(secs);
    } else if (strcmp(cmd, "topo") == 0) {
        /* payload: 可选 "DELAY_MS"。给了就先等这么久再 dump（add-on 会在等待
         * 期间逐台发 node_desc 触发按需路由发现）；不给就立刻 dump 现有表。 */
        int delay = payload ? atoi(payload) : 0;
        ESP_LOGI(TAG, "topo dump (delay %d ms)", delay);
        if (delay > 0) zb_topo_request_full((uint32_t)delay);
        else           zb_topo_request();
    } else if (strcmp(cmd, "scan_chan") == 0) {
        zb_gateway_scan_channels();
    } else if (strcmp(cmd, "set_channel") == 0) {
        uint8_t ch = (uint8_t)atoi(buf);
        ESP_LOGI(TAG, "set_channel %u", ch);
        zb_gateway_set_channel(ch);
    } else if (strcmp(cmd, "ota_start") == 0) {
        /* payload: "SIZE,VERSION[,IMAGETYPE]"
         * IMAGETYPE: 0x0001 = 终端节点，0x0002 = 中继（省略按 0x0001） */
        uint32_t size = (uint32_t)atol(payload);
        const char *comma = strchr(payload, ',');
        uint32_t ver = comma ? (uint32_t)atol(comma + 1) : 1;
        uint16_t type = 0;
        const char *comma2 = comma ? strchr(comma + 1, ',') : NULL;
        if (comma2) type = (uint16_t)strtoul(comma2 + 1, NULL, 0);
        zb_ota_server_start(size, ver, type);
    } else if (strcmp(cmd, "set_dev_power") == 0) {
        /* payload: "ADDR,EP,DBM" —— 给中继写厂商专有簇 0xFC00/0x0001 */
        unsigned long a = strtoul(payload, NULL, 0);
        const char *c1 = strchr(payload, ',');
        uint8_t ep = c1 ? (uint8_t)atoi(c1 + 1) : 1;
        const char *c2 = c1 ? strchr(c1 + 1, ',') : NULL;
        int dbm = c2 ? atoi(c2 + 1) : 8;
        if (dbm > 20)  dbm = 20;
        if (dbm < -24) dbm = -24;
        ESP_LOGI(TAG, "set_dev_power 0x%04x ep%u → %d dBm", (uint16_t)a, (unsigned)ep, dbm);
        zb_gateway_write_tx_power((uint16_t)a, ep, (int8_t)dbm);
    } else if (strcmp(cmd, "fw_ota_start") == 0) {
        /* 通过串口刷协调器固件 */
        serial_ota_start((uint32_t)atol(payload));
    } else if (strcmp(cmd, "fw_ota_data") == 0) {
        /* payload = "OFF,HEX" —— HEX 部分长度 = len 减去偏移字段 */
        long off = atol(payload);
        const char *hex = strchr(payload, ',');
        if (hex) {
            hex++;
            serial_ota_data((uint32_t)off, hex, len - (int)(hex - payload));
        }
    } else if (strcmp(cmd, "fw_ota_end") == 0) {
        serial_ota_end();
    } else if (strcmp(cmd, "ota_data") == 0) {
        /* payload = "OFF,HEX"（最多 1024 B = 2048 个十六进制字符） */
        zb_ota_server_feed_data(payload, (uint16_t)len);
    } else if (strcmp(cmd, "ota_stop") == 0) {
        /* 中止 OTA 会话（也允许主机侧发起，免得一次误启动吃掉
         * 一个小时的无线带宽）。 */
        zb_ota_server_stop();
    } else if (strcmp(cmd, "switch2wifi") == 0) {
        ESP_LOGI(TAG, "switch2wifi -> ZCL an alle Devices");
        zb_gateway_send_switch2wifi_all();
    } else if (strcmp(cmd, "set_tx_power") == 0) {
        /* payload = "-24" … "20"（dBm）。运行期改功率，方便不重刷固件就做
         * 对比：功率过大时陶瓷天线的模块可能把 PA 推进压缩区，功率过小则
         * 直接损失链路预算。哪个更好由实测决定，不靠猜。 */
        int dbm = atoi(buf);
        if (dbm >  20) dbm =  20;
        if (dbm < -24) dbm = -24;
        ESP_LOGI(TAG, "set_tx_power %d dBm", dbm);
        zb_gateway_set_tx_power((int8_t)dbm);
    } else if (strcmp(cmd, "lqi") == 0) {
        /* 立刻取所有设备的 LQI/RSSI 快照，不必等心跳 */
        zb_gateway_report_lqi();
    } else if (strcmp(cmd, "set_sleep") == 0) {
        uint32_t secs = (uint32_t)atol(buf);
        if (secs < 10) secs = 10;
        ESP_LOGI(TAG, "set_sleep %lu s", (unsigned long)secs);
        zb_gateway_send_set_sleep(secs);
    }

    zb_unlock(held);
}

/* ── 心跳任务：开机立即一次，之后每 60 秒 ──────────────────────────────── */
#define HEARTBEAT_INTERVAL_MS 60000

static void heartbeat_task(void *arg) {
    /* 尺寸跟着 ZB_GATEWAY_MAX_DEVICES 走（每台设备 64 B + 信封），否则设备
     * 超过约 32 台时 LQI 列表会被截断。
     * ★ static：64 台设备时这两个缓冲区加起来约 8,6 KB。作为局部变量会占掉
     *   12 KB 栈的 70%；static 版本落在 .bss 而不是栈上
     *   （本任务只有一个实例，静态是安全的）。 */
    static char lqi_buf[ZB_GATEWAY_MAX_DEVICES * 64 + 128];
    static char line[sizeof(lqi_buf) + 192]; /* 心跳 JSON，含 lqi_buf + 额外开销 */
    vTaskDelay(pdMS_TO_TICKS(5000));  /* 等 Zigbee 启动完 */
    while (1) {
        uint32_t uptime_s = (uint32_t)(esp_timer_get_time() / 1000000ULL);
        /* 只有那两个 SDK getter 需要 ZBOSS 锁；lqi_json 和 get_last_rssi
         * 用的是自己的表（s_dev_mutex）。 */
        bool held = zb_lock();
        uint8_t  ch   = esp_zb_get_current_channel();
        uint16_t pan  = esp_zb_get_pan_id();
        zb_unlock(held);
        int8_t   rssi = zb_gateway_get_last_rssi();
        zb_gateway_lqi_json(lqi_buf, sizeof(lqi_buf));
        snprintf(line, sizeof(line),
            "{\"t\":\"heartbeat\",\"uptime\":%lu,\"ch\":%u,\"pan\":\"0x%04x\",\"rssi\":%d,\"dev\":%s}",
            (unsigned long)uptime_s, (unsigned)ch, (unsigned)pan, (int)rssi, lqi_buf);
        ha_mqtt_emit_raw(line);
        vTaskDelay(pdMS_TO_TICKS(HEARTBEAT_INTERVAL_MS));
    }
}

/* ── 入网按键 ──────────────────────────────────────────────────────────── */
static void btn_task(void *arg) {
    gpio_set_direction(BTN_PERMIT_JOIN, GPIO_MODE_INPUT);
    gpio_set_pull_mode(BTN_PERMIT_JOIN, GPIO_PULLUP_ONLY);

    bool last = true;

    while (1) {
        bool cur = gpio_get_level(BTN_PERMIT_JOIN);
        if (last && !cur) {
            ESP_LOGI(TAG, "Permit Join %d s (Boot-Taste)", PERMIT_JOIN_SECS);
            bool held = zb_lock();
            zb_gateway_permit_join(PERMIT_JOIN_SECS);
            zb_unlock(held);
        }
        last = cur;
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

/* ── app_main ───────────────────────────────────────────────────────────── */
void app_main(void) {
    ESP_LOGI(TAG, "=== ESP32-C6 Zigbee Gateway ===");
    ESP_LOGI(TAG, "");
    ESP_LOGI(TAG, "  JSON : UART1 TX=GPIO16 RX=GPIO17 → Pi /dev/ttyACM0");

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ha_mqtt_set_cmd_cb(on_uart_cmd);
    ESP_ERROR_CHECK(ha_mqtt_init(NULL, NULL, NULL));

    zb_gateway_start();

    xTaskCreate(btn_task,       "btn",       2048, NULL, 3, NULL);
    /* 栈：64 台设备时 lqi_buf + line 约 6,6 KB，再加 snprintf/MQTT 缓冲 */
    xTaskCreate(heartbeat_task, "heartbeat", 12288, NULL, 2, NULL);
}
