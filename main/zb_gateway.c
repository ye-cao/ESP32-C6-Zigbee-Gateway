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

/* ── Gerätedatenbank (nur Adress-Tracking, kein Name) ───────────────────── */
/* Netzgröße = Tabellengröße. esp_zb_overall_network_size_set() (SDK ≥1.2.2)
 * dimensioniert Nachbar- und Routing-Tabelle der ZBOSS-Stack-Lib auf diesen
 * Wert; Default ist 64, vorher waren hier hart 32 verdrahtet – mehr ließ die
 * Bibliothek (1.0.9, feste Tabellen) gar nicht zu. MUSS vor esp_zb_init()
 * gesetzt werden, danach ist es wirkungslos. */
#define MAX_DEVICES ZB_GATEWAY_MAX_DEVICES
typedef struct {
    uint16_t short_addr;
    uint8_t  ieee[8];
    bool     used;
    bool     interviewed;   /* Interview (Bind/Reporting) schon durchgeführt */
    uint8_t  lqi;   /* letzte LQI vom APS-Layer */
    int8_t   rssi;  /* letzte RSSI vom IEEE 802.15.4 Layer (dBm) */
    char     mfr[33];   /* Basic 0x0004 ManufacturerName */
    char     model[33]; /* Basic 0x0005 ModelIdentifier */
} zb_device_t;

static zb_device_t  s_devices[MAX_DEVICES];
static SemaphoreHandle_t s_dev_mutex;
static volatile int8_t  s_last_rssi = 0;

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
    ESP_LOGW(TAG, "Gerätedatenbank voll (max %d)", MAX_DEVICES);
    return NULL;
}

/* ── Persistenz der Gerätetabelle ─────────────────────────────────────────
 * ZBOSS legt Netzwerkschlüssel, PAN, Kanal und die Adresstabelle selbst im
 * NVRAM ab (Partition zb_storage) und setzt das Netz beim Boot fort, solange
 * niemand nvram_erase_at_start setzt. Unsere eigene Tabelle (Kurzadresse +
 * IEEE für LQI-Snapshot und Interview) war dagegen reiner RAM: nach einem
 * Neustart war sie leer, bis jedes Gerät erneut einen Device-Announce
 * schickte – schlafende Endgeräte brauchen dafür Stunden.
 * Deshalb hier eine kleine NVS-Kopie. interviewed wird beim Laden bewusst
 * auf false gesetzt: das Interview läuft erst, wenn das Gerät wieder
 * antwortet, sonst würden Bind/Reporting ins Leere gehen. */
#define DEVTAB_MAGIC   0x44425531u   /* 'DBU1' */
#define DEVTAB_KEY     "devtab"

typedef struct {
    uint32_t magic;
    uint8_t  count;
    struct { uint16_t addr; uint8_t ieee[8]; } e[MAX_DEVICES];
} __attribute__((packed)) devtab_blob_t;

/* Statisch, nicht auf dem Stack: Aufruf erfolgt im ZB-Task, aber die
 * Stack-Budget-Regel dieses Projekts gilt einheitlich (s. LQI-Puffer). */
static devtab_blob_t s_devtab;

