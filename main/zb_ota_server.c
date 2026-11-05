/*
 * zb_ota_server.c —— Zigbee OTA 升级服务端（协调器侧）
 *
 * 镜像由树莓派经 UART 一点点喂上来：
 *   1. ota_task 给树莓派发 {"t":"ota_req","off":O,"sz":S}
 *   2. 树莓派回 {"cmd":"ota_data","off":O,"data":"HEX"}
 *   3. zb_ota_server_feed_data() 把 HEX 解码进本地窗口
 *   4. ota_next_data_cb()（ZBOSS 任务里）从窗口取数据喂给客户端
 *
 * ══ 本文件记录的两个坑（2026-10-26）════════════════════════════════════════
 *
 * ★★ 1) 回调为什么不再无限阻塞
 *    回调跑在 ZBOSS 任务里。数据提供方是 rx_task → on_uart_cmd() → zb_lock()。
 *    如果回调等一个只有 rx_task 才能给、而 rx_task 又在等 ZBOSS 任务手里的锁的
 *    信号量，整个流程会卡死 OTA_WAIT_MS 那么久。
 *    所以：on_uart_cmd 处理 "ota_data" 时**不再持锁**（见 main.c），
 *    回调则优先直接从窗口里拿数据。
 *
 * ★★ 2) 为什么必须先预填再 notify
 *    客户端在收到 Image Notify 后约 0.5–1 s 就发第一个 Image Block Request。
 *    这时窗口要是还空的，这一块就交不出来 —— 而回调返回错误码会让协议栈
 *    **直接中止整个会话**（日志里 `ota_srv_status aborted`，之后只剩 45 s 重试，
 *    重试一上来又立刻中止）。
 *    所以：先取够 OTA_PRELOAD 字节，再调 notify_req()。
 *
 * ★★ 3) `index` 是镜像序号，**不是**文件偏移
 *    2026-10-26 实测：n=1..8 次调用 idx 恒为 0，`size` 从第 2 次起由 8 跳到 64。
 *    协议栈要的就是 "the NEXT data" —— 严格顺序供给（靠 s_cur_off）。
 */
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <stdarg.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_zigbee_core.h"
#include "esp_zigbee_ota.h"
#include "esp_zigbee_attribute.h"
#include "zcl/esp_zigbee_zcl_ota.h"
#include "ha_mqtt.h"
#include "zb_ota_server.h"

#define TAG            "ota_srv"
#define OTA_ENDPOINT   1
#define OTA_MFR_CODE   0x131B
#define OTA_IMAGE_TYPE OTA_IMAGE_TYPE_ENDDEVICE   /* 默认值；可用 ota_start 覆盖 */

#define OTA_WIN        8192   /* 本地窗口大小（字节） */
#define OTA_FETCH      1024   /* 每次 ota_req 向树莓派取多少字节（HEX 即 2048 字符） */
#define OTA_AHEAD      1024   /* 窗口剩余量小于此值就补货 */
#define OTA_PRELOAD    2048   /* notify 之前窗口里至少要积这么多 */
#define OTA_MAX_BLOCK   232   /* 客户端单次可请求的最大块长 */
#define OTA_WAIT_MS    6000   /* 等树莓派回一块的超时 */
#define OTA_MISS_MS    2500   /* 回调里的应急等待（仅缺口时用） */
#define OTA_DBG_MAX       8

/* ── Zustand ────────────────────────────────────────────────────────────── */
static SemaphoreHandle_t s_data_sem  = NULL;
static TaskHandle_t      s_task      = NULL;
static uint32_t s_last_stat_ms = 0, s_last_cb = 0;

static uint8_t  s_win[OTA_WIN];
static uint32_t s_win_off  = 0;
static uint32_t s_win_len  = 0;

static volatile bool     s_active   = false;
static volatile bool     s_preload  = false;   /* notify 还没发出去 */
static volatile uint32_t s_img_size = 0;
static volatile uint32_t s_cur_off  = 0;
static volatile bool     s_eof      = false;

static uint32_t s_ver = 1;
static uint16_t s_type = OTA_IMAGE_TYPE;

static volatile uint32_t s_rx_len = 0;
static volatile uint32_t s_rx_off = 0;

