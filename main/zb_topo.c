/*
 * zb_topo.c – 网络拓扑导出（谁直连、谁经中继转发）
 *
 * 详见 zb_topo.h。这里只负责把表 dump 成 JSON 行，交 ha_mqtt_emit_raw()
 * 走 UART → add-on。判读逻辑全部放在 add-on 里，网关不猜。
 *
 * ★★★ 2026-10-26: 新增 Mgmt_Lqi_Req 逐台采集（这是拓扑能用的关键）
 *
 * 之前只 dump 协调器自己的三张表 ⇒ 根本不知道「某台设备挂在哪」：
 *   · 邻居表只装**一跳**邻居，挂在别的路由器下面的设备压根不在里面；
 *   · 路由表按需建，直接邻居根本不建表项，刷新后经常是空的；
 *   · 于是 add-on 只能把「协调器听得见」当成「直连协调器」，
 *     整张图变成所有设备都挂在协调器下面 —— 看不出中继的价值。
 *
 * Z2M 的做法（也是 Zigbee 规范的做法）：向**每台设备**发 Mgmt_Lqi_Req
 * (ZDO 0x0031)，读它**自己的**邻居表。里面 rel=0 的那一条就是它的父节点，
 * 而且 lqi 是**这一跳**的真实链路质量。这才是 mesh 图的权威数据。
 *
 * 参考实现：tostmann/esp-coordinator 的 commands_impl.h
 *   cmd_handle<ZDO_MGMT_LQI_REQ>（zb_zdo_mgmt_lqi_req(buf, cb)，响应里
 *   resp+1 后面紧跟 neighbor_table_list_count 条记录）。
 *
 * 输出行（按顺序）：
 *   {"t":"topo_nbr","n":[…]}                  协调器自己的邻居表
 *   {"t":"topo_rt","n":[…]}                   协调器的路由表
 *   {"t":"topo_rr","n":[…]}                   源路由记录
 *   {"t":"topo_mesh","a":"0x159e","st":0,"tot":3,"nb":[
 *        {"a":"0x2ad6","rel":0,"ty":1,"dp":17,"lqi":25,"pj":1}, …]}   每台设备一行
 *   {"t":"topo",…,"mesh":3,"done":true}       汇总头，**最后**发
 *
 * 字段：rel 0=parent 1=child 2=sibling 3=none 4=prev-child 5=unauth-child
 *       ty  0=coordinator 1=router 2=end device 3=unknown
 *       dp  深度  lqi 这一跳的链路质量  pj permit_join  st Mgmt_Lqi 状态码
 *
 * ★ 只能在 ZBOSS 任务里跑（读 NIB / 发 ZDP 请求），入口一律走
 *   esp_zb_scheduler_alarm，绝不从 uart_rx 任务直接调。
 * ★ 大缓冲必须 static：本文件所有缓冲都在 .bss，不占栈。
 */
#include <stdio.h>
#include <string.h>
#include <stdbool.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_log.h"
#include "esp_zigbee_core.h"
#include "nwk/esp_zigbee_nwk.h"
#include "zboss_api.h"
#include "zboss_api_zdo.h"

#include "ha_mqtt.h"
#include "zb_topo.h"
#include "zb_gateway.h"

#define TAG "topo"

/* 一张表的 JSON 缓冲。64 个邻居最坏情况 ~4 KB，所以给 4 KB 并做截断保护。 */
#define TOPO_BUF 4096

/* ── Mgmt_Lqi 采集参数 ───────────────────────────────────────────────────── */
#define MESH_MAX_DEV      32    /* 一次最多采集多少台设备 */
#define MESH_MAX_NB       24    /* 单台设备最多记多少条邻居（一般 3~8 条） */
#define MESH_BUF        1536    /* 单台设备一行的 JSON 缓冲 */
#define MESH_CHUNK_GAP     120  /* 同设备分页续取的间隔 ms */
#define MESH_DEV_GAP       200  /* 换下一台设备的间隔 ms */
#define MESH_DEADLINE   15000   /* 整个采集的总超时（有设备不应答时兜底） */

