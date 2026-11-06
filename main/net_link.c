/*
 * net_link.c —— Wi-Fi STA + TCP 客户端：给「不插 USB」用的第二条传输通道
 *
 * ════════════════════════════════════════════════════════════════════════
 * 为什么可以只换传输层
 * ════════════════════════════════════════════════════════════════════════
 * 这个项目的协议边界是「一行一条 JSON」，序列化与反序列化分别落在
 * ha_mqtt.c 的 emit() / rx_task() 里。两者都不关心字节从哪个 fd 来：
 *   - 发送：emit() → 队列 → tx_task → stdout（USB-CDC）
 *   - 接收：rx_task → fgets(stdin) → 一整串 if-else → on_uart_cmd
 * 所以只要把「stdout」和「stdin」换成 socket，on_uart_cmd 与整个
 * zb_gateway.c（2000 行）一行都不用改。
 *
 * ════════════════════════════════════════════════════════════════════════
 * 为什么默认仍然是串口
 * ════════════════════════════════════════════════════════════════════════
 * 传输通道存在 NVS（命名空间 netlink）里，默认 0 = 串口。
 * 这是刻意的：Wi-Fi 配置写错、路由器换了、AP 加密方式变了……任何一种情况下
 * 板子都还能插回 USB，用一条 {"cmd":"net_cfg",…} 把配置改回来。
 * 如果默认走 Wi-Fi，一次写错就得重新 USB 刷机。
 *
 * ════════════════════════════════════════════════════════════════════════
 * ★★★ 与 ZBOSS 大锁的关系
 * ════════════════════════════════════════════════════════════════════════
 * 收包路径 net_task → ha_mqtt_handle_line() → on_uart_cmd() → zb_gateway_*。
 * 这和原来的 uart_rx 任务是完全一样的调用链，**同样必须靠 on_uart_cmd 里的
 * zb_lock() 挡住**（见 main.c 顶部那一大段注释）。这里不需要额外加锁 ——
 * 也不能加，否则 ota_data 那条免锁路径会被堵死，OTA 立刻卡住。
 *
 * ════════════════════════════════════════════════════════════════════════
 * ★ 射频共存
 * ════════════════════════════════════════════════════════════════════════
 * ESP32-C6 只有一个 2.4 GHz 射频，Wi-Fi 与 802.15.4 靠软件时分复用
 * （CONFIG_ESP_COEX_SW_COEXIST_ENABLE）。选信道时让 Zigbee 与 Wi-Fi 错开：
 *   Zigbee 25 (2475 MHz)  ↔  Wi-Fi 1 或 6
 * 千万别用 Zigbee 20 —— 它正好压在 Wi-Fi 11 上，两个都会掉包。
 */
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"

#include "esp_log.h"
#include "esp_err.h"
#include "nvs.h"
#include "esp_netif.h"
#include "esp_event.h"
#include "esp_wifi.h"

#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "lwip/inet.h"

#include "net_link.h"
#include "ha_mqtt.h"

#define TAG "netlink"

#define NVS_NS       "netlink"
#define MAX_LINE     4096          /* 与 ha_mqtt.c 的 BUF_SIZE 一致 */
#define RX_CHUNK     512

/* 事件位 */
#define BIT_IP       BIT0          /* 已关联且拿到 IP */
#define BIT_STARTED  BIT1          /* Wi-Fi 已 start（可 connect） */

static SemaphoreHandle_t s_lock;   /* 保护 s_sock / s_tcp_up */
static EventGroupHandle_t s_ev;

static volatile bool     s_tcp_up = false;
static int               s_sock   = -1;
static char              s_ip[20] = "0.0.0.0";
static volatile uint32_t s_cfg_gen = 0;   /* 配置代次：变了就重新下发 */

/* 运行期状态，只用于状态回执 */
static volatile bool     s_wifi_started = false;
static volatile bool     s_wifi_inited  = false;
static bool              s_netif_created = false;
static bool              s_task_started  = false;
static bool              s_handlers_registered = false;

/* ★ 上一次被 AP/驱动踢掉的原因码（wifi_err_reason_t）。
 *   为什么非要有这个：ESP_LOGI 在本固件里被 null_vprintf 静默了，串口上看不到
 *   「Wi-Fi 断开，原因=xx」这类日志；而没有这个码，"连不上"就只剩猜 ——
 *   密码错(15)、找不到AP(201)、认证方式不匹配(还是 15) 三种情况在
 *   net_status 里表现完全一样（ip=0.0.0.0、rssi=0）。2026-11-06 就因为看不出来，
 *   差点去怀疑信号强度和 AP 距离，实际是纯 WPA3 网络没开 SAE。 */
static volatile int      s_last_reason = 0;
/* ★★★ s_task_started 必须是**文件级**的，而且 net_link_start 和
 *     net_link_configure 共用它。第一版把开关做成了 net_link_configure 里的
 *     函数级 static，于是「开机 tr=1 已经建过任务」之后再发一条 net_cfg，
 *     会再建**第二个** net_task：两个任务同时 esp_wifi_connect、同时
 *     tcp_connect、抢同一个 s_sock，收包循环互相踩。这种双任务问题在日志里
 *     表现为连接莫名其妙每几秒重建一次，极难定位。 */

typedef struct {
    char     ssid[33];
    char     pass[65];
    char     host[64];
    uint16_t port;
    uint8_t  transport;        /* NET_LINK_UART / NET_LINK_TCP */
} net_cfg_t;

static net_cfg_t s_cfg;