/* ★★ 客户端在 Image Block Request 里报的 fileOffset（由 aps_data_ind_cb 填）。
 * 这是唯一能知道「客户端现在要哪一段」的途径 —— 回调签名
 * esp_zb_ota_next_data_callback_t(msg, index, size, **data) 里**没有**偏移，
 * `index` 是镜像表序号（恒 0）。SDK 的语义是 give me the NEXT data，
 * 于是它默认「服务端游标和客户端永远同步」。
 *
 * 一旦这个假设被打破（协调器在传输中途重启 → 服务端游标归零，而客户端
 * 还记着它自己的偏移），服务端就会把文件开头的字节当成「下一块」发出去，
 * 客户端照单写进自己的 flash ⇒ **镜像中段被污染，整包作废**。
 * 实测 2026-10-26 11:04：协调器刷机后客户端在 50240，服务端在 20288，
 * 差 30 KB 全是错位数据（当晚「传了 99.4% 但版本没生效」也是这个病）。 */
static volatile uint32_t s_cli_off = 0;
static volatile bool     s_cli_ok  = false;
static volatile uint32_t s_resync  = 0;
/* ★ 交付校验：对「真正发给协议栈的字节」按顺序算 FNV-1a。
 * add-on 侧对「真正发出去的字节」算同一个哈希，两边一比就能证明
 * 中继收到的数据是否逐字节正确 —— 不用连接中继的串口。 */
static volatile uint32_t s_served = 0;
static volatile uint32_t s_hash   = 2166136261u;
static volatile uint32_t s_cb_n = 0, s_hit = 0, s_miss = 0, s_to = 0, s_rx_n = 0, s_bad = 0;

