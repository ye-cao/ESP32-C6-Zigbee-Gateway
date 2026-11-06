#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_partition.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "ha/esp_zigbee_ha_standard.h"
#include "esp_zigbee_cluster.h"
#include "esp_zigbee_attribute.h"
#include "esp_zigbee_endpoint.h"
#include "zcl/esp_zigbee_zcl_on_off.h"
#include "zb_gateway.h"
#include "zb_ota_server.h"
#include "ha_mqtt.h"
#include "aps/esp_zigbee_aps.h"
#include "esp_ieee802154.h"
#include "esp_zigbee_secur.h"

#define ZB_DEFAULT_CHANNEL  20
#define ZB_FALLBACK_CHANNEL 25
#define ZB_NVS_NAMESPACE    "zb_gw"
#define ZB_NVS_KEY_CHANNEL  "channel"

#define TAG "zb_gw"
#define ZB_ENDPOINT 1

/* 厂商专有簇 0xFC00（与中继固件的发射功率簇同一个）：
 *  属性 0x0001 = 中继发射功率 dBm（S8，网关写 → 中继）
 *  属性 0x0002 = 中继自报的父节点短地址（uint16，中继写 → 网关）
 * 两个方向走的是同一个簇，便于对照。 */
#define REP_CLUSTER_INFO   0xFC00
#define REP_ATTR_TX_POWER  0x0001
#define REP_ATTR_PARENT    0x0002

/* 中继上报的父节点。0xFFFF = 还没收到过。仅作展示/拓扑用。 */
static uint16_t s_rep_parent = 0xFFFF;

/* ── 设备表（只跟踪地址，不存名字） ─────────────────────────────────────── */
/* 网络规模 = 表的容量。esp_zb_overall_network_size_set()（SDK ≥1.2.2）按这个值
 * 给 ZBOSS 库分配邻居表和路由表；默认 64，早先这里硬写 32 —— 老库（1.0.9，
 * 表大小固定）本来也放不下更多。**必须在 esp_zb_init() 之前**设置，
 * 之后再调没有任何效果。 */
#define MAX_DEVICES ZB_GATEWAY_MAX_DEVICES
typedef struct {
    uint16_t short_addr;
    uint8_t  ieee[8];
    bool     used;
    bool     interviewed;   /* 已完成 Interview（Bind/Reporting） */
    uint8_t  lqi;   /* APS 层给的最后一次 LQI（0 = 还没拿到过有效值） */
    int8_t   rssi;  /* IEEE 802.15.4 层给的最后一次 RSSI（dBm） */
    uint32_t lqi_ms; /* lqi/rssi 最后一次更新的时间（开机后的毫秒数） */
    char     mfr[33];   /* Basic 0x0004 ManufacturerName */
    char     model[33]; /* Basic 0x0005 ModelIdentifier */
} zb_device_t;

static zb_device_t  s_devices[MAX_DEVICES];
static SemaphoreHandle_t s_dev_mutex;
static volatile int8_t  s_last_rssi = 0;

/* 只有 esp_zb_start() 走完之后才置 true（见 zb_task）。
 * uart_rx 任务比它先跑起来 ⇒ 所有对外入口都必须先查这个标志，
 * 否则会在 ZBOSS 里空指针解引用。 */
static volatile bool s_zb_ready = false;

static zb_device_t *find_or_add(uint16_t addr, const uint8_t *ieee) {
    for (int i = 0; i < MAX_DEVICES; i++) {
        if (s_devices[i].used && s_devices[i].short_addr == addr)
            return &s_devices[i];
    }
    for (int i = 0; i < MAX_DEVICES; i++) {
        if (!s_devices[i].used) {
            s_devices[i].used       = true;
            s_devices[i].short_addr = addr;
            if (ieee) memcpy(s_devices[i].ieee, ieee, 8);
            return &s_devices[i];
        }
    }
    ESP_LOGW(TAG, "设备表已满（上限 %d）", MAX_DEVICES);
    return NULL;
}

/* ── 设备表的持久化 ───────────────────────────────────────────────────────
 * 网络密钥、PAN、信道和地址表 ZBOSS 自己会存进 NVRAM（分区 zb_storage），
 * 只要没人设 nvram_erase_at_start，开机就接着用原来的网络。
 * 但我们这张表（短地址 + IEEE，用于 LQI 快照和 Interview）以前纯在 RAM 里：
 * 一重启就空了，得等每台设备重新发一次 Device-Announce —— 睡眠终端设备
 * 等这个要几个小时。
 * 所以这里存一份 NVS 副本。载入时 interviewed 故意置 false：Interview 要等
 * 设备重新应答之后才跑，否则 Bind/Reporting 全是空打。 */
#define DEVTAB_MAGIC   0x44425531u   /* 'DBU1' */
#define DEVTAB_KEY     "devtab"

typedef struct {
    uint32_t magic;
    uint8_t  count;
    struct { uint16_t addr; uint8_t ieee[8]; } e[MAX_DEVICES];
} __attribute__((packed)) devtab_blob_t;

/* 用静态变量而不是栈上：虽然是在 ZB 任务里调用，但本项目的栈预算规则
 * 一律适用（参见 LQI 缓冲那段）。 */
static devtab_blob_t s_devtab;