typedef struct {
    uint16_t a;
    uint8_t  rel;   /* relationship: 0=parent 1=child 2=sibling … */
    uint8_t  ty;    /* device type: 0=coord 1=router 2=ed 3=unknown */
    uint8_t  dp;    /* depth */
    uint8_t  lqi;   /* 这一跳的链路质量 */
    uint8_t  pj;    /* permit join */
} mesh_nb_t;

static uint16_t  s_mesh_list[MESH_MAX_DEV];
static uint8_t   s_mesh_n;          /* 待采集台数 */
static uint8_t   s_mesh_i;          /* 当前第几台 */
static uint16_t  s_mesh_cur;        /* 正在采集的地址 */
static uint8_t   s_mesh_nbcnt;      /* 已收到的邻居条数 */
static uint8_t   s_mesh_ok;         /* 成功采集的台数 */
static uint8_t   s_mesh_fail;       /* 采不到/不支持的台数 */
static volatile bool s_mesh_running = false;
static mesh_nb_t s_mesh_nb[MESH_MAX_NB];
static char      s_mesh_buf[MESH_BUF];

/* 三张表的条目数：dump 时记下，汇总头里回报，避免再遍历一次 NIB。 */
static int s_last_nbr = 0, s_last_rt = 0, s_last_rr = 0;

/* 前向声明（状态机里互相调用） */
static void mesh_send(uint16_t addr, uint8_t start);
static void mesh_dev_next_cb(uint8_t param);

/* 逐表复用同一块缓冲：emit() 是入队（不阻塞），但内容已经拷进堆块，
 * 所以用完即可重用。放 .bss 而不是栈上 —— 这个回调跑在 ZBOSS 任务里。 */
static char s_buf[TOPO_BUF];

/* ── 邻居表 ─────────────────────────────────────────────────────────────── */
static int dump_neighbors(void) {
    int pos = 0, cnt = 0;
    pos += snprintf(s_buf + pos, TOPO_BUF - pos, "{\"t\":\"topo_nbr\",\"n\":[");

    esp_zb_nwk_info_iterator_t it = ESP_ZB_NWK_INFO_ITERATOR_INIT;
    esp_zb_nwk_neighbor_info_t nbr;
    while (esp_zb_nwk_get_next_neighbor(&it, &nbr) == ESP_OK) {
        if (TOPO_BUF - pos < 96) break;          /* 留够一条条目的余量 */
        pos += snprintf(s_buf + pos, TOPO_BUF - pos,
                        "%s{\"a\":\"0x%04x\",\"ty\":%u,\"dp\":%u,\"rel\":%u,"
                        "\"lqi\":%u,\"rssi\":%d,\"oc\":%u,\"age\":%u,\"rx\":%u}",
                        cnt ? "," : "",
                        nbr.short_addr, nbr.device_type, nbr.depth,
                        nbr.relationship, nbr.lqi, nbr.rssi,
                        nbr.outgoing_cost, nbr.age, nbr.rx_on_when_idle);
        cnt++;
    }
    snprintf(s_buf + pos, TOPO_BUF - pos, "]}");
    ha_mqtt_emit_raw(s_buf);
    return cnt;
}

/* ── 路由表：到某地址的下一跳 ───────────────────────────────────────────── */
static int dump_routes(void) {
    int pos = 0, cnt = 0;
    pos += snprintf(s_buf + pos, TOPO_BUF - pos, "{\"t\":\"topo_rt\",\"n\":[");

    esp_zb_nwk_info_iterator_t it = ESP_ZB_NWK_INFO_ITERATOR_INIT;
    esp_zb_nwk_route_info_t rt;
    while (esp_zb_nwk_get_next_route(&it, &rt) == ESP_OK) {
        if (TOPO_BUF - pos < 80) break;
        if (rt.flags.group_id) continue;         /* 组播地址不是物理设备，跳过 */
        pos += snprintf(s_buf + pos, TOPO_BUF - pos,
                        "%s{\"d\":\"0x%04x\",\"nh\":\"0x%04x\",\"st\":%u,\"ex\":%u}",
                        cnt ? "," : "",
                        rt.dest_addr, rt.next_hop_addr,
                        (unsigned)rt.flags.status, rt.expiry);
        cnt++;
    }
    snprintf(s_buf + pos, TOPO_BUF - pos, "]}");
    ha_mqtt_emit_raw(s_buf);
    return cnt;
}

