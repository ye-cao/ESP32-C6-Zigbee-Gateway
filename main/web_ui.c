#include <string.h>
#include <stdio.h>
#include "esp_log.h"
#include "esp_http_server.h"
#include "esp_timer.h"
#include "ha_mqtt.h"
#include "zb_gateway.h"
#include "mr60bha2.h"
#include "esp_zigbee_core.h"

#define TAG "web_ui"

/* ── Eingebettetes HTML ─────────────────────────────────────────────────── */
static const char HTML[] =
"<!DOCTYPE html><html><head>"
"<meta charset='utf-8'>"
"<meta name='viewport' content='width=device-width,initial-scale=1'>"
"<title>Zigbee Gateway</title>"
"<style>"
"body{font-family:sans-serif;max-width:820px;margin:20px auto;padding:0 16px;background:#f0f2f5}"
"h1{color:#1a237e;margin-bottom:4px}"
"h2{margin:0 0 12px;font-size:1.1em;color:#333}"
".card{background:#fff;border-radius:8px;padding:16px;margin:12px 0;box-shadow:0 1px 4px rgba(0,0,0,.15)}"
".row{display:flex;flex-wrap:wrap;gap:12px}"
".row .card{flex:1;min-width:220px}"
".ok{color:#2e7d32;font-weight:bold}"
".err{color:#c62828;font-weight:bold}"
"table{width:100%;border-collapse:collapse}"
"th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #eee}"
"th{background:#f5f5f5;font-size:.9em;color:#555}"
"button{background:#1565c0;color:#fff;border:none;padding:9px 22px;"
"border-radius:4px;cursor:pointer;font-size:.95em}"
"button:hover{background:#0d47a1}"
".tag{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.85em}"
".tag.ok{background:#e8f5e9;color:#2e7d32}"
".tag.err{background:#ffebee;color:#c62828}"
"#pjst{margin-left:10px;color:#2e7d32;font-weight:bold}"
"</style></head><body>"
"<h1>&#x26A1; ESP32-C6 Zigbee Gateway</h1>"
"<div class='row'>"
" <div class='card'>"
"  <h2>MQTT</h2>"
"  <p>Status: <span id='mqtt'>…</span></p>"
"  <p>Topic: <code id='topic'>…</code></p>"
" </div>"
" <div class='card'>"
"  <h2>System</h2>"
"  <p>Uptime: <span id='uptime'>…</span></p>"
"  <p>Zigbee: <span id='zigbee'>…</span></p>"
" </div>"
" <div class='card'>"
"  <h2>MR60 Sensor</h2>"
"  <p>&#x2764; <span id='bpm'>–</span> bpm</p>"
"  <p>&#x1F4A8; <span id='rpm'>–</span> /min</p>"
"  <p>Status: <span id='mr60st'>–</span></p>"
" </div>"
"</div>"
"<div class='card'>"
" <h2>Zigbee Ger&auml;te (<span id='devcnt'>0</span>)</h2>"
" <table><thead><tr><th>Addr</th><th>IEEE</th></tr></thead>"
" <tbody id='devtbl'><tr><td colspan='2'>Keine Ger&auml;te</td></tr></tbody></table>"
" <p style='margin-top:14px'>"
"  <button onclick='permitJoin()'>Permit Join (180 s)</button>"
"  <span id='pjst'></span>"
" </p>"
"</div>"
"<script>"
"function fmt(s){"
" var h=Math.floor(s/3600),m=Math.floor(s%3600/60),sec=s%60;"
" return h+'h '+('0'+m).slice(-2)+'m '+('0'+sec).slice(-2)+'s';"
"}"
"function poll(){"
" fetch('/api/status').then(r=>r.json()).then(d=>{"
"  var mc=document.getElementById('mqtt');"
"  mc.textContent=d.mqtt?'verbunden':'getrennt';"
"  mc.className='tag '+(d.mqtt?'ok':'err');"
"  document.getElementById('topic').textContent=d.topic||'–';"
"  document.getElementById('uptime').textContent=fmt(d.uptime);"
"  document.getElementById('bpm').textContent=d.bpm>=0?d.bpm:'–';"
"  document.getElementById('rpm').textContent=d.rpm>=0?d.rpm:'–';"
"  var st=['Init','Kalibrierung','Messung'];"
"  document.getElementById('mr60st').textContent=st[d.mr60_status]||d.mr60_status;"
"  document.getElementById('zigbee').textContent="
"   d.pan_id?'PAN 0x'+d.pan_id.toString(16).toUpperCase()+' Kanal '+d.channel:'–';"
" }).catch(()=>{});"
" fetch('/api/devices').then(r=>r.json()).then(d=>{"
"  document.getElementById('devcnt').textContent=d.length;"
"  var tb=document.getElementById('devtbl');"
"  if(!d.length){tb.innerHTML=\"<tr><td colspan='2'>Keine Ger\\u00e4te</td></tr>\";return;}"
"  tb.innerHTML=d.map(function(v){"
"   return '<tr><td>'+v.addr+'</td><td><code>'+v.ieee+'</code></td></tr>';"
"  }).join('');"
" }).catch(()=>{});"
"}"
"function permitJoin(){"
" fetch('/api/permit_join',{method:'POST',body:'180'})"
" .then(()=>{"
"  var s=document.getElementById('pjst');"
"  s.textContent='\\u2713 ge\\u00f6ffnet';"
"  setTimeout(()=>s.textContent='',4000);"
" });"
"}"
"poll();setInterval(poll,3000);"
"</script></body></html>";