/* ── NVS ──────────────────────────────────────────────────────────────── */
static void cfg_defaults(void) {
    memset(&s_cfg, 0, sizeof(s_cfg));
    s_cfg.transport = NET_LINK_UART;   /* ★ 默认串口，见文件头 */
    s_cfg.port      = 12345;
}

static void cfg_load(void) {
    cfg_defaults();
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) {
        ESP_LOGI(TAG, "NVS 里还没有 %s，按默认值（串口）运行", NVS_NS);
        return;
    }
    size_t n;
    n = sizeof(s_cfg.ssid);
    if (nvs_get_str(h, "ssid", s_cfg.ssid, &n) != ESP_OK) s_cfg.ssid[0] = 0;
    n = sizeof(s_cfg.pass);
    if (nvs_get_str(h, "pass", s_cfg.pass, &n) != ESP_OK) s_cfg.pass[0] = 0;
    n = sizeof(s_cfg.host);
    if (nvs_get_str(h, "host", s_cfg.host, &n) != ESP_OK) s_cfg.host[0] = 0;
    uint16_t p = 0;
    if (nvs_get_u16(h, "port", &p) == ESP_OK && p) s_cfg.port = p;
    uint8_t t = 0;
    if (nvs_get_u8(h, "tr", &t) == ESP_OK) s_cfg.transport = t;
    nvs_close(h);

    ESP_LOGI(TAG, "配置：通道=%s ssid='%s' host='%s:%u'%s",
             s_cfg.transport == NET_LINK_TCP ? "Wi-Fi" : "串口",
             s_cfg.ssid, s_cfg.host, (unsigned)s_cfg.port,
             s_cfg.pass[0] ? "（有密码）" : "（无密码）");
}

static esp_err_t cfg_save(void) {
    nvs_handle_t h;
    esp_err_t r = nvs_open(NVS_NS, NVS_READWRITE, &h);
    if (r != ESP_OK) return r;
    nvs_set_str(h, "ssid", s_cfg.ssid);
    nvs_set_str(h, "pass", s_cfg.pass);
    nvs_set_str(h, "host", s_cfg.host);
    nvs_set_u16(h, "port", s_cfg.port);
    nvs_set_u8 (h, "tr",   s_cfg.transport);
    r = nvs_commit(h);
    nvs_close(h);
    return r;
}

/* ── Wi-Fi 事件 ───────────────────────────────────────────────────────── */
/* 事件组 / 互斥量：谁先用到谁建，但**只建一次**。
 *
 * ★★★ 2026-11-06 血的教训：原来只有 net_task_spawn() 里建这两个东西。
 *   而串口模式（tr=0）下 net_task 根本不会创建，于是 s_ev 是 NULL；
 *   一旦有人在这时懒加载拉起 Wi-Fi（比如发一条 wifi_scan 做诊断），
 *   WIFI_EVENT_STA_START 回调里的 xEventGroupSetBits(NULL, …) 会直接
 *   assert 崩溃并重启：
 *       assert failed: xEventGroupSetBits event_groups.c:561 (xEventGroup)
 *   表现是"命令发出去没有任何回执" —— 看起来像命令没被识别，实际设备重启了。
 *   所以凡是会拉起 Wi-Fi 的路径，都必须先走这里。 */
static esp_err_t net_sync_ensure(void) {
    if (!s_ev)   s_ev   = xEventGroupCreate();
    if (!s_lock) s_lock = xSemaphoreCreateMutex();
    return (s_ev && s_lock) ? ESP_OK : ESP_ERR_NO_MEM;
}