/* ── 源路由记录：完整中继链路 ───────────────────────────────────────────── */
static int dump_route_records(void) {
    int pos = 0, cnt = 0;
    pos += snprintf(s_buf + pos, TOPO_BUF - pos, "{\"t\":\"topo_rr\",\"n\":[");

    esp_zb_nwk_info_iterator_t it = ESP_ZB_NWK_INFO_ITERATOR_INIT;
    esp_zb_nwk_route_record_info_t rr;
    while (esp_zb_nwk_get_next_route_record(&it, &rr) == ESP_OK) {
        if (TOPO_BUF - pos < 64) break;
        pos += snprintf(s_buf + pos, TOPO_BUF - pos,
                        "%s{\"d\":\"0x%04x\",\"rc\":%u,\"p\":[",
                        cnt ? "," : "", rr.dest_address, rr.relay_count);
        uint8_t rc = rr.relay_count;
        if (rc > ESP_ZB_NWK_MAX_SOURCE_ROUTE) rc = ESP_ZB_NWK_MAX_SOURCE_ROUTE;
        for (uint8_t i = 0; i < rc && (TOPO_BUF - pos) > 16; i++) {
            pos += snprintf(s_buf + pos, TOPO_BUF - pos, "%s\"0x%04x\"",
                            i ? "," : "", rr.path[i]);
        }
        pos += snprintf(s_buf + pos, TOPO_BUF - pos, "]}");
        cnt++;
    }
    snprintf(s_buf + pos, TOPO_BUF - pos, "]}");
    ha_mqtt_emit_raw(s_buf);
    return cnt;
}

/* ══ Mgmt_Lqi 采集状态机 ═════════════════════════════════════════════════ */

static void mesh_emit_and_next(void);

/* 汇总头：只有收到它，add-on 才认为本轮到齐了（不靠超时猜）。 */
static void mesh_finish(void) {
    s_mesh_running = false;

    char head[192];
    snprintf(head, sizeof(head),
             "{\"t\":\"topo\",\"pan\":\"0x%04x\",\"ch\":%d,\"addr\":\"0x%04x\","
             "\"nbr\":%d,\"rt\":%d,\"rr\":%d,\"mesh\":%u,\"mesh_fail\":%u,\"done\":true}",
             esp_zb_get_pan_id(), esp_zb_get_current_channel(),
             esp_zb_get_short_address(),
             /* 三张表在 topo_start 里已经发过，这里只回报采集结果；
              * 数字用上面记下的值，避免再遍历一次 NIB。 */
             s_last_nbr, s_last_rt, s_last_rr,
             (unsigned)s_mesh_ok, (unsigned)s_mesh_fail);
    ha_mqtt_emit_raw(head);

    ESP_LOGI(TAG, "拓扑已导出：邻居 %d / 路由 %d / 源路由 %d / 设备表 %u 成功 %u 失败 "
                  "· zb_task 栈余 %u B",
             s_last_nbr, s_last_rt, s_last_rr,
             (unsigned)s_mesh_ok, (unsigned)s_mesh_fail,
             (unsigned)uxTaskGetStackHighWaterMark(NULL));
}

/* 把当前设备的邻居列表发成一行 JSON。 */
static void mesh_emit_and_next(void) {
    int pos = 0;
    pos += snprintf(s_mesh_buf + pos, MESH_BUF - pos,
                    "{\"t\":\"topo_mesh\",\"a\":\"0x%04x\",\"st\":0,\"tot\":%u,\"nb\":[",
                    s_mesh_cur, (unsigned)s_mesh_nbcnt);
    for (uint8_t i = 0; i < s_mesh_nbcnt && (MESH_BUF - pos) > 64; i++) {
        mesh_nb_t *e = &s_mesh_nb[i];
        pos += snprintf(s_mesh_buf + pos, MESH_BUF - pos,
                        "%s{\"a\":\"0x%04x\",\"rel\":%u,\"ty\":%u,\"dp\":%u,"
                        "\"lqi\":%u,\"pj\":%u}",
                        i ? "," : "", e->a, e->rel, e->ty, e->dp, e->lqi, e->pj);
    }
    snprintf(s_mesh_buf + pos, MESH_BUF - pos, "]}");
    ha_mqtt_emit_raw(s_mesh_buf);

    if (s_mesh_nbcnt) s_mesh_ok++; else s_mesh_fail++;

    /* 下一台 */
    if (++s_mesh_i >= s_mesh_n) {
        mesh_finish();
        return;
    }
    s_mesh_cur    = s_mesh_list[s_mesh_i];
    s_mesh_nbcnt  = 0;
    esp_zb_scheduler_alarm(mesh_dev_next_cb, 0, MESH_DEV_GAP);
}