static void devtab_save(void) {
    nvs_handle_t h;
    if (nvs_open(ZB_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return;
    s_devtab.magic = DEVTAB_MAGIC;
    s_devtab.count = 0;
    xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
    for (int i = 0; i < MAX_DEVICES; i++) {
        /* IEEE 0 ist "noch nicht aufgelöst" – solche Einträge bringen nach
         * dem Neustart nichts, weil ohne IEEE kein Interview möglich ist. */
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
    ESP_LOGI(TAG, "%d Geräte aus NVS wiederhergestellt "
                  "(Interview folgt, sobald sie sich melden)", n);
}

/* ══════════════════════════════════════════════════════════════════════════ */
/*  Interview nach Join: Active-EP → Simple-Desc → Bind → Configure-Reporting  */
/*                                                                             */
/*  Hintergrund: Ein „nackter“ Coordinator, der nach dem Join nichts weiter     */
/*  tut, wird von vielen Endgeräten nach ~1 Minute aufgegeben – sie verlassen  */
/*  das Netzwerk wieder (Symptom: „pairt erfolgreich, ist aber kurz darauf      */
/*  wieder ungepairt“). Echte Gateways (Xiaomi / Hue / …) führen nach dem      */
/*  Join die Standard-Sequenz aus: aktive Endpoints abfragen →                 */
/*  Simple-Descriptor lesen → Bindings anlegen → Attribute-Reporting           */
/*  konfigurieren. Genau das macht dieser Block.                                */
/* ══════════════════════════════════════════════════════════════════════════ */

#define IV_MAX_EPS      8
#define IV_MAX_CLUSTERS 16

/* Server-Cluster (Input) des Endgeräts → an den Coordinator binden und
 * Reporting auf das Hauptattribut konfigurieren.
 * ACHTUNG: attr == IV_NO_REPORT (0xFFFF) → nur binden. 0x0000 ist eine
 * gültige Attribut-ID (On/Off, Temperatur, Feuchte, … primäres Attribut) –
 * der frühere Check `if (attr)` hat alle diese Cluster nie gemeldet! */
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

/* Client-Cluster (Output) des Endgeräts → nur binden (Tastenbefehle,
 * Szenen, Gruppen landen dann als ZCL-Command beim Coordinator). */
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
static uint16_t  s_iv_change;   /* reportable change – static, wird vom Stack gelesen */

/* Letzter per APS empfangener Frame – die SET_ATTR_VALUE-Message (Tastenbefehl)
 * enthält KEINE Quell-Adresse, daher nehmen wir sie aus dem unmittelbar
 * vorher verarbeiteten APS-Frame (aps_data_ind_cb läuft im gleichen
 * Stack-Durchlauf VOR dem ZCL-Callback). */
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

/* Basic-Cluster-Read: Zustand + Callback (Definition weiter unten) */
static uint16_t s_basic_pending_addr = 0xFFFF;
static uint8_t  s_basic_pending_ep   = 1;   /* echte Geräte-EP (ep1-Hardcodewort repariert) */
static void read_basic_cb(uint8_t param);
/* Vorwärtdeklaration – start_basic_scan ist erst weiter unten definiert,
 * wird aber schon beim Interview-Abschluss (oben) aufgerufen. */
static void start_basic_scan(uint16_t addr, const uint8_t *eps, uint8_t n);

static void iv_ieee_str(const uint8_t *ieee, char *out, size_t len) {
    snprintf(out, len, "%02x:%02x:%02x:%02x:%02x:%02x:%02x:%02x",
             ieee[7], ieee[6], ieee[5], ieee[4],
             ieee[3], ieee[2], ieee[1], ieee[0]);
}

/* FIFO der ausgehenden Bind-Requests – nur für die Zuordnung der Responses
 * beim Logging (ZBOSS liefert pro Response nur Status + ctx). */
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

    /* als interviewiert markieren – kein Re-Interview bei jedem Frame */
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
    ESP_LOGI(TAG, "Interview 0x%04x: Active-EP …", addr);

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
        ESP_LOGI(TAG, "Interview 0x%04x abgeschlossen (eps: %s)",
                 s_iv.short_addr, eps);
        /* Basic-Attribute (Hersteller/Modell) lesen – über alle Endpunkte */
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
        ESP_LOGW(TAG, "Interview 0x%04x: Active-EP fehlgeschlagen (0x%02x)",
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
    ESP_LOGI(TAG, "Interview 0x%04x: %u Endpoint(s) [%s]",
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
        ESP_LOGW(TAG, "Interview: Bind ep%u cluster 0x%04x → 0x%02x",
                 (unsigned)ep, cluster, (unsigned)status);
    }
}

/* ZCL unterscheidet ANALOGE und DISKRETE Datentypen. Nur bei analogen gehört
 * das Feld "reportable change" in den Configure-Reporting-Record; bei
 * diskreten (Bool, Bitmap, Enum, Strings …) ist es laut ZCL 2.5.7.1 NICHT
 * enthalten. Wird es trotzdem mitgeschickt, ist der Record zu lang – manche
 * Endgeräte (z. B. Feibit HGZB-043) quittieren ihn mit SUCCESS, speichern
 * aber Unsinn und melden danach nie wieder eine Zustandsänderung. Genau das
 * war der Grund, warum HA den Schalterzustand nie gesehen hat. */
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
        ESP_LOGW(TAG, "Interview: Simple-Desc ep%u fehlgeschlagen (0x%02x)",
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

    /* Input-Cluster (Server am Gerät) → binden + Reporting */
    for (uint8_t i = 0; i < in_n; i++) {
        uint16_t cl = sd->app_cluster_list[i];
        for (size_t k = 0; k < sizeof(s_iv_input) / sizeof(s_iv_input[0]); k++) {
            if (s_iv_input[k].cluster != cl) continue;
            iv_bind(ep, cl);
            if (s_iv_input[k].attr != IV_NO_REPORT) iv_config_report(ep, cl, &s_iv_input[k]);
            break;
        }
    }

    /* Output-Cluster (Client am Gerät) → nur binden */
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

/* Modell-Scan: das Basic-Cluster sitzt je nach Hersteller auf ep1 oder auf
 * jedem Schalter-Endpunkt (Feibit/Nue: ep16..18). Beim Interview wurde nur
 * die erste EP probiert → „basic" kam nie zurück, das Modell blieb unbekannt.
 * Deshalb hier nacheinander alle bekannten Endpunkte durchprobieren. */
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
    s_scan_eps[s_scan_n++] = 1;          /* Basic liegt am häufigsten auf ep1 */
    for (uint8_t k = 0; k < n && s_scan_n < BASIC_SCAN_MAX; k++) {
        if (eps[k] != 1) s_scan_eps[s_scan_n++] = eps[k];
    }
    s_scan_i = 0;
    esp_zb_scheduler_alarm(read_basic_scan_cb, 0, 500);
}

/* ── IEEE-Auflösung für „stille“ Rejoins ────────────────────────────────────
 * Nach einem Coordinator-Reboot kommen viele Geräte ohne Device-Announce
 * zurück: sie senden einfach wieder APS-Daten. Dann kennen wir ihre IEEE
 * nicht, das Interview wurde bisher übersprungen („ohne IEEE kein Bind“) und
 * damit wurde auch nie das Basic-Cluster gelesen → kein Modell in HA.
 * Deshalb die IEEE hier per ZDO nachträglich auflösen und das Interview
 * danach starten. Rate-Limit: maximal 1 Request pro 30 s im ganzen Netz. */
static TickType_t s_ieee_last_tick = 0;

/* SDK ≥1.6.0 hat den Callback-Typ neu definiert: die Antwort kommt als
 * esp_zb_zdo_ieee_addr_rsp_t* statt als rohes ieee_addr-Array. */
static void ieee_resolved_cb(esp_zb_zdp_status_t zdo_status,
                             esp_zb_zdo_ieee_addr_rsp_t *resp, void *user_ctx) {
    uint16_t addr = (uint16_t)(uintptr_t)user_ctx;
    if (zdo_status != ESP_ZB_ZDP_STATUS_SUCCESS || resp == NULL) {
        ESP_LOGW(TAG, "IEEE-Request 0x%04x fehlgeschlagen (0x%02x)", addr, zdo_status);
        return;
    }
    xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
    zb_device_t *d = find_or_add(addr, resp->ieee_addr);
    bool need = d && !d->interviewed;
    xSemaphoreGive(s_dev_mutex);
    ESP_LOGI(TAG, "IEEE 0x%04x aufgelöst → Interview%s", addr, need ? "" : " (schon erfolgt)");
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

/* ── Keep-Alive-Test: liest nach Autorisierung periodisch Basic-Attribute ──
 * These: TI-Geräte (Z-Stack) verlassen das Netz ~10 s nach "Device
 * Authorized" (rejoin=0), obwohl Interview + Bind erfolgreich waren.
 * Verdacht: Gerät erwartet aktive Kommunikation vom Coordinator.
 * 12 Runden à 5 s = 60 s Abdeckung; jede Runde loggt {"t":"keepalive",...}. */
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
    /* Read-Attribute über read_basic_cb an die echte EP senden */
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

        /* On/Off aktiv zurückgelesen (nach einem Fernbefehl). Das Gerät
         * schickt keinen Report, antwortet aber auf Read Attributes –
         * damit wird die optimistische Annahme des Gateways korrigiert. */
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
        ESP_LOGI(TAG, "OTA srv 0x%04x: %s", m->zcl_addr.u.short_addr, st);
        return ESP_OK;
    }

    /* Tastenbefehl eines gebundenen Schalters: Das On/Off-Server-Cluster am
     * Coordinator setzt das OnOff-Attribut → landet hier (gleicher Pfad wie im
     * HA-On/Off-Light-Beispiel). Die Message enthält keine Quell-Adresse, daher
     * aus s_last_aps (wird von aps_data_ind_cb unmittelbar vorher gefüllt). */
    if (callback_id == ESP_ZB_CORE_SET_ATTR_VALUE_CB_ID) {
        const esp_zb_zcl_set_attr_value_message_t *m = message;
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
                ESP_LOGI(TAG, "Schalter 0x%04x ep%u → %s",
                         src, (unsigned)ep, v ? "ON" : "OFF");
            } else {
                ha_mqtt_emit_raw("{\"t\":\"cmd\",\"cluster\":\"on_off\",\"src\":\"unknown\"}");
            }
        }
        return ESP_OK;
    }

    /* Configure-Reporting-Response (Antwort des Endgeräts auf unser Interview) */
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
     * report_attr_message_t würde falsche Offsets lesen (Müll-Publishes).
     * SET_ATTR_VALUE wird oben separat mit korrektem Typ verarbeitet. */
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
                ESP_LOGI(TAG, "Erster Start – Netzwerk wird gebildet");
                esp_zb_bdb_start_top_level_commissioning(
                    ESP_ZB_BDB_MODE_NETWORK_FORMATION);
            } else {
                ESP_LOGI(TAG, "Neustart – Netzwerk wiederhergestellt");
                esp_zb_bdb_start_top_level_commissioning(
                    ESP_ZB_BDB_MODE_NETWORK_STEERING);
            }
        } else {
            ESP_LOGE(TAG, "Zigbee-Init fehlgeschlagen: %s",
                     esp_err_to_name(err));
        }
        break;

    case ESP_ZB_BDB_SIGNAL_FORMATION:
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "Netzwerk gebildet – PAN 0x%04hx Kanal %d",
                     esp_zb_get_pan_id(), esp_zb_get_current_channel());
            esp_zb_bdb_start_top_level_commissioning(
                ESP_ZB_BDB_MODE_NETWORK_STEERING);
        } else {
            ESP_LOGW(TAG, "Netzwerkbildung fehlgeschlagen – erneuter Versuch");
            esp_zb_scheduler_alarm(
                (esp_zb_callback_t)(void *)esp_zb_bdb_start_top_level_commissioning,
                ESP_ZB_BDB_MODE_NETWORK_FORMATION, 1000);
        }
        break;

    case ESP_ZB_BDB_SIGNAL_STEERING:
        if (err == ESP_OK) {
            /* NICHT esp_zb_bdb_open_network(0) hier aufrufen!
             * Das Boot-Steering-Signal kann NACH einem per BOOT-Taste/UI
             * geöffneten Permit-Join-Fenster ankommen und es sofort wieder
             * schließen (Bug: Fenster wurde ~1 s nach dem Öffnen gekillt).
             * Nach dem Boot ist das Netzwerk ohnehin geschlossen. */
            ESP_LOGI(TAG, "Zigbee bereit – Netzwerk wiederhergestellt");
        }
        break;

    case ESP_ZB_ZDO_SIGNAL_DEVICE_ANNCE: {
        esp_zb_zdo_signal_device_annce_params_t *p =
            esp_zb_app_signal_get_params(sg_p);
        uint16_t a = p->device_short_addr;
        ESP_LOGI(TAG, "Neues Gerät: 0x%04hx", a);

        xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
        /* Rejoin mit neuer Short-Adresse: alten Eintrag gleicher IEEE entfernen */
        for (int i = 0; i < MAX_DEVICES; i++) {
            if (s_devices[i].used && s_devices[i].short_addr != a &&
                memcmp(s_devices[i].ieee, p->ieee_addr, 8) == 0) {
                ESP_LOGI(TAG, "Gerät 0x%04hx → 0x%04hx (Rejoin, neue Adresse)",
                         s_devices[i].short_addr, a);
                memset(&s_devices[i], 0, sizeof(s_devices[i]));
            }
        }
        zb_device_t *dev = find_or_add(a, p->ieee_addr);
        uint8_t dev_ieee[8] = { 0 };
        if (dev) memcpy(dev_ieee, p->ieee_addr, 8);
        xSemaphoreGive(s_dev_mutex);
        /* Neu aufgenommene bzw. migrierte Adresse sofort sichern, damit ein
         * Neustart die Tabelle nicht wieder verliert. */
        if (dev) devtab_save();

        /* IEEE-Adresse als String */
        char ieee_str[24] = "?";
        if (dev) iv_ieee_str(dev_ieee, ieee_str, sizeof(ieee_str));

        /* Join-Event MIT Top-Level-IEEE (so kann der Pi alte Einträge nach
         * Rejoin mit neuer Short-Adresse dedupen) */
        {
            char line[176];
            snprintf(line, sizeof(line),
                     "{\"t\":\"zigbee\",\"addr\":%u,\"sub\":\"join\","
                     "\"p\":{\"event\":\"joined\",\"addr\":\"0x%04x\",\"ieee\":\"%s\"},"
                     "\"ieee\":\"%s\"}",
                     (unsigned)a, a, ieee_str, ieee_str);
            ha_mqtt_emit_raw(line);
        }

        /* Basic-Attribute werden nach dem Interview mit echter EP gelesen
         * (siehe iv_request_simple_desc – alter ep1-Pfad war fehlerhaft) */

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

    /* Gerät verlässt das Netzwerk (oder wird entfernt) – das ist das Signal,
     * das unser Problem „pairt, ist aber ~1 min später wieder ungepairt“
     * sichtbar macht. rejoin=1 heißt: Leave MIT Rejoin (Adresswechsel). */
    case ESP_ZB_ZDO_SIGNAL_LEAVE_INDICATION: {
        esp_zb_zdo_signal_leave_indication_params_t *p =
            esp_zb_app_signal_get_params(sg_p);
        if (p) {
            keepalive_stop();
            char ieee_str[24];
            iv_ieee_str(p->device_addr, ieee_str, sizeof(ieee_str));
            ESP_LOGW(TAG, "Gerät verlässt Netzwerk: 0x%04hx (%s) rejoin=%u",
                     p->short_addr, ieee_str, (unsigned)p->rejoin);
            char line[192];
            snprintf(line, sizeof(line),
                     "{\"t\":\"zigbee\",\"addr\":%u,\"sub\":\"left\","
                     "\"p\":{\"event\":\"left\",\"addr\":\"0x%04x\",\"ieee\":\"%s\",\"rejoin\":%u},"
                     "\"ieee\":\"%s\"}",
                     (unsigned)p->short_addr, p->short_addr, ieee_str,
                     (unsigned)p->rejoin, ieee_str);
            ha_mqtt_emit_raw(line);

            /* Endgültiges Leave (rejoin=0): Eintrag aus der Tabelle und aus
             * der NVS-Kopie entfernen. Bei rejoin=1 bleibt er stehen – das
             * Gerät kommt mit neuer Kurzadresse zurück (ANNCE migriert). */
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
            ESP_LOGW(TAG, "Device-Update 0x%04hx status=%u",
                     p->short_addr, (unsigned)p->status);
            char line[112];
            snprintf(line, sizeof(line),
                     "{\"t\":\"zb_signal\",\"sig\":\"DEVICE_UPDATE\",\"addr\":\"0x%04x\",\"status\":%u}",
                     p->short_addr, (unsigned)p->status);
            ha_mqtt_emit_raw(line);
        }
        break;
    }

    /* Autorisierung durch Trust Center – type/status sind die Schlüssel-
     * werte für die Leave-Ursache (0x01=TCLK, 0x02=SE-CBKE; status
     * 0x00=ok, 0x01=timeout, 0x02=failed). Danach startet der Keep-Alive-
     * Test (12 Runden Read-Attribute à 5 s). */
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
            ESP_LOGW(TAG, "Device authorized 0x%04x type=%u status=%u",
                     p->short_addr, (unsigned)p->authorization_type,
                     (unsigned)p->authorization_status);
            if (p->authorization_status == 0 && s_iv.ep_count > 0) {
                keepalive_start(p->short_addr, s_iv.eps[0]);
            }
        }
        break;
    }

    default: {
        /* Alle übrigen Signale als JSON-Zeile ausgeben – so ist die komplette
         * Signal-Kette (inkl. der Sekunden vor einem Leave) im Add-on-Log
         * sichtbar. ESP_LOGD allein wäre unsichtbar (anderer UART). */
        const char *name = esp_zb_zdo_signal_to_string(sig);
        char line[128];
        snprintf(line, sizeof(line),
                 "{\"t\":\"zb_signal\",\"sig\":\"%s\",\"id\":\"0x%02x\",\"err\":\"0x%x\"}",
                 name ? name : "?", (unsigned)sig, (unsigned)err);
        ha_mqtt_emit_raw(line);
        ESP_LOGD(TAG, "ZB-Signal: %s (0x%x) %s",
                 name ? name : "?", (unsigned)sig, esp_err_to_name(err));
        break;
    }
    }
}

