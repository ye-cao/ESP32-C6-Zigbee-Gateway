/*
 * ha_mqtt.c  ——  JSON-Lines 收发层（与「走哪根线」无关）
 *
 * 两条传输通道共用这一份代码：
 *   - 串口：CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG=y 把 printf()/stdin 指向
 *           原生 USB-CDC 端口（ttyACM1），不需要 UART 驱动；
 *   - Wi-Fi：net_link.c 提供的 TCP 连接。
 * 发送在 emit() → tx_task 里二选一（TCP 优先，回退串口）；接收则是
 * rx_task（fgets(stdin)）和 net_link 的收包循环各自攒够一行，都调
 * ha_mqtt_handle_line()。**解析只有一份**，不会出现「串口能用、Wi-Fi 不能用」。
 *
 * IDF 自身的日志用 null_vprintf 静音。
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdarg.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "driver/usb_serial_jtag.h"
#include "ha_mqtt.h"
#include "net_link.h"

#define BUF_SIZE    4096

static mqtt_cmd_cb_t     s_cmd_cb   = NULL;

/* ★★★ 2026-10-25：把「发送」从 ZBOSS 任务里解耦出去。
 *
 * 以前 emit() 直接 puts()+fflush()。一条 4 KB 的 JSON（拓扑）在 115200 波特下
 * 要阻塞 ~350 ms，整个拓扑 dump ~1.4 s —— 而且**全都在 ZBOSS 任务里**。
 * 这段时间 MAC 层一次迭代都跑不到，ZBOSS 的队列积压后直接挂：
 *   Guru Meditation (Load access fault)，落在 zb_mac_logic_iteration /
 *   zb_sched_loop_iteration，随后 rst:0xc (SW_CPU) —— 也就是重启。
 * 连每个 APS 帧（aps_data_ind_cb）都在阻塞写 ~200 B。
 *
 * 现在：emit() 把整行拷到堆上，只把指针塞进队列（永不阻塞，队列满就丢弃并计数），
 * 由独立的 tx_task 慢慢写。调用方 —— 不管是 ZBOSS、rx 还是 heartbeat 任务 ——
 * 立刻返回。 */
#define TX_Q_DEPTH      16
static QueueHandle_t     s_tx_q      = NULL;
static volatile uint32_t s_tx_drop   = 0;

/* ★★★ 2026-11-07：输出改为「通道跟随」，不再看 tr 配置二选一。
 *
 * 原实现是：transport == TCP 时只写 socket，写成功就一个字都不发串口。
 * 于是 add-on 切到 TCP 之后，USB 那根线只剩供电 —— 设备连不上 Wi-Fi 时
 * 连个能看的通道都没有，而 OTA 恰恰最需要这条保底通道。
 *
 * 现在两条通道同时保持在线，但**只往 add-on 实际挂着的那条发**：
 * 记住最近一次「从哪条通道收到命令」，就往那条发。add-on 每 5 s 一次心跳
 * 就能把这个判断钉死；切换通道后最多一个心跳周期就自动跟上。
 * 两边都超时（或开机还没收到过任何命令）→ 退回双发，宁可多一份也不漏。
 *
 * 为什么不靠 usb_serial_jtag_is_connected() 判断串口：它只能说明 USB 主机
 * 插着（SOF 包），说明不了「有没有进程在读」。插着线却没人读时，阻塞写会
 * 把 tx_task 永久卡死（VFS 的 tx timeout 是 portMAX_DELAY）。 */
#define LINK_FOLLOW_US   (20LL * 1000 * 1000)   /* 20 s 内有输入才算活跃 */
static volatile uint8_t s_rx_link    = 0xFF;   /* 0xFF = 还不知道 */
static volatile int64_t s_rx_link_us = 0;

/* ★★★ 「谁在说话」和「add-on 在哪条通道」是两件事，必须分开记。
 *
 *   只按 s_rx_link 判断的话，任何人在任何通道上说一句话都会改写通道选择 ——
 *   最典型的坏情况：板子插在电脑上，调试脚本/串口终端随手发一条命令，正走
 *   TCP 的 add-on 就被"抢"到串口去了，上报于是全打进一个没人要的串口。
 *   所以再加一层：只有**带身份的心跳**（{"cmd":"ping","who":"addon"}）
 *   才代表 add-on 的归属，它是第一判据；没有它时才退回 s_rx_link。 */
static volatile uint8_t s_addon_link = 0xFF;   /* add-on 最近一次心跳来自哪条 */
static volatile int64_t s_addon_us   = 0;