static void on_wifi_event(void *arg, esp_event_base_t base,
                          int32_t id, void *data) {
    (void)arg;
    /* ★ s_ev 有可能还没建起来（见 net_sync_ensure 的说明）。
     *   回调里绝不能对 NULL 调 xEventGroupSetBits —— assert 一响就是重启。 */
    if (!s_ev) return;
    if (base == WIFI_EVENT) {
        switch (id) {
        case WIFI_EVENT_STA_START:
            ESP_LOGI(TAG, "STA 已启动，开始连接 '%s'", s_cfg.ssid);
            xEventGroupSetBits(s_ev, BIT_STARTED);
            /* 真正的连接待会由 net_task 发起，避免两个地方同时 connect */
            break;
        case WIFI_EVENT_STA_CONNECTED:
            ESP_LOGI(TAG, "已关联 AP，等 DHCP");
            s_last_reason = 0;
            break;
        case WIFI_EVENT_STA_DISCONNECTED: {
            const wifi_event_sta_disconnected_t *d =
                (const wifi_event_sta_disconnected_t *)data;
            s_last_reason = d ? d->reason : -1;
            ESP_LOGW(TAG, "Wi-Fi 断开，原因码 %d，等 net_task 重连", s_last_reason);
            xEventGroupClearBits(s_ev, BIT_IP);
            break;
        }
        default:
            break;
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
        snprintf(s_ip, sizeof(s_ip), IPSTR, IP2STR(&e->ip_info.ip));
        ESP_LOGI(TAG, "拿到 IP：%s", s_ip);
        xEventGroupSetBits(s_ev, BIT_IP);
    }
}

/* ── Wi-Fi 初始化（幂等）────────────────────────────────────────────── */
static esp_err_t wifi_begin(void) {
    /* ★ 先把事件组/锁备好：esp_wifi_start() 之后马上就会有 STA_START 回调，
     *   那时 s_ev 必须已经存在（见 net_sync_ensure 的说明）。 */
    esp_err_t er = net_sync_ensure();
    if (er != ESP_OK) {
        ESP_LOGE(TAG, "事件组/互斥量创建失败");
        return er;
    }

    /* 这两个在别的组件里可能已经建过，重复调用返回 INVALID_STATE —— 不算错 */
    esp_err_t r = esp_netif_init();
    if (r != ESP_OK && r != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "esp_netif_init 失败：%s", esp_err_to_name(r));
        return r;
    }
    r = esp_event_loop_create_default();
    if (r != ESP_OK && r != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "事件循环创建失败：%s", esp_err_to_name(r));
        return r;
    }
    /* ★ netif 的创建必须放在 s_wifi_inited 的早退之后**之前**补齐。
     *   原因：net_link_scan_early() 只做扫频需要的初始化（不建 netif），
     *   然后把 s_wifi_inited 置位。如果这里仍然"一进来就 if (s_wifi_inited)
     *   return"，后面 net_task 就永远拿不到 STA netif ⇒ DHCP 永远失败，
     *   表现成"关联上了但没有 IP"，而且一次比一次难查。
     *   现在改成：netif 单独用 s_netif_created 保证，wifi_init 用它自己的标记。 */
    if (!s_netif_created) {
        if (!esp_netif_create_default_wifi_sta()) {
            ESP_LOGE(TAG, "创建默认 STA netif 失败");
            return ESP_FAIL;
        }
        s_netif_created = true;
    }
    if (s_wifi_inited) return ESP_OK;

    wifi_init_config_t ic = WIFI_INIT_CONFIG_DEFAULT();
    r = esp_wifi_init(&ic);
    if (r != ESP_OK) {
        ESP_LOGE(TAG, "esp_wifi_init 失败：%s", esp_err_to_name(r));
        return r;
    }
    if (!s_handlers_registered) {
        esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                           on_wifi_event, NULL, NULL);
        esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                           on_wifi_event, NULL, NULL);
        s_handlers_registered = true;
    }

    r = esp_wifi_set_storage(WIFI_STORAGE_RAM);   /* 参数由我们自己管，别让驱动也存一份 */
    if (r != ESP_OK) ESP_LOGW(TAG, "set_storage 失败：%s", esp_err_to_name(r));

    r = esp_wifi_set_mode(WIFI_MODE_STA);
    if (r != ESP_OK) {
        ESP_LOGE(TAG, "set_mode 失败：%s", esp_err_to_name(r));
        return r;
    }
    s_wifi_inited = true;
    return ESP_OK;
}

/* 下发当前 s_cfg 到 Wi-Fi 驱动 */
static void wifi_apply_cfg(void) {
    wifi_config_t wc;
    memset(&wc, 0, sizeof(wc));
    strncpy((char *)wc.sta.ssid,     s_cfg.ssid, sizeof(wc.sta.ssid) - 1);
    strncpy((char *)wc.sta.password, s_cfg.pass, sizeof(wc.sta.password) - 1);
    /* ★★★ 认证方式必须显式打开 WPA3-SAE，不能只写 WPA2_PSK。
     *
     *   原来的写法是「有密码就 WIFI_AUTH_WPA2_PSK」，对普通 WPA2 路由器没
     *   问题，但遇上**纯 WPA3-个人（SAE）**的 AP 就永远关联不上 —— 不报错，
     *   只是反复 STA_DISCONNECTED，net_status 里一直是 ip=0.0.0.0 / rssi=0，
     *   看上去和"密码错了"、"信号太弱"完全一样。
     *   2026-11-06 实测：家里的 'yecao' 就是 WPA3-个人（netsh 里显示
     *   「身份验证: WPA3 - 个人」），改成 WPA2_WPA3_PSK 之后一次连通。
     *
     *   WPA2_WPA3_PSK = 「WPA2 与 WPA3 都接受」，比单选 WPA3_PSK 更保险：
     *   同一个 SSID 换个只支持 WPA2 的老路由器也还能用。
     *
     *   空密码 ⇒ 开放网络，这条保持原样（否则连开放 AP 会一直 AUTH_EXPIRE）。
     */
    wc.sta.threshold.authmode = s_cfg.pass[0] ? WIFI_AUTH_WPA2_WPA3_PSK
                                              : WIFI_AUTH_OPEN;
    /* PMF（管理帧保护）是 WPA3 的硬性要求。capable=true / required=false
     * 表示"能谈就谈"，WPA2 的 AP 也不会因此被拒。 */
    wc.sta.pmf_cfg.capable  = true;
    wc.sta.pmf_cfg.required = false;
    /* ★ SAE 的 PWE 推导有两种：H2E（哈希到元素）和 hunt-and-peck（老办法）。
     *   IDF 默认 UNSPECIFIED，会按 AP 的 RSNXE 自己选；但实测这个 AP 的
     *   5 GHz BSSID 明确标注「不支持 H2E」，自动选可能挑错边。
     *   写成 BOTH 就是两种都试，兼容性最好。 */
    wc.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;

    esp_err_t r = esp_wifi_set_config(WIFI_IF_STA, &wc);
    if (r != ESP_OK) ESP_LOGE(TAG, "set_config 失败：%s", esp_err_to_name(r));
}