/* ── Gespeicherten Kanal aus NVS lesen (Default: ZB_DEFAULT_CHANNEL) ─────── */
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
    ESP_LOGI(TAG, "Zigbee primär Kanal %d, Fallback Kanal %d", ch, fallback);

    /* Tabellen dimensionieren – zwingend VOR esp_zb_init(). */
    esp_err_t siz = esp_zb_overall_network_size_set(MAX_DEVICES);
    ESP_LOGI(TAG, "Netzgröße %d Geräte (overall_network_size_set=%s)",
             MAX_DEVICES, esp_err_to_name(siz));

    esp_zb_cfg_t cfg = {
        .esp_zb_role        = ESP_ZB_DEVICE_TYPE_COORDINATOR,
        .install_code_policy = false,
        .nwk_cfg.zczr_cfg   = { .max_children = MAX_DEVICES },
    };
    esp_zb_init(&cfg);
    esp_zb_set_tx_power(20);   /* max TX-Power: 20 dBm */

    /* NVRAM NIE beim Start löschen: dort liegen Netzwerkschlüssel, PAN,
     * Kanal und die Adresstabelle. Genau das hält die Pairings über einen
     * Neustart – wer hier true setzt, verliert bei jedem Boot das Netz. */
    esp_zb_nvram_erase_at_start(false);

    /* Eigene Gerätetabelle aus der NVS-Kopie ergänzen, damit LQI-Snapshot
     * und Keep-Alive die Geräte schon vor ihrem ersten Announce kennen. */
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

    /* Temperature + On/Off als Client registrieren → empfängt Attribute-Reports */
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

    /* On/Off zusätzlich als SERVER registrieren: so landen On/Off-Befehle
     * gebundener Schalter (Tastendruck) als ESP_ZB_CORE_SET_ATTR_VALUE_CB_ID
     * bei uns – gleicher Pfad wie im HA-On/Off-Light-Beispiel. Die
     * Client-Rolle bleibt für Attribut-Reports von Server-Geräten erhalten. */
    esp_zb_on_off_cluster_cfg_t on_off_srv_cfg = { .on_off = false };
    esp_zb_cluster_list_add_on_off_cluster(cl,
        esp_zb_on_off_cluster_create(&on_off_srv_cfg),
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

    /* IAS Zone (0x0500) – PIR/Tür/Rauch-Sensoren */
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
    /* SDK ≥1.2.0: Endpunkt-Konfiguration als Struct statt Einzelparameter */
    esp_zb_endpoint_config_t ep_cfg = {
        .endpoint           = ZB_ENDPOINT,
        .app_profile_id     = ESP_ZB_AF_HA_PROFILE_ID,
        .app_device_id      = ESP_ZB_HA_ON_OFF_SWITCH_DEVICE_ID,
        .app_device_version = 0,
    };
    esp_zb_ep_list_add_ep(ep, cl, ep_cfg);

    /* EP 0x0B (11) – Spiegelpunkt für Feibit FB56/ZSW1x (3A Nue HGZB-043):
     * diese Schalter senden ihre On/Off-Attribut-Reports an die
     * Coordinator-EP 0x0B (Ziel-Endpoint aus der Fremd-Gateway-Konfiguration,
     * z. B. Xiaomi). Ohne diesen Endpunkt verwirft die ZCL-Schicht die
     * Reports (beobachtet: dep=11, keine Callbacks).
     * WICHTIG: Die Reports kommen mit Frame-Control 0x08 = direction
     * to-client → ZBOSS sucht die CLIENT-Rolle. Deshalb beide Rollen
     * registrieren (wie bei EP1). */
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
    esp_zb_stack_main_loop();   /* SDK ≥1.5.0: ersetzt esp_zb_main_loop_iteration() */
}