static int null_vprintf(const char *fmt, va_list args) {
    (void)fmt; (void)args;
    return 0;
}

/* ★ 临时把 IDF 的日志放出来，排查完再关。
 *
 * 为什么需要这个开关：ha_mqtt_init() 把日志换成了 null_vprintf，串口上只剩
 * JSON 行 —— 这是为了让 add-on 的解析不被日志污染。代价是 Wi-Fi 驱动自己
 * 的诊断信息（"sta is connecting"、"scan failed"、PHY 报错之类）一个字都看不到，
 * 只能靠 net_status 的一个数字瞎猜。2026-11-06 查 wifi_scan 返回 0 个 AP
 * 时就是被这个卡住的。
 *
 * ★ 只能在**传输通道是 TCP** 时开：那时数据走 socket，日志混进串口无害。
 *   串口通道下开会把日志插进 JSON 流里，add-on 会收到解析不了的半截行。 */
void ha_mqtt_log_verbose(bool on) {
    esp_log_set_vprintf(on ? vprintf : null_vprintf);
    if (on) {
        ESP_LOGW("ha_mqtt", "日志输出已打开 —— 串口上会混进 IDF 日志，排查完请发 log_off");
    }
}

/* 标注「这一行是从哪条通道来的」。TCP 收包循环和串口 rx_task 各自调用。 */
void ha_mqtt_note_link(uint8_t link) {
    if (link > NET_LINK_TCP) return;
    s_rx_link    = link;
    s_rx_link_us = esp_timer_get_time();
}

/* 当前该往哪条通道发：返回 NET_LINK_UART / NET_LINK_TCP；0xFF 表示
 * 「不确定，两条都发」。 */
static uint8_t active_link(void) {
    /* ① 第一判据：add-on 自己的心跳。它才是「谁在收上报」的权威答案，
     *    调试脚本、串口终端发的命令一律不能覆盖它。 */
    uint8_t al = s_addon_link;
    if (al <= NET_LINK_TCP &&
        esp_timer_get_time() - s_addon_us <= LINK_FOLLOW_US) {
        return al;
    }

    /* ② 没有 add-on 心跳（老版本 add-on / 刚上电还没发）⇒ 退一步：
     *    哪条通道最近有输入就认为它在哪。这保证纯脚本调试时也能正常收数据。 */
    uint8_t l = s_rx_link;
    if (l > NET_LINK_TCP) return 0xFF;                     /* 还没收到过任何命令 */
    if (esp_timer_get_time() - s_rx_link_us > LINK_FOLLOW_US) {
        return 0xFF;                                       /* 两条都静了：双发保底 */
    }
    return l;
}

static void tx_task(void *arg) {
    (void)arg;
    char *line = NULL;
    while (1) {
        if (xQueueReceive(s_tx_q, &line, portMAX_DELAY) != pdTRUE) continue;
        if (!line) continue;

        uint8_t link = active_link();

        /* ① Wi-Fi/TCP：能发就发，失败（未连接/正在重连）不再触发串口回退 —— */
        /*    两条通道是并存关系，走哪条由 link 决定。 */
        if ((link == NET_LINK_TCP || link == 0xFF) &&
            net_link_transport() == NET_LINK_TCP) {
            (void)net_link_send_line(line);
        }

        /* ② 串口。★ 必须带 is_connected()：USB-Serial-JTAG 的 VFS 写路径在没有
         *    主机时是 portMAX_DELAY 永久阻塞（usb_serial_jtag_vfs.c 里那行
         *    `timeout = s_ctx.non_blocking ? 0 : portMAX_DELAY`），FIFO 一满
         *    tx_task 就永久卡死，整条上报链路静默失效 —— 历史上表现成
         *    「拔线后网页全空，但设备还在联网」。
         *    这里只在「add-on 明确挂着串口」或「两条都静默」时才写，
         *    所以正常情况下不会往一个没人读的串口白写。 */
        if ((link == NET_LINK_UART || link == 0xFF) &&
            usb_serial_jtag_is_connected()) {
            fputs(line, stdout);
            fputc('\n', stdout);
            fflush(stdout);
        }
        free(line);
    }
}