/* ── TCP ──────────────────────────────────────────────────────────────── */
static int tcp_connect(const char *host, uint16_t port) {
    struct sockaddr_in sa;
    memset(&sa, 0, sizeof(sa));
    sa.sin_family = AF_INET;
    sa.sin_port   = htons(port);

    /* 允许直接填主机名（add-on 的容器名之类），解析失败再当点分十进制试 */
    if (inet_pton(AF_INET, host, &sa.sin_addr) != 1) {
        struct addrinfo hints, *res = NULL;
        memset(&hints, 0, sizeof(hints));
        hints.ai_family   = AF_INET;
        hints.ai_socktype = SOCK_STREAM;
        if (getaddrinfo(host, NULL, &hints, &res) != 0 || !res) {
            ESP_LOGW(TAG, "解析不了主机名 '%s'", host);
            return -1;
        }
        sa.sin_addr = ((struct sockaddr_in *)res->ai_addr)->sin_addr;
        freeaddrinfo(res);
    }

    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        ESP_LOGW(TAG, "socket() 失败：errno=%d", errno);
        return -1;
    }

    /* 非阻塞 connect + select 超时：阻塞式 connect 在目标不在线时要等
     * ARP 超时（几十秒），期间整个 net_task 卡住，连重连都做不了。 */
    int fl = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, fl | O_NONBLOCK);

    int rc = connect(fd, (struct sockaddr *)&sa, sizeof(sa));
    if (rc < 0 && errno != EINPROGRESS) {
        close(fd);
        return -1;
    }
    if (rc < 0) {
        fd_set wf;
        FD_ZERO(&wf);
        FD_SET(fd, &wf);
        struct timeval tv = { .tv_sec = 5, .tv_usec = 0 };
        rc = select(fd + 1, NULL, &wf, NULL, &tv);
        if (rc <= 0) {
            close(fd);
            return -1;
        }
        int err = 0;
        socklen_t len = sizeof(err);
        getsockopt(fd, SOL_SOCKET, SO_ERROR, &err, &len);
        if (err) {
            close(fd);
            return -1;
        }
    }

    /* 恢复阻塞；但给写操作 200 ms 上限 —— tx_task 里绝不能无限等 */
    fcntl(fd, F_SETFL, fl & ~O_NONBLOCK);
    struct timeval snd = { .tv_sec = 0, .tv_usec = 200000 };
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &snd, sizeof(snd));
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY,  &one, sizeof(one));
    setsockopt(fd, SOL_SOCKET,  SO_KEEPALIVE, &one, sizeof(one));
    return fd;
}

/* 把一段字节喂进行缓冲，遇到 '\n' 就交给 ha_mqtt_handle_line()。
 * ★ 这里不做任何 JSON 解析 —— 解析逻辑在 ha_mqtt.c，串口和 TCP 共用同一份，
 *   否则「串口能用的命令、Wi-Fi 不能用」这类偏差迟早出现（历史上
 *   node_desc / fw_ota_data / dev_remove 已经因为「两处只改一处」死过三次）。 */
static void feed_bytes(const char *data, int len, char *acc, size_t *acc_len) {
    for (int i = 0; i < len; i++) {
        char c = data[i];
        if (c == '\n') {
            while (*acc_len > 0 &&
                   (acc[*acc_len - 1] == '\r')) {
                acc[--(*acc_len)] = '\0';
            }
            acc[*acc_len] = '\0';
            if (*acc_len) ha_mqtt_handle_line(acc);
            *acc_len = 0;
        } else if (*acc_len < MAX_LINE - 1) {
            acc[(*acc_len)++] = c;
        } else {
            *acc_len = 0;   /* 溢出：整行丢弃，等下一个 '\n' 重新对齐 */
        }
    }
}

static void tcp_rx_loop(int fd) {
    static char acc[MAX_LINE];
    char  buf[RX_CHUNK];
    size_t acc_len = 0;

    while (1) {
        fd_set rf;
        FD_ZERO(&rf);
        FD_SET(fd, &rf);
        struct timeval tv = { .tv_sec = 1, .tv_usec = 0 };
        int rc = select(fd + 1, &rf, NULL, NULL, &tv);
        if (rc == 0) continue;                 /* 超时：回去再看一眼连接状态 */
        if (rc < 0) {
            if (errno == EINTR) continue;
            return;
        }
        int n = recv(fd, buf, sizeof(buf), 0);
        if (n == 0) return;                    /* 对端关闭 */
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR) continue;
            return;
        }
        feed_bytes(buf, n, acc, &acc_len);
    }
}

/* ── 状态广播 ─────────────────────────────────────────────────────────── */
static void announce(bool up) {
    char line[224];
    int8_t rssi = 0;
    if (s_wifi_started) {
        wifi_ap_record_t ap;
        if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) rssi = ap.rssi;
    }
    snprintf(line, sizeof(line),
        "{\"t\":\"net\",\"up\":%s,\"ip\":\"%s\",\"rssi\":%d,\"host\":\"%s\",\"port\":%u}",
        up ? "true" : "false", s_ip, (int)rssi, s_cfg.host, (unsigned)s_cfg.port);
    ha_mqtt_emit_raw(line);
}

