/*
 * repeater.c – 纯 Zigbee 中继（Zigbee Router）
 *
 * 用途：常电 ESP32-C6 加入现有网络后转发报文，扩展覆盖。
 * 业务端点只保留 Basic + Identify，不挂任何可控/可上报的业务簇：
 *   → 网关/HA 里能看到它在线，但不会多出虚假的开关/温度实体。
 *
 * 在此基础上加了两样东西：
 *   1. OTA client 簇（image type 0x0002）—— 见 zb_ota_client.c
 *   2. 厂商专有簇 0xFC00 的可写属性 0x0001（S8，dBm）——
 *      网关/Web 端写进来就立刻改发射功率并落 NVS，不用重新刷固件
 *
 * 操作（长按分级，同一个 BOOT 键）：
 *   短按 <1 s      → 循环功率档 8 → 14 → 20 → 8 dBm（立即生效 + 存 NVS）
 *   长按 1~5 s     → 主动再试一次入网（协调器需先开 permit join）
 *   长按 ≥5 s      → 恢复出厂（退网 + 清除配对信息），用于换网络
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
#include "nvs.h"
#include "ha/esp_zigbee_ha_standard.h"
#include "esp_zigbee_core.h"
#include "esp_zigbee_cluster.h"
#include "esp_zigbee_attribute.h"
#include "esp_zigbee_endpoint.h"
#include "zcl/esp_zigbee_zcl_core.h"
#include "zb_ota_client.h"

/* zb_nwk_get_parent() 声明在 zboss_api_nwk.h，但那个头依赖 zb_types.h 里的
 * zb_uint8_t 等 typedef，单独 include 会一片 "unknown type name"。
 * 这里直接用等价原型：zb_uint16_t 就是 unsigned short，ABI 完全一致。 */
extern unsigned short zb_nwk_get_parent(void);

/* ── Zigbee 大锁 ──────────────────────────────────────────────────────────
 * ★★★ SDK 文档的硬性要求：**外来任务**调任何 Zigbee SDK API 之前必须先拿锁
 *   （只有「在 Zigbee 回调里」调用才豁免）。
 *
 *   漏掉会怎样：`port_uxCriticalNesting[]` 是**每核全局**的计数，不是每任务。
 *   ZBOSS 任务进/出临界区时会对它做 ±1，外来任务若在它恰好处于临界区时
 *   插进来调 SDK，计数就被弄下溢 ⇒
 *     assert failed: vPortExitCritical port.c:618 (port_uxCriticalNesting[0] > 0)
 *     rst:0xc (SW_CPU)  → 直接重启
 *
 *   这个坑在协调器上已经踩实过（见 .workbuddy/memory/MEMORY.md §0）。
 *   在中继上表现为：OTA 下载到几 KB 就重启一次，客户端从 0 重来，
 *   **永远传不完**（2026-10-26 13:26 日志实证：ZDO Device Unavailable
 *   + 4 秒后重新 announce）。OTA 期间 ZBOSS 最忙，撞车概率最大，
 *   所以平时看不出问题，一刷固件必崩。 */
static void zb_lock(void)   { esp_zb_lock_acquire(portMAX_DELAY); }
static void zb_unlock(void) { esp_zb_lock_release(); }

#define TAG           "REPEATER"
#define ENDPOINT      1
#define MAX_CHILDREN  20      /* 本中继下面最多挂多少个终端/休眠设备 */
#define BTN_GPIO      GPIO_NUM_9
#define BTN_TAP_MS    1000    /* <1 s = 切功率 */
#define BTN_RESET_MS  5000    /* ≥5 s = 恢复出厂 */

/* 自定义簇：发射功率控制
 * 0xFC00 属于 ZCL 保留的厂商专有段（0xFC00–0xFFFF）。
 * 属性 0x0001 = 当前发射功率 dBm（S8，可读可写）。 */