/* ⚠️ 这个函数会被 ZBOSS 任务调用 —— 绝不允许阻塞。 */
static void emit(const char *line) {
    if (!s_tx_q || !line) return;
    size_t n = strlen(line) + 1;
    char *c = malloc(n);
    if (!c) { s_tx_drop++; return; }
    memcpy(c, line, n);
    if (xQueueSend(s_tx_q, &c, 0) != pdTRUE) {
        free(c);
        s_tx_drop++;              /* 队列满了：宁丢一条，也不能卡住调用方 */
    }
}


/* 返回 "key" 后面那个值的指针 —— 容忍冒号后的空格。
 * 必须这么做，因为 Python 的 json.dumps() 产出的是 {"addr": "0x07da"}：
 * 写死 strstr(buf, "\"addr\":\"") 会**什么都找不到**，命令于是静默失败。 */
static const char *json_val(const char *buf, const char *key) {
    const char *k = strstr(buf, key);
    if (!k) return NULL;
    k += strlen(key);
    while (*k == ' ' || *k == '\t') k++;
    if (*k != ':') return NULL;
    k++;
    while (*k == ' ' || *k == '\t') k++;
    return k;
}

static void rx_task(void *arg) {
    char *buf = malloc(BUF_SIZE);
    configASSERT(buf);

    /* USB-Serial-JTAG-CDC 每次 fgets() 只给 ~64 字节，而 fgets() 把它当行尾。
     * 于是长命令只会以碎片到达：fw_ota_data 缺少结尾的 '"'，strchr() 找不到结束，
     * 整条命令被**静默丢弃**。固件 OTA 每次都稳稳卡在 1280 字节（16 帧）就是
     * 这么来的 —— 而且一条错误提示都没有。短命令（dev_cmd，45 字符）能用，
     * 只因为所有字段都落在断点之前。
     * 所以：先把碎片攒起来，等到真正的行尾再处理。 */
    static char acc[BUF_SIZE];
    size_t acc_len = 0;

    while (1) {
        if (!fgets(buf, BUF_SIZE, stdin)) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        size_t raw = strlen(buf);
        int eol = (raw > 0 && (buf[raw-1] == '\n' || buf[raw-1] == '\r'));

        /* 去掉行尾换行 */
        size_t n = raw;
        while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) buf[--n] = '\0';

        if (!eol) {                      /* 半行 ⇒ 先缓存 */
            if (acc_len + n < sizeof(acc)) {
                memcpy(acc + acc_len, buf, n);
                acc_len += n;
            } else {
                acc_len = 0;             /* 溢出：整段丢弃 */
            }
            continue;
        }
        if (acc_len) {                   /* 行结束了，但前面是被切开的 */
            if (acc_len + n >= sizeof(acc)) {
                acc_len = 0;
                continue;
            }
            memcpy(acc + acc_len, buf, n);
            acc_len += n;
            memcpy(buf, acc, acc_len);
            buf[acc_len] = '\0';
            n = acc_len;
            acc_len = 0;
        }
        if (n == 0) continue;
        /* 标注来源：这一行是从串口来的 ⇒ 以后就往串口发（通道跟随）。 */
        ha_mqtt_note_link(NET_LINK_UART);
        ha_mqtt_handle_line(buf);
    }
}

/* ── 一行 JSON → 命令 ────────────────────────────────────────────────────
 *
 * ★★ 这个函数是「串口」和「Wi-Fi」两个入口的**唯一**交汇点。
 *    历史上 node_desc / fw_ota_data / dev_remove 三次「静默死亡」全是因为
 *    命令在 main.c::on_uart_cmd 里登记了、这里没接线（或者反过来）。
 *    以后加命令，这里是第一处、on_uart_cmd 是第二处，**两处都要改**。
 *
 * 注意：本函数会就地改写 buf（把引号换成 '\0' 之类），调用方别指望调用后
 * 原样保留。两条通道各传各的缓冲，互不影响；唯一共享的是下面几个 static
 * payload 缓冲，前提是「同一条命令不会被两个通道同时收到」—— 实际部署里
 * 也只有一个通道在收，所以是安全的。 */