/* ── net_task ─────────────────────────────────────────────────────────── */
static void net_task(void *arg) {
    (void)arg;
    /* ★★ 初值故意设成 0xFFFFFFFF 而不是 0。
     *    如果写 0，而 s_cfg_gen 开机时也是 0，第一次进循环时
     *    「配置变了」的判断就是假的 ⇒ wifi_apply_cfg() 被跳过 ⇒
     *    esp_wifi_connect() 拿着**空 SSID** 去连，永远连不上，
     *    日志还只会嘟囔一句 AUTH_EXPIRE，看起来像密码错。 */
    uint32_t applied = 0xFFFFFFFFu;
    uint32_t backoff = 2000;

    while (1) {
        /* ── 1. 起 Wi-Fi（只做一次）── */
        if (!s_wifi_started) {
            if (wifi_begin() != ESP_OK) {
                ESP_LOGE(TAG, "Wi-Fi 初始化失败，10 s 后重试");
                vTaskDelay(pdMS_TO_TICKS(10000));
                continue;
            }
            /* ★ 配置必须在 esp_wifi_start() **之前**下发，理由见上面 applied 的注释 */
            wifi_apply_cfg();
            applied = s_cfg_gen;

            esp_err_t r = esp_wifi_start();
            if (r != ESP_OK && r != ESP_ERR_WIFI_INIT_STATE) {
                ESP_LOGE(TAG, "esp_wifi_start 失败：%s", esp_err_to_name(r));
                vTaskDelay(pdMS_TO_TICKS(5000));
                continue;
            }
            s_wifi_started = true;
            xEventGroupWaitBits(s_ev, BIT_STARTED, pdFALSE, pdTRUE,
                                pdMS_TO_TICKS(5000));
        } else if (applied != s_cfg_gen) {
            /* ── 2. 运行期改过配置（net_cfg）⇒ 重新下发 ── */
            ESP_LOGI(TAG, "配置已变更，重新下发 Wi-Fi 参数");
            wifi_apply_cfg();
            applied = s_cfg_gen;
            esp_wifi_disconnect();
            vTaskDelay(pdMS_TO_TICKS(300));
        }

        /* ── 3. 发起连接（只在还没有 IP 时；驱动自己会重试，我们负责叫醒它）── */
        if (!(xEventGroupGetBits(s_ev) & BIT_IP)) {
            ESP_LOGI(TAG, "连接 Wi-Fi '%s' …", s_cfg.ssid);
            esp_err_t cr = esp_wifi_connect();
            /* 已经连着的时候再 connect 会返回 ESP_ERR_WIFI_CONN，不是错 */
            if (cr != ESP_OK && cr != ESP_ERR_WIFI_CONN) {
                ESP_LOGW(TAG, "esp_wifi_connect：%s", esp_err_to_name(cr));
            }
        }

        /* ── 4. 等关联 + DHCP ── */
        EventBits_t b = xEventGroupWaitBits(s_ev, BIT_IP, pdFALSE, pdTRUE,
                                            pdMS_TO_TICKS(30000));
        if (!(b & BIT_IP)) {
            ESP_LOGW(TAG, "30 s 内没拿到 IP —— 检查 SSID/密码/信号，重试");
            esp_wifi_disconnect();
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }

        /* ── 5. 连 TCP ── */
        int fd = tcp_connect(s_cfg.host, s_cfg.port);
        if (fd < 0) {
            ESP_LOGW(TAG, "连不上 %s:%u，%lu ms 后重试",
                     s_cfg.host, (unsigned)s_cfg.port, (unsigned long)backoff);
            vTaskDelay(pdMS_TO_TICKS(backoff));
            backoff = backoff < 30000 ? backoff * 2 : 30000;
            continue;
        }

        backoff = 2000;
        xSemaphoreTake(s_lock, portMAX_DELAY);
        s_sock   = fd;
        s_tcp_up = true;
        xSemaphoreGive(s_lock);

        ESP_LOGI(TAG, "TCP 已连上 %s:%u —— 数据通道切换到 Wi-Fi",
                 s_cfg.host, (unsigned)s_cfg.port);

        /* ★★ 补发一条 {"t":"boot"}。
         *    ha_mqtt_init() 里那条 boot 是在 TCP 还没连上时发出的，只能落到
         *    串口回退上；而 add-on 在 link=tcp 时**只读 TCP**，永远看不到它。
         *    后果很实在：网关状态不上线、发射功率不会被重新下发
         *    （固件不持久化 TX 功率，见 zb_gateway 那边的说明）。
         *    每次重连都补发是安全的 —— add-on 的 boot 处理是幂等的。 */
        ha_mqtt_emit_raw("{\"t\":\"boot\"}");
        announce(true);

        tcp_rx_loop(fd);

        xSemaphoreTake(s_lock, portMAX_DELAY);
        if (s_tcp_up) {
            ESP_LOGW(TAG, "TCP 断开，2 s 后重连");
        }
        s_tcp_up = false;
        s_sock   = -1;
        xSemaphoreGive(s_lock);

        shutdown(fd, SHUT_RDWR);
        close(fd);
        announce(false);
        vTaskDelay(pdMS_TO_TICKS(2000));       /* 掐断时不空转重连 */
    }
}

/* ── 对外接口 ─────────────────────────────────────────────────────────── */
/* 建事件组/互斥量并起 net_task。★ 只允许成功一次 —— 见 s_task_started 处的说明。 */
static esp_err_t net_task_spawn(void) {
    if (s_task_started) return ESP_OK;
    if (net_sync_ensure() != ESP_OK) {
        ESP_LOGE(TAG, "事件组/互斥量创建失败");
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreate(net_task, "net_rx", 5120, NULL, 4, NULL) != pdPASS) {
        ESP_LOGE(TAG, "net_task 创建失败");
        return ESP_ERR_NO_MEM;
    }
    s_task_started = true;
    return ESP_OK;
}

bool net_link_ready(void)      { return s_tcp_up; }
uint8_t net_link_transport(void) {
    return s_cfg.transport;
}