/* ── Handler: GET / ─────────────────────────────────────────────────────── */
static esp_err_t handle_root(httpd_req_t *req) {
    httpd_resp_set_type(req, "text/html; charset=utf-8");
    httpd_resp_send(req, HTML, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

/* ── Handler: GET /api/status ───────────────────────────────────────────── */
static esp_err_t handle_status(httpd_req_t *req) {
    mr60_data_t m = {0};
    mr60bha2_get(&m);

    uint16_t pan  = esp_zb_get_pan_id();
    uint8_t  chan = esp_zb_get_current_channel();
    uint64_t uptime_s = (uint64_t)(esp_timer_get_time() / 1000000ULL);

    char buf[256];
    snprintf(buf, sizeof(buf),
        "{\"mqtt\":%s,\"topic\":\"%s\","
        "\"uptime\":%llu,"
        "\"bpm\":%d,\"rpm\":%d,\"mr60_status\":%u,"
        "\"pan_id\":%u,\"channel\":%u}",
        ha_mqtt_connected() ? "true" : "false",
        ha_mqtt_base_topic(),
        uptime_s,
        m.bpm, m.rpm, (unsigned)m.status,
        (unsigned)pan, (unsigned)chan);

    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_send(req, buf, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

/* ── Handler: GET /api/devices ──────────────────────────────────────────── */
static esp_err_t handle_devices(httpd_req_t *req) {
    char buf[1024];
    zb_gateway_devices_json(buf, sizeof(buf));

    httpd_resp_set_type(req, "application/json");
    httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
    httpd_resp_send(req, buf, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

/* ── Handler: POST /api/permit_join ─────────────────────────────────────── */
static esp_err_t handle_permit_join(httpd_req_t *req) {
    char body[8] = "180";
    int len = req->content_len;
    if (len > 0 && len < (int)sizeof(body)) {
        httpd_req_recv(req, body, len);
        body[len] = '\0';
    }
    uint8_t secs = (uint8_t)atoi(body);
    if (secs == 0) secs = 180;
    ESP_LOGI(TAG, "开放入网 %u s（Web UI）", secs);
    zb_gateway_permit_join(secs);

    httpd_resp_set_type(req, "application/json");
    httpd_resp_send(req, "{\"ok\":true}", HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

/* ── Server starten ─────────────────────────────────────────────────────── */
void web_ui_start(void) {
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port = 80;

    httpd_handle_t server = NULL;
    if (httpd_start(&server, &cfg) != ESP_OK) {
        ESP_LOGE(TAG, "HTTP 服务启动失败");
        return;
    }

    static const httpd_uri_t routes[] = {
        { .uri = "/",                .method = HTTP_GET,  .handler = handle_root        },
        { .uri = "/api/status",      .method = HTTP_GET,  .handler = handle_status      },
        { .uri = "/api/devices",     .method = HTTP_GET,  .handler = handle_devices     },
        { .uri = "/api/permit_join", .method = HTTP_POST, .handler = handle_permit_join },
    };
    for (int i = 0; i < 4; i++)
        httpd_register_uri_handler(server, &routes[i]);

    ESP_LOGI(TAG, "Web-UI gestartet auf http://Port 80");
}