void ha_mqtt_handle_line(char *buf) {
    if (!buf || !s_cmd_cb) return;

    /* ★★★ 这两个必须放在链首。
     *   下面的分支大量使用无引号的 strstr()（`"topo"`、`"probe"`、`"lqi"`
     *   之类），只要 SSID 或 Wi-Fi 密码里恰好含这些字母组合，命令就会被
     *   前面那条分支劫持 —— 表现是「发 net_cfg 没反应」，而且极难想到原因。
     *   net_cfg 的载荷本身是用户填的任意文本，所以必须最先认它。 */
    if (strstr(buf, "net_cfg")) {
        /* Wi-Fi / 传输通道配置：
         *   {"cmd":"net_cfg","ssid":"…","pass":"…","host":"…","port":12345,"tr":1}
         * 拼成 payload = "SSID\x1fPASS\x1fHOST\x1fPORT\x1fTR" 交给 main.c。
         *
         * ★ 用 \x1f（unit separator）而不是逗号：SSID 和密码里完全可能有
         *   逗号。用逗号切会把密码拦腰截断，症状是「一直连不上但看不出
         *   为什么」—— 这种 bug 查起来最费时间。
         * ★ 空字段 = 「这一项不改」，由 main.c 解释。
         * ★★ 这个分支和 main.c::on_uart_cmd 里的同名分支**必须成对存在**。
         *    只改一处，协调器就回 {"t":"error","why":"unknown_cmd"} ——
         *    node_desc / fw_ota_data / dev_remove 已经这么死过三次。
         *    本次第一版又漏了一遍，是提交后自审才抓到的。 */
        static char payload[320];
        static const char *const keys[5] = {
            "\"ssid\"", "\"pass\"", "\"host\"", "\"port\"", "\"tr\""
        };
        size_t pos = 0;
        for (int i = 0; i < 5; i++) {
            if (i && pos < sizeof(payload) - 1) payload[pos++] = '\x1f';
            const char *v = json_val(buf, keys[i]);
            if (!v) continue;
            if (*v == '"') {
                v++;
                while (*v && *v != '"' && pos < sizeof(payload) - 1)
                    payload[pos++] = *v++;
            } else {
                while (*v && *v != ',' && *v != '}' && *v != ' ' &&
                       pos < sizeof(payload) - 1)
                    payload[pos++] = *v++;
            }
        }
        payload[pos] = '\0';
        s_cmd_cb("net_cfg", payload, (int)pos);
        return;
    }
    /* 心跳：add-on 每 5 s 发一次 {"cmd":"ping"}，用来钉住「它到底挂在哪条
     * 通道上」—— 这就是通道跟随的判据来源。
     * ★ 必须排在 net_cfg 之后：net_cfg 的载荷是用户填的 SSID/密码，完全可能
     *   含上面那四个字母，让它先认才不会把配置命令劫持成心跳。
     * ★ 用带引号的 "ping" 匹配，不用裸 ping：命令链里大量无引号 strstr，
     *   裸关键字太容易误伤（比如以后加个 "pingpong" 之类的字段）。
     * 回 {"t":"pong","link":…} 让 add-on 能确认跟随是否生效。 */
    if (strstr(buf, "\"ping\"")) {
        /* ★★ 只有带身份的心跳才登记「add-on 挂在哪条通道」。
         *    add-on 发 {"cmd":"ping","who":"addon"}；
         *    诊断脚本 / 串口终端发不带 who 的 ping，照样回 pong，但**不改写
         *    归属** —— 否则插上 USB 调试一下，正走 TCP 的 add-on 就被抢到
         *    串口去了，上报全打进一个没人要的口。
         *    pong 里的 who 字段把这次判定也回给对方，便于确认谁被认作 add-on。 */
        bool is_addon = (strstr(buf, "\"addon\"") != NULL);
        if (is_addon) {
            s_addon_link = s_rx_link;
            s_addon_us   = esp_timer_get_time();
        }
        char out[128];
        snprintf(out, sizeof(out),
                 "{\"t\":\"pong\",\"link\":\"%s\",\"who\":\"%s\",\"up\":%lu}",
                 (s_rx_link == NET_LINK_TCP) ? "tcp" : "uart",
                 is_addon ? "addon" : "other",
                 (unsigned long)(esp_timer_get_time() / 1000000));
        ha_mqtt_emit_raw(out);
        return;
    }
    if (strstr(buf, "net_status")) {
        /* {"cmd":"net_status"} —— 回一条 {"t":"net_status",…} */
        s_cmd_cb("net_status", "", 0);
        return;
    }
    if (strstr(buf, "wifi_scan")) {
        /* {"cmd":"wifi_scan"} —— 让固件自己扫一遍，回一条 {"t":"wifi_scan",…}
         * ★ 放在这里是因为本函数的命令链是"无引号 strstr"式的，
         *   同一条命令必须在这里和 main.c::on_uart_cmd 各登记一次，
         *   否则 C6 只会回 {"t":"error","why":"unknown_cmd"}。 */
        s_cmd_cb("wifi_scan", "", 0);
        return;
    }
    if (strstr(buf, "wifi_join")) {
        /* {"cmd":"wifi_join"} —— 真正连一次现有 SSID，回 {"t":"wifi_join",…}
         * ★ 判 Wi-Fi 通不通要用"连"，不能用"扫"：扫描 13 个信道每道只停
         *   120 ms，边际链路上会全灭；connect 守在目标信道上能累计几十秒。 */
        s_cmd_cb("wifi_join", "", 0);
        return;
    }
    /* ★ log_on / log_off 必须排在 wifi_scan 之后、且用完整串匹配 ——
     *   "log_on" 里没有会和上面几条撞的字符组合，但顺序上放在这里可以确保
     *   wifi_scan 之类名字更长的先被匹配到。 */
    if (strstr(buf, "\"log_on\"")) {
        ha_mqtt_log_verbose(true);
        return;
    }
    if (strstr(buf, "\"log_off\"")) {
        ha_mqtt_log_verbose(false);
        return;
    }

    if (strstr(buf, "permit_join")) {
            char *p = strstr(buf, "\"sec\":");
            char secs_str[8] = "180";
            if (p) snprintf(secs_str, sizeof(secs_str), "%d", atoi(p + 6));
            s_cmd_cb("permit_join", secs_str, strlen(secs_str));
        } else if (strstr(buf, "fw_ota_start")) {
            /* 协调器固件更新 —— 必须排在 ota_start 前面，
             * 因为 "fw_ota_start" 里含有子串 "ota_start" */
            char *ps = strstr(buf, "\"size\":");
            char payload[16] = "";
            if (ps) snprintf(payload, sizeof(payload), "%ld", atol(ps + 7));
            s_cmd_cb("fw_ota_start", payload, strlen(payload));
        } else if (strstr(buf, "fw_ota_data")) {
            /* payload = "OFF,HEX"（HEX 可能 ~900 字符 ⇒ 必须 static） */
            const char *po = json_val(buf, "\"off\"");
            char *pd = (char *)json_val(buf, "\"data\"");
            if (po && pd) {
                if (*pd == '"') pd++;
                char *end = strchr(pd, '"');
                if (end) {
                    *end = '\0';
                    static char payload[BUF_SIZE];
                    snprintf(payload, sizeof(payload), "%ld,", atol(po));
                    size_t pre = strlen(payload);
                    size_t hexlen = (size_t)(end - pd);
                    if (pre + hexlen < sizeof(payload)) {
                        memcpy(payload + pre, pd, hexlen);
                        payload[pre + hexlen] = '\0';
                        s_cmd_cb("fw_ota_data", payload, (int)(pre + hexlen));
                    }
                }
            }
        } else if (strstr(buf, "fw_ota_end")) {
            s_cmd_cb("fw_ota_end", "", 0);
        } else if (strstr(buf, "dev_cmd")) {
            /* 设备控制：{"cmd":"dev_cmd","addr":"0x07da","ep":16,"sw":"on"}
             * payload = "ADDR,EP,CMD"   CMD：0=关 1=开 2=翻转
             * 注意：动作字段叫 "sw" 而**不是** "cmd" —— 否则 strstr() 会先命中
             * 开头的 {"cmd":"dev_cmd",…}，每个动作都被当成 "on"。 */
            const char *pa = json_val(buf, "\"addr\"");
            const char *pe = json_val(buf, "\"ep\"");
            const char *pc = json_val(buf, "\"sw\"");
            if (pa && pe && pc) {
                unsigned long addr = strtoul((*pa == '"') ? pa + 1 : pa, NULL, 16);
                const char *sv = (*pc == '"') ? pc + 1 : pc;
                char payload[32];
                int ep  = atoi(pe);
                int cid = 1;                                  /* 默认：开 */
                if (strncmp(sv, "off", 3) == 0)         cid = 0;
                else if (strncmp(sv, "toggle", 6) == 0) cid = 2;
                snprintf(payload, sizeof(payload), "%lu,%d,%d", addr, ep, cid);
                s_cmd_cb("dev_cmd", payload, strlen(payload));
            }
        } else if (strstr(buf, "probe")) {
            /* 型号探测：{"cmd":"probe","addr":"0x07da","ep":1}
             * payload = "ADDR,EP" */
            const char *pa = json_val(buf, "\"addr\"");
            const char *pe = json_val(buf, "\"ep\"");
            if (pa && pe) {
                unsigned long addr = strtoul((*pa == '"') ? pa + 1 : pa, NULL, 16);
                char payload[24];
                snprintf(payload, sizeof(payload), "%lu,%d", addr, atoi(pe));
                s_cmd_cb("probe", payload, strlen(payload));
            }
        } else if (strstr(buf, "node_desc")) {
            /* 节点描述符：{"cmd":"node_desc","addr":"0x07da"}
             * payload = "ADDR" —— 用来确认这台设备是不是以路由器（ZR）身份工作。 */
            const char *pa = json_val(buf, "\"addr\"");
            if (pa) {
                unsigned long addr = strtoul((*pa == '"') ? pa + 1 : pa, NULL, 16);
                char payload[16];
                snprintf(payload, sizeof(payload), "%lu", addr);
                s_cmd_cb("node_desc", payload, strlen(payload));
            }
        } else if (strstr(buf, "dev_remove")) {
            /* 彻底删除设备：{"cmd":"dev_remove","addr":"0x023b"}
             * payload = "ADDR" —— 跟 node_desc 一样，只是不要 EP。
             * ★★ 本文件是接线的另一半：光在 main.c 里认识这个命令没用，
             *    这里也必须登记。node_desc 和 fw_ota_data 就是这么「静默死亡」的
             *    （回执是 "unknown_cmd"，日志里只有一句无信息的报错）。
             *    这个分支要放在「模式恰好是其子串」的分支之前
             *    （"dev_remove" 不含别的命令名，所以位置可以随便挑）。 */
            const char *pa = json_val(buf, "\"addr\"");
            if (pa) {
                unsigned long addr = strtoul((*pa == '"') ? pa + 1 : pa, NULL, 16);
                char payload[16];
                snprintf(payload, sizeof(payload), "%lu", addr);
                s_cmd_cb("dev_remove", payload, strlen(payload));
            }
        } else if (strstr(buf, "read_onoff")) {
            /* 回读开关状态：{"cmd":"read_onoff","addr":"0x07da","ep":16}
             * payload = "ADDR,EP"（寻址方式同 probe）
             * 必须回读，因为终端设备收到远程命令后不会主动上报。 */
            const char *pa = json_val(buf, "\"addr\"");
            const char *pe = json_val(buf, "\"ep\"");
            if (pa && pe) {
                unsigned long addr = strtoul((*pa == '"') ? pa + 1 : pa, NULL, 16);
                char payload[24];
                snprintf(payload, sizeof(payload), "%lu,%d", addr, atoi(pe));
                s_cmd_cb("read_onoff", payload, strlen(payload));
            }
        } else if (strstr(buf, "set_channel")) {
            char *p = strstr(buf, "\"ch\":");
            char ch_str[4] = "20";
            if (p) snprintf(ch_str, sizeof(ch_str), "%d", atoi(p + 5));
            s_cmd_cb("set_channel", ch_str, strlen(ch_str));
        } else if (strstr(buf, "scan_chan")) {
            s_cmd_cb("scan_chan", "", 0);
        } else if (strstr(buf, "topo")) {
            /* 网络拓扑导出：{"cmd":"topo"} 或 {"cmd":"topo","delay":3000}
             * delay 为空 ⇒ 立刻 dump 现有表；给了 ⇒ 等这么久再 dump
             * （add-on 会在等待期间逐台发 node_desc 触发按需路由发现）。
             * 输出见 main/zb_topo.c。 */
            char val[12] = "";
            char *pd = strstr(buf, "\"delay\":");
            if (pd) snprintf(val, sizeof(val), "%d", atoi(pd + 8));
            s_cmd_cb("topo", val, strlen(val));
        } else if (strstr(buf, "switch2wifi")) {
            s_cmd_cb("switch2wifi", "", 0);
        } else if (strstr(buf, "set_sleep")) {
            char *p = strstr(buf, "\"s\":");
            char val[16] = "600";
            if (p) snprintf(val, sizeof(val), "%d", atoi(p + 4));
            s_cmd_cb("set_sleep", val, strlen(val));
        } else if (strstr(buf, "set_dev_power")) {
            /* 中继发射功率：{"cmd":"set_dev_power","addr":"0x1234","ep":1,"dbm":14}
             * payload = "ADDR,EP,DBM" → ZCL 写 0xFC00/0x0001（S8）。
             * 与 set_tx_power（改网关自己的 PA）是两件事，别混。 */
            char *pa = strstr(buf, "\"addr\":");
            char *pe = strstr(buf, "\"ep\":");
            char *pd = strstr(buf, "\"dbm\":");
            char payload[32] = "";
            if (pa) {
                const char *q = pa + 7;
                if (*q == '"') q++;
                unsigned long addr = strtoul(q, NULL, 0);
                int ep  = pe ? atoi(pe + 5) : 1;
                int dbm = pd ? atoi(pd + 6) : 8;
                snprintf(payload, sizeof(payload), "%lu,%d,%d", addr, ep, dbm);
            }
            s_cmd_cb("set_dev_power", payload, strlen(payload));
        } else if (strstr(buf, "set_tx_power")) {
            /* 运行期改发射功率：{"cmd":"set_tx_power","dbm":14}
             * payload = "14"（dBm）。这样不重新刷机也能调功率，并回报实际生效值。 */
            char *p = strstr(buf, "\"dbm\":");
            char val[8] = "20";
            if (p) snprintf(val, sizeof(val), "%d", atoi(p + 6));
            s_cmd_cb("set_tx_power", val, strlen(val));
        } else if (strstr(buf, "\"lqi\"")) {
            /* 即时取一次 LQI/RSSI：{"cmd":"lqi"}
             * 只在按需时用 —— 心跳里带的是同一份列表，但那是「最后一个收到的帧」的值，
             * 60 s 之后很可能早就过期了。 */
            s_cmd_cb("lqi", "", 0);
        } else if (strstr(buf, "ota_start")) {
            /* {"cmd":"ota_start","size":N,"ver":V,"type":0x0002}
             * type 决定给哪一类设备推固件（0x0001 终端节点 / 0x0002 中继）；
             * 缺省时固件侧按 0x0001 处理，保持对老 host 端兼容。 */
            char *ps = strstr(buf, "\"size\":");
            char *pv = strstr(buf, "\"ver\":");
            char *pt = strstr(buf, "\"type\":");
            char payload[48] = "";
            if (ps) {
                long sz  = atol(ps + 7);
                long ver = pv ? atol(pv + 6) : 1;
                if (pt) {
                    const char *q = pt + 7;
                    if (*q == '"') q++;
                    unsigned long ty = strtoul(q, NULL, 0);
                    snprintf(payload, sizeof(payload), "%ld,%ld,%lu", sz, ver, ty);
                } else {
                    snprintf(payload, sizeof(payload), "%ld,%ld", sz, ver);
                }
            }
            s_cmd_cb("ota_start", payload, strlen(payload));
        } else if (strstr(buf, "ota_data")) {
            /* ★★ 用 json_val 而不是 strstr("\"data\":\"")（2026-10-26）。
             * 树莓派用 json.dumps() 发块，产出的是
             *      {"cmd": "ota_data", "off": 0, "data": "1f8b..."}
             * —— 冒号后面有空格。写死的 "\"data\":\"" 在里面**什么都找不到**，
             * 于是每一块都被静默丢弃。症状：服务端老老实实发 ota_req，
             * 树莓派老老实实回，C6 等 6 s 永远拿不到数据 ⇒ OTA 卡在 8 / 610976 字节，
             * 之后客户端超时放弃，每 45 s 再来问一次。 */
            /* ★ payload 变成 "OFF,HEX" —— 固件需要 OFF 才能把这一块放到窗口里
             * 正确的位置。没有偏移时是无脑追加：客户端一旦重传（把同一块再要一遍），
             * 后面所有内容整体错位，客户端于是写出一份静默损坏的镜像。 */
            char *pd = (char *)json_val(buf, "\"data\"");
            const char *po = json_val(buf, "\"off\"");
            if (pd && po && *pd == '"') {
                pd++;
                char *end = strchr(pd, '"');
                if (end && end > pd) {
                    *end = '\0';
                    static char opayload[BUF_SIZE];
                    int pre = snprintf(opayload, sizeof(opayload), "%ld,", atol(po));
                    size_t hexlen = (size_t)(end - pd);
                    if (pre > 0 && (size_t)pre + hexlen < sizeof(opayload)) {
                        memcpy(opayload + pre, pd, hexlen);
                        opayload[pre + hexlen] = '\0';
                        s_cmd_cb("ota_data", opayload, (int)(pre + hexlen));
                    }
                }
            }
        } else if (strstr(buf, "ota_stop")) {
            s_cmd_cb("ota_stop", "", 0);
        } else if (strstr(buf, "\"cmd\"")) {
            /* 未知命令**不要**静默丢弃。
             * 已经有两个 bug 就是这么无声无息死掉的：fw_ota_data、
             * node_desc（main.c 里有这个命令，这里没接线 ⇒ 一点输出都没有）。 */
            const char *pc = strstr(buf, "\"cmd\":\"");
            char name[24] = "?";
            if (pc) {
                pc += 7;
                size_t i = 0;
                while (i < sizeof(name) - 1 && pc[i] && pc[i] != '"') {
                    name[i] = pc[i];
                    i++;
                }
                name[i] = '\0';
            }
            char line[96];
            snprintf(line, sizeof(line),
                     "{\"t\":\"error\",\"why\":\"unknown_cmd\",\"cmd\":\"%s\"}", name);
            emit(line);
        }
}