static void mesh_dev_next_cb(uint8_t param) {
    (void)param;
    if (!s_mesh_running) return;
    mesh_send(s_mesh_cur, 0);
}

/* 失败/不支持的设备也必须留一行，否则 add-on 无从区分「采集失败」和
 * 「还没轮到」。 */
static void mesh_fail_and_next(uint8_t status) {
    int pos = snprintf(s_mesh_buf, MESH_BUF,
                       "{\"t\":\"topo_mesh\",\"a\":\"0x%04x\",\"st\":%u,"
                       "\"tot\":0,\"nb\":[]}", s_mesh_cur, (unsigned)status);
    (void)pos;
    ha_mqtt_emit_raw(s_mesh_buf);

    s_mesh_fail++;
    if (++s_mesh_i >= s_mesh_n) {
        mesh_finish();
        return;
    }
    s_mesh_cur   = s_mesh_list[s_mesh_i];
    s_mesh_nbcnt = 0;
    esp_zb_scheduler_alarm(mesh_dev_next_cb, 0, MESH_DEV_GAP);
}

static void mesh_chunk_next_cb(uint8_t start) {
    if (!s_mesh_running) return;
    mesh_send(s_mesh_cur, start);
}

/* Mgmt_Lqi_rsp：resp 后面紧跟 neighbor_table_list_count 条记录。 */
static void mesh_lqi_cb(zb_uint8_t param) {
    zb_zdo_mgmt_lqi_resp_t *resp = NULL;
    if (zb_buf_len(param) >= sizeof(zb_zdo_mgmt_lqi_resp_t)) {
        resp = (zb_zdo_mgmt_lqi_resp_t *)zb_buf_begin(param);
    }

    if (!resp || resp->status != ZB_ZDP_STATUS_SUCCESS) {
        uint8_t st = resp ? resp->status : 0xFF;
        zb_buf_free(param);
        if (s_mesh_running) mesh_fail_and_next(st);
        return;
    }

    const zb_zdo_neighbor_table_record_t *rec =
        (const zb_zdo_neighbor_table_record_t *)(resp + 1);
    uint8_t cnt = resp->neighbor_table_list_count;
    /* 防御：缓冲区里真的装得下这么多条吗？ */
    uint16_t room = (uint16_t)((zb_buf_len(param) - sizeof(*resp)) /
                               sizeof(zb_zdo_neighbor_table_record_t));
    if (cnt > room) cnt = (uint8_t)room;

    for (uint8_t i = 0; i < cnt && s_mesh_nbcnt < MESH_MAX_NB; i++) {
        mesh_nb_t *e = &s_mesh_nb[s_mesh_nbcnt++];
        e->a   = rec[i].network_addr;
        e->rel = (uint8_t)ZB_ZDO_RECORD_GET_RELATIONSHIP(rec[i].type_flags);
        e->ty  = (uint8_t)ZB_ZDO_RECORD_GET_DEVICE_TYPE(rec[i].type_flags);
        e->dp  = rec[i].depth;
        e->lqi = rec[i].lqi;
        e->pj  = rec[i].permit_join;
    }

    /* 还有后续分页？（邻居表超过一帧容量时 ZBOSS 只回一部分） */
    uint16_t got = (uint16_t)resp->start_index + resp->neighbor_table_list_count;
    bool more = (got < resp->neighbor_table_entries) &&
                (s_mesh_nbcnt < MESH_MAX_NB);
    uint8_t next_start = (uint8_t)got;

    zb_buf_free(param);

    if (!s_mesh_running) return;
    if (more) esp_zb_scheduler_alarm(mesh_chunk_next_cb, next_start, MESH_CHUNK_GAP);
    else      mesh_emit_and_next();
}