esp_err_t net_link_send_line(const char *line) {
    if (!line) return ESP_ERR_INVALID_ARG;
    if (!s_tcp_up) return ESP_ERR_INVALID_STATE;

    xSemaphoreTake(s_lock, portMAX_DELAY);
    esp_err_t r = ESP_FAIL;
    int fd = s_sock;
    if (fd >= 0 && s_tcp_up) {
        size_t len = strlen(line);
        size_t off = 0;
        bool ok = true;
        while (off < len) {
            int n = send(fd, line + off, len - off, 0);
            if (n <= 0) { ok = false; break; }
            off += (size_t)n;
        }
        if (ok && send(fd, "\n", 1, 0) == 1) {
            r = ESP_OK;
        }
        if (r != ESP_OK) {
            /* 写坏了（对端没了 / 主机重启）：立刻 shutdown 把 recv 也唤醒，
             * 让 net_task 走重连流程，而不是干等 60 s 的心跳超时。 */
            s_tcp_up = false;
            shutdown(fd, SHUT_RDWR);
        }
    }
    xSemaphoreGive(s_lock);
    return r;
}

void net_link_kick(void) {
    if (!s_lock) {                 /* 串口模式下根本没有这条通道 */
        s_tcp_up = false;
        return;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    s_tcp_up = false;
    if (s_sock >= 0) shutdown(s_sock, SHUT_RDWR);
    xSemaphoreGive(s_lock);
}

esp_err_t net_link_configure(const char *ssid, const char *pass,
                             const char *host, int port, int transport) {
    if (ssid)  snprintf(s_cfg.ssid, sizeof(s_cfg.ssid), "%s", ssid);
    if (pass)  snprintf(s_cfg.pass, sizeof(s_cfg.pass), "%s", pass);
    if (host)  snprintf(s_cfg.host, sizeof(s_cfg.host), "%s", host);
    if (port > 0 && port <= 65535) s_cfg.port = (uint16_t)port;
    if (transport == NET_LINK_UART || transport == NET_LINK_TCP) {
        s_cfg.transport = (uint8_t)transport;
    }

    esp_err_t r = cfg_save();
    if (r != ESP_OK) ESP_LOGE(TAG, "写 NVS 失败：%s", esp_err_to_name(r));

    s_cfg_gen++;

    if (s_cfg.transport == NET_LINK_TCP) {
        if (!s_cfg.ssid[0] || !s_cfg.host[0]) {
            ESP_LOGW(TAG, "通道=Wi-Fi 但 ssid 或 host 为空，会一直重连");
        }
        /* 已经起过就不会再建（s_task_started 守着）；s_cfg_gen 变了由它自己重下发 */
        esp_err_t tr = net_task_spawn();
        if (tr != ESP_OK) {
            s_cfg.transport = NET_LINK_UART;
            return tr;
        }
    } else {
        net_link_kick();     /* 切回串口：把现有 TCP 掐掉 */
    }

    ESP_LOGI(TAG, "配置已更新：通道=%s ssid='%s' host='%s:%u'",
             s_cfg.transport == NET_LINK_TCP ? "Wi-Fi" : "串口",
             s_cfg.ssid, s_cfg.host, (unsigned)s_cfg.port);
    return r;
}

void net_link_status_json(char *out, size_t out_len) {
    if (!out || !out_len) return;
    int8_t rssi = 0;
    int ch = 0;
    if (s_wifi_started) {
        wifi_ap_record_t ap;
        if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) {
            rssi = ap.rssi;
            ch   = ap.primary;
        }
    }
    /* ★ state 必须跟着 transport 走。
     *   只看 s_wifi_started 会出错：Wi-Fi 只要被启动仪式过（哪怕后来 net_cfg
     *   把 tr 改回 0），它就一直是 true，于是串口模式的设备也回
     *   state="wifi_only" —— 排查时会以为"它正在等 Wi-Fi"，其实是串口在工作。 */
    const char *st = (s_cfg.transport != NET_LINK_TCP) ? "uart"
                   : (!s_wifi_started ? "starting"
                                      : (s_tcp_up ? "tcp_up" : "wifi_only"));
    snprintf(out, out_len,
        "{\"t\":\"net_status\",\"transport\":\"%s\",\"state\":\"%s\","
        "\"ssid\":\"%s\",\"host\":\"%s\",\"port\":%u,\"ip\":\"%s\","
        "\"rssi\":%d,\"ap_ch\":%d,\"why\":%d}",
        s_cfg.transport == NET_LINK_TCP ? "wifi" : "uart", st,
        s_cfg.ssid, s_cfg.host, (unsigned)s_cfg.port, s_ip,
        (int)rssi, ch, s_last_reason);
}

/* ── 扫频：把 C6 自己"看见"了哪些 AP 报给主机 ─────────────────────────────
 *
 * ★ 为什么值得占一块固件代码：判断"为什么连不上"时，主机的扫描结果没有
 *   参考价值 —— 电脑的天线、支持的频段、Windows 的扫描缓存都和 C6 不同。
 *   2026-11-06 就是这样：这一头 netsh 看得见 'yecao'，C6 那头回的是
 *   why=201（NO_AP_FOUND），两边对不上，只能靠猜。让 C6 自己扫一次，
 *   "它看见什么、多强、什么加密"立刻变成事实。
 *
 * 输出：{"t":"wifi_scan","n":N,"aps":[{"s":"ssid","r":-45,"c":1,"a":3},…]}
 *   a = wifi_auth_mode_t：0=OPEN 1=WEP 2=WPA 3=WPA2 4=WPA/WPA2
 *       5=WPA2-ENT 6=WPA3 7=WPA2/WPA3 8=WPA3-EXT
 *
 * 注意：扫频会让 STA 短暂离开当前信道，正在传的数据会顿一下 ——
 * 这是诊断命令，不要放进轮询里。
 */