/* ── Öffentliche API ────────────────────────────────────────────────────── */
void zb_gateway_start(void) {
    memset(s_devices, 0, sizeof(s_devices));
    s_dev_mutex = xSemaphoreCreateMutex();

    esp_zb_platform_config_t pcfg = {
        /* SDK ≥1.3.0: Enums mit ZB_-Präfix (vorher RADIO_MODE_NATIVE) */
        .radio_config = { .radio_mode = ZB_RADIO_MODE_NATIVE },
        .host_config  = { .host_connection_mode = ZB_HOST_CONNECTION_MODE_NONE },
    };
    ESP_ERROR_CHECK(esp_zb_platform_config(&pcfg));
    xTaskCreate(zb_task, "zb_main", 8192, NULL, 5, NULL);
}

void zb_gateway_permit_join(uint8_t seconds) {
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
        ESP_LOGE(TAG, "Ungültiger Kanal: %d (erlaubt: 11-26)", ch);
        return;
    }

    /* Kanal in eigenem NVS-Namespace speichern */
    nvs_handle_t h;
    if (nvs_open(ZB_NVS_NAMESPACE, NVS_READWRITE, &h) == ESP_OK) {
        nvs_set_u8(h, ZB_NVS_KEY_CHANNEL, ch);
        nvs_commit(h);
        nvs_close(h);
        ESP_LOGI(TAG, "Kanal %d in NVS gespeichert", ch);
    } else {
        ESP_LOGE(TAG, "NVS-Schreibfehler");
        return;
    }

    /* Zigbee FAT-Storage löschen → nächster Boot bildet neues Netz */
    const esp_partition_t *part = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, "zb_storage");
    if (part) {
        esp_err_t err = esp_partition_erase_range(part, 0, part->size);
        ESP_LOGI(TAG, "zb_storage gelöscht: %s", esp_err_to_name(err));
    } else {
        ESP_LOGW(TAG, "Partition zb_storage nicht gefunden");
    }

    ESP_LOGI(TAG, "Neustart für Kanalwechsel auf %d...", ch);
    vTaskDelay(pdMS_TO_TICKS(200));
    esp_restart();
}