static void mesh_timeout_cb(uint8_t param) {
    (void)param;
    if (!s_mesh_running) return;
    ESP_LOGW(TAG, "Mgmt_Lqi 采集超时，强制收尾（%u/%u 台）",
             (unsigned)s_mesh_i, (unsigned)s_mesh_n);
    mesh_finish();
}

/* 向 addr 请求第 start 条起的邻居列表。 */
static void mesh_send(uint16_t addr, uint8_t start) {
    zb_bufid_t buf = zb_buf_get_out();
    if (buf == ZB_BUF_INVALID) {
        ESP_LOGW(TAG, "kein ZBOSS-Puffer für Mgmt_Lqi – Rest übersprungen");
        mesh_finish();
        return;
    }
    /* ★★★ 2026-10-26: hier lag der Fehler.
     * Erst mit zb_buf_begin() in den Puffer geschrieben, aber die Länge blieb
     * 0 ⇒ ZBOSS hat die Parameter nicht übernommen und mit dst_addr = 0x0000
     * an SICH SELBST geschickt. Ergebnis: jede "Gerätetabelle" war in
     * Wahrheit die Nachbartabelle des Koordinators (gleiche Adressen, gleiche
     * LQI) – und damit die Topologie genauso wertlos wie vorher.
     * ZB_BUF_GET_PARAM() (= zb_buf_get_tail_func) reserviert den Platz UND
     * zählt ihn zur Pufferlänge – genau wie esp_zb_zdo_*_req() es intern tut. */
    zb_zdo_mgmt_lqi_param_t *p =
        ZB_BUF_GET_PARAM((zb_uint8_t)buf, zb_zdo_mgmt_lqi_param_t);
    if (!p) {
        zb_buf_free(buf);
        ESP_LOGW(TAG, "Mgmt_Lqi-Parameter nicht platzierbar");
        mesh_fail_and_next(0xFD);
        return;
    }
    p->dst_addr    = addr;
    p->start_index = start;

    /* Rückgabe: ZDP-TSN oder 0xFF, wenn gerade kein ZDP-Transaktion frei ist. */
    zb_uint8_t tsn = zb_zdo_mgmt_lqi_req((zb_uint8_t)buf, mesh_lqi_cb);
    if (tsn == 0xFF) {
        zb_buf_free(buf);
        ESP_LOGW(TAG, "Mgmt_Lqi an 0x%04x nicht möglich (ZDP belegt)", addr);
        mesh_fail_and_next(0xFE);
    }
}

/* 入口：先 dump 三张表，再开始逐台采集。 */
static void topo_start(uint8_t param) {
    (void)param;

    s_last_nbr = dump_neighbors();
    s_last_rt  = dump_routes();
    s_last_rr  = dump_route_records();

    s_mesh_n     = zb_gateway_known_addrs(s_mesh_list, MESH_MAX_DEV);
    s_mesh_i     = 0;
    s_mesh_ok    = 0;
    s_mesh_fail  = 0;
    s_mesh_nbcnt = 0;

    if (s_mesh_n == 0) {
        mesh_finish();
        return;
    }

    s_mesh_running = true;
    s_mesh_cur     = s_mesh_list[0];
    esp_zb_scheduler_alarm(mesh_timeout_cb, 0, MESH_DEADLINE);
    esp_zb_scheduler_alarm(mesh_dev_next_cb, 0, 50);
}

void zb_topo_request(void) {
    esp_zb_scheduler_alarm(topo_start, 0, 10);
}

void zb_topo_request_full(uint32_t delay_ms) {
    /* 「先探测再 dump」由 add-on 负责（它知道设备列表，逐个发 node_desc），
     * 网关这边只需要等一会儿再开始。 */
    esp_zb_scheduler_alarm(topo_start, 0, (delay_ms ? delay_ms : 3000));
}