#define CLUSTER_TX_POWER  0xFC00
#define ATTR_TX_POWER     0x0001

/* 同一个簇的另一个属性：0x0002 = 我自己的父节点短地址（uint16）。
 * 方向相反 —— 由中继**写**进协调器（协调器那边注册成可写属性 0x0002）。
 * 为什么值得做：Zigbee 的邻居表里 relationship/depth 这两个字段很多
 * 设备（含 ZBOSS 自己）根本不维护，网关只能靠「谁听得见谁」去猜父节点。
 * 而中继自己一句 zb_nwk_get_parent() 就有确切答案，白白浪费了。 */
#define CLUSTER_INFO      0xFC00
#define ATTR_PARENT       0x0002

/* NVS：namespace "rep"，key "txp" = 发射功率（int8，存成 u8 偏移 24 规避符号问题） */
#define NVS_NS      "rep"
#define NVS_KEY_TXP "txp"
#define NVS_KEY_BOOTS "boots"   /* 连续启动计数，见 bootn_bump() */
#define TXP_SHIFT   24        /* 存 (dbm + 24)，读回再减 24；0..44 都能表示 */

/* 功率档位。20 = 芯片上限，但峰值电流也最高：供电偏弱（长 USB 线 /
 * 弱 USB 口 / 无稳压）时会触发 Brownout 复位，表现是启动后刚开射频
 * 就重启、日志里出现 "E BOD: Brownout detector was triggered"。
 * 遇到这种情况就往低档切（Web 端或短按按键都行），或换独立 5V 供电。 */
static const int8_t POWER_STEPS[] = { 8, 14, 20 };
#define POWER_STEP_NUM  (sizeof(POWER_STEPS) / sizeof(POWER_STEPS[0]))

/* 属性存储：这个变量的地址会交给 ZCL 属性表，必须 static 且长期有效。
 * 默认档 14 dBm：8 dBm 在隔墙/远距离时连协调器都可能够不到，
 * 20 dBm 又会把弱供电的板子打到 Brownout，14 是折中。 */
static int8_t s_tx_power = 14;

/* 丢链路后的重入网退避计数（见 rejoin_backoff）。 */
static uint32_t s_no_link = 0;

/* ── 发射功率：应用 + 持久化 ─────────────────────────────────────────────── */
static void nvs_load_power(void) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return;
    uint8_t v = 0;
    if (nvs_get_u8(h, NVS_KEY_TXP, &v) == ESP_OK && v <= (uint8_t)(TXP_SHIFT * 2 - 4)) {
        s_tx_power = (int8_t)((int)v - TXP_SHIFT);
    }
    nvs_close(h);
}

static void nvs_store_power(int8_t dbm) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_u8(h, NVS_KEY_TXP, (uint8_t)((int)dbm + TXP_SHIFT));
    nvs_commit(h);
    nvs_close(h);
}

/* ── 掉压自愈：上一轮若是 Brownout 复位 ⇒ 这次启动自动降一档 ──────────────
 *
 * 为什么必须有：20 dBm 峰值电流最大。供电偏弱时，射频**第一次发射**就会把
 * VDD 拉塌，芯片立刻 BOD 复位。而 net_task 的初始延时正好是 15 s，所以现象是
 * 「启动 → 15 s → E BOD + rst:0x3 → 重启」的**精确 15 秒循环**，
 * 看起来极像固件死循环，非常容易查错方向。
 *
 * 更麻烦的是：功率是存在 NVS 里的。用户在 Web 端调到 20 dBm 之后，人不在旁边
 * 就再也连不上了（每次都在 15 s 内重启，根本来不及写回低档）—— 自锁。
 * 所以这里让它**下次启动时自己退一档**：20→14→8，退到 8 还崩就只报警不动了。
 *
 * 想再回高档：短按按键循环，或 Web 端写入（前提是供电问题已解决）。
 * ★ 判决依据有两个，任一成立即降档：
 *   ① esp_reset_reason() == ESP_RST_BROWNOUT —— 直接、快，一次就知道；
 *   ② 连续启动计数 >= 3（STEERING 成功才清零）—— 兜底，因为 brownout 复位
 *      在 C6 上走的是 "LP_SW_HPSYS"（软复位路径），reset_reason 查表可能落到
 *      ESP_RST_SW，光靠 ① 会漏。 */