esp_err_t ha_mqtt_init(const char *broker_uri,
                       const char *username,
                       const char *password) {
    (void)broker_uri; (void)username; (void)password;

    /* ★★★ 2026-10-25：解耦发送路径（见 emit() 处的注释）。
     * 顺序很重要：队列**必须**在第一次 emit() 之前建好，
     * 否则连开机那行都会被静默丢掉。 */
    s_tx_q = xQueueCreate(TX_Q_DEPTH, sizeof(char *));
    configASSERT(s_tx_q);
    /* 4096 B 够用：tx_task 只持一个指针 + libc 缓冲。 */
    xTaskCreate(tx_task, "uart_tx", 4096, NULL, 3, NULL);

    /* 关掉 IDF 自己的日志 —— ttyACM1 上只留我们的 JSON */
    esp_log_set_vprintf(null_vprintf);

    /* ★ 2048 → 4096：rx_task 会一路走到 on_uart_cmd → zb_gateway_*，
     * JSON 拆分 + snprintf 比单纯的 fgets() 路径要费栈。
     * 栈很便宜，溢出一次就是一次重启。 */
    xTaskCreate(rx_task, "uart_rx", 4096, NULL, 4, NULL);

    /* 传输通道。NVS 里选了 Wi-Fi 才会真的拉起 net_task；默认串口，
     * 所以这里通常是空操作（只读一次 NVS 然后返回）。 */
    net_link_start();

    /* 稍等一下，等 USB-CDC 就绪 */
    vTaskDelay(pdMS_TO_TICKS(500));
    emit("{\"t\":\"boot\"}");
    return ESP_OK;
}