static void devtab_save(void) {
    nvs_handle_t h;
    if (nvs_open(ZB_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return;
    s_devtab.magic = DEVTAB_MAGIC;
    s_devtab.count = 0;
    xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
    for (int i = 0; i < MAX_DEVICES; i++) {
        /* IEEE 为 0 表示"还没解析出来" —— 这种条目重启后没有任何用，
         * 因为拿不到 IEEE 就做不了 Interview。 */
        if (!s_devices[i].used || s_devices[i].ieee[0] == 0) continue;
        s_devtab.e[s_devtab.count].addr = s_devices[i].short_addr;
        memcpy(s_devtab.e[s_devtab.count].ieee, s_devices[i].ieee, 8);
        if (++s_devtab.count >= MAX_DEVICES) break;
    }
    xSemaphoreGive(s_dev_mutex);
    nvs_set_blob(h, DEVTAB_KEY, &s_devtab, sizeof(s_devtab));
    nvs_commit(h);
    nvs_close(h);
}

static void devtab_load(void) {
    nvs_handle_t h;
    size_t len = sizeof(s_devtab);
    if (nvs_open(ZB_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return;
    esp_err_t err = nvs_get_blob(h, DEVTAB_KEY, &s_devtab, &len);
    nvs_close(h);
    if (err != ESP_OK || len < sizeof(uint32_t) + 1 ||
        s_devtab.magic != DEVTAB_MAGIC) return;

    int n = 0;
    for (int i = 0; i < s_devtab.count && i < MAX_DEVICES; i++) {
        memset(&s_devices[i], 0, sizeof(s_devices[i]));
        s_devices[i].used        = true;
        s_devices[i].short_addr  = s_devtab.e[i].addr;
        memcpy(s_devices[i].ieee, s_devtab.e[i].ieee, 8);
        s_devices[i].interviewed = false;
        n++;
    }
    ESP_LOGI(TAG, "已从 NVS 恢复 %d 台设备"
                  "（等设备重新上报后再做设备采访）", n);
}

/* ══════════════════════════════════════════════════════════════════════════ */
/*  入网后的 Interview：Active-EP → Simple-Desc → Bind → Configure-Reporting   */
/*                                                                             */
/*  背景：一个"裸"协调器在设备入网后什么都不做，很多终端设备约 1 分钟后就会     */
/*  放弃并退网（症状：「配对成功，但转眼又变未配对」）。真正的网关               */
/*  （小米 / Hue / …）入网后都会走标准流程：查活动端点 →                        */
/*  读 Simple Descriptor → 建 Binding → 配置属性上报。                          */
/*  下面这一段干的就是这件事。                                                  */
/* ══════════════════════════════════════════════════════════════════════════ */

#define IV_MAX_EPS      8
#define IV_MAX_CLUSTERS 16

/* 终端设备的 Server 簇（Input）→ 绑到协调器上，并给主属性配上报。
 * 注意：attr == IV_NO_REPORT（0xFFFF）时**只绑定**。0x0000 是合法的属性 ID
 * （On/Off、温度、湿度…… 都是各自的主属性）—— 以前写 `if (attr)` 判断，
 * 结果这些簇一个都没配上上报！ */
#define IV_NO_REPORT 0xFFFFu
typedef struct {
    uint16_t cluster;
    uint16_t attr;
    uint8_t  type;
    uint16_t min_int;
    uint16_t max_int;
    uint16_t change;
} iv_report_cfg_t;

static const iv_report_cfg_t s_iv_input[] = {
    { ESP_ZB_ZCL_CLUSTER_ID_ON_OFF,                   0x0000, ESP_ZB_ZCL_ATTR_TYPE_BOOL,     1,  300, 1 },
    { ESP_ZB_ZCL_CLUSTER_ID_TEMP_MEASUREMENT,         0x0000, ESP_ZB_ZCL_ATTR_TYPE_S16,      5,  300, 100 },
    { ESP_ZB_ZCL_CLUSTER_ID_REL_HUMIDITY_MEASUREMENT, 0x0000, ESP_ZB_ZCL_ATTR_TYPE_U16,     30, 300, 200 },
    { ESP_ZB_ZCL_CLUSTER_ID_ILLUMINANCE_MEASUREMENT,  0x0000, ESP_ZB_ZCL_ATTR_TYPE_U16,     30, 300, 100 },
    { ESP_ZB_ZCL_CLUSTER_ID_OCCUPANCY_SENSING,        0x0000, ESP_ZB_ZCL_ATTR_TYPE_8BITMAP,  1, 300, 1 },
    { ESP_ZB_ZCL_CLUSTER_ID_IAS_ZONE,                 0x0000, ESP_ZB_ZCL_ATTR_TYPE_16BITMAP, 1, 300, 1 },
    { ESP_ZB_ZCL_CLUSTER_ID_POWER_CONFIG,             0x0021, ESP_ZB_ZCL_ATTR_TYPE_U8,     300, 3600, 1 },
    { ESP_ZB_ZCL_CLUSTER_ID_POWER_CONFIG,             0x0020, ESP_ZB_ZCL_ATTR_TYPE_U8,     300, 3600, 1 },
};

/* 终端设备的 Client 簇（Output）→ 只绑定（按键命令、场景、组播
 * 之后就会以 ZCL Command 的形式送到协调器）。 */
static const uint16_t s_iv_output[] = {
    ESP_ZB_ZCL_CLUSTER_ID_ON_OFF,
    ESP_ZB_ZCL_CLUSTER_ID_LEVEL_CONTROL,
    ESP_ZB_ZCL_CLUSTER_ID_SCENES,
    ESP_ZB_ZCL_CLUSTER_ID_GROUPS,
};

typedef struct {
    bool     busy;
    uint16_t short_addr;
    uint8_t  ieee[8];
    uint8_t  ep_count;
    uint8_t  eps[IV_MAX_EPS];
    uint8_t  ep_idx;
} iv_ctx_t;

static iv_ctx_t  s_iv;
static uint8_t   s_coord_ieee[8];
static uint16_t  s_iv_change;   /* 上报变化量 —— 必须 static，协议栈会直接读它 */

/* 最后一个经 APS 收到的帧 —— SET_ATTR_VALUE 消息（按键命令）里**没有**
 * 源地址，所以只能从紧邻的上一帧取（同一次协议栈轮次里，
 * aps_data_ind_cb 先于 ZCL 回调执行）。 */
static struct {
    uint16_t addr;
    uint8_t  ep;
    uint16_t cluster;
} s_last_aps;

static void iv_active_ep_cb(esp_zb_zdp_status_t status, uint8_t ep_count,
                            uint8_t *ep_id_list, void *user_ctx);
static void iv_simple_desc_cb(esp_zb_zdp_status_t status,
                              esp_zb_af_simple_desc_1_1_t *sd, void *user_ctx);
static void iv_bind_cb(esp_zb_zdp_status_t status, void *user_ctx);

/* 读 Basic 簇：状态 + 回调（定义在后面） */
static uint16_t s_basic_pending_addr = 0xFFFF;
static uint8_t  s_basic_pending_ep   = 1;   /* 设备真实端点（修掉了写死 ep1 的老毛病） */
static void read_basic_cb(uint8_t param);
/* 前向声明 —— start_basic_scan 在后面才定义，
 * 但前面 Interview 收尾时就要调用它。 */
static void start_basic_scan(uint16_t addr, const uint8_t *eps, uint8_t n);

static void iv_ieee_str(const uint8_t *ieee, char *out, size_t len) {
    snprintf(out, len, "%02x:%02x:%02x:%02x:%02x:%02x:%02x:%02x",
             ieee[7], ieee[6], ieee[5], ieee[4],
             ieee[3], ieee[2], ieee[1], ieee[0]);
}

/* 发出的 Bind 请求队列 —— 只用于日志里把 Response 对上号
 * （ZBOSS 每份 Response 只给 status + ctx）。 */
typedef struct { uint16_t addr; uint8_t ep; uint16_t cluster; } iv_bind_rec_t;
static iv_bind_rec_t s_iv_bind_fifo[16];
static uint8_t s_iv_bind_head = 0, s_iv_bind_tail = 0;

static void iv_bind_fifo_push(uint16_t addr, uint8_t ep, uint16_t cluster) {
    uint8_t next = (uint8_t)((s_iv_bind_tail + 1) % 16);
    if (next == s_iv_bind_head) return;   /* voll – nur Logging, verwerfen */
    s_iv_bind_fifo[s_iv_bind_tail].addr    = addr;
    s_iv_bind_fifo[s_iv_bind_tail].ep      = ep;
    s_iv_bind_fifo[s_iv_bind_tail].cluster = cluster;
    s_iv_bind_tail = next;
}

static bool iv_bind_fifo_pop(uint16_t *addr, uint8_t *ep, uint16_t *cluster) {
    if (s_iv_bind_head == s_iv_bind_tail) return false;
    *addr    = s_iv_bind_fifo[s_iv_bind_head].addr;
    *ep      = s_iv_bind_fifo[s_iv_bind_head].ep;
    *cluster = s_iv_bind_fifo[s_iv_bind_head].cluster;
    s_iv_bind_head = (uint8_t)((s_iv_bind_head + 1) % 16);
    return true;
}

static void interview_start(uint16_t addr, const uint8_t *ieee) {
    if (addr == 0x0000 || addr == 0xFFFF) return;

    s_iv.busy       = true;
    s_iv.short_addr = addr;
    if (ieee) memcpy(s_iv.ieee, ieee, 8);
    else      memset(s_iv.ieee, 0, 8);
    s_iv.ep_count   = 0;
    s_iv.ep_idx     = 0;
    esp_zb_get_long_address(s_coord_ieee);

    /* 标记为已完成 Interview —— 免得每来一帧就重做一遍 */
    xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
    for (int i = 0; i < MAX_DEVICES; i++) {
        if (s_devices[i].used && s_devices[i].short_addr == addr) {
            s_devices[i].interviewed = true;
            break;
        }
    }
    xSemaphoreGive(s_dev_mutex);

    char line[96];
    snprintf(line, sizeof(line),
             "{\"t\":\"interview\",\"addr\":\"0x%04x\",\"state\":\"start\"}", addr);
    ha_mqtt_emit_raw(line);
    ESP_LOGI(TAG, "设备采访 0x%04x：读取 Active-EP …", addr);

    esp_zb_zdo_active_ep_req_param_t req = { .addr_of_interest = addr };
    esp_zb_zdo_active_ep_req(&req, iv_active_ep_cb, NULL);
}

static void iv_request_simple_desc(uint8_t idx) {
    if (!s_iv.busy || idx >= s_iv.ep_count) {
        char eps[48] = "";
        size_t pos = 0;
        for (uint8_t i = 0; i < s_iv.ep_count && pos < sizeof(eps) - 4; i++) {
            pos += snprintf(eps + pos, sizeof(eps) - pos, "%s%u",
                            i ? "," : "", s_iv.eps[i]);
        }
        char line[112];
        snprintf(line, sizeof(line),
                 "{\"t\":\"interview\",\"addr\":\"0x%04x\",\"state\":\"done\",\"eps\":[%s]}",
                 s_iv.short_addr, eps);
        ha_mqtt_emit_raw(line);
        ESP_LOGI(TAG, "设备采访 0x%04x 完成（端点：%s）",
                 s_iv.short_addr, eps);
        /* 读 Basic 属性（厂商/型号）—— 遍历所有端点 */
        if (s_iv.ep_count > 0) {
            start_basic_scan(s_iv.short_addr, s_iv.eps, s_iv.ep_count);
        }
        s_iv.busy = false;
        return;
    }
    s_iv.ep_idx = idx;
    esp_zb_zdo_simple_desc_req_param_t req = {
        .addr_of_interest = s_iv.short_addr,
        .endpoint         = s_iv.eps[idx],
    };
    esp_zb_zdo_simple_desc_req(&req, iv_simple_desc_cb, NULL);
}

static void iv_active_ep_cb(esp_zb_zdp_status_t status, uint8_t ep_count,
                            uint8_t *ep_id_list, void *user_ctx) {
    (void)user_ctx;
    if (!s_iv.busy) return;

    if (status != ESP_ZB_ZDP_STATUS_SUCCESS || ep_count == 0 || !ep_id_list) {
        char line[96];
        snprintf(line, sizeof(line),
                 "{\"t\":\"interview\",\"addr\":\"0x%04x\",\"state\":\"ep_failed\",\"status\":\"0x%02x\"}",
                 s_iv.short_addr, (unsigned)status);
        ha_mqtt_emit_raw(line);
        ESP_LOGW(TAG, "设备采访 0x%04x：读取 Active-EP 失败 (0x%02x)",
                 s_iv.short_addr, (unsigned)status);
        s_iv.busy = false;
        return;
    }

    if (ep_count > IV_MAX_EPS) ep_count = IV_MAX_EPS;
    memcpy(s_iv.eps, ep_id_list, ep_count);
    s_iv.ep_count = ep_count;
    s_iv.ep_idx   = 0;

    char eps[48] = "";
    size_t pos = 0;
    for (uint8_t i = 0; i < ep_count && pos < sizeof(eps) - 4; i++) {
        pos += snprintf(eps + pos, sizeof(eps) - pos, "%s%u", i ? "," : "", s_iv.eps[i]);
    }
    char line[112];
    snprintf(line, sizeof(line),
             "{\"t\":\"interview\",\"addr\":\"0x%04x\",\"state\":\"ep_list\",\"eps\":[%s]}",
             s_iv.short_addr, eps);
    ha_mqtt_emit_raw(line);
    ESP_LOGI(TAG, "设备采访 0x%04x：共 %u 个端点 [%s]",
             s_iv.short_addr, (unsigned)ep_count, eps);

    iv_request_simple_desc(0);
}

static void iv_bind(uint8_t ep, uint16_t cluster) {
    esp_zb_zdo_bind_req_param_t req;
    memset(&req, 0, sizeof(req));
    memcpy(req.src_address, s_iv.ieee, sizeof(req.src_address));
    req.src_endp      = ep;
    req.cluster_id    = cluster;
    req.dst_addr_mode = ESP_ZB_ZDO_BIND_DST_ADDR_MODE_64_BIT_EXTENDED;
    memcpy(req.dst_address_u.addr_long, s_coord_ieee, 8);
    req.dst_endp      = ZB_ENDPOINT;
    req.req_dst_addr  = s_iv.short_addr;

    iv_bind_fifo_push(s_iv.short_addr, ep, cluster);
    esp_zb_zdo_device_bind_req(&req, iv_bind_cb, NULL);

    char line[112];
    snprintf(line, sizeof(line),
             "{\"t\":\"interview\",\"act\":\"bind\",\"addr\":\"0x%04x\",\"ep\":%u,\"cluster\":\"0x%04x\"}",
             s_iv.short_addr, (unsigned)ep, cluster);
    ha_mqtt_emit_raw(line);
}

static void iv_bind_cb(esp_zb_zdp_status_t status, void *user_ctx) {
    (void)user_ctx;
    uint16_t addr = s_iv.short_addr;
    uint8_t  ep = 0;
    uint16_t cluster = 0;
    bool known = iv_bind_fifo_pop(&addr, &ep, &cluster);

    char line[128];
    if (known) {
        snprintf(line, sizeof(line),
                 "{\"t\":\"interview\",\"act\":\"bind_resp\",\"addr\":\"0x%04x\",\"ep\":%u,"
                 "\"cluster\":\"0x%04x\",\"status\":\"0x%02x\"}",
                 addr, (unsigned)ep, cluster, (unsigned)status);
    } else {
        snprintf(line, sizeof(line),
                 "{\"t\":\"interview\",\"act\":\"bind_resp\",\"status\":\"0x%02x\"}",
                 (unsigned)status);
    }
    ha_mqtt_emit_raw(line);
    if (known && status != ESP_ZB_ZDP_STATUS_SUCCESS) {
        ESP_LOGW(TAG, "设备采访：绑定 ep%u 簇 0x%04x → 0x%02x",
                 (unsigned)ep, cluster, (unsigned)status);
    }
}

/* ZCL 把数据类型分成「模拟量」和「离散量」。只有模拟量才需要在
 * Configure-Reporting 记录里带 "reportable change" 字段；离散量
 * （Bool、Bitmap、Enum、字符串……）按 ZCL 2.5.7.1 是**不能**带的。
 * 硬带上就会让记录超长 —— 有些终端设备（例如 Feibit HGZB-043）会回 SUCCESS，
 * 却存进一堆垃圾，之后再也报不出状态变化。HA 一直看不到开关状态，
 * 根因就是这个。 */
static bool iv_type_is_analog(uint8_t t) {
    if (t >= ESP_ZB_ZCL_ATTR_TYPE_U8 && t <= ESP_ZB_ZCL_ATTR_TYPE_S64) return true;  /* 0x20–0x2f */
    if (t >= ESP_ZB_ZCL_ATTR_TYPE_SEMI && t <= ESP_ZB_ZCL_ATTR_TYPE_DOUBLE) return true; /* 0x38–0x3a */
    if (t >= ESP_ZB_ZCL_ATTR_TYPE_TIME_OF_DAY && t <= ESP_ZB_ZCL_ATTR_TYPE_UTC_TIME) return true; /* 0xe0–0xe2 */
    return false;   /* Bool/Bitmap/Enum/String/Array/Struct/Set → diskret */
}

static void iv_config_report(uint8_t ep, uint16_t cluster, const iv_report_cfg_t *cfg) {
    esp_zb_zcl_config_report_record_t rec;
    memset(&rec, 0, sizeof(rec));
    rec.direction        = 0x00;   /* 0x00 = Attribut wird gemeldet (Server → Client) */
    rec.attributeID      = cfg->attr;
    rec.attrType         = cfg->type;
    rec.min_interval     = cfg->min_int;
    rec.max_interval     = cfg->max_int;
    s_iv_change          = cfg->change;
    rec.reportable_change = iv_type_is_analog(cfg->type) ? &s_iv_change : NULL;

    esp_zb_zcl_config_report_cmd_t req;
    memset(&req, 0, sizeof(req));
    req.zcl_basic_cmd.dst_addr_u.addr_short = s_iv.short_addr;
    req.zcl_basic_cmd.dst_endpoint = ep;
    req.zcl_basic_cmd.src_endpoint = ZB_ENDPOINT;
    req.address_mode   = ESP_ZB_APS_ADDR_MODE_16_ENDP_PRESENT;
    req.clusterID      = cluster;
    req.record_number  = 1;
    req.record_field   = &rec;

    esp_zb_zcl_config_report_cmd_req(&req);

    char line[128];
    snprintf(line, sizeof(line),
             "{\"t\":\"interview\",\"act\":\"config_report\",\"addr\":\"0x%04x\",\"ep\":%u,"
             "\"cluster\":\"0x%04x\",\"attr\":\"0x%04x\"}",
             s_iv.short_addr, (unsigned)ep, cluster, cfg->attr);
    ha_mqtt_emit_raw(line);
}

static void iv_simple_desc_cb(esp_zb_zdp_status_t status,
                              esp_zb_af_simple_desc_1_1_t *sd, void *user_ctx) {
    (void)user_ctx;
    if (!s_iv.busy) return;

    uint8_t ep = s_iv.eps[s_iv.ep_idx];
    if (status != ESP_ZB_ZDP_STATUS_SUCCESS || !sd) {
        ESP_LOGW(TAG, "设备采访：Simple-Desc ep%u 失败 (0x%02x)",
                 (unsigned)ep, (unsigned)status);
        iv_request_simple_desc((uint8_t)(s_iv.ep_idx + 1));
        return;
    }

    uint8_t in_n  = sd->app_input_cluster_count;
    uint8_t out_n = sd->app_output_cluster_count;
    if (in_n  > IV_MAX_CLUSTERS) in_n  = IV_MAX_CLUSTERS;
    if (out_n > IV_MAX_CLUSTERS) out_n = IV_MAX_CLUSTERS;

    /* Simple-Descriptor dokumentieren */
    {
        char in_s[120] = "", out_s[120] = "";
        size_t pos = 0;
        for (uint8_t i = 0; i < in_n && pos < sizeof(in_s) - 8; i++) {
            pos += snprintf(in_s + pos, sizeof(in_s) - pos, "%s0x%04x",
                            i ? "," : "", (unsigned)sd->app_cluster_list[i]);
        }
        pos = 0;
        const uint16_t *out = sd->app_cluster_list + sd->app_input_cluster_count;
        for (uint8_t i = 0; i < out_n && pos < sizeof(out_s) - 8; i++) {
            pos += snprintf(out_s + pos, sizeof(out_s) - pos, "%s0x%04x",
                            i ? "," : "", (unsigned)out[i]);
        }
        char line[440];
        snprintf(line, sizeof(line),
                 "{\"t\":\"interview\",\"addr\":\"0x%04x\",\"ep\":%u,\"profile\":\"0x%04x\","
                 "\"device\":\"0x%04x\",\"in\":[%s],\"out\":[%s]}",
                 s_iv.short_addr, (unsigned)ep,
                 (unsigned)sd->app_profile_id, (unsigned)sd->app_device_id, in_s, out_s);
        ha_mqtt_emit_raw(line);
    }

    /* Input 簇（设备侧是 Server）→ 绑定 + 配上报 */
    for (uint8_t i = 0; i < in_n; i++) {
        uint16_t cl = sd->app_cluster_list[i];
        for (size_t k = 0; k < sizeof(s_iv_input) / sizeof(s_iv_input[0]); k++) {
            if (s_iv_input[k].cluster != cl) continue;
            iv_bind(ep, cl);
            if (s_iv_input[k].attr != IV_NO_REPORT) iv_config_report(ep, cl, &s_iv_input[k]);
            break;
        }
    }

    /* Output 簇（设备侧是 Client）→ 只绑定 */
    const uint16_t *out = sd->app_cluster_list + sd->app_input_cluster_count;
    for (uint8_t i = 0; i < out_n; i++) {
        uint16_t cl = out[i];
        for (size_t k = 0; k < sizeof(s_iv_output) / sizeof(s_iv_output[0]); k++) {
            if (s_iv_output[k] != cl) continue;
            iv_bind(ep, cl);
            break;
        }
    }

    iv_request_simple_desc((uint8_t)(s_iv.ep_idx + 1));
}

/* ── Basic-Cluster: Hersteller + Model beim Join lesen ─────────────────── */
static uint16_t s_basic_attrs[2] = { 0x0004, 0x0005 };

static void read_basic_at(uint16_t addr, uint8_t ep) {
    esp_zb_zcl_read_attr_cmd_t req = {
        .zcl_basic_cmd = {
            .dst_addr_u  = { .addr_short = addr },
            .dst_endpoint = ep,
            .src_endpoint = ZB_ENDPOINT,
        },
        .address_mode = ESP_ZB_APS_ADDR_MODE_16_ENDP_PRESENT,
        .clusterID    = ESP_ZB_ZCL_CLUSTER_ID_BASIC,
        .attr_number  = 2,
        .attr_field   = s_basic_attrs,
    };
    esp_zb_zcl_read_attr_cmd_req(&req);
}

static void read_basic_cb(uint8_t param) {
    (void)param;
    if (s_basic_pending_addr == 0xFFFF) return;
    read_basic_at(s_basic_pending_addr, s_basic_pending_ep);
    s_basic_pending_addr = 0xFFFF;
}

/* 型号扫描：Basic 簇有的厂商放在 ep1，有的放在每个开关端点上
 * （Feibit/Nue：ep16..18）。Interview 时只试了第一个端点 ⇒ "basic" 从来没
 * 回来过，型号一直是未知。所以这里挨个把已知端点试一遍。 */
#define BASIC_SCAN_MAX 9
static uint16_t s_scan_addr = 0xFFFF;
static uint8_t  s_scan_eps[BASIC_SCAN_MAX];
static uint8_t  s_scan_n = 0, s_scan_i = 0;

static void read_basic_scan_cb(uint8_t param) {
    (void)param;
    if (s_scan_addr == 0xFFFF || s_scan_i >= s_scan_n) { s_scan_addr = 0xFFFF; return; }
    read_basic_at(s_scan_addr, s_scan_eps[s_scan_i++]);
    if (s_scan_i < s_scan_n) esp_zb_scheduler_alarm(read_basic_scan_cb, 0, 800);
    else s_scan_addr = 0xFFFF;
}

static void start_basic_scan(uint16_t addr, const uint8_t *eps, uint8_t n) {
    s_scan_addr = addr;
    s_scan_n    = 0;
    s_scan_eps[s_scan_n++] = 1;          /* Basic 大多在 ep1 上 */
    for (uint8_t k = 0; k < n && s_scan_n < BASIC_SCAN_MAX; k++) {
        if (eps[k] != 1) s_scan_eps[s_scan_n++] = eps[k];
    }
    s_scan_i = 0;
    esp_zb_scheduler_alarm(read_basic_scan_cb, 0, 500);
}

/* ── 给「安静」重连的设备补 IEEE ─────────────────────────────────────────────
 * 协调器重启后，很多设备不会发 Device-Announce 就回来了：它们直接接着发
 * APS 数据。这时我们不知道它们的 IEEE，Interview 会被跳过（"没 IEEE 就不绑"），
 * Basic 簇也就永远没读过 → HA 里没有型号。
 * 所以这里用 ZDO 事后补解析 IEEE，然后再补一次 Interview。
 * 限速：全网最多每 30 s 一个请求。 */
static TickType_t s_ieee_last_tick = 0;

/* SDK ≥1.6.0 改了回调类型：应答是 esp_zb_zdo_ieee_addr_rsp_t*，
 * 不再是裸的 ieee_addr 数组。 */
static void ieee_resolved_cb(esp_zb_zdp_status_t zdo_status,
                             esp_zb_zdo_ieee_addr_rsp_t *resp, void *user_ctx) {
    uint16_t addr = (uint16_t)(uintptr_t)user_ctx;
    if (zdo_status != ESP_ZB_ZDP_STATUS_SUCCESS || resp == NULL) {
        ESP_LOGW(TAG, "IEEE 地址请求 0x%04x 失败 (0x%02x)", addr, zdo_status);
        return;
    }
    xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
    zb_device_t *d = find_or_add(addr, resp->ieee_addr);
    bool need = d && !d->interviewed;
    xSemaphoreGive(s_dev_mutex);
    ESP_LOGI(TAG, "IEEE 0x%04x 已解析 → 设备采访%s", addr, need ? "" : " (schon erfolgt)");
    if (d) devtab_save();
    if (need) interview_start(addr, resp->ieee_addr);
}

static void request_ieee(uint16_t addr) {
    esp_zb_zdo_ieee_addr_req_param_t req = {
        .dst_nwk_addr     = addr,
        .addr_of_interest = addr,
        .request_type     = 0x00,   /* Single device response */
        .start_index      = 0,
    };
    esp_zb_zdo_ieee_addr_req(&req, ieee_resolved_cb, (void *)(uintptr_t)addr);
}

/* ── 保活测试：授权后周期性读 Basic 属性 ──────────────────────────────────
 * 假设：TI 设备（Z-Stack）在 "Device Authorized"(rejoin=0) 之后约 10 s 退网，
 * 尽管 Interview + Bind 都成功了。
 * 怀疑：设备期待协调器主动跟它通信。
 * 12 轮 × 5 s = 覆盖 60 s；每轮打一行 {"t":"keepalive",...}。 */
static uint16_t s_ka_addr = 0xFFFF;   /* 0xFFFF = kein Keep-Alive aktiv */
static uint8_t  s_ka_ep   = 1;
static uint8_t  s_ka_n    = 0;
#define KA_ROUNDS      12
#define KA_INTERVAL_MS 5000

static void keepalive_cb(uint8_t param) {
    if (s_ka_addr == 0xFFFF) return;
    s_ka_n = param;
    char line[96];
    snprintf(line, sizeof(line),
             "{\"t\":\"keepalive\",\"addr\":\"0x%04x\",\"ep\":%u,\"round\":%u}",
             s_ka_addr, (unsigned)s_ka_ep, (unsigned)s_ka_n);
    ha_mqtt_emit_raw(line);
    /* 通过 read_basic_cb 向真实端点发 Read Attributes */
    s_basic_pending_addr = s_ka_addr;
    s_basic_pending_ep   = s_ka_ep;
    esp_zb_scheduler_alarm(read_basic_cb, 0, 10);
    if (s_ka_n < KA_ROUNDS) {
        esp_zb_scheduler_alarm(keepalive_cb, (uint8_t)(s_ka_n + 1), KA_INTERVAL_MS);
    } else {
        s_ka_addr = 0xFFFF;
    }
}

static void keepalive_start(uint16_t addr, uint8_t ep) {
    s_ka_addr = 0xFFFF;               /* laufende Alarms entwerden */
    s_ka_addr = addr;
    s_ka_ep   = ep;
    s_ka_n    = 0;
    char line[96];
    snprintf(line, sizeof(line),
             "{\"t\":\"keepalive\",\"addr\":\"0x%04x\",\"ep\":%u,\"round\":0}",
             addr, (unsigned)ep);
    ha_mqtt_emit_raw(line);
    esp_zb_scheduler_alarm(keepalive_cb, 1, KA_INTERVAL_MS);
}

static void keepalive_stop(void) {
    s_ka_addr = 0xFFFF;
}

/* ── ZCL-Attribut-Callback ──────────────────────────────────────────────── */
static esp_err_t zb_action_handler(esp_zb_core_action_callback_id_t callback_id,
                                    const void *message) {
    /* Basic cluster read response */
    if (callback_id == ESP_ZB_CORE_CMD_READ_ATTR_RESP_CB_ID) {
        const esp_zb_zcl_cmd_read_attr_resp_message_t *m = message;
        if (!m) return ESP_OK;

        /* 远程命令之后主动回读 On/Off。设备不会发 Report，
         * 但会回应 Read Attributes ——
         * 用这个纠正网关的乐观假设。 */
        if (m->info.cluster == ESP_ZB_ZCL_CLUSTER_ID_ON_OFF) {
            uint16_t ra = m->info.src_address.u.short_addr;
            for (esp_zb_zcl_read_attr_resp_variable_t *v = m->variables; v; v = v->next) {
                if (v->status != ESP_ZB_ZCL_STATUS_SUCCESS) continue;
                if (v->attribute.id != ESP_ZB_ZCL_ATTR_ON_OFF_ON_OFF_ID) continue;
                if (!v->attribute.data.value) continue;
                uint8_t val = *(uint8_t *)v->attribute.data.value;
                char pl[24];
                snprintf(pl, sizeof(pl), "{\"v\":%u}", val ? 1u : 0u);
                ha_mqtt_publish_zigbee_ep(ra, m->info.src_endpoint, "on_off", pl);
            }
            return ESP_OK;
        }

        if (m->info.cluster != ESP_ZB_ZCL_CLUSTER_ID_BASIC) return ESP_OK;
        uint16_t addr = m->info.src_address.u.short_addr;
        xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
        zb_device_t *dev = find_or_add(addr, NULL);
        if (dev) {
            for (esp_zb_zcl_read_attr_resp_variable_t *v = m->variables; v; v = v->next) {
                if (v->status != ESP_ZB_ZCL_STATUS_SUCCESS) continue;
                char *dst = (v->attribute.id == 0x0004) ? dev->mfr
                          : (v->attribute.id == 0x0005) ? dev->model : NULL;
                if (!dst) continue;
                const uint8_t *s = (const uint8_t *)v->attribute.data.value;
                uint8_t len = s[0]; if (len > 32) len = 32;
                memcpy(dst, s + 1, len); dst[len] = '\0';
            }
        }
        xSemaphoreGive(s_dev_mutex);
        if (dev && (dev->mfr[0] || dev->model[0])) {
            char pl[100];
            snprintf(pl, sizeof(pl),
                     "{\"mfr\":\"%s\",\"model\":\"%s\"}", dev->mfr, dev->model);
            ha_mqtt_publish_zigbee(addr, "basic", pl);
        }
        return ESP_OK;
    }

    /* OTA Server: Bild-Query vom Client */
    if (callback_id == ESP_ZB_CORE_OTA_UPGRADE_SRV_QUERY_IMAGE_CB_ID) {
        const esp_zb_zcl_ota_upgrade_server_query_image_message_t *m = message;
        if (m && m->table_idx) *m->table_idx = 0;
        return ESP_OK;
    }

    /* OTA Server: Transfer-Status */
    if (callback_id == ESP_ZB_CORE_OTA_UPGRADE_SRV_STATUS_CB_ID) {
        const esp_zb_zcl_ota_upgrade_server_status_message_t *m = message;
        if (!m) return ESP_OK;
        const char *st =
            m->server_status == ESP_ZB_ZCL_OTA_UPGRADE_SERVER_STARTED ? "started" :
            m->server_status == ESP_ZB_ZCL_OTA_UPGRADE_SERVER_ABORTED ? "aborted" : "done";
        char line[80];
        snprintf(line, sizeof(line),
                 "{\"t\":\"ota_srv_status\",\"addr\":\"0x%04x\",\"status\":\"%s\"}",
                 m->zcl_addr.u.short_addr, st);
        ha_mqtt_emit_raw(line);
        /* 会话已结束或已中止 ⇒ 停掉补货任务，
         * 否则它还会继续向树莓派要没人需要的块。 */
        if (m->server_status != ESP_ZB_ZCL_OTA_UPGRADE_SERVER_STARTED) {
            zb_ota_server_stop();
        }
        ESP_LOGI(TAG, "OTA srv 0x%04x: %s", m->zcl_addr.u.short_addr, st);
        return ESP_OK;
    }

    /* 已绑定开关的按键命令：协调器上的 On/Off Server 簇被写入 OnOff 属性
     * → 落到这里（跟 HA 的 On/Off Light 示例是同一条路径）。消息里没有源地址，
     * 所以从 s_last_aps 取（aps_data_ind_cb 在它之前刚刚填好）。 */
    if (callback_id == ESP_ZB_CORE_SET_ATTR_VALUE_CB_ID) {
        const esp_zb_zcl_set_attr_value_message_t *m = message;

        /* ★ 中继自报父节点（0xFC00 / 0x0002，uint16）。
         * 中继每 30 s 自检成功时写一次，值就是 zb_nwk_get_parent()。
         * 这条比邻居表+BFS 推断可靠得多 —— 是设备自己说的。 */
        if (m && m->info.status == ESP_ZB_ZCL_STATUS_SUCCESS &&
            m->info.cluster == REP_CLUSTER_INFO &&
            m->attribute.id == REP_ATTR_PARENT &&
            m->attribute.data.value != NULL) {
            uint16_t p = *(const uint16_t *)m->attribute.data.value;
            /* SET_ATTR_VALUE 的 message 里没有源地址（和下面 On/Off 那条一样），
             * 只能从 s_last_aps 取 —— 它由 aps_data_ind_cb 在紧邻之前填好。 */
            uint16_t src = s_last_aps.addr;
            uint8_t  sep = s_last_aps.ep;
            s_rep_parent = p;
            char line[128];
            snprintf(line, sizeof(line),
                     "{\"t\":\"parent\",\"addr\":\"0x%04x\",\"p\":\"0x%04x\",\"ep\":%u}",
                     src, p, (unsigned)sep);
            ha_mqtt_emit_raw(line);
            ESP_LOGI(TAG, "0x%04x 自报父节点 0x%04x", src, p);
            return ESP_OK;
        }

        if (m && m->info.status == ESP_ZB_ZCL_STATUS_SUCCESS &&
            m->info.cluster == ESP_ZB_ZCL_CLUSTER_ID_ON_OFF &&
            m->attribute.id == ESP_ZB_ZCL_ATTR_ON_OFF_ON_OFF_ID &&
            m->attribute.data.type == ESP_ZB_ZCL_ATTR_TYPE_BOOL &&
            m->attribute.data.value) {
            bool v = *(const bool *)m->attribute.data.value;
            uint16_t src = s_last_aps.addr;
            uint8_t  ep  = s_last_aps.ep;
            if (src != 0x0000 && src != 0xFFFF && ep != 0) {
                char pl[24];
                snprintf(pl, sizeof(pl), "{\"v\":%u}", v ? 1u : 0u);
                ha_mqtt_publish_zigbee_ep(src, ep, "on_off", pl);
                char line[112];
                snprintf(line, sizeof(line),
                         "{\"t\":\"cmd\",\"addr\":\"0x%04x\",\"ep\":%u,\"cluster\":\"on_off\",\"v\":%u}",
                         src, (unsigned)ep, v ? 1u : 0u);
                ha_mqtt_emit_raw(line);
                ESP_LOGI(TAG, "开关 0x%04x ep%u → %s",
                         src, (unsigned)ep, v ? "ON" : "OFF");
            } else {
                ha_mqtt_emit_raw("{\"t\":\"cmd\",\"cluster\":\"on_off\",\"src\":\"unknown\"}");
            }
        }
        return ESP_OK;
    }

    /* Configure-Reporting 的应答（设备对我们 Interview 的回话） */
    if (callback_id == ESP_ZB_CORE_CMD_REPORT_CONFIG_RESP_CB_ID) {
        const esp_zb_zcl_cmd_config_report_resp_message_t *m = message;
        if (m) {
            unsigned ok = 0, bad = 0;
            for (esp_zb_zcl_config_report_resp_variable_t *v = m->variables; v; v = v->next) {
                if (v->status == ESP_ZB_ZCL_STATUS_SUCCESS) ok++;
                else bad++;
            }
            char line[160];
            snprintf(line, sizeof(line),
                     "{\"t\":\"interview\",\"act\":\"config_report_resp\",\"addr\":\"0x%04x\",\"ep\":%u,"
                     "\"cluster\":\"0x%04x\",\"status\":\"0x%02x\",\"ok\":%u,\"bad\":%u}",
                     m->info.src_address.u.short_addr, (unsigned)m->info.src_endpoint,
                     m->info.cluster, (unsigned)m->info.status, ok, bad);
            ha_mqtt_emit_raw(line);
        }
        return ESP_OK;
    }

    /* Nur Attribut-Reports verarbeiten. Andere Callback-IDs (u. a.
     * SET_ATTR_VALUE_CB_ID) liefern andere Struct-Typen – ein Cast auf
     * report_attr_message_t 会读错偏移（发出垃圾数据）。
     * SET_ATTR_VALUE 在上面已经按正确类型单独处理过了。 */
    if (callback_id != ESP_ZB_CORE_REPORT_ATTR_CB_ID)
        return ESP_OK;

    const esp_zb_zcl_report_attr_message_t *msg =
        (const esp_zb_zcl_report_attr_message_t *)message;
    if (!msg) return ESP_ERR_INVALID_ARG;

    uint16_t addr = msg->src_address.u.short_addr;

    ESP_LOGD(TAG, "ZCL 0x%04x ep%u cluster 0x%04x attr 0x%04x",
             addr, (unsigned)msg->src_endpoint, msg->cluster, msg->attribute.id);

    char payload[64];

    switch (msg->cluster) {
    case ESP_ZB_ZCL_CLUSTER_ID_ON_OFF: {
        uint8_t v = *(uint8_t *)msg->attribute.data.value;
        snprintf(payload, sizeof(payload), "{\"v\":%u}", v);
        ha_mqtt_publish_zigbee_ep(addr, msg->src_endpoint, "on_off", payload);
        break;
    }
    case ESP_ZB_ZCL_CLUSTER_ID_TEMP_MEASUREMENT: {
        int16_t raw = *(int16_t *)msg->attribute.data.value;
        snprintf(payload, sizeof(payload), "{\"raw\":%d}", raw);
        ha_mqtt_publish_zigbee_ep(addr, msg->src_endpoint, "temperature", payload);
        break;
    }
    case ESP_ZB_ZCL_CLUSTER_ID_REL_HUMIDITY_MEASUREMENT: {
        uint16_t raw = *(uint16_t *)msg->attribute.data.value;
        snprintf(payload, sizeof(payload), "{\"raw\":%u}", raw);
        ha_mqtt_publish_zigbee_ep(addr, msg->src_endpoint, "humidity", payload);
        break;
    }
    case ESP_ZB_ZCL_CLUSTER_ID_ILLUMINANCE_MEASUREMENT: {
        uint16_t raw = *(uint16_t *)msg->attribute.data.value;
        snprintf(payload, sizeof(payload), "{\"raw\":%u}", raw);
        ha_mqtt_publish_zigbee_ep(addr, msg->src_endpoint, "illuminance", payload);
        break;
    }
    case ESP_ZB_ZCL_CLUSTER_ID_OCCUPANCY_SENSING: {
        uint8_t occ = *(uint8_t *)msg->attribute.data.value;
        snprintf(payload, sizeof(payload), "{\"occ\":%u}", occ);
        ha_mqtt_publish_zigbee_ep(addr, msg->src_endpoint, "occupancy", payload);
        break;
    }
    case ESP_ZB_ZCL_CLUSTER_ID_IAS_ZONE: {
        uint16_t zs = *(uint16_t *)msg->attribute.data.value;
        snprintf(payload, sizeof(payload), "{\"zone_status\":%u,\"motion\":%u}", zs, (uint8_t)(zs & 1));
        ha_mqtt_publish_zigbee_ep(addr, msg->src_endpoint, "ias_zone", payload);
        break;
    }
    case ESP_ZB_ZCL_CLUSTER_ID_POWER_CONFIG: {
        uint16_t attr_id = msg->attribute.id;
        if (attr_id == 0x0021) {
            uint8_t raw = *(uint8_t *)msg->attribute.data.value;
            snprintf(payload, sizeof(payload), "{\"pct\":%u,\"raw\":%u}", raw / 2, raw);
        } else if (attr_id == 0x0020) {
            uint8_t raw = *(uint8_t *)msg->attribute.data.value;
            snprintf(payload, sizeof(payload), "{\"voltage_mv\":%u,\"raw\":%u}", (unsigned)(raw * 100U), raw);
        } else {
            uint8_t raw = *(uint8_t *)msg->attribute.data.value;
            snprintf(payload, sizeof(payload), "{\"attr\":\"0x%04x\",\"raw\":%u}", attr_id, raw);
        }
        ha_mqtt_publish_zigbee_ep(addr, msg->src_endpoint, "battery", payload);
        break;
    }
    default: {
        snprintf(payload, sizeof(payload),
                 "{\"cluster\":\"0x%04x\",\"attr\":\"0x%04x\"}",
                 msg->cluster, msg->attribute.id);
        ha_mqtt_publish_zigbee_ep(addr, msg->src_endpoint, "raw", payload);
        break;
    }
    }
    return ESP_OK;
}

/* ── Netzwerk-Signal-Handler ────────────────────────────────────────────── */
void esp_zb_app_signal_handler(esp_zb_app_signal_t *signal_struct) {
    uint32_t *sg_p = signal_struct->p_app_signal;
    esp_err_t err  = signal_struct->esp_err_status;
    esp_zb_app_signal_type_t sig = *sg_p;

    switch (sig) {
    case ESP_ZB_ZDO_SIGNAL_SKIP_STARTUP:
        esp_zb_bdb_start_top_level_commissioning(
            ESP_ZB_BDB_MODE_INITIALIZATION);
        break;

    case ESP_ZB_BDB_SIGNAL_DEVICE_FIRST_START:
    case ESP_ZB_BDB_SIGNAL_DEVICE_REBOOT:
        if (err == ESP_OK) {
            if (esp_zb_bdb_is_factory_new()) {
                ESP_LOGI(TAG, "首次启动 – 正在组建网络");
                esp_zb_bdb_start_top_level_commissioning(
                    ESP_ZB_BDB_MODE_NETWORK_FORMATION);
            } else {
                ESP_LOGI(TAG, "重启 – 网络已恢复");
                esp_zb_bdb_start_top_level_commissioning(
                    ESP_ZB_BDB_MODE_NETWORK_STEERING);
            }
        } else {
            ESP_LOGE(TAG, "Zigbee 初始化失败：%s",
                     esp_err_to_name(err));
        }
        break;

    case ESP_ZB_BDB_SIGNAL_FORMATION:
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "网络已组建 – PAN 0x%04hx 信道 %d",
                     esp_zb_get_pan_id(), esp_zb_get_current_channel());
            esp_zb_bdb_start_top_level_commissioning(
                ESP_ZB_BDB_MODE_NETWORK_STEERING);
        } else {
            ESP_LOGW(TAG, "组网失败 – 将重试");
            esp_zb_scheduler_alarm(
                (esp_zb_callback_t)(void *)esp_zb_bdb_start_top_level_commissioning,
                ESP_ZB_BDB_MODE_NETWORK_FORMATION, 1000);
        }
        break;

    case ESP_ZB_BDB_SIGNAL_STEERING:
        if (err == ESP_OK) {
            /* 这里**不要**调 esp_zb_bdb_open_network(0)！
             * boot-steering 信号可能在「已用 BOOT 键/网页开好的配网窗口」
             * 之后才到，一刀就把窗口关了（bug：窗口开了约 1 s 就被干掉）。
             * 而且开机之后网络本来就是关着的。 */
            ESP_LOGI(TAG, "Zigbee 就绪 – 网络已恢复");
        }
        break;

    case ESP_ZB_ZDO_SIGNAL_DEVICE_ANNCE: {
        esp_zb_zdo_signal_device_annce_params_t *p =
            esp_zb_app_signal_get_params(sg_p);
        uint16_t a = p->device_short_addr;
        ESP_LOGI(TAG, "新设备：0x%04hx", a);

        xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
        /* 带新短地址重连：先把同 IEEE 的旧条目删掉 */
        for (int i = 0; i < MAX_DEVICES; i++) {
            if (s_devices[i].used && s_devices[i].short_addr != a &&
                memcmp(s_devices[i].ieee, p->ieee_addr, 8) == 0) {
                ESP_LOGI(TAG, "设备 0x%04hx → 0x%04hx（重新入网，地址变化）",
                         s_devices[i].short_addr, a);
                memset(&s_devices[i], 0, sizeof(s_devices[i]));
            }
        }
        zb_device_t *dev = find_or_add(a, p->ieee_addr);
        uint8_t dev_ieee[8] = { 0 };
        if (dev) memcpy(dev_ieee, p->ieee_addr, 8);
        xSemaphoreGive(s_dev_mutex);
        /* 新收下的、或迁移过来的地址立刻存盘，
         * 免得重启一次又把表丢了。 */
        if (dev) devtab_save();

        /* IEEE 地址转成字符串 */
        char ieee_str[24] = "?";
        if (dev) iv_ieee_str(dev_ieee, ieee_str, sizeof(ieee_str));

        /* Join 事件里带上顶层 IEEE（这样树莓派能在「重连换地址」之后
         * 把旧条目去重） */
        {
            char line[176];
            snprintf(line, sizeof(line),
                     "{\"t\":\"zigbee\",\"addr\":%u,\"sub\":\"join\","
                     "\"p\":{\"event\":\"joined\",\"addr\":\"0x%04x\",\"ieee\":\"%s\"},"
                     "\"ieee\":\"%s\"}",
                     (unsigned)a, a, ieee_str, ieee_str);
            ha_mqtt_emit_raw(line);
        }

        /* Basic 属性在 Interview 之后用真实端点去读
         * （见 iv_request_simple_desc —— 以前写死 ep1 的路径是错的） */

        /* Interview: Active-EP → Simple-Desc → Bind → Configure-Reporting */
        if (dev) interview_start(a, dev_ieee);
        break;
    }

    case ESP_ZB_NWK_SIGNAL_PERMIT_JOIN_STATUS: {
        uint8_t *sec = esp_zb_app_signal_get_params(sg_p);
        bool open = (*sec > 0);
        ha_mqtt_logf(TAG, "PERMIT_JOIN_STATUS: %s (%d s)",
                     open ? "offen" : "geschlossen", *sec);
        ha_mqtt_publish_permit_join(open, *sec);
        break;
    }

    /* 设备离网（或被删除）—— 正是这个信号让「配对成功、约 1 分钟后
     * 又变未配对」这个毛病变得可见。rejoin=1 表示：带重连的 Leave
     * （典型是换地址）。 */
    case ESP_ZB_ZDO_SIGNAL_LEAVE_INDICATION: {
        esp_zb_zdo_signal_leave_indication_params_t *p =
            esp_zb_app_signal_get_params(sg_p);
        if (p) {
            keepalive_stop();
            char ieee_str[24];
            iv_ieee_str(p->device_addr, ieee_str, sizeof(ieee_str));
            ESP_LOGW(TAG, "设备离开网络：0x%04hx (%s) rejoin=%u",
                     p->short_addr, ieee_str, (unsigned)p->rejoin);
            char line[192];
            snprintf(line, sizeof(line),
                     "{\"t\":\"zigbee\",\"addr\":%u,\"sub\":\"left\","
                     "\"p\":{\"event\":\"left\",\"addr\":\"0x%04x\",\"ieee\":\"%s\",\"rejoin\":%u},"
                     "\"ieee\":\"%s\"}",
                     (unsigned)p->short_addr, p->short_addr, ieee_str,
                     (unsigned)p->rejoin, ieee_str);
            ha_mqtt_emit_raw(line);

            /* 彻底离网（rejoin=0）：从表里和 NVS 副本里都删掉。
             * rejoin=1 时保留 —— 设备会带着新短地址回来（ANNCE 会给它迁移）。 */
            if (!p->rejoin) {
                xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
                for (int i = 0; i < MAX_DEVICES; i++) {
                    if (s_devices[i].used &&
                        memcmp(s_devices[i].ieee, p->device_addr, 8) == 0) {
                        memset(&s_devices[i], 0, sizeof(s_devices[i]));
                    }
                }
                xSemaphoreGive(s_dev_mutex);
                devtab_save();
            }
        }
        break;
    }

    /* 0x00 secured rejoin, 0x01 unsecured join, 0x02 Device left,
     * 0x03 Trust-Center rejoin */
    case ESP_ZB_ZDO_SIGNAL_DEVICE_UPDATE: {
        esp_zb_zdo_signal_device_update_params_t *p =
            esp_zb_app_signal_get_params(sg_p);
        if (p) {
            keepalive_stop();
            ESP_LOGW(TAG, "设备更新 0x%04hx status=%u",
                     p->short_addr, (unsigned)p->status);
            char line[112];
            snprintf(line, sizeof(line),
                     "{\"t\":\"zb_signal\",\"sig\":\"DEVICE_UPDATE\",\"addr\":\"0x%04x\",\"status\":%u}",
                     p->short_addr, (unsigned)p->status);
            ha_mqtt_emit_raw(line);
        }
        break;
    }

    /* Trust Center 的授权结果 —— type/status 是判断离网原因的关键值
     * （0x01=TCLK，0x02=SE-CBKE；status 0x00=ok，0x01=timeout，0x02=failed）。
     * 之后启动保活测试（12 轮 Read Attributes，每 5 s 一轮）。 */
    case ESP_ZB_ZDO_SIGNAL_DEVICE_AUTHORIZED: {
        esp_zb_zdo_signal_device_authorized_params_t *p =
            esp_zb_app_signal_get_params(sg_p);
        if (p) {
            char ieee_str[24];
            iv_ieee_str(p->long_addr, ieee_str, sizeof(ieee_str));
            char line[160];
            snprintf(line, sizeof(line),
                     "{\"t\":\"zb_signal\",\"sig\":\"DEVICE_AUTHORIZED\","
                     "\"addr\":\"0x%04x\",\"ieee\":\"%s\",\"type\":%u,\"status\":%u}",
                     p->short_addr, ieee_str,
                     (unsigned)p->authorization_type,
                     (unsigned)p->authorization_status);
            ha_mqtt_emit_raw(line);
            ESP_LOGW(TAG, "设备已授权 0x%04x type=%u status=%u",
                     p->short_addr, (unsigned)p->authorization_type,
                     (unsigned)p->authorization_status);
            if (p->authorization_status == 0 && s_iv.ep_count > 0) {
                keepalive_start(p->short_addr, s_iv.eps[0]);
            }
        }
        break;
    }

    default: {
        /* 其余信号全部按 JSON 行输出 —— 这样整条信号链
         * （包括离网前那几秒）在 add-on 日志里都能看到。
         * 只用 ESP_LOGD 是看不见的（走的是另一个 UART）。 */
        const char *name = esp_zb_zdo_signal_to_string(sig);
        char line[128];
        snprintf(line, sizeof(line),
                 "{\"t\":\"zb_signal\",\"sig\":\"%s\",\"id\":\"0x%02x\",\"err\":\"0x%x\"}",
                 name ? name : "?", (unsigned)sig, (unsigned)err);
        ha_mqtt_emit_raw(line);
        ESP_LOGD(TAG, "ZBOSS 信号：%s (0x%x) %s",
                 name ? name : "?", (unsigned)sig, esp_err_to_name(err));
        break;
    }
    }
}

/* ── 从 NVS 读保存的信道（默认 ZB_DEFAULT_CHANNEL） ────────────────────── */
static uint8_t load_channel(void) {
    uint8_t ch = ZB_DEFAULT_CHANNEL;
    nvs_handle_t h;
    if (nvs_open(ZB_NVS_NAMESPACE, NVS_READONLY, &h) == ESP_OK) {
        nvs_get_u8(h, ZB_NVS_KEY_CHANNEL, &ch);
        nvs_close(h);
    }
    if (ch < 11 || ch > 26) ch = ZB_DEFAULT_CHANNEL;
    return ch;
}

static bool aps_data_ind_cb(esp_zb_apsde_data_ind_t ind);  /* fwd decl */

/* ── Zigbee-Haupttask ───────────────────────────────────────────────────── */
static void zb_task(void *arg) {
    uint8_t ch = load_channel();
    uint8_t fallback = (ch == ZB_FALLBACK_CHANNEL) ? ZB_DEFAULT_CHANNEL
                                                    : ZB_FALLBACK_CHANNEL;
    ESP_LOGI(TAG, "Zigbee 主信道 %d，备用信道 %d", ch, fallback);

    /* 先定表的大小 —— 必须在 esp_zb_init() 之前。 */
    esp_err_t siz = esp_zb_overall_network_size_set(MAX_DEVICES);
    ESP_LOGI(TAG, "网络容量 %d 台设备 (overall_network_size_set=%s)",
             MAX_DEVICES, esp_err_to_name(siz));

    esp_zb_cfg_t cfg = {
        .esp_zb_role        = ESP_ZB_DEVICE_TYPE_COORDINATOR,
        .install_code_policy = false,
        .nwk_cfg.zczr_cfg   = { .max_children = MAX_DEVICES },
    };
    esp_zb_init(&cfg);
    esp_zb_set_tx_power(20);   /* max TX-Power: 20 dBm */

    /* 启动时**永远不要**清 NVRAM：网络密钥、PAN、信道和地址表都在里面。
     * 配对能扛过重启全靠它 —— 谁在这里写 true，每次开机都丢网。 */
    esp_zb_nvram_erase_at_start(false);

    /* 用自己的 NVS 副本把设备表补回来，这样 LQI 快照和保活
     * 在设备第一次 Announce 之前就认得它们。 */
    devtab_load();

    /* Endpoint manuell aufbauen: Basic + Identify + OTA-Server */
    esp_zb_cluster_list_t *cl = esp_zb_zcl_cluster_list_create();

    esp_zb_basic_cluster_cfg_t basic_cfg = { .zcl_version = 3, .power_source = 0x01 };
    esp_zb_cluster_list_add_basic_cluster(cl,
        esp_zb_basic_cluster_create(&basic_cfg), ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);

    esp_zb_identify_cluster_cfg_t id_cfg = { .identify_time = 0 };
    esp_zb_cluster_list_add_identify_cluster(cl,
        esp_zb_identify_cluster_create(&id_cfg), ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);

    esp_zb_cluster_list_add_ota_cluster(cl,
        zb_ota_server_cluster_create(), ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);

    /* 把 Temperature + On/Off 注册成 Client ⇒ 能收到属性上报 */
    esp_zb_temperature_meas_cluster_cfg_t temp_cfg = {
        .measured_value = (int16_t)0x8000,
        .min_value = -5000,
        .max_value =  8500,
    };
    esp_zb_cluster_list_add_temperature_meas_cluster(cl,
        esp_zb_temperature_meas_cluster_create(&temp_cfg),
        ESP_ZB_ZCL_CLUSTER_CLIENT_ROLE);

    /* Humidity Measurement (0x0405) CLIENT */
    esp_zb_humidity_meas_cluster_cfg_t hum_cfg = {
        .measured_value = 0, .min_value = 0, .max_value = 10000,
    };
    esp_zb_cluster_list_add_humidity_meas_cluster(cl,
        esp_zb_humidity_meas_cluster_create(&hum_cfg),
        ESP_ZB_ZCL_CLUSTER_CLIENT_ROLE);

    esp_zb_on_off_cluster_cfg_t on_off_cfg = { .on_off = false };
    esp_zb_cluster_list_add_on_off_cluster(cl,
        esp_zb_on_off_cluster_create(&on_off_cfg),
        ESP_ZB_ZCL_CLUSTER_CLIENT_ROLE);

    /* On/Off 再额外注册成 **SERVER**：这样已绑定开关发来的 On/Off 命令
     * （按键）才会以 ESP_ZB_CORE_SET_ATTR_VALUE_CB_ID 落到我们这里 ——
     * 跟 HA 的 On/Off Light 示例是同一条路径。
     * Client 角色继续保留，用于接收 Server 设备的属性上报。 */
    esp_zb_on_off_cluster_cfg_t on_off_srv_cfg = { .on_off = false };
    esp_zb_cluster_list_add_on_off_cluster(cl,
        esp_zb_on_off_cluster_create(&on_off_srv_cfg),
        ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);

    /* ★ 中继上报父节点用的厂商专有簇（0xFC00，与中继的发射功率簇同一个）。
     * 中继自己知道答案（zb_nwk_get_parent），每 30 s 自检成功时就把它写进
     * 属性 0x0002 —— 这样拓扑上的「中继挂在谁下面」是**设备自报**，
     * 不再靠邻居表 + BFS 推断。写进来会走 SET_ATTR_VALUE 回调。 */
    esp_zb_attribute_list_t *rep_info = esp_zb_zcl_attr_list_create(REP_CLUSTER_INFO);
    esp_zb_custom_cluster_add_custom_attr(rep_info, REP_ATTR_PARENT,
        ESP_ZB_ZCL_ATTR_TYPE_U16, ESP_ZB_ZCL_ATTR_ACCESS_READ_WRITE,
        &s_rep_parent);
    esp_zb_cluster_list_add_custom_cluster(cl, rep_info,
        ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);

    /* Illuminance (0x0400) */
    esp_zb_illuminance_meas_cluster_cfg_t illum_cfg = {
        .measured_value = 0, .min_value = 0, .max_value = 0xfffe,
    };
    esp_zb_cluster_list_add_illuminance_meas_cluster(cl,
        esp_zb_illuminance_meas_cluster_create(&illum_cfg), ESP_ZB_ZCL_CLUSTER_CLIENT_ROLE);

    /* Occupancy Sensing (0x0406) – Bewegungsmelder */
    esp_zb_occupancy_sensing_cluster_cfg_t occ_cfg = {
        .occupancy = 0, .sensor_type = 0, .sensor_type_bitmap = 1,
    };
    esp_zb_cluster_list_add_occupancy_sensing_cluster(cl,
        esp_zb_occupancy_sensing_cluster_create(&occ_cfg), ESP_ZB_ZCL_CLUSTER_CLIENT_ROLE);

    /* IAS Zone（0x0500）—— 人体/门磁/烟感 */
    esp_zb_ias_zone_cluster_cfg_t ias_cfg = {
        .zone_state = 0, .zone_type = ESP_ZB_ZCL_IAS_ZONE_ZONETYPE_MOTION, .zone_status = 0,
    };
    esp_zb_cluster_list_add_ias_zone_cluster(cl,
        esp_zb_ias_zone_cluster_create(&ias_cfg), ESP_ZB_ZCL_CLUSTER_CLIENT_ROLE);

    /* Power Configuration (0x0001) – Batterie */
    esp_zb_power_config_cluster_cfg_t pwr_cfg = { .main_voltage = 0 };
    esp_zb_cluster_list_add_power_config_cluster(cl,
        esp_zb_power_config_cluster_create(&pwr_cfg), ESP_ZB_ZCL_CLUSTER_CLIENT_ROLE);

    /* Custom Cluster 0xFF01: switch2wifi-Befehl an Client (CLIENT-Role) */
    esp_zb_attribute_list_t *sw_attr = esp_zb_zcl_attr_list_create(0xFF01);
    esp_zb_cluster_list_add_custom_cluster(cl, sw_attr, ESP_ZB_ZCL_CLUSTER_CLIENT_ROLE);

    esp_zb_ep_list_t *ep = esp_zb_ep_list_create();
    /* SDK ≥1.2.0：端点配置改成结构体，不再是零散参数 */
    esp_zb_endpoint_config_t ep_cfg = {
        .endpoint           = ZB_ENDPOINT,
        .app_profile_id     = ESP_ZB_AF_HA_PROFILE_ID,
        .app_device_id      = ESP_ZB_HA_ON_OFF_SWITCH_DEVICE_ID,
        .app_device_version = 0,
    };
    esp_zb_ep_list_add_ep(ep, cl, ep_cfg);

    /* EP 0x0B（11）—— 给 Feibit FB56/ZSW1x（3A Nue HGZB-043）准备的镜像端点：
     * 这些开关把 On/Off 属性上报发到协调器的 EP 0x0B（目标端点来自它们原先
     * 那套外来网关的配置，例如小米）。没有这个端点，ZCL 层会直接丢弃上报
     * （实测：dep=11，一个回调都没有）。
     * 重要：上报的 Frame Control 是 0x08 = direction to-client ⇒
     * ZBOSS 会去找 CLIENT 角色。所以两个角色都要注册（跟 EP1 一样）。 */
    {
        esp_zb_cluster_list_t *cl_b = esp_zb_zcl_cluster_list_create();
        esp_zb_cluster_list_add_on_off_cluster(cl_b,
            esp_zb_on_off_cluster_create(&on_off_srv_cfg),
            ESP_ZB_ZCL_CLUSTER_CLIENT_ROLE);
        esp_zb_cluster_list_add_on_off_cluster(cl_b,
            esp_zb_on_off_cluster_create(&on_off_srv_cfg),
            ESP_ZB_ZCL_CLUSTER_SERVER_ROLE);
        esp_zb_endpoint_config_t ep_b_cfg = {
            .endpoint           = 11,
            .app_profile_id     = ESP_ZB_AF_HA_PROFILE_ID,
            .app_device_id      = ESP_ZB_HA_ON_OFF_SWITCH_DEVICE_ID,
            .app_device_version = 0,
        };
        esp_zb_ep_list_add_ep(ep, cl_b, ep_b_cfg);
    }

    esp_zb_device_register(ep);

    esp_zb_core_action_handler_register(zb_action_handler);
    esp_zb_set_primary_network_channel_set(1U << ch);
    esp_zb_set_secondary_network_channel_set(1U << fallback);

    esp_zb_aps_data_indication_handler_register(aps_data_ind_cb);

    /* ★ 退网修复：TC 不强制要求终端做 TC-Link-Key 交换。
     * ZBOSS 默认要求 r21 设备 join 后完成 TCLK(授权)交换；部分 TI Z-Stack
     * 设备无法完成该交换，15 秒后 DEVICE_AUTHORIZED(type=1 R21_TCLK,
     * status=1 TIMEOUT) → 设备被踢退网。设为 false 后允许设备不带
     * TC-link-key 留在网内（APS 层用全局 link key，NWK 加密不变）。
     * 依据：espressif/esp-zigbee-sdk issue #60 + arduino-esp32 #10868
     * （Espressif 工程师确认的方案，须在 esp_zb_start 之前调用）。 */
    esp_zb_secur_link_key_exchange_required_set(false);

    ESP_ERROR_CHECK(esp_zb_start(false));

    /* ★★ 2026-10-25：从这里开始，别的任务才可以调 ZBOSS。
     * 之前不行 —— esp_zb_scheduler_alarm() 会解引用一个还不存在的队列，
     * 直接 "Load access fault"（MTVAL=0xd）。重启循环就是这么来的：
     * add-on 开机后立刻发 read_onoff，而 uart_rx 任务是跟当前这个任务
     * **并行**跑的。 */
    s_zb_ready = true;
    ESP_LOGI(TAG, "Zigbee 协议栈就绪 – 开始接收串口命令");

    esp_zb_stack_main_loop();   /* SDK ≥1.5.0: ersetzt esp_zb_main_loop_iteration() */
}

bool zb_gateway_is_ready(void) { return s_zb_ready; }

/* ── 对外接口 ───────────────────────────────────────────────────────────── */
void zb_gateway_start(void) {
    memset(s_devices, 0, sizeof(s_devices));
    s_dev_mutex = xSemaphoreCreateMutex();

    esp_zb_platform_config_t pcfg = {
        /* SDK ≥1.3.0：枚举带 ZB_ 前缀（以前叫 RADIO_MODE_NATIVE） */
        .radio_config = { .radio_mode = ZB_RADIO_MODE_NATIVE },
        .host_config  = { .host_connection_mode = ZB_HOST_CONNECTION_MODE_NONE },
    };
    ESP_ERROR_CHECK(esp_zb_platform_config(&pcfg));
    /* ★ 2026-10-25：8192 → 16384。拓扑 dump（zb_topo.c）跑在 ZBOSS 任务里，
     * 在那里要拼出最多 4 KB 的 JSON 行 + snprintf + stdio。
     * panic 稳定落在 zb_sched_loop_iteration / zb_mac_logic_iteration ——
     * 这是 ZBOSS 任务栈溢出的经典症状。拓扑 dump 时会打印 high-water mark。 */
    xTaskCreate(zb_task, "zb_main", 16384, NULL, 5, NULL);
}

void zb_gateway_permit_join(uint8_t seconds) {
    /* BOOT 键可能在协议栈启动前就被按下 ⇒ 不判就是空指针解引用 */
    if (!s_zb_ready) {
        ha_mqtt_logf(TAG, "permit_join(%u) ignoriert – Zigbee-Stack startet noch", seconds);
        return;
    }
    ha_mqtt_logf(TAG, "esp_zb_bdb_open_network(%u) called", seconds);
    esp_err_t err = esp_zb_bdb_open_network(seconds);
    ha_mqtt_logf(TAG, "esp_zb_bdb_open_network(%u) = 0x%x", seconds, err);
}

void zb_gateway_devices_json(char *buf, size_t len) {
    size_t pos = 0;
    pos += snprintf(buf + pos, len - pos, "[");
    xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
    bool first = true;
    for (int i = 0; i < MAX_DEVICES; i++) {
        if (!s_devices[i].used) continue;
        const uint8_t *e = s_devices[i].ieee;
        pos += snprintf(buf + pos, len - pos,
            "%s{\"addr\":\"0x%04x\",\"ieee\":\"%02x:%02x:%02x:%02x:%02x:%02x:%02x:%02x\"}",
            first ? "" : ",",
            s_devices[i].short_addr,
            e[7], e[6], e[5], e[4], e[3], e[2], e[1], e[0]);
        first = false;
    }
    xSemaphoreGive(s_dev_mutex);
    snprintf(buf + pos, len - pos, "]");
}

void zb_gateway_set_channel(uint8_t ch) {
    if (ch < 11 || ch > 26) {
        ESP_LOGE(TAG, "无效信道：%d（允许 11-26）", ch);
        return;
    }

    /* Kanal in eigenem NVS-Namespace speichern */
    nvs_handle_t h;
    if (nvs_open(ZB_NVS_NAMESPACE, NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_u8(h, ZB_NVS_KEY_CHANNEL, ch);
        nvs_commit(h);
        nvs_close(h);
        ESP_LOGI(TAG, "信道 %d 已写入 NVS", ch);
    } else {
        ESP_LOGE(TAG, "NVS 写入失败");
        return;
    }

    /* 删掉 Zigbee FAT 存储 ⇒ 下次启动重建新网络 */
    const esp_partition_t *part = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, "zb_storage");
    if (part) {
        esp_err_t err = esp_partition_erase_range(part, 0, part->size);
        ESP_LOGI(TAG, "zb_storage 已擦除：%s", esp_err_to_name(err));
    } else {
        ESP_LOGW(TAG, "未找到 zb_storage 分区");
    }

    ESP_LOGI(TAG, "即将重启以切换到信道 %d…", ch);
    vTaskDelay(pdMS_TO_TICKS(200));
    esp_restart();
}


/* ── APS-Data-Indication: LQI je Device tracken ────────────────────────── */
static bool aps_data_ind_cb(esp_zb_apsde_data_ind_t ind) {
    int8_t rssi = esp_ieee802154_get_recent_rssi();
    xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
    zb_device_t *d = find_or_add(ind.src_short_addr, NULL);
    if (d) {
        /* ★ LQI 只在**有效**时覆盖。802.15.4 的 LQI 有效范围是 1..255，
         * 0 表示"这一帧没带有效 LQI"（弱链路 / 某些帧类型会这样）。
         * 无条件赋值会把上一次的好值抹成 0 ⇒ 界面显示"信号 0"，用户以为
         * 显示坏了（2026-10-26 中继在弱覆盖点上长期 LQI 0 就是因为这个）。
         * 保留上一次的有效值，配合 JSON 里的 lqia（多久前测的）就既准确
         * 又不骗人。 */
        if (ind.lqi != 0) {
            d->lqi    = (uint8_t)ind.lqi;
            d->lqi_ms = (uint32_t)(esp_timer_get_time() / 1000ULL);
        }
        /* RSSI 同理：esp_ieee802154_get_recent_rssi() 是"radio 最近一帧"，
         * 多设备并发上报时会串台（实测两台设备同时报 -81）。0 = 无效。 */
        if (rssi != 0) {
            d->rssi   = rssi;
            d->lqi_ms = (uint32_t)(esp_timer_get_time() / 1000ULL);
            s_last_rssi = rssi;
        }
    }
    xSemaphoreGive(s_dev_mutex);

    /* 记下最后一帧的源地址/端点 ——
     * SET_ATTR_VALUE 消息（按键命令）里不带源地址。 */
    s_last_aps.addr    = ind.src_short_addr;
    s_last_aps.ep      = ind.src_endpoint;
    s_last_aps.cluster = ind.cluster_id;

    /* ★ OTA: 客户端每次要块时会在 Image Block Request 里带上 fileOffset。
     * 这是**唯一**能知道客户端进度的途径 —— SDK 的 next_data 回调根本不
     * 给偏移（`index` 是镜像表序号，恒 0）。把偏移转给 OTA 服务端，
     * 它就能在「协调器中途重启 / 块重传」时校正自己的读指针。
     * 报文实测布局（2026-10-26，ASDU 27 B）：
     *   [0]=FC 01  [1]=TSN  [2]=Cmd 03  [3]=03  [4..5]=Mfr 1b13
     *   [6..7]=ImageType  [8..11]=FileVersion  [12..15]=FileOffset(LE)
     *   [16]=MaxDataSize  ...
     * 注：hex 里多出来的 [3] 是这一版协商报文的既有字节，别删 —— 删掉后
     * 后面的 mfr/type/version 全部错位（已用三帧交叉验证）。 */
    if (ind.cluster_id == 0x0019 && ind.asdu && ind.asdu_length >= 16 &&
        ind.asdu[2] == 0x03 &&
        (uint16_t)(ind.asdu[4] | (ind.asdu[5] << 8)) == 0x131b) {
        uint32_t foff = (uint32_t)ind.asdu[12]
                      | ((uint32_t)ind.asdu[13] << 8)
                      | ((uint32_t)ind.asdu[14] << 16)
                      | ((uint32_t)ind.asdu[15] << 24);
        zb_ota_server_note_offset(foff);
    }

    /* Diagnose: jeden eingehenden APS-Frame roh mitschneiden.
     * ASDU = ZCL 帧（Frame-Control、TSN、Command-ID……）—— 一眼就能看出
     * 按键命令到底有没有到、以什么形式到。 */
    if (ind.src_short_addr != 0x0000 && ind.asdu && ind.asdu_length > 0) {
        char hex[64];
        size_t hx = 0;
        uint32_t n = ind.asdu_length < 24u ? ind.asdu_length : 24u;
        for (uint32_t i = 0; i < n && hx + 2 < sizeof(hex); i++) {
            hx += (size_t)snprintf(hex + hx, sizeof(hex) - hx, "%02x", ind.asdu[i]);
        }
        char line[192];
        snprintf(line, sizeof(line),
                 "{\"t\":\"aps\",\"src\":\"0x%04x\",\"sep\":%u,\"dep\":%u,"
                 "\"cl\":\"0x%04x\",\"sec\":%u,\"len\":%u,\"hex\":\"%s\"}",
                 ind.src_short_addr, (unsigned)ind.src_endpoint,
                 (unsigned)ind.dst_endpoint, ind.cluster_id,
                 (unsigned)ind.security_status, (unsigned)ind.asdu_length, hex);
        ha_mqtt_emit_raw(line);
    }

    /* 还没 Interview 过 ⇒ 补做一次（这台设备在刷机前就配好了，
     * 不会重新来登记）。 */
    if (d && !d->interviewed && ind.src_short_addr != 0x0000) {
        bool have_ieee = false;
        for (int i = 0; i < 8; i++) {
            if (d->ieee[i]) { have_ieee = true; break; }
        }
        if (have_ieee) {
            uint8_t ieee[8];
            memcpy(ieee, d->ieee, 8);
            interview_start(ind.src_short_addr, ieee);
        } else if (xTaskGetTickCount() - s_ieee_last_tick > pdMS_TO_TICKS(30000)) {
            /* IEEE 未知（安静重连）⇒ 用 ZDO 事后补 */
            s_ieee_last_tick = xTaskGetTickCount();
            request_ieee(ind.src_short_addr);
        }
    }
    return false;   /* nicht konsumiert */
}

/* LQI-JSON: [{"addr":"0x0684","lqi":220,"rssi":-70,"lqia":3},...]
 *
 * lqia = 这个 lqi/rssi 是**多少秒前**测到的（0 = 本轮刚更新）。
 * 为什么需要：LQI 现在只在有效时才覆盖（见 aps_data_ind_cb），所以一台
 * 静默设备的 lqi 会一直保留旧值 —— 不带年龄就会把陈旧数据当成实时值。 */
void zb_gateway_lqi_json(char *buf, size_t len) {
    xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
    char *p = buf;
    size_t r = len;
    int n = snprintf(p, r, "[");
    if (n < 0) n = 0;
    if ((size_t)n < r) { p += n; r -= (size_t)n; }
    else               { p += r - 1; r = 1; }
    bool first = true;
    uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000ULL);
    for (int i = 0; i < MAX_DEVICES && r > 4; i++) {
        if (!s_devices[i].used) continue;
        /* 还没测到过（lqi_ms == 0）→ 用一个大数表示"从未"
         * （4294967295 秒 ≈ 136 年，前端显示成"—"）。 */
        uint32_t age = (s_devices[i].lqi_ms == 0)
                     ? 0xFFFFFFFFu
                     : (now_ms - s_devices[i].lqi_ms) / 1000u;
        n = snprintf(p, r, "%s{\"addr\":\"0x%04x\",\"lqi\":%u,\"rssi\":%d,\"lqia\":%lu}",
                     first ? "" : ",",
                     s_devices[i].short_addr,
                     (unsigned)s_devices[i].lqi,
                     (int)s_devices[i].rssi,
                     (unsigned long)age);
        if (n < 0) break;
        if ((size_t)n < r) { p += n; r -= (size_t)n; }
        else               { r = 1; break; }   /* 缓冲满 —— 剩下的丢掉 */
        first = false;
    }
    snprintf(p, r, "]");
    xSemaphoreGive(s_dev_mutex);
}

int8_t zb_gateway_get_last_rssi(void) { return s_last_rssi; }

/* 把所有已知短地址（不含 0x0000）拷进 out[]。
 * 给拓扑用：只有跟我们讲过话的设备才有可能回 Mgmt_Lqi_rsp ——
 * 邻居表里的死地址只会让整轮采集白白多拖几秒。返回个数。 */
uint8_t zb_gateway_known_addrs(uint16_t *out, uint8_t max) {
    if (!out || !max) return 0;
    uint8_t n = 0;
    xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
    for (int i = 0; i < MAX_DEVICES && n < max; i++) {
        if (!s_devices[i].used) continue;
        if (s_devices[i].short_addr == 0x0000) continue;
        out[n++] = s_devices[i].short_addr;
    }
    xSemaphoreGive(s_dev_mutex);
    return n;
}

/* 按需取一次 LQI 快照。
 *
 * 心跳里带的是同一份列表，但 60 s 才有一次。比较发射功率时这太慢了：
 * 那个值只对**最后一个**收到的帧有效，可能已经是几分钟前的。
 * 这里刻意带上时间戳（uptime_ms），好区分「3 分钟前的状态」和「刚测的」。
 *
 * 注意 —— 两个缓冲**必须**是静态的：
 * 这个函数跑在 uart_rx 任务里，而它只有 2048 B 栈
 * （ha_mqtt.c: xTaskCreate(rx_task, "uart_rx", 2048, ...)）。一个局部
 * char[2048+128] 光是它本身就超了 —— 命令第一次调用就崩。
 * heartbeat 任务负担得起（8192 B），这里不行。
 *
 * 尺寸：MAX_DEVICES 台 × 每台最多约 53 字符（"addr","lqi","rssi","lqia"
 * 加逗号），每项按 64 取整，再加包头。 */
#define LQI_JSON_MAX  (MAX_DEVICES * 64 + 128)
#define LQI_LINE_MAX  (LQI_JSON_MAX + 64)
void zb_gateway_report_lqi(void) {
    static char buf[LQI_JSON_MAX];
    static char line[LQI_LINE_MAX];
    zb_gateway_lqi_json(buf, sizeof(buf));
    uint32_t ms = (uint32_t)(esp_timer_get_time() / 1000ULL);
    ESP_LOGI(TAG, "LQI 快照已生成（%u B，%llu ms）",
             (unsigned)strlen(buf), (unsigned long long)ms);
    snprintf(line, sizeof(line), "{\"t\":\"lqi\",\"ms\":%lu,\"dev\":%s}",
             (unsigned long)ms, buf);
    ha_mqtt_emit_raw(line);
}

/* ── Sendeleistung ──────────────────────────────────────────────────────── */
/* 设定值和实际生效值不是一回事：esp_zb_set_tx_power() 内部会把值
 * 夹到 PHY 允许的范围内。所以设完之后要用 esp_ieee802154_get_txpower()
 * 读回来一起报 —— 否则比较功率时量的是设定值，不是发射功率。 */
void zb_gateway_set_tx_power(int8_t dbm) {
    esp_zb_set_tx_power(dbm);
    int8_t eff = esp_ieee802154_get_txpower();
    char line[96];
    snprintf(line, sizeof(line),
             "{\"t\":\"txpower\",\"want\":%d,\"eff\":%d}", (int)dbm, (int)eff);
    ha_mqtt_emit_raw(line);
}

int8_t zb_gateway_get_tx_power(void) {
    return esp_ieee802154_get_txpower();
}

void zb_gateway_list_devices(void) {
    xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
    ESP_LOGI(TAG, "已配对设备：");
    for (int i = 0; i < MAX_DEVICES; i++) {
        if (s_devices[i].used) {
            ESP_LOGI(TAG, "  [%d] 0x%04x", i, s_devices[i].short_addr);
        }
    }
    xSemaphoreGive(s_dev_mutex);
}

/* ── 彻底删除设备（手动从列表里移除） ──────────────────────────────────── */
/* 场景：设备搬到别的网络去了（或者坏了、拆了）。
 *
 * 为什么非得在这儿删：这张设备表是 add-on 构建设备列表的**唯一**来源 ——
 * 每次心跳（60 s）都会把 s_devices 当作 LQI 数组发下去，而 `t == "lqi"`
 * 会据此把缺的条目重新补上。所以在网页上"本地删除"只顶 60 秒：卡片会回来，
 * 因为是**我们**还在上报那个死地址。要删就得在这里删，不在 add-on 那边。
 *
 * ★ 边界：ZBOSS 自己的邻居表/路由表**不会**被这个调用清掉 ——
 *   esp-zigbee-lib 里没有这个 API（没有 bdb_dev_remove）。
 *   死邻居会在链路状态老化之后自己掉出去。喂给 HA 和网页的那个条目是我们
 *   自己的，那个必须干净。
 *
 * 顺序：先 RAM + NVS（确定性的，不依赖无线电），再在后台发 Mgmt_Leave_req。
 * 这个 Leave 只是对「还挂在我们网里」的设备讲个客气；已经配到别处的设备
 * 永远不会回它 —— 所以删除绝不能依赖它。 */
static uint16_t s_rm_addr     = 0;
static uint8_t  s_rm_ieee[8]  = {0};
static bool     s_rm_has_ieee = false;

static void rm_leave_ack_cb(esp_zb_zdp_status_t status, void *ctx) {
    ESP_LOGW(TAG, "Mgmt_Leave 0x%04x: status=0x%02x%s",
             s_rm_addr, (unsigned)status,
             (status == ESP_ZB_ZDP_STATUS_SUCCESS) ? "" : " (keine Antwort)");
}

static void rm_leave_task(void *arg) {
    /* 这是**外来任务** ⇒ 必须持 ZBOSS 锁，而且只包住请求本身：
     * 一台已经不在的设备，会让回调直到 ZDO 超时（好几秒）才触发。 */
    if (s_rm_has_ieee && s_zb_ready && esp_zb_lock_acquire(portMAX_DELAY)) {
        esp_zb_zdo_mgmt_leave_req_param_t req;
        memset(&req, 0, sizeof(req));
        memcpy(req.device_address, s_rm_ieee, sizeof(req.device_address));
        req.dst_nwk_addr    = s_rm_addr;
        req.rejoin          = 0;   /* 彻底出去，不许重连 */
        req.remove_children = 0;
        esp_zb_zdo_device_leave_req(&req, rm_leave_ack_cb, NULL);
        esp_zb_lock_release();
    }
    vTaskDelete(NULL);
}

void zb_gateway_remove_device(uint16_t addr) {
    if (addr == 0x0000 || addr >= 0xFFF8) return;   /* Koordinator / Broadcast */

    s_rm_addr     = addr;
    s_rm_has_ieee = false;
    memset(s_rm_ieee, 0, sizeof(s_rm_ieee));

    bool found = false;
    xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
    for (int i = 0; i < MAX_DEVICES; i++) {
        if (!s_devices[i].used || s_devices[i].short_addr != addr) continue;
        found = true;
        if (s_devices[i].ieee[0]) {
            memcpy(s_rm_ieee, s_devices[i].ieee, sizeof(s_rm_ieee));
            s_rm_has_ieee = true;
        }
        memset(&s_devices[i], 0, sizeof(s_devices[i]));
    }
    xSemaphoreGive(s_dev_mutex);
    devtab_save();

    char ieee_str[24] = "unbekannt";
    if (s_rm_has_ieee) iv_ieee_str(s_rm_ieee, ieee_str, sizeof(ieee_str));

    char line[160];
    snprintf(line, sizeof(line),
             "{\"t\":\"dev_removed\",\"addr\":\"0x%04x\",\"ieee\":\"%s\","
             "\"found\":%s,\"leave\":%s}",
             addr, ieee_str,
             found ? "true" : "false",
             s_rm_has_ieee ? "true" : "false");
    ha_mqtt_emit_raw(line);
    ESP_LOGW(TAG, "设备 0x%04x 已移除（原在表中：%s，IEEE：%s）",
             addr, found ? "ja" : "nein", ieee_str);

    xTaskCreate(rm_leave_task, "dev_leave", 3072, NULL, 2, NULL);
}

/* ── Channel-Scan (Kanal 11-26, ~4s je Kanal) ──────────────────────────── */
/* forward decl: library exports zdo variant */
void esp_zb_zdo_active_scan_request(uint32_t channel_mask, uint8_t scan_duration, esp_zb_zdo_scan_complete_callback_t user_cb);

#define SCAN_CH_FIRST   11
#define SCAN_CH_LAST    26
#define SCAN_DURATION   7     /* 2^(7+1)-1 * 15.36 ms ≈ 3.9 s per channel */

static SemaphoreHandle_t s_scan_sem        = NULL;
static uint8_t           s_scan_cur_ch     = 0;
static uint8_t           s_scan_net_count  = 0;
static char              s_scan_net_buf[512];
static TaskHandle_t      s_scan_task_h     = NULL;

static void scan_cb(esp_zb_zdp_status_t status, uint8_t count,
                    esp_zb_network_descriptor_t *nwk) {
    /* Gefundene Netzwerke als JSON-Array bauen */
    char *p = s_scan_net_buf;
    int   r = (int)sizeof(s_scan_net_buf);
    int   n = snprintf(p, r, "[");
    p += n; r -= n;
    for (uint8_t i = 0; i < count && r > 4; i++) {
        n = snprintf(p, r, "%s{\"pan\":\"0x%04x\",\"open\":%s}",
                     i ? "," : "",
                     nwk[i].short_pan_id,
                     nwk[i].permit_joining ? "true" : "false");
        p += n; r -= n;
    }
    snprintf(p, r, "]");
    s_scan_net_count = count;
    xSemaphoreGive(s_scan_sem);
}

static void scan_task(void *arg) {
    char line[600];
    for (uint8_t ch = SCAN_CH_FIRST; ch <= SCAN_CH_LAST; ch++) {
        s_scan_cur_ch    = ch;
        s_scan_net_count = 0;
        strcpy(s_scan_net_buf, "[]");

        snprintf(line, sizeof(line),
                 "{\"t\":\"scan\",\"state\":\"scanning\",\"ch\":%d}", ch);
        ha_mqtt_emit_raw(line);

        /* ★ scan_task 是**外来任务** ⇒ 必须持 ZBOSS 锁
         * （esp_zigbee_core.h / esp_zb_lock_acquire）。但只包住请求本身 ——
         * 等回调（最长 12 s）不能把 ZBOSS 任务堵住。 */
        if (s_zb_ready && esp_zb_lock_acquire(portMAX_DELAY)) {
            esp_zb_zdo_active_scan_request(1U << ch, SCAN_DURATION, scan_cb);
            esp_zb_lock_release();
        }

        /* Auf Callback warten (Timeout 12 s) */
        xSemaphoreTake(s_scan_sem, pdMS_TO_TICKS(12000));

        snprintf(line, sizeof(line),
                 "{\"t\":\"scan\",\"ch\":%d,\"count\":%d,\"nets\":%s}",
                 ch, s_scan_net_count, s_scan_net_buf);
        ha_mqtt_emit_raw(line);

        vTaskDelay(pdMS_TO_TICKS(300));
    }
    ha_mqtt_emit_raw("{\"t\":\"scan\",\"state\":\"done\"}");
    s_scan_task_h = NULL;
    vTaskDelete(NULL);
}

void zb_gateway_scan_channels(void) {
    if (s_scan_task_h) {
        ha_mqtt_emit_raw("{\"t\":\"scan\",\"state\":\"busy\"}");
        return;
    }
    if (!s_scan_sem) s_scan_sem = xSemaphoreCreateBinary();
    xTaskCreate(scan_task, "chan_scan", 4096, NULL, 2, &s_scan_task_h);
}

/* ── HA/树莓派发来的设备控制（给某个端点发 On/Off） ─────────────────────── */
void zb_gateway_probe_basic(uint16_t addr, uint8_t ep) {
    if (addr == 0x0000 || addr == 0xFFFF) return;
    s_basic_pending_addr = addr;
    s_basic_pending_ep   = ep;
    esp_zb_scheduler_alarm(read_basic_cb, 0, 100);
}

/* 重要：esp_zb_zcl_*_cmd_req() 只能在 ZBOSS 任务上下文里跑。
 * UART-RX 是另一个任务 —— 从那里直接调会被协议栈**静默丢弃**
 * （不报错，也没有帧发出去）。所以请求要走 ZBOSS 调度器排队。 */
static uint16_t s_dev_cmd_addr = 0xFFFF;
static uint8_t  s_dev_cmd_ep   = 0;
static uint8_t  s_dev_cmd_id   = 0;

static void dev_cmd_send(uint8_t param) {
    (void)param;
    if (s_dev_cmd_addr == 0xFFFF) return;

    esp_zb_zcl_on_off_cmd_t req = {
        .zcl_basic_cmd = {
            .dst_addr_u   = { .addr_short = s_dev_cmd_addr },
            .dst_endpoint = s_dev_cmd_ep,
            .src_endpoint = ZB_ENDPOINT,
        },
        .address_mode  = ESP_ZB_APS_ADDR_MODE_16_ENDP_PRESENT,
        .on_off_cmd_id = s_dev_cmd_id,      /* 0=off 1=on 2=toggle */
    };
    uint8_t tsn = esp_zb_zcl_on_off_cmd_req(&req);

    char line[112];
    snprintf(line, sizeof(line),
             "{\"t\":\"dev_cmd\",\"addr\":\"0x%04x\",\"ep\":%u,\"cmd\":%u,\"tsn\":%u}",
             s_dev_cmd_addr, (unsigned)s_dev_cmd_ep, (unsigned)s_dev_cmd_id,
             (unsigned)tsn);
    ha_mqtt_emit_raw(line);
    ESP_LOGI(TAG, "dev_cmd → 0x%04x ep%u cmd=%u (tsn %u)",
             s_dev_cmd_addr, (unsigned)s_dev_cmd_ep, (unsigned)s_dev_cmd_id,
             (unsigned)tsn);

    s_dev_cmd_addr = 0xFFFF;
}

void zb_gateway_dev_cmd(uint16_t addr, uint8_t ep, uint8_t on_off_cmd) {
    if (addr == 0x0000 || addr == 0xFFFF || ep == 0) return;
    if (on_off_cmd > 2) on_off_cmd = 2;
    s_dev_cmd_addr = addr;
    s_dev_cmd_ep   = ep;
    s_dev_cmd_id   = on_off_cmd;
    esp_zb_scheduler_alarm(dev_cmd_send, 0, 10);
}

/* ── 中继发射功率（写厂商专有簇 0xFC00 / 属性 0x0001） ─────────────────────
 * 中继固件把这个属性实现成「写入即生效 + 落 NVS」，所以改功率不再需要
 * 重新刷固件。属性类型 S8（dBm，-24…20）。簇号/属性号的定义见文件头部。
 * 同样受「esp_zb_zcl_*_cmd_req 只能在 ZBOSS 任务里调」的约束，走 scheduler。 */

static uint16_t s_wr_addr = 0xFFFF;
static uint8_t  s_wr_ep   = 0;
static int8_t   s_wr_val  = 0;   /* 属性值的存储，必须活到命令发出去 */

static void tx_power_send(uint8_t param) {
    (void)param;
    if (s_wr_addr == 0xFFFF) return;

    esp_zb_zcl_attribute_t attr = {
        .id   = REP_ATTR_TX_POWER,
        .data = {
            .type  = ESP_ZB_ZCL_ATTR_TYPE_S8,
            .size  = sizeof(int8_t),
            .value = &s_wr_val,
        },
    };
    esp_zb_zcl_write_attr_cmd_t req = {
        .zcl_basic_cmd = {
            .dst_addr_u   = { .addr_short = s_wr_addr },
            .dst_endpoint = s_wr_ep,
            .src_endpoint = ZB_ENDPOINT,
        },
        .address_mode = ESP_ZB_APS_ADDR_MODE_16_ENDP_PRESENT,
        .clusterID    = REP_CLUSTER_INFO,
        .manuf_specific = 0,
        .direction      = ESP_ZB_ZCL_CMD_DIRECTION_TO_SRV,
        .dis_default_resp = 0,
        .manuf_code   = 0,
        .attr_number  = 1,
        .attr_field   = &attr,
    };
    uint8_t tsn = esp_zb_zcl_write_attr_cmd_req(&req);

    char line[128];
    snprintf(line, sizeof(line),
             "{\"t\":\"wr_power\",\"addr\":\"0x%04x\",\"ep\":%u,\"dbm\":%d,\"tsn\":%u}",
             s_wr_addr, (unsigned)s_wr_ep, (int)s_wr_val, (unsigned)tsn);
    ha_mqtt_emit_raw(line);
    ESP_LOGI(TAG, "write 0xFC00/0x0001 → 0x%04x ep%u = %d dBm (tsn %u)",
             s_wr_addr, (unsigned)s_wr_ep, (int)s_wr_val, (unsigned)tsn);

    s_wr_addr = 0xFFFF;
}

void zb_gateway_write_tx_power(uint16_t addr, uint8_t ep, int8_t dbm) {
    if (addr == 0x0000 || addr == 0xFFFF || ep == 0) return;
    if (dbm > 20)  dbm = 20;
    if (dbm < -24) dbm = -24;
    s_wr_addr = addr;
    s_wr_ep   = ep;
    s_wr_val  = dbm;
    esp_zb_scheduler_alarm(tx_power_send, 0, 10);
}


/* ── Node Descriptor（ZDO 0x0002）：路由器（ZR）还是终端（ZED）？ ──────────
 * 只有路由器才转发别人的流量 —— 一个挂在市电上却按 ZED 实现的墙壁开关，
 * 当不了中继。MAC capability 标志位是唯一靠得住的判据：
 *   bit1 = 1 → FFD / 路由器，bit2 = 1 → 市电供电，bit3 = 1 → 常开接收。 */
static uint16_t s_nd_addr = 0xFFFF;

static void node_desc_cb(esp_zb_zdp_status_t status, uint16_t addr,
                         esp_zb_af_node_desc_t *nd, void *user_ctx) {
    (void)user_ctx;
    char line[192];
    if (status != ESP_ZB_ZDP_STATUS_SUCCESS || nd == NULL) {
        snprintf(line, sizeof(line),
                 "{\"t\":\"node_desc\",\"addr\":\"0x%04x\",\"error\":\"0x%02x\"}",
                 addr, (unsigned)status);
        ha_mqtt_emit_raw(line);
        return;
    }
    uint8_t cap     = nd->mac_capability_flags;
    bool    router  = (cap & 0x02) != 0;
    bool    mains   = (cap & 0x04) != 0;
    bool    rx_idle = (cap & 0x08) != 0;
    snprintf(line, sizeof(line),
             "{\"t\":\"node_desc\",\"addr\":\"0x%04x\",\"cap\":%u,"
             "\"role\":\"%s\",\"power\":\"%s\",\"rx_idle\":%s}",
             addr, (unsigned)cap,
             router  ? "router"     : "end_device",
             mains   ? "mains"      : "battery",
             rx_idle ? "true"       : "false");
    ha_mqtt_emit_raw(line);
}

static void node_desc_send(uint8_t param) {
    (void)param;
    if (s_nd_addr == 0xFFFF) return;
    esp_zb_zdo_node_desc_req_param_t req = { .dst_nwk_addr = s_nd_addr };
    esp_zb_zdo_node_desc_req(&req, node_desc_cb, NULL);
    s_nd_addr = 0xFFFF;
}

void zb_gateway_node_desc(uint16_t addr) {
    if (addr == 0x0000 || addr == 0xFFFF) return;
    s_nd_addr = addr;
    esp_zb_scheduler_alarm(node_desc_send, 0, 10);
}


/* ── 主动回读 On/Off ─────────────────────────────────────────────────────
 * HGZB-043 收到远程命令只回一个 Default Response，**不发** Report 属性。
 * 但读是可以的：对簇 0x0006、属性 0x0000 发 Read Attributes。这样 HA 拿到的
 * 是**真实**状态，而不是网关的乐观假设（设备拒收命令时那个假设就是错的）。
 *
 * 用多个槽位，是因为一个 HA 场景很容易同时切十台设备：
 * 只留一个地址的话，后一次调用会覆盖前一次，请求就发到错误的设备上去了。
 *
 * ★ 用固定槽位替代环形缓冲（2026-10-17 那个 bug）：
 *   环形缓冲的 head/tail 两个指针跨两个任务。只要两个回调交叉一下 ——
 *   或者回调里的 esp_zb_scheduler_alarm() 失败一次 —— head 和 tail 就错开，
 *   缓冲**从此永久**被判为"非空"。之后每个请求都撞上满缓冲被丢弃：
 *   回读功能静默失效，直到 C6 重启。从外面看就是"命令没送到" ——
 *   而 MQTT 那边还一直显示着乐观的假定状态。
 *   现在每个请求各占一个槽，槽号作为参数进 alarm。没有共享指针，
 *   不会互相纠缠；丢一个 alarm 就正好少一份回执。 */
#define RD_SLOTS 8

static uint16_t s_rd_attr[1] = { ESP_ZB_ZCL_ATTR_ON_OFF_ON_OFF_ID };
static struct {
    uint16_t addr;
    uint8_t  ep;
    volatile bool busy;          /* true = Alarm laeuft noch */
} s_rd_slots[RD_SLOTS];

static void read_onoff_send(uint8_t slot) {
    if (slot >= RD_SLOTS) return;

    uint16_t addr = s_rd_slots[slot].addr;
    uint8_t  ep   = s_rd_slots[slot].ep;
    s_rd_slots[slot].busy = false;      /* Slot sofort freigeben */

    if (addr == 0xFFFF) return;

    esp_zb_zcl_read_attr_cmd_t req = {
        .zcl_basic_cmd = {
            .dst_addr_u   = { .addr_short = addr },
            .dst_endpoint = ep,
            .src_endpoint = ZB_ENDPOINT,
        },
        .address_mode = ESP_ZB_APS_ADDR_MODE_16_ENDP_PRESENT,
        .clusterID    = ESP_ZB_ZCL_CLUSTER_ID_ON_OFF,
        .attr_number  = 1,
        .attr_field   = s_rd_attr,
    };
    esp_zb_zcl_read_attr_cmd_req(&req);
}

void zb_gateway_read_onoff(uint16_t addr, uint8_t ep) {
    if (addr == 0x0000 || addr == 0xFFFF || ep == 0) return;
    if (!s_zb_ready) return;    /* 第二道保险 —— 见 main.c/on_uart_cmd */

    for (uint8_t i = 0; i < RD_SLOTS; i++) {
        if (s_rd_slots[i].busy) continue;
        s_rd_slots[i].addr = addr;
        s_rd_slots[i].ep   = ep;
        s_rd_slots[i].busy = true;
        /* 每个槽错开 45 ms：这样回执是一个个回来，
         * 而不是在回调里挤成一堆。 */
        esp_zb_scheduler_alarm(read_onoff_send, i, (uint32_t)(10 + i * 45));
        return;
    }
    ESP_LOGW(TAG, "read_onoff：%d 个槽位全被占用 – 请求已丢弃", RD_SLOTS);
}


/* ── 向所有已知设备发送 switch2wifi 命令 ────────────────────────────────── */
#define SW2WIFI_CLUSTER  0xFF01
#define SW2WIFI_CMD_ID   0x01

static void send_switch2wifi_cb(uint8_t param) {
    (void)param;
    xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
    int sent = 0;
    for (int i = 0; i < MAX_DEVICES; i++) {
        if (!s_devices[i].used) continue;
        esp_zb_zcl_custom_cluster_cmd_req_t req = {
            .zcl_basic_cmd = {
                .dst_addr_u  = { .addr_short = s_devices[i].short_addr },
                .dst_endpoint = 1,
                .src_endpoint = ZB_ENDPOINT,
            },
            .address_mode  = ESP_ZB_APS_ADDR_MODE_16_ENDP_PRESENT,
            .profile_id    = ESP_ZB_AF_HA_PROFILE_ID,
            .cluster_id    = SW2WIFI_CLUSTER,
            .custom_cmd_id = SW2WIFI_CMD_ID,
            .direction     = ESP_ZB_ZCL_CMD_DIRECTION_TO_SRV,
            .data          = { .type = ESP_ZB_ZCL_ATTR_TYPE_NULL, .size = 0, .value = NULL },
        };
        esp_zb_zcl_custom_cluster_cmd_req(&req);
        char line[80];
        snprintf(line, sizeof(line),
                 "{\"t\":\"switch2wifi_cmd\",\"dst\":\"0x%04x\"}", s_devices[i].short_addr);
        ha_mqtt_emit_raw(line);
        sent++;
    }
    xSemaphoreGive(s_dev_mutex);
    if (sent == 0)
        ha_mqtt_emit_raw("{\"t\":\"switch2wifi_cmd\",\"error\":\"no_devices\"}");
}

void zb_gateway_send_switch2wifi_all(void) {
    esp_zb_scheduler_alarm(send_switch2wifi_cb, 0, 0);
}

/* ── 向所有设备发送 set_sleep 命令 ──────────────────────────────────────── */
static uint32_t s_sleep_pending_s = 600;

static void send_set_sleep_cb(uint8_t param) {
    (void)param;
    xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
    for (int i = 0; i < MAX_DEVICES; i++) {
        if (!s_devices[i].used) continue;
        esp_zb_zcl_custom_cluster_cmd_req_t req = {
            .zcl_basic_cmd = {
                .dst_addr_u  = { .addr_short = s_devices[i].short_addr },
                .dst_endpoint = 1,
                .src_endpoint = ZB_ENDPOINT,
            },
            .address_mode  = ESP_ZB_APS_ADDR_MODE_16_ENDP_PRESENT,
            .profile_id    = ESP_ZB_AF_HA_PROFILE_ID,
            .cluster_id    = 0xFF01,
            .custom_cmd_id = 0x02,
            .direction     = ESP_ZB_ZCL_CMD_DIRECTION_TO_SRV,
            .data          = { .type = ESP_ZB_ZCL_ATTR_TYPE_U32,
                               .size = 4, .value = &s_sleep_pending_s },
        };
        esp_zb_zcl_custom_cluster_cmd_req(&req);
        char line[80];
        snprintf(line, sizeof(line),
                 "{\"t\":\"set_sleep_cmd\",\"dst\":\"0x%04x\",\"s\":%lu}",
                 s_devices[i].short_addr, (unsigned long)s_sleep_pending_s);
        ha_mqtt_emit_raw(line);
    }
    xSemaphoreGive(s_dev_mutex);
}

void zb_gateway_send_set_sleep(uint32_t seconds) {
    s_sleep_pending_s = seconds;
    esp_zb_scheduler_alarm(send_set_sleep_cb, 0, 0);
}