/* ── APS-Data-Indication: LQI je Device tracken ────────────────────────── */
static bool aps_data_ind_cb(esp_zb_apsde_data_ind_t ind) {
    int8_t rssi = esp_ieee802154_get_recent_rssi();
    xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
    zb_device_t *d = find_or_add(ind.src_short_addr, NULL);
    if (d) {
        d->lqi  = (uint8_t)ind.lqi;
        d->rssi = rssi;
        s_last_rssi = rssi;
    }
    xSemaphoreGive(s_dev_mutex);

    /* Quell-Adresse/Endpoint des letzten Frames merken – die
     * SET_ATTR_VALUE-Message (Tastenbefehl) führt keine Quell-Adresse. */
    s_last_aps.addr    = ind.src_short_addr;
    s_last_aps.ep      = ind.src_endpoint;
    s_last_aps.cluster = ind.cluster_id;

    /* Diagnose: jeden eingehenden APS-Frame roh mitschneiden.
     * ASDU = ZCL-Frame (Frame-Control, TSN, Command-ID, …) – damit ist
     * sichtbar, ob Tastenbefehle überhaupt ankommen und in welcher Form. */
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

    /* Noch nicht interviewiert → Interview nachholen (Gerät, das schon vor
     * dem Flash gepairt war und sich nicht neu anmeldet). */
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
            /* IEEE unbekannt (stiller Rejoin) → per ZDO nachträglich holen */
            s_ieee_last_tick = xTaskGetTickCount();
            request_ieee(ind.src_short_addr);
        }
    }
    return false;   /* nicht konsumiert */
}