static int8_t power_step_down(int8_t cur) {
    if (cur <= POWER_STEPS[0]) return cur;          /* 已在最低档，别再降 */
    for (size_t i = 1; i < POWER_STEP_NUM; i++) {
        if (POWER_STEPS[i] == cur) return POWER_STEPS[i - 1];
    }
    return POWER_STEPS[0];                          /* 非标档位（如 17）→ 落到最低档 */
}

/* 启动计数：每次启动 +1，STEERING 成功（= 真的发出去了）才清零。
 * 正常插拔只 +1 就清零，不会误降档；只有真正的重启循环才累积到 3。 */
static void bootn_bump(uint8_t *out) {
    nvs_handle_t h;
    uint8_t n = 0;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) { *out = 0; return; }
    nvs_get_u8(h, NVS_KEY_BOOTS, &n);
    if (n < 250) n++;
    nvs_set_u8(h, NVS_KEY_BOOTS, n);
    nvs_commit(h);
    nvs_close(h);
    *out = n;
}

static void bootn_clear(void) {
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_u8(h, NVS_KEY_BOOTS, 0);
    nvs_commit(h);
    nvs_close(h);
}

static const char *reset_reason_str(esp_reset_reason_t r) {
    switch (r) {
    case ESP_RST_POWERON:   return "POWERON";
    case ESP_RST_EXT:       return "EXT";
    case ESP_RST_SW:        return "SW";
    case ESP_RST_PANIC:     return "PANIC";
    case ESP_RST_INT_WDT:   return "INT_WDT";
    case ESP_RST_TASK_WDT:  return "TASK_WDT";
    case ESP_RST_WDT:       return "WDT";
    case ESP_RST_DEEPSLEEP: return "DEEPSLEEP";
    case ESP_RST_BROWNOUT:  return "BROWNOUT";
    case ESP_RST_SDIO:      return "SDIO";
    case ESP_RST_USB:       return "USB";
    case ESP_RST_JTAG:      return "JTAG";
    case ESP_RST_EFUSE:     return "EFUSE";
    default:                return "UNKNOWN";
    }
}

/* 改功率：夹到 -24..20，先设 PA，再改属性，最后落盘 */
static void apply_tx_power(int8_t dbm, const char *why) {
    if (dbm > 20)  dbm = 20;
    if (dbm < -24) dbm = -24;

    s_tx_power = dbm;
    esp_zb_set_tx_power(dbm);          /* 1.6.8：返回 void，没有错误码 */
    /* 本地属性同步一次，保证网关读回的是实际在跑的档位 */
    esp_zb_zcl_set_attribute_val(ENDPOINT, CLUSTER_TX_POWER,
                                 ESP_ZB_ZCL_CLUSTER_SERVER_ROLE,
                                 ATTR_TX_POWER, &s_tx_power, false);
    nvs_store_power(dbm);

    ESP_LOGI(TAG, "发射功率 → %d dBm（%s）", dbm, why);
}

/* 按键兜底：在档位表里往后跳一格 */
static void cycle_power(void) {
    int idx = 0;
    for (size_t i = 0; i < POWER_STEP_NUM; i++) {
        if (POWER_STEPS[i] == s_tx_power) { idx = (int)(i + 1) % (int)POWER_STEP_NUM; break; }
    }
    apply_tx_power(POWER_STEPS[idx], "按键短按");
}

