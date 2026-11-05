# -*- coding: utf-8 -*-
"""ha_trace.py – Zeitlinie eines Schaltbefehls.

发完命令之后，连续 4 s 每 0.1 s 读一次状态。要看的是：
在那条立刻返回的"乐观回声"（~0.01 s）之后，还会不会来第二次更新
—— 那就是设备的回读（~0.7 s）。
若之后状态保持不变，说明设备确实执行了；若跳回去，说明它**没有**执行。
"""
import json, os, time, urllib.request

U = os.environ["HA_URL"]
T = os.environ["HA_TOKEN"]

ENT = [
    ("switch.hgzb_043_hgzb_043_shang_kai_guan",                  "三联-上 ep16"),
    ("switch.hgzb_043_hgzb_043_zhong_kai_guan_2",                "三联-中 ep17"),
    ("switch.hgzb_043_hgzb_043_xia_kai_guan_2",                  "三联-下 ep18"),
    ("switch.ftb56_zsn18zy1_1_ftb56_zsn18zy1_1_kai_guan_2",      "二联-1 ep1"),
    ("switch.ftb56_zsn18zy1_1_ftb56_zsn18zy1_1_ep2_kai_guan_2",  "二联-2 ep2"),
]


def req(path, data=None):
    r = urllib.request.Request(U + path, headers={"Authorization": "Bearer " + T})
    if data is not None:
        r.data = json.dumps(data).encode()
        r.add_header("Content-Type", "application/json")
        r.method = "POST"
    with urllib.request.urlopen(r, timeout=15) as resp:
        return json.loads(resp.read())


def snap(eid):
    d = req("/api/states/" + eid)
    return d["state"], d["last_changed"], d["last_updated"]


def trace(eid, label, cmd):
    s0, c0, u0 = snap(eid)
    req("/api/services/switch/" + cmd, {"entity_id": eid})
    t0 = time.time()
    events = [(0.0, s0, "vorher")]
    last_key = (s0, c0, u0)
    while time.time() - t0 < 4.0:
        s, c, u = snap(eid)
        if (s, c, u) != last_key:
            events.append((round(time.time() - t0, 2), s,
                           "state" if s != last_key[0] else "只有 last_updated（又收到一次 MQTT）"))
            last_key = (s, c, u)
        time.sleep(0.1)
    final = last_key[0]
    print("  %-9s %s" % (cmd, " → ".join("%s@%.2fs" % (e[1], e[0]) for e in events)))
    for t, s, why in events:
        if why != "vorher":
            print("            +%.2fs  %-4s  %s" % (t, s, why))
    print("            4 s 后的终态：%s" % final)
    return s0, final


print("时间线：命令→状态（立即回声 vs 真正的回读）\n")
for eid, label in ENT:
    print("=== %s   %s" % (label, eid))
    trace(eid, label, "turn_on")
    time.sleep(1.5)
    trace(eid, label, "turn_off")
    time.sleep(1.5)
    print()