/* LQI-JSON: [{"addr":"0x0684","lqi":220},...] */
void zb_gateway_lqi_json(char *buf, size_t len) {
    xSemaphoreTake(s_dev_mutex, portMAX_DELAY);
    char *p = buf;
    size_t r = len;
    int n = snprintf(p, r, "[");
    if (n < 0) n = 0;
    if ((size_t)n < r) { p += n; r -= (size_t)n; }
    else               { p += r - 1; r = 1; }
    bool first = true;
    for (int i = 0; i < MAX_DEVICES && r > 4; i++) {
        if (!s_devices[i].used) continue;
        n = snprintf(p, r, "%s{\"addr\":\"0x%04x\",\"lqi\":%u,\"rssi\":%d}",
                     first ? "" : ",",
                     s_devices[i].short_addr,
                     (unsigned)s_devices[i].lqi,
                     (int)s_devices[i].rssi);
        if (n < 0) break;
        if ((size_t)n < r) { p += n; r -= (size_t)n; }
        else               { r = 1; break; }   /* Puffer voll – Rest verwerfen */
        first = false;
    }
    snprintf(p, r, "]");
    xSemaphoreGive(s_dev_mutex);
}

int8_t zb_gateway_get_last_rssi(void) { return s_last_rssi; }

/* LQI-Momentaufnahme auf Anfrage.
 *
 * Im Heartbeat steckt dieselbe Liste, aber nur alle 60 s. Für den Vergleich
 * von Sendeleistungen ist das zu langsam: der Wert gilt nur für den *letzten*
 * empfangenen Frame, und der kann Minuten alt sein. Hier wird bewusst der
 * Zeitstempel mitgeschickt (uptime_ms), damit man „Zustand von vor 3 Minuten"
 * von „gerade eben gemessen" unterscheiden kann.
 *
 * ACHTUNG – beide Puffer MÜSSEN statisch sein:
 * Diese Funktion läuft im uart_rx-Task, und der hat nur 2048 B Stack
 * (ha_mqtt.c: xTaskCreate(rx_task, "uart_rx", 2048, ...)). Ein lokaler
 * char[2048+128] hätte den Stack allein schon überschritten – Absturz beim
 * ersten Aufruf des Befehls. Der Heartbeat-Task darf es sich leisten (8192 B),
 * dieser hier nicht.
 *
 * Größe: MAX_DEVICES Geräte à maximal ~41 Zeichen ("addr","lqi","rssi" +
 * Komma), 48 als runde Obergrenze je Eintrag plus Umschlag. */