/* 真正发起扫描并拼 JSON。要求 Wi-Fi 已经 init + start。
 * tag 决定回执里的 "t"，这样"开机早期扫描"和"命令扫描"在日志里能分开看。 */
static esp_err_t scan_format(const char *tag, char *out, size_t out_len) {
    if (!out || out_len < 64) return ESP_ERR_INVALID_ARG;

    wifi_scan_config_t sc;
    memset(&sc, 0, sizeof(sc));
    sc.show_hidden = true;          /* 隐藏 SSID 也要列出来，名字显示为空 */
    /* ★★ 驻留时间必须显式给，不能留 0。
     *    全 0 的配置在实机上表现为「scan 返回 ESP_OK，但 AP 数量是 0」——
     *    n=0 / aps=[]，连隔壁邻居的 AP 都扫不到，看起来像射频坏了。
     *    2026-11-06 就是这么被误导了一轮。
     *    active.min/max 是每个信道的探测时长（ms）：min 给 120 是 IDF 的
     *    常规取值，max 1500 让信号弱的 AP 也有机会回 probe response。 */
    sc.scan_type            = WIFI_SCAN_TYPE_ACTIVE;
    sc.scan_time.active.min = 120;
    sc.scan_time.active.max = 1500;

    /* ★ 先断开再扫。STA 正在 esp_wifi_connect() 的重试循环里时，扫描请求
     *   可能被驱动吞掉（返回 OK 但结果为空）。断开一下把状态清干净，
     *   net_task 那边下一轮会自己再连，不需要我们操心。 */
    esp_wifi_disconnect();
    vTaskDelay(pdMS_TO_TICKS(300));

    esp_err_t r = esp_wifi_scan_start(&sc, true);   /* true = 阻塞到扫完 */
    if (r != ESP_OK) {
        snprintf(out, out_len, "{\"t\":\"%s\",\"err\":\"%s\"}",
                 tag, esp_err_to_name(r));
        return r;
    }

    uint16_t n = 0;
    esp_err_t nr = esp_wifi_scan_get_ap_num(&n);
    if (n > 24) n = 24;             /* 上限，别把输出撑爆 */
    static wifi_ap_record_t recs[24];
    uint16_t got = n;
    esp_err_t gr = ESP_OK;
    if (got) {
        gr = esp_wifi_scan_get_ap_records(&got, recs);
        if (gr != ESP_OK) got = 0;
    }

    /* ★ n 和 got 必须分开报。
     *   n   = 驱动说"扫到了几个"
     *   got = 我实际取回来几个
     *   把两者合成一个字段的话，"取记录失败"会被显示成"扫到 0 个"，
     *   把排查方向从"驱动"带到"射频"上去 —— 这个坑我自己先踩了一次。 */
    size_t pos = 0;
    pos += snprintf(out + pos, out_len - pos,
                    "{\"t\":\"%s\",\"n\":%u,\"got\":%u,\"aps\":[",
                    tag, (unsigned)n, (unsigned)got);
    if (nr != ESP_OK || gr != ESP_OK) {
        pos += snprintf(out + pos, out_len - pos, "],\"e_num\":\"%s\",\"e_rec\":\"%s\"}",
                        esp_err_to_name(nr), esp_err_to_name(gr));
        return ESP_OK;
    }
    for (uint16_t i = 0; i < got && pos + 96 < out_len; i++) {
        /* SSID 里理论上可能有引号/反斜杠，JSON 会被打断 —— 只保留可打印字符 */
        char ssid[33];
        size_t k = 0;
        for (; k < 32 && recs[i].ssid[k]; k++) {
            unsigned char c = recs[i].ssid[k];
            ssid[k] = (c >= 0x20 && c != '"' && c != '\\') ? (char)c : '?';
        }
        ssid[k] = 0;
        pos += snprintf(out + pos, out_len - pos,
                        "%s{\"s\":\"%s\",\"r\":%d,\"c\":%u,\"a\":%u}",
                        i ? "," : "", ssid, (int)recs[i].rssi,
                        (unsigned)recs[i].primary, (unsigned)recs[i].authmode);
    }
    snprintf(out + pos, out_len - pos, "]}");
    return ESP_OK;
}

esp_err_t net_link_scan_json(char *out, size_t out_len) {
    if (!out || out_len < 64) return ESP_ERR_INVALID_ARG;

    /* 串口模式下 net_task 根本没起（net_link_start 直接 return 了），
     * 所以要在这里按需把 Wi-Fi 拉起来。wifi_begin 现在是幂等的，
     * 重复调用安全；esp_wifi_start 的 ESP_ERR_WIFI_INIT_STATE 表示
     * "已经起过了"，同样当成功处理。
     *
     * ★★ 任何一步失败都必须**写一条错误回执**再返回。
     *    原来这里是直接 `return r`，调用方（main.c）不管返回值照样把
     *    缓冲区发出去，而缓冲区是空的 ⇒ 串口上什么都不出现，
     *    表现成"这条命令石沉大海"。2026-11-06 在 tr=0 的设备上就是这样
     *    白试了两轮。诊断命令尤其不能静默失败。 */
    if (!s_wifi_inited) {
        esp_err_t r = wifi_begin();
        if (r != ESP_OK) {
            snprintf(out, out_len, "{\"t\":\"wifi_scan\",\"err\":\"wifi_begin:%s\"}",
                     esp_err_to_name(r));
            return r;
        }
    }
    if (!s_wifi_started) {
        esp_err_t r = esp_wifi_start();
        if (r != ESP_OK && r != ESP_ERR_WIFI_INIT_STATE) {
            snprintf(out, out_len, "{\"t\":\"wifi_scan\",\"err\":\"start:%s\"}",
                     esp_err_to_name(r));
            return r;
        }
        s_wifi_started = true;
    }
    return scan_format("wifi_scan", out, out_len);
}