/* ── Zigbee core action：ZCL 写属性 / OTA 进度 ──────────────────────────── */
static esp_err_t action_handler(esp_zb_core_action_callback_id_t id, const void *msg) {
    switch (id) {
    case ESP_ZB_CORE_SET_ATTR_VALUE_CB_ID: {
        const esp_zb_zcl_set_attr_value_message_t *m = msg;
        if (!m) break;
        if (m->info.cluster == CLUSTER_TX_POWER &&
            m->attribute.id == ATTR_TX_POWER &&
            m->attribute.data.value != NULL) {
            int8_t v = *(int8_t *)m->attribute.data.value;
            apply_tx_power(v, "网关写入");
        }
        break;
    }
    case ESP_ZB_CORE_OTA_UPGRADE_VALUE_CB_ID:
        return zb_ota_client_handle(msg);
    default:
        break;
    }
    return ESP_OK;
}

/* ── 入网 / 退网 ────────────────────────────────────────────────────────── */
static void rejoin(void) {
    s_no_link = 0;                     /* 人工/主动发起 → 从头计时，别被旧退避拖住 */
    ESP_LOGI(TAG, "开始寻找网络（协调器需处于允许入网状态）…");
    esp_zb_bdb_start_top_level_commissioning(ESP_ZB_BDB_MODE_NETWORK_STEERING);
}

/* 延迟重入网：ZDO/comissioning 只能在 ZBOSS 任务里发起 */
static void rejoin_later(uint32_t ms) {
    esp_zb_scheduler_alarm((esp_zb_callback_t)(void *)esp_zb_bdb_start_top_level_commissioning,
                           ESP_ZB_BDB_MODE_NETWORK_STEERING, ms);
}

/* 重入网退避：2 → 4 → 8 → 16 → 32 → 60 s（封顶）。
 *
 * 原来是无条件 2 s 重试。在覆盖盲区里这就是空转：每 13 秒入网一次又掉线，
 * 日志被刷满、射频一直满功率发、还白白发热，而且**看不出到底是盲区还是
 * 别的毛病**（因为正常和异常长得一样）。指数退避让日志一眼可读，
 * 也让旁观者能判断"它是在硬撑还是真连不上"。
 * 一旦入网成功或自检通过立刻归零，所以搬回覆盖区最多等 1 分钟。 */
static uint32_t rejoin_backoff(void) {
    uint32_t ms = 2000u;
    for (uint32_t i = 0; i < s_no_link && ms < 60000u; i++) ms <<= 1;
    if (ms > 60000u) ms = 60000u;
    if (s_no_link < 16) s_no_link++;   /* 只用于算退避，封顶防溢出 */
    return ms;
}

static void factory_reset(void) {
    ESP_LOGW(TAG, "长按 5 秒 – 恢复出厂，退网并清除配对信息");
    esp_zb_bdb_reset_via_local_action();
    vTaskDelay(pdMS_TO_TICKS(1500));
    esp_restart();
}