void ha_mqtt_set_cmd_cb(mqtt_cmd_cb_t cb) { s_cmd_cb = cb; }

void ha_mqtt_publish_zigbee(uint16_t addr, const char *subtopic, const char *payload) {
    ha_mqtt_publish_zigbee_ep(addr, 1, subtopic, payload);
}

void ha_mqtt_publish_zigbee_ep(uint16_t addr, uint8_t endpoint, const char *subtopic, const char *payload) {
    char sub[64];
    char line[224];
    if (endpoint > 1) {
        snprintf(sub, sizeof(sub), "ep%u/%s", (unsigned)endpoint, subtopic);
    } else {
        snprintf(sub, sizeof(sub), "%s", subtopic);
    }
    snprintf(line, sizeof(line),
        "{\"t\":\"zigbee\",\"addr\":%u,\"sub\":\"%s\",\"p\":%s}",
        (unsigned)addr, sub, payload);
    emit(line);
}

void ha_mqtt_publish_heartbeat(uint32_t uptime_s, uint8_t channel, uint16_t pan_id) {
    char line[96];
    snprintf(line, sizeof(line),
        "{\"t\":\"heartbeat\",\"uptime\":%lu,\"ch\":%u,\"pan\":\"0x%04x\"}",
        (unsigned long)uptime_s, (unsigned)channel, (unsigned)pan_id);
    emit(line);
}

void ha_mqtt_publish_permit_join(bool open, uint8_t seconds) {
    char line[80];
    snprintf(line, sizeof(line),
        "{\"t\":\"permit_join\",\"p\":{\"open\":%s,\"seconds\":%u}}",
        open ? "true" : "false", (unsigned)seconds);
    emit(line);
}

bool        ha_mqtt_connected(void)          { return true; }
const char *ha_mqtt_base_topic(void)         { return "c6/uart"; }
void        ha_mqtt_logf(const char *t, const char *f, ...) { (void)t; (void)f; }
void        ha_mqtt_emit_raw(const char *line) { emit(line); }