#define LQI_JSON_MAX  (MAX_DEVICES * 48 + 128)
#define LQI_LINE_MAX  (LQI_JSON_MAX + 64)
void zb_gateway_report_lqi(void) {
    static char buf[LQI_JSON_MAX];
    static char line[LQI_LINE_MAX];
    zb_gateway_lqi_json(buf, sizeof(buf));
    uint32_t ms = (uint32_t)(esp_timer_get_time() / 1000ULL);
    ESP_LOGI(TAG, "lqi-Snapshot gebaut (%u B, %llu ms)",
             (unsigned)strlen(buf), (unsigned long long)ms);
    snprintf(line, sizeof(line), "{\"t\":\"lqi\",\"ms\":%lu,\"dev\":%s}",
             (unsigned long)ms, buf);
    ha_mqtt_emit_raw(line);
}

/* ── Sendeleistung ──────────────────────────────────────────────────────── */
/* Wunschwert und wirksamer Wert sind nicht dasselbe: esp_zb_set_tx_power()
 * klemmt den Wert intern auf den PHY-Bereich. Deshalb wird nach dem Setzen
 * über esp_ieee802154_get_txpower() zurückgelesen und mitgeschickt – sonst
 * misst man beim Vergleich die Wunschzahl statt der Sendeleistung. */
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
    ESP_LOGI(TAG, "Gepairte Geräte:");
    for (int i = 0; i < MAX_DEVICES; i++) {
        if (s_devices[i].used) {
            ESP_LOGI(TAG, "  [%d] 0x%04x", i, s_devices[i].short_addr);
        }
    }
    xSemaphoreGive(s_dev_mutex);
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

        esp_zb_zdo_active_scan_request(1U << ch, SCAN_DURATION, scan_cb);

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