/* ── 按键：短按切功率，1~5 s 重入网，≥5 s 恢复出厂 ──────────────────────── */
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
            if (!long_fired && held >= BTN_RESET_MS) {
                long_fired = true;
                zb_lock(); factory_reset(); zb_unlock();   /* 外来任务 → 拿锁 */
            }
        } else {
            if (held > 0 && held < BTN_TAP_MS) {
                zb_lock(); cycle_power(); zb_unlock();
            } else if (held >= BTN_TAP_MS && held < BTN_RESET_MS) {
                zb_lock(); rejoin(); zb_unlock();
            }
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
            /* NVRAM 里的网络信息还在，ZBOSS 会自行 rejoin。
             * 注意：短地址也一样从 NVRAM 恢复，所以下面这行打出来的地址
             * 不代表"现在真的连得上"——能不能连通要看 net_task 的自检。 */
            ESP_LOGI(TAG, "重启 – 尝试恢复原网络（PAN 0x%04hx 信道 %d 短地址 0x%04hx 功率 %d）",
                     esp_zb_get_pan_id(), esp_zb_get_current_channel(),
                     esp_zb_get_short_address(), (int)s_tx_power);
        }
        break;

    case ESP_ZB_BDB_SIGNAL_STEERING:
        if (err == ESP_OK) {
            /* 入网成功 = 双向收发都通了 ⇒ 当前供电档位撑得住，清掉启动计数。 */
            bootn_clear();
            s_no_link = 0;
            ESP_LOGI(TAG, "已入网 – PAN 0x%04hx 信道 %d 短地址 0x%04hx 父节点 0x%04hx 功率 %d dBm",
                     esp_zb_get_pan_id(), esp_zb_get_current_channel(),
                     esp_zb_get_short_address(), (uint16_t)zb_nwk_get_parent(),
                     (int)s_tx_power);
        } else {
            ESP_LOGW(TAG, "入网失败 (%s)，1 秒后重试", esp_err_to_name(err));
            esp_zb_scheduler_alarm(
                (esp_zb_callback_t)(void *)esp_zb_bdb_start_top_level_commissioning,
                ESP_ZB_BDB_MODE_NETWORK_STEERING, 1000);
        }
        break;

    case ESP_ZB_NWK_SIGNAL_NO_ACTIVE_LINKS_LEFT:
        /* 路由器一个活跃邻居都没有 ⇒ 它现在既不转发也到不了协调器。
         * 这是"这个位置覆盖不到"的最强证据（比自检超时更直接）。
         *
         * ★ 退避而不是死循环：原来固定 2 s 重试，在盲区里就是每 13 秒
         *   入网→掉线→再入网的空转，日志全是同一行，根本分不清是盲区
         *   还是别的毛病。现在 2/4/8/16/32/60 s 递增，第几次、等多久一目了然。 */
        if (!esp_zb_bdb_is_factory_new() && !zb_ota_client_busy()) {
            uint32_t ms = rejoin_backoff();
            ESP_LOGW(TAG, "周边没有任何活跃邻居 – 覆盖不到，%lu 秒后重新寻找网络（第 %lu 次 · 父节点 0x%04hx）",
                     (unsigned long)(ms / 1000u), (unsigned long)s_no_link,
                     (uint16_t)zb_nwk_get_parent());
            rejoin_later(ms);
        }
        break;

    case ESP_ZB_ZDO_SIGNAL_LEAVE: {
        esp_zb_zdo_signal_leave_params_t *p = esp_zb_app_signal_get_params(sg_p);
        if (p && p->leave_type == ESP_ZB_NWK_LEAVE_TYPE_RESET) {
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
    esp_zb_set_tx_power(s_tx_power);

    /* NVRAM 不能每次启动就清：里面存着网络密钥、PAN、信道和地址表。
     * 清掉就等于每断电一次都要重新入网。 */
    esp_zb_nvram_erase_at_start(false);

    esp_zb_cluster_list_t *cl = esp_zb_zcl_cluster_list_create();

    esp_zb_basic_cluster_cfg_t basic_cfg = { .zcl_version = 3, .power_source = 0x01 };
    esp_zb_attribute_list_t *basic_attr = esp_zb_basic_cluster_create(&basic_cfg);
    /* ZCL 字符串：首字节是长度。
     * ★ 必须拆成两个相邻字面量：写成 "\x10ESP32…" 时编译器会把 \x10E
     *   当成一个十六进制转义（E 也是十六进制数字），长度字节就错了。 */
    static const char mfr_str[]   = "\x09" "Espressif";          /* len=9  */
    /* ★ 长度字节必须等于字符数：ESP32-C6-Repeater 是 17 个字符，写成 \x10
     *   会把最后一个 r 截掉，网关/HA 里显示成 "ESP32-C6-Repeate"。
     *   同一条规矩：\x 后面紧跟十六进制数字会被并进转义，所以务必拆成两段写。 */
    static const char model_str[] = "\x11" "ESP32-C6-Repeater";  /* len=17 */
    esp_zb_basic_cluster_add_attr(basic_attr, ESP_ZB_ZCL_ATTR_BASIC_MANUFACTURER_NAME_ID, (void *)mfr_str);
    esp_zb_basic_cluster_add_attr(basic_attr, ESP_ZB_ZCL_ATTR_BASIC_MODEL_IDENTIFIER_ID,  (void *)model_str);
    esp_zb_cluster_list_add_basic_cluster(cl, basic_attr, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);

    esp_zb_identify_cluster_cfg_t id_cfg = { .identify_time = 0 };
    esp_zb_cluster_list_add_identify_cluster(cl,
        esp_zb_identify_cluster_create(&id_cfg), ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);

    /* 厂商专有簇：运行时可写的发射功率 */
    esp_zb_attribute_list_t *pwr_attr = esp_zb_zcl_attr_list_create(CLUSTER_TX_POWER);
    esp_zb_custom_cluster_add_custom_attr(pwr_attr, ATTR_TX_POWER,
        ESP_ZB_ZCL_ATTR_TYPE_S8, ESP_ZB_ZCL_ATTR_ACCESS_READ_WRITE, &s_tx_power);
    esp_zb_cluster_list_add_custom_cluster(cl, pwr_attr, ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);

    /* OTA client（image type 0x0002）—— 必须挂在同一个端点上，
     * 网关的 zb_ota_server 就是往 endpoint 1 发的 */
    esp_zb_cluster_list_add_ota_cluster(cl,
        zb_ota_client_cluster_create(), ESP_ZB_ZCL_CLUSTER_CLIENT_ROLE);

    esp_zb_ep_list_t *ep = esp_zb_ep_list_create();
    esp_zb_endpoint_config_t ep_cfg = {
        .endpoint           = ENDPOINT,
        .app_profile_id     = ESP_ZB_AF_HA_PROFILE_ID,
        .app_device_id      = ESP_ZB_HA_RANGE_EXTENDER_DEVICE_ID,
        .app_device_version = 0,
    };
    esp_zb_ep_list_add_ep(ep, cl, ep_cfg);
    esp_zb_device_register(ep);
    esp_zb_core_action_handler_register(action_handler);

    ESP_ERROR_CHECK(esp_zb_start(false));
    esp_zb_stack_main_loop();
}

/* ── 网络自检：这个位置到底有没有能打到协调器的信号？ ─────────────────────
 * 光看"重启 – 尝试恢复原网络"没用：NVRAM 里有网络信息，板子会一直以为自己
 * 还在网里，即使实际打不到协调器也不会报错。所以每 30 秒做一次**真实的往返**：
 * 向协调器 0x0000 发 ZDO Node_Desc_req，看它答不答。
 *   - 答了（SUCCESS）⇒ 这个位置可用
 *   - 不答（TIMEOUT / 其它状态）⇒ 覆盖不到，换个位置
 * 顺便打一行 PAN / 信道 / 短地址 / 功率，方便对照。 */
#define NET_CHECK_MS  30000

/* 连续失败多少次后主动重新入网。6 次 ≈ 3 分钟。
 * 别调太小：链路边缘时自检本来就会时通时断，阈值太低会变成每 90 秒
 * 重新找一次网络（自己打断自己）。真正离网的情况由
 * ESP_ZB_NWK_SIGNAL_NO_ACTIVE_LINKS_LEFT 立刻兜住。 */
#define PROBE_FAIL_REJOIN  6
static int s_probe_fail = 0;

/* ── 自报父节点：写协调器 0xFC00/0x0002（uint16） ─────────────────────────
 * 只在网络自检成功时发（= 链路是通的），30 s 一次。
 * 写入目标固定是协调器 0x0000 / 端点 1（网关的 ZB_ENDPOINT）。
 * 值要先落在静态变量里：ZCL 请求只保存指针，不能指向栈上的临时值。 */
static uint16_t s_parent_addr = 0xFFFF;

static void report_parent(uint16_t par) {
    s_parent_addr = par;
    esp_zb_zcl_attribute_t attr = {
        .id   = ATTR_PARENT,
        .data = {
            .type  = ESP_ZB_ZCL_ATTR_TYPE_U16,
            .size  = sizeof(uint16_t),
            .value = &s_parent_addr,
        },
    };
    esp_zb_zcl_write_attr_cmd_t req = {
        .zcl_basic_cmd = {
            .dst_addr_u   = { .addr_short = 0x0000 },
            .dst_endpoint = 1,
            .src_endpoint = ENDPOINT,
        },
        .address_mode     = ESP_ZB_APS_ADDR_MODE_16_ENDP_PRESENT,
        .clusterID        = CLUSTER_INFO,
        .manuf_specific   = 0,
        .direction        = ESP_ZB_ZCL_CMD_DIRECTION_TO_SRV,
        .dis_default_resp = 0,
        .manuf_code       = 0,
        .attr_number      = 1,
        .attr_field       = &attr,
    };
    esp_zb_zcl_write_attr_cmd_req(&req);
}

static void net_probe_cb(esp_zb_zdp_status_t status, uint16_t addr,
                         esp_zb_af_node_desc_t *nd, void *user_ctx) {
    (void)addr; (void)user_ctx;
    /* 192 字节：中文每字 3 字节，下面两条模板分别是 ~85 / ~98 字节，
     * 用 96 会触发 -Werror=format-truncation。别往下调。 */
    char line[192];
    if (status == ESP_ZB_ZDP_STATUS_SUCCESS && nd != NULL) {
        s_probe_fail = 0;
        s_no_link    = 0;              /* 真的往返成功 ⇒ 退避归零 */
        /* ★ 自检成功才算「这次启动是健康的」，把连续启动计数清零。
         *   为什么不能只在 STEERING 成功时清：正常插拔时设备是从 NVRAM
         *   恢复网络的，**根本不走 steering**，计数就永远不清 ⇒ 拔插三次
         *   会被误判成重启循环，白白把功率降一档。
         *   用一次性标志，避免每 30 s 写一次 NVS。 */
        static bool bootn_done = false;
        if (!bootn_done) { bootn_clear(); bootn_done = true; }
        /* 自检通了 ⇒ 顺手把自己的父节点写给协调器（0xFC00/0x0002）。
         * 只有入网了才有父节点，未入网时 zb_nwk_get_parent() 返回
         * ZB_UNKNOWN_SHORT_ADDR，直接跳过。 */
        uint16_t par = (uint16_t)zb_nwk_get_parent();
        if (par != 0xFFFF) report_parent(par);
        snprintf(line, sizeof(line),
                 "网络自检 OK – 协调器应答 cap=0x%02x · PAN 0x%04hx ch %d addr 0x%04hx "
                 "父节点 0x%04hx · 功率 %d",
                 nd->mac_capability_flags, esp_zb_get_pan_id(),
                 esp_zb_get_current_channel(), esp_zb_get_short_address(),
                 par, (int)s_tx_power);
        ESP_LOGI(TAG, "%s", line);
    } else {
        s_probe_fail++;
        snprintf(line, sizeof(line),
                 "网络自检失败 – 协调器未应答 (status 0x%02x) · addr 0x%04hx 父节点 0x%04hx "
                 "ch %d 功率 %d（第 %d 次）",
                 (unsigned)status, esp_zb_get_short_address(),
                 (uint16_t)zb_nwk_get_parent(),
                 esp_zb_get_current_channel(), (int)s_tx_power, s_probe_fail);
        ESP_LOGW(TAG, "%s", line);

        /* 连续失败 ⇒ 大概率已经不在网里（被挪到覆盖盲区 / 协调器换了网络）。
         * 主动重新寻找网络：协调器开着 permit join 就能自愈，没开就下次再来。
         * OTA 下载中不做，会把镜像传输打断。 */
        if (s_probe_fail >= PROBE_FAIL_REJOIN) {
            s_probe_fail = 0;
            if (!zb_ota_client_busy()) {
                ESP_LOGW(TAG, "连续 %d 次收不到协调器应答 – 尝试重新寻找网络", PROBE_FAIL_REJOIN);
                rejoin_later(1000);
            }
        }
    }
}

/* ZDO 请求只能在 ZBOSS 任务里发，所以从自检任务里排队过去 */
static void net_probe_send(uint8_t param) {
    (void)param;
    if (esp_zb_bdb_is_factory_new()) return;   /* 还没入网，没什么可问的 */
    esp_zb_zdo_node_desc_req_param_t req = { .dst_nwk_addr = 0x0000 };
    esp_zb_zdo_node_desc_req(&req, net_probe_cb, NULL);
}

static void net_task(void *arg) {
    vTaskDelay(pdMS_TO_TICKS(15000));          /* 先给入网留时间 */
    while (1) {
        /* ★ net_task 是外来任务 → 调 SDK（esp_zb_scheduler_alarm）必须拿锁。
         *   原来这里裸调，OTA 期间每 30 s 就可能撞一次临界区 → 重启。 */
        zb_lock();
        esp_zb_scheduler_alarm(net_probe_send, 0, 0);
        zb_unlock();
        vTaskDelay(pdMS_TO_TICKS(NET_CHECK_MS));
    }
}

/* ── 入口 ──────────────────────────────────────────────────────────────── */
void app_main(void) {
    ESP_LOGI(TAG, "=== ESP32-C6 Zigbee 中继（Router + OTA + 可调功率）===");

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    nvs_load_power();

    /* ── 掉压自愈 ────────────────────────────────────────────────────────
     * 必须在 esp_zb_init() / 开射频**之前**判，否则下一次发射照样把电压拉塌。
     * 判决依据见 power_step_down() 上方注释（reset_reason 或连续启动计数）。 */
    esp_reset_reason_t rr = esp_reset_reason();
    uint8_t bootn = 0;
    bootn_bump(&bootn);
    ESP_LOGI(TAG, "复位原因 %s · 连续启动 %u 次 · NVS 功率 %d dBm",
             reset_reason_str(rr), (unsigned)bootn, (int)s_tx_power);

    if (rr == ESP_RST_BROWNOUT || bootn >= 3) {
        int8_t nb = power_step_down(s_tx_power);
        if (nb != s_tx_power) {
            ESP_LOGE(TAG, "供电不足（%s，连续启动 %u 次）– 功率 %d → %d dBm 自动降档",
                     reset_reason_str(rr), (unsigned)bootn, (int)s_tx_power, (int)nb);
            s_tx_power = nb;
            nvs_store_power(nb);
        } else {
            ESP_LOGE(TAG, "供电不足（%s）但已在最低档 %d dBm – 请换 ≥1 A 的独立 5V 供电；"
                          "长 USB 线 / 电脑前置口也会掉压",
                     reset_reason_str(rr), (int)s_tx_power);
        }
    }

    esp_zb_platform_config_t pcfg = {
        .radio_config = { .radio_mode = ZB_RADIO_MODE_NATIVE },
        .host_config  = { .host_connection_mode = ZB_HOST_CONNECTION_MODE_NONE },
    };
    ESP_ERROR_CHECK(esp_zb_platform_config(&pcfg));

    xTaskCreate(zb_task, "zb_main", 8192, NULL, 5, NULL);
    xTaskCreate(btn_task, "btn", 2048, NULL, 3, NULL);
    xTaskCreate(net_task, "net_chk", 3072, NULL, 2, NULL);
}
