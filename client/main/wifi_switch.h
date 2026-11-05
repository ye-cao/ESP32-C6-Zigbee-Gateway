#pragma once
#include <stdbool.h>

/* 超时秒数：测试用 1 分钟，正式用 600 */
#define WIFI_SWITCH_TIMEOUT_S  60

/* NVS 标志是否已置？（在 app_main 里、Zigbee 启动前检查） */
bool wifi_switch_is_pending(void);

/* 置标志 + 立即重启 ⇒ 下次启动就跑 WiFi 模式 */
void wifi_switch_trigger(void);

/* 执行 WiFi 模式（会阻塞到 esp_restart）—— 只能在 app_main 里调 */
void wifi_switch_run(void);