/* ── Gerätesteuerung von HA/Pi (On/Off-Befehl an einen Endpoint) ───────── */
void zb_gateway_probe_basic(uint16_t addr, uint8_t ep) {
    if (addr == 0x0000 || addr == 0xFFFF) return;
    s_basic_pending_addr = addr;
    s_basic_pending_ep   = ep;
    esp_zb_scheduler_alarm(read_basic_cb, 0, 100);
}

/* WICHTIG: esp_zb_zcl_*_cmd_req() darf nur im ZBOSS-Task-Kontext laufen.
 * Der UART-RX-Task ist ein anderer Task – ein direkter Aufruf von dort wird
 * vom Stack stillschweigend verworfen (kein Fehler, kein Frame on air).
 * Deshalb wird der Request über den ZBOSS-Scheduler eingereiht. */
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


/* ── Node-Descriptor (ZDO 0x0002): Router (ZR) oder End Device (ZED)? ──────
 * Nur Router leiten Fremdverkehr weiter – ein Wandschalter, der als ZED
 * implementiert ist, obwohl er am Netz hängt, taugt NICHT als Repeater.
 * Die MAC-Capability-Flags sind die einzige verlässliche Auskunft darüber:
 *   bit1 = 1 → FFD / Router, bit2 = 1 → Netzbetrieb, bit3 = 1 → RX immer an. */
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


/* ── On/Off aktiv zurücklesen ─────────────────────────────────────────────
 * Das HGZB-043 beantwortet einen Fernbefehl nur mit Default-Response und
 * schickt KEIN Report-Attribut. Lesen geht aber: Read Attributes auf
 * Cluster 0x0006, Attribut 0x0000. Damit bekommt HA den ECHTEN Zustand
 * zurueck, statt sich auf die optimistische Annahme des Gateways zu
 * verlassen (die z. B. daneben liegt, wenn das Geraet den Befehl ablehnt).
 *
 * Mehrere Slots, weil eine HA-Szene schnell zehn Geraete gleichzeitig
 * schaltet: mit nur einer Adresse haette der letzte Aufruf die vorherigen
 * ueberschrieben und die Anfrage waere beim falschen Geraet gelandet.
 *
 * ★ Feste Slots statt Ringpuffer (Bug vom 2026-10-17):
 *   Der Ringpuffer hatte zwei Zeiger (head/tail) in zwei Tasks. Sobald sich
 *   zwei Callbacks ueberschnitten – oder ein esp_zb_scheduler_alarm() im
 *   Callback scheiterte – liefen head und tail auseinander und der Puffer
 *   galt DAUERHAFT als "nicht leer". Jede weitere Anfrage fiel auf einen
 *   vollen Puffer und wurde verworfen: das Zuruecklesen war stillschweigend
 *   tot, bis der C6 neu startete. Von aussen sah es aus wie "Befehl kommt
 *   nicht an" – MQTT zeigte weiter den angenommenen Zustand.
 *   Jetzt bekommt jede Anfrage einen eigenen Slot, dessen Index als Parameter
 *   in den Alarm geht. Kein gemeinsamer Zeiger, nichts kann sich verheddern;
 *   geht ein Alarm verloren, fehlt genau eine Antwort. */
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

    for (uint8_t i = 0; i < RD_SLOTS; i++) {
        if (s_rd_slots[i].busy) continue;
        s_rd_slots[i].addr = addr;
        s_rd_slots[i].ep   = ep;
        s_rd_slots[i].busy = true;
        /* 45 ms Versatz je Slot: die Antworten kommen dann einzeln zurueck
         * und nicht als Burst im Callback. */
        esp_zb_scheduler_alarm(read_onoff_send, i, (uint32_t)(10 + i * 45));
        return;
    }
    ESP_LOGW(TAG, "read_onoff: alle %d Slots belegt – Anfrage verworfen", RD_SLOTS);
}


/* ── switch2wifi-Befehl an alle bekannten Devices senden ────────────────── */
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

/* ── set_sleep-Befehl an alle Devices senden ────────────────────────────── */
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