/* ★ 开机早期扫频：必须在 esp_zb_start() **之前**调用。
 *
 * 存在的意义：用来把"Wi-Fi 到底能不能收到信号"和"Zigbee 是否把射频占住了"
 * 这两件事分开。如果这一步能扫到 AP，而开机之后（Zigbee 已经在跑）扫不到，
 * 那就是共存/射频仲裁的问题；如果这一步也是 0 个，那就是 Wi-Fi 本身
 * （天线、PHY、板子）的问题，别再去动共存配置。
 *
 * 这里**故意不建 STA netif**：此时还没起 Zigbee，多建一个默认 netif
 * 有可能和后续初始化抢同一个实例。扫频不需要 lwIP。
 * wifi_begin() 之后会自己把 netif 补上（见那边的注释）。
 */
esp_err_t net_link_scan_early(char *out, size_t out_len) {
    if (!out || out_len < 64) return ESP_ERR_INVALID_ARG;

    esp_err_t r = esp_netif_init();
    if (r != ESP_OK && r != ESP_ERR_INVALID_STATE) return r;
    r = esp_event_loop_create_default();
    if (r != ESP_OK && r != ESP_ERR_INVALID_STATE) return r;

    /* 事件组要早建：下面等 STA_START 要靠它置位 */
    if (net_sync_ensure() != ESP_OK) {
        snprintf(out, out_len, "{\"t\":\"wifi_bootscan\",\"err\":\"no_mem\"}");
        return ESP_ERR_NO_MEM;
    }

    if (!s_wifi_inited) {
        wifi_init_config_t ic = WIFI_INIT_CONFIG_DEFAULT();
        r = esp_wifi_init(&ic);
        if (r != ESP_OK) {
            snprintf(out, out_len, "{\"t\":\"wifi_bootscan\",\"err\":\"init:%s\"}",
                     esp_err_to_name(r));
            return r;
        }
        if (!s_handlers_registered) {
            esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                               on_wifi_event, NULL, NULL);
            esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                               on_wifi_event, NULL, NULL);
            s_handlers_registered = true;
        }
        esp_wifi_set_storage(WIFI_STORAGE_RAM);
        r = esp_wifi_set_mode(WIFI_MODE_STA);
        if (r != ESP_OK) {
            snprintf(out, out_len, "{\"t\":\"wifi_bootscan\",\"err\":\"mode:%s\"}",
                     esp_err_to_name(r));
            return r;
        }
        s_wifi_inited = true;
    }
    if (!s_wifi_started) {
        r = esp_wifi_start();
        if (r != ESP_OK && r != ESP_ERR_WIFI_INIT_STATE) {
            snprintf(out, out_len, "{\"t\":\"wifi_bootscan\",\"err\":\"start:%s\"}",
                     esp_err_to_name(r));
            return r;
        }
        s_wifi_started = true;
    }

    /* ★★★ 必须等 WIFI_EVENT_STA_START 之后才能扫描。
     *   esp_wifi_start() 是异步的：它返回 OK 不代表 STA 已经就绪。
     *   在 STA_START 之前调 esp_wifi_scan_start()，**返回 ESP_OK、1 毫秒就回来、
     *   结果永远是 0 个 AP** —— 看起来跟"射频坏了"一模一样。
     *   2026-11-06 第一次跑开机扫描就栽在这儿：日志显示 403 ms 出结果，
     *   于是误判成"Zigbee 没起来时也扫不到 ⇒ 不是共存问题"，结论整个跑偏。
     *   正常路径不会踩这个坑，因为那时 STA 早就 start 过了。 */
    if (!(xEventGroupGetBits(s_ev) & BIT_STARTED)) {
        EventBits_t b = xEventGroupWaitBits(s_ev, BIT_STARTED, pdFALSE, pdTRUE,
                                            pdMS_TO_TICKS(5000));
        if (!(b & BIT_STARTED)) {
            snprintf(out, out_len,
                     "{\"t\":\"wifi_bootscan\",\"err\":\"STA_START 等超时\"}");
            return ESP_ERR_TIMEOUT;
        }
    }
    return scan_format("wifi_bootscan", out, out_len);
}

esp_err_t net_link_start(void) {
    cfg_load();
    if (s_cfg.transport != NET_LINK_TCP) {
        ESP_LOGI(TAG, "传输通道 = 串口（USB-Serial-JTAG），未启用 Wi-Fi");
        return ESP_OK;
    }
    if (!s_cfg.ssid[0] || !s_cfg.host[0]) {
        /* ★ 不能 ESP_ERROR_CHECK —— 拔掉 USB 之后 abort 就是一块砖。
         *   降级回串口，至少还能用 USB 改配置。 */
        ESP_LOGW(TAG, "配置不完整（ssid='%s' host='%s'）→ 本次仍走串口",
                 s_cfg.ssid, s_cfg.host);
        s_cfg.transport = NET_LINK_UART;
        return ESP_ERR_INVALID_STATE;
    }

    /* 事件组/互斥量都交给 net_task_spawn 建，避免两处各建一套 */
    if (net_task_spawn() != ESP_OK) {
        ESP_LOGE(TAG, "net_task 起不来 → 降级串口");
        s_cfg.transport = NET_LINK_UART;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}