static void ota_dbg(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void ota_dbg(const char *fmt, ...)
{
    char body[96];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(body, sizeof(body), fmt, ap);
    va_end(ap);
    char line[128];
    snprintf(line, sizeof(line), "{\"t\":\"ota_dbg\",%s}", body);
    ha_mqtt_emit_raw(line);
}

static inline bool win_covers(uint32_t off, uint32_t n)
{
    return off >= s_win_off && off + n <= s_win_off + s_win_len;
}

/* ── APS 指示回调报上来的客户端偏移 ────────────────────────────────────────
 * 跑在 ZBOSS 任务里（回调内可以不加锁直接用 SDK API），
 * 这里只写几个 volatile 变量 —— 不加锁、不阻塞。 */
void zb_ota_server_note_offset(uint32_t off)
{
    if (!s_active) return;
    if (off > s_img_size) return;          /* Unsinn / fremdes Image */
    s_cli_off = off;
    s_cli_ok  = true;
}

/* ── 窗口：腾出空间（只允许在 ota_task 里调） ───────────────────────────── */
static void win_compact(void)
{
    if (OTA_WIN - s_win_len >= OTA_FETCH) return;
    uint32_t keep = s_cur_off;
    if (keep <= s_win_off || keep >= s_win_off + s_win_len) {
        s_win_off = keep;
        s_win_len = 0;
        return;
    }
    uint32_t drop = keep - s_win_off;
    memmove(s_win, s_win + drop, s_win_len - drop);
    s_win_off += drop;
    s_win_len -= drop;
}

/* ── 回调：协议栈来要下一块（ZBOSS 任务） ───────────────────────────────── */
static esp_err_t ota_next_data_cb(esp_zb_ota_zcl_information_t msg,
                                   uint16_t index, uint8_t size, uint8_t **data)
{
    if (!s_active) return ESP_ERR_INVALID_STATE;

    s_cb_n++;
    /* ★★ `index` 不是文件偏移，而是镜像在 OTA 表里的序号 —— 单个镜像时恒为 0
     *    （2026-10-26 实测：n=1..8 全是 idx=0，`size` 从第 2 次起为 64）。
     *    文档说这 API 要的就是 "the NEXT OTA data"，所以严格顺序供给、自己计数 ——
     *    但（见下一段）只在「客户端确实站在我们以为的位置」时才成立。 */
    uint32_t want = s_cur_off;

    /* ── 重新对齐：客户端在每次块请求里都会报自己的偏移。
     *    一旦跟我们自己的指针不一致（协调器中途重启、块重传、回执丢了），
     *    就跟着客户端走，而不是闭眼往下数。否则等于把错的字节写进它的 flash。 */
    if (s_cli_ok && s_cli_off != s_cur_off) {
        s_resync++;
        if (s_resync <= OTA_DBG_MAX) {
            ota_dbg("\"resync\":%lu,\"srv\":%lu,\"cli\":%lu",
                    (unsigned long)s_resync, (unsigned long)s_cur_off,
                    (unsigned long)s_cli_off);
        }
        s_cur_off = s_cli_off;
        want = s_cli_off;
        if (!win_covers(want, size ? size : 1)) {
            /* 窗口不在同一位置 ⇒ 直接丢掉，ota_task 从 `want` 重新装。 */
            s_win_off = want;
            s_win_len = 0;
        }
    }

    /* 镜像末尾凑不满一整块 —— 剩下多少算多少。
     * ★ 协议栈只会从 *data 读 `size` 个字节，所以窗口里有这 `size` 个就够
     *   （以前硬要求 OTA_MAX_BLOCK=232 ⇒ 每到窗口末尾就白等 2.5 s）。 */
    uint32_t need = size ? size : 1;
    if (want >= s_img_size)              need = 1;
    else if (want + need > s_img_size)   need = s_img_size - want;

    if (s_cb_n <= OTA_DBG_MAX) {
        ota_dbg("\"n\":%lu,\"src\":\"0x%04x\",\"cmd\":%u,\"idx\":%u,\"sz\":%u,\"off\":%lu",
                (unsigned long)s_cb_n, (unsigned)msg.src_addr.u.short_addr,
                (unsigned)msg.command_id, (unsigned)index, (unsigned)size,
                (unsigned long)want);
    }

    if (win_covers(want, need)) {
        uint8_t *p = s_win + (want - s_win_off);
        uint32_t h = s_hash;
        for (uint32_t i = 0; i < size; i++) h = (h ^ p[i]) * 16777619u;
        s_hash   = h;
        s_served += size;
        *data = p;
        s_cur_off = want + size;
        s_hit++;
        return ESP_OK;
    }

    /* 缺口：有预填 + 提前量时一般不会出现。
     * 应急：短暂等一下（rx_task 处理 ota_data 已经不持锁，数据可以在这期间到达）
     * 再试一次。 */
    s_miss++;
    if (s_miss <= OTA_DBG_MAX) {
        ota_dbg("\"miss\":%lu,\"off\":%lu,\"win\":[%lu,+%lu)",
                (unsigned long)s_miss, (unsigned long)want,
                (unsigned long)s_win_off, (unsigned long)s_win_len);
    }
    for (int i = 0; i < (OTA_MISS_MS / 10); i++) {
        vTaskDelay(pdMS_TO_TICKS(10));
        if (win_covers(want, need)) {
            uint8_t *p = s_win + (want - s_win_off);
            uint32_t h = s_hash;
            for (uint32_t i = 0; i < size; i++) h = (h ^ p[i]) * 16777619u;
            s_hash   = h;
            s_served += size;
            *data = p;
            s_cur_off = want + size;
            return ESP_OK;
        }
    }
    ota_dbg("\"giveup\":%lu,\"off\":%lu", (unsigned long)s_miss, (unsigned long)want);
    return ESP_ERR_NOT_FOUND;
}

/* ── 树莓派送来的一块数据（rx_task → main.c on_uart_cmd，**不持** Zigbee 锁）── */
/* payload = "OFF,HEX"。★ 带 OFF 是为了把数据写到窗口里正确的位置。
 * 老版本是无脑往 s_win_len 后面追加 —— 一旦出现重传（客户端把同一块再要一次）
 * 或者迟到的块，整段就错位写进缓冲，客户端于是写出一份**静默损坏**的镜像
 * （esp_ota_end 会失败，而服务端这边毫无察觉）。 */
void zb_ota_server_feed_data(const char *payload, uint16_t len)
{
    if (!s_data_sem || !s_active || len == 0) return;

    const char *hex = strchr(payload, ',');
    if (!hex) return;
    hex++;
    uint32_t off = (uint32_t)strtoul(payload, NULL, 0);

    uint32_t n = (len - (uint16_t)(hex - payload)) / 2;
    if (n == 0) return;
    if (off < s_win_off) return;                       /* 对这一窗来说太旧，丢弃 */
    uint32_t rel = off - s_win_off;
    if (rel + n > OTA_WIN) return;                     /* 装不进窗口 */

    s_rx_n++;
    if (s_rx_n <= 3 || (s_rx_n % 64) == 0) {
        ota_dbg("\"rx\":%lu,\"off\":%lu,\"len\":%lu,\"rel\":%lu",
                (unsigned long)s_rx_n, (unsigned long)off, (unsigned long)n,
                (unsigned long)rel);
    }

    for (uint32_t i = 0; i < n; i++) {
        char h[3] = { hex[i * 2], hex[i * 2 + 1], '\0' };
        s_win[rel + i] = (uint8_t)strtol(h, NULL, 16);
    }
    s_rx_off = off;
    s_rx_len = n;                  /* s_win_len 只由 ota_task 抬高 */
    xSemaphoreGive(s_data_sem);
}

/* ── eine Runde nachladen; true = Daten angekommen ──────────────────────── */
static bool fetch_once(void)
{
    uint32_t tail = s_win_off + s_win_len;
    if (tail >= s_img_size) { s_eof = true; return false; }

    uint32_t n = OTA_FETCH;
    if (tail + n > s_img_size) n = s_img_size - tail;

    s_rx_len = 0;
    s_rx_off = tail;
    while (xSemaphoreTake(s_data_sem, 0) == pdTRUE) { }   /* alte Signale weg */
    char req[64];
    snprintf(req, sizeof(req),
             "{\"t\":\"ota_req\",\"off\":%lu,\"sz\":%lu}",
             (unsigned long)tail, (unsigned long)n);
    ha_mqtt_emit_raw(req);

    if (xSemaphoreTake(s_data_sem, pdMS_TO_TICKS(OTA_WAIT_MS)) != pdTRUE || s_rx_len == 0) {
        s_to++;
        ota_dbg("\"timeout\":%lu,\"off\":%lu,\"sz\":%lu",
                (unsigned long)s_to, (unsigned long)tail, (unsigned long)n);
        vTaskDelay(pdMS_TO_TICKS(200));
        return false;
    }
    if (s_rx_off != tail) {
        /* 这一块不属于窗口末尾 ⇒ 宁可丢掉，也不要放错位置 */
        s_bad++;
        ota_dbg("\"badblk\":%lu,\"want\":%lu,\"got\":%lu",
                (unsigned long)s_bad, (unsigned long)tail, (unsigned long)s_rx_off);
        return false;
    }
    s_win_len += s_rx_len;
    if (s_win_off + s_win_len > s_img_size) s_win_len = s_img_size - s_win_off;
    return true;
}

/* ── Nachlade-Task ──────────────────────────────────────────────────────── */
static void ota_task(void *arg)
{
    (void)arg;
    uint32_t last_dbg = 0xFFFFFFFF;

    for (;;) {
        if (!s_active) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        /* 阶段 1：先预填，预填够了才发 Image Notify。 */
        if (s_preload) {
            uint32_t need = s_img_size < OTA_PRELOAD ? s_img_size : OTA_PRELOAD;
            while (s_active && s_preload && s_win_len < need) {
                win_compact();
                if (!fetch_once()) break;
            }
            if (!s_active || !s_preload) continue;
            if (s_win_len >= need) {
                s_preload = false;
                esp_zb_ota_upgrade_server_notify_req_t req = {
                    .endpoint         = OTA_ENDPOINT,
                    .index            = 0,
                    .notify_on        = 1,
                    .ota_upgrade_time = 0,
                    .ota_file_header  = {
                        .manufacturer_code = OTA_MFR_CODE,
                        .image_type        = s_type,
                        .file_version      = s_ver,
                        .image_size        = s_img_size,
                    },
                    .next_data_cb = ota_next_data_cb,
                };
                /* Fremder Task → Lock holen (Vorschrift der SDK-Doku). */
                esp_zb_lock_acquire(portMAX_DELAY);
                esp_err_t err = esp_zb_ota_upgrade_server_notify_req(&req);
                esp_zb_lock_release();

                char line[128];
                snprintf(line, sizeof(line),
                         "{\"t\":\"ota_status\",\"status\":\"%s\",\"size\":%lu,"
                         "\"ver\":%lu,\"type\":\"0x%04x\"}",
                         err == ESP_OK ? "started" : "error",
                         (unsigned long)s_img_size, (unsigned long)s_ver,
                         (unsigned)s_type);
                ha_mqtt_emit_raw(line);
                ESP_LOGI(TAG, "notify %s size=%lu ver=%lu type=0x%04x (preload %lu B)",
                         err == ESP_OK ? "ok" : "FEHLER",
                         (unsigned long)s_img_size, (unsigned long)s_ver,
                         (unsigned)s_type, (unsigned long)s_win_len);
            }
            continue;
        }

        /* Phase 2: Vorrat halten. */
        uint32_t tail = s_win_off + s_win_len;
        if (s_cur_off > tail) {          /* 客户端一下跳到窗口之外了 */
            s_win_off = s_cur_off;
            s_win_len = 0;
            tail = s_cur_off;
        }
        /* 每 5 s 一行汇总：吞吐和卡点只看这一行就够 */
        uint32_t now_ms = (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
        if (now_ms - s_last_stat_ms >= 5000) {
            uint32_t dt = now_ms - s_last_stat_ms;
            uint32_t dc = s_cb_n - s_last_cb;
            s_last_stat_ms = now_ms;
            s_last_cb = s_cb_n;
            ota_dbg("\"stat\":1,\"cb\":%lu,\"bps\":%lu,\"cur\":%lu,"
                    "\"win\":%lu,\"len\":%lu,\"miss\":%lu,\"to\":%lu,"
                    "\"bad\":%lu,\"served\":%lu,\"sync\":%lu",
                    (unsigned long)s_cb_n,
                    (unsigned long)(dt ? (dc * 1000u) / dt : 0),
                    (unsigned long)s_cur_off, (unsigned long)s_win_off,
                    (unsigned long)s_win_len, (unsigned long)s_miss,
                    (unsigned long)s_to, (unsigned long)s_bad,
                    (unsigned long)s_served, (unsigned long)s_resync);
        }
        if (tail >= s_img_size || s_eof) {
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        if (s_cur_off + OTA_AHEAD <= tail) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        win_compact();
        if (OTA_WIN - s_win_len < OTA_FETCH) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        if (fetch_once()) {
            if (tail % 8192 == 0 && tail != last_dbg) {
                last_dbg = tail;
                ota_dbg("\"load\":%lu,\"cb\":%lu,\"hit\":%lu,\"miss\":%lu",
                        (unsigned long)tail, (unsigned long)s_cb_n,
                        (unsigned long)s_hit, (unsigned long)s_miss);
            }
        }
    }
}

/* ── 开始 OTA 会话（在 rx_task 里跑，持 Zigbee 锁） ─────────────────────── */
void zb_ota_server_start(uint32_t size, uint32_t version, uint16_t image_type) {
    if (!s_data_sem) s_data_sem = xSemaphoreCreateBinary();
    if (!s_task) xTaskCreate(ota_task, "ota_srv", 3584, NULL, 4, &s_task);

    s_win_off  = 0;
    s_win_len  = 0;
    s_cur_off  = 0;
    s_rx_len   = 0;
    s_eof      = false;
    s_img_size = size;
    s_ver      = version;
    s_type     = image_type ? image_type : OTA_IMAGE_TYPE;
    s_cb_n = s_hit = s_miss = s_to = s_rx_n = s_bad = 0;
    s_served = 0; s_hash = 2166136261u;
    s_cli_ok = false; s_cli_off = 0; s_resync = 0;
    s_last_stat_ms = (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
    s_last_cb = 0;

    /* ★ 这里**还不发** notify_req：ota_task 要先预填 OTA_PRELOAD 字节。
     *   在这里等会死锁（数据正是由当前这个 rx_task 提供的）。 */
    s_active  = true;
    s_preload = true;
    ESP_LOGI(TAG, "OTA vorbereiten: size=%lu ver=%lu type=0x%04x",
             (unsigned long)size, (unsigned long)version, (unsigned)s_type);
}

void zb_ota_server_stop(void) {
    if (!s_active) return;
    s_active  = false;
    s_preload = false;
    ota_dbg("\"stop\":1,\"hit\":%lu,\"miss\":%lu,\"to\":%lu,"
            "\"bad\":%lu,\"served\":%lu,\"sync\":%lu,\"hash\":\"0x%08lx\"",
            (unsigned long)s_hit, (unsigned long)s_miss, (unsigned long)s_to,
            (unsigned long)s_bad, (unsigned long)s_served,
            (unsigned long)s_resync, (unsigned long)s_hash);
    s_win_len = 0;
}

/* ── 生成 OTA 服务端簇属性 ──────────────────────────────────────────────── */
esp_zb_attribute_list_t *zb_ota_server_cluster_create(void)
{
    esp_zb_attribute_list_t *attr = esp_zb_zcl_attr_list_create(
        ESP_ZB_ZCL_CLUSTER_ID_OTA_UPGRADE);

    esp_zb_zcl_ota_upgrade_server_variable_t var = {
        .query_jitter = 100,
        .current_time = 0,
        .file_count   = 1,
    };
    esp_zb_ota_cluster_add_attr(attr,
        ESP_ZB_ZCL_ATTR_OTA_UPGRADE_SERVER_DATA_ID, &var);
    return attr;
}
