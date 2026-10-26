#pragma once

/*
 * zb_topo – 网络拓扑导出（谁直连、谁经中继）
 *
 * 数据来源两部分：
 *
 * A. 协调器本地 NIB（不需要无线报文）：
 *   1. 邻居表  esp_zb_nwk_get_next_neighbor()      → 我直接能听到谁
 *   2. 路由表  esp_zb_nwk_get_next_route()         → 到某地址的**下一跳**是谁
 *   3. 源路由  esp_zb_nwk_get_next_route_record()  → 到某地址的完整中继路径
 *
 * B. ★ 逐台 Mgmt_Lqi_Req (ZDO 0x0031) —— 这才是拓扑能用的关键：
 *   向每台设备要它**自己的**邻居表，其中 rel=0 的那一条就是**它的父节点**，
 *   且 lqi 是**这一跳**的真实链路质量。协调器 NIB 只知道自己的邻居，
 *   挂在别的路由器下面的设备根本不在里面，光靠 A 画不出树。
 *   做法与 Z2M / tostmann/esp-coordinator 一致。
 *
 * ★ 限制：
 *   - 路由表**按需建**，只保留最近真正用过的路径（几十秒过期）；直接邻居
 *     甚至根本不建表项。所以 B 才是主证据，A 只做兜底。
 *   - 这些函数读 NIB / 发 ZDP，只能在 ZBOSS 任务里调 ⇒ 一律经
 *     esp_zb_scheduler_alarm 排队，不能从 uart_rx 任务直接调。
 *   - B 需要设备应答，设备离线时会等 ZDP 超时；内部有 15 s 总德律（deadline）。
 */

/* 只 dump 当前表（快，不发无线报文） */
void zb_topo_request(void);

/* 先给所有已知设备发一次 Node_Desc（触发路由发现），延迟 delay_ms 后再 dump。
 * delay_ms 建议 ≥ 2500：一轮单播往返通常 0.3~1.5 s。 */
void zb_topo_request_full(uint32_t delay_ms);
