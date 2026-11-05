# -*- coding: utf-8 -*-
"""txpower_ab.py – 用实测比较 C6 的发射功率。

要回答的问题是：在陶瓷天线上 +20 dBm 到底
是增加了覆盖，还是让 PA 把最上面那几个 dB 烧成了失真？
猜不能代替测 —— 所以去测。

关于测量对象（重要）
---------------------
C6 报的 LQI 和 RSSI 描述的是**上行**（设备 → C6）：
两者都在收到的帧上测量（zb_gateway.c，aps_data_ind_cb →
ind.lqi / esp_ieee802154_get_recent_rssi）。网关的发射功率
对它们毫无影响。在这里比 LQI 的前后值，量到的是噪声，
结论一定是错的。

随发射功率变化的只有**下行**（C6 → 设备）。
下行帧到底有没有到达，唯一的硬证据是设备的应答。
所以：往 On/Off 连发 N 次 Read Attributes，
数有多少次收到应答。这就是测量对象。

    python txpower_ab.py                        # 20/14/8/2 dBm, 10 Versuche
    python txpower_ab.py --levels 20,17,14 --n 20
    python txpower_ab.py --dry-run              # 只显示会发什么

环境变量：MQ_HOST / MQ_PORT / MQ_USER / MQ_PASS（同 mqtt_probe.py）。
"""
import argparse
import json
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mqtt_probe as mp                                    # noqa: E402

BASE = os.environ.get("BASE_TOPIC", "gw/coordinator")

POWER_TOPIC = f"{BASE}/cmd/set_tx_power"
LQI_TOPIC   = f"{BASE}/cmd/lqi"
READ_TOPIC  = f"{BASE}/cmd/read_onoff"
TX_TOPIC    = f"{BASE}/tx_power"
LQI_UP      = f"{BASE}/lqi"


# ── Topic-Hilfen ───────────────────────────────────────────────────────────
def state_topic(addr, ep):
    """Read Attributes 的应答会发到哪个 topic（见 _ha_publish）。"""
    sub = "on_off" if int(ep) <= 1 else "ep%d/on_off" % int(ep)
    return f"{BASE}/zigbee/{addr}/{sub}"


def key_of(payload):
    """JSON → dict，失败给 None。"""
    try:
        v = json.loads(payload)
    except Exception:
        return None
    return v if isinstance(v, dict) else None


def dev_row(lqi_msg, addr):
    """从一条 {"t":"lqi",…} 消息里取出设备的 LQI/RSSI。"""
    for d in (lqi_msg or {}).get("dev", []):
        if d.get("addr") == addr:
            return d.get("lqi"), d.get("rssi")
    return None, None


# ── Messung ────────────────────────────────────────────────────────────────
class Runner(mp.Cli):
    """连接和收包都继承自 mqtt_probe.Cli。"""

    def __init__(self, dry=False, settle=6.0, timeout=4.0):
        super().__init__()
        self.dry = dry
        # dry-run 下什么都不发，也就没什么好等的。
        self.settle = 0.0 if dry else settle
        self.timeout = 0.01 if dry else timeout

    def open(self):
        if self.dry:
            return
        typ, fl, rc = self.connect("txpower-ab")
        if rc != 0:
            sys.exit("CONNACK rc=%d —— broker 上没有权限" % rc)
        for t in (f"{BASE}/zigbee/#", TX_TOPIC, LQI_UP):
            self.subscribe(t, 0)
        self.sk.settimeout(0.5)

    def pub(self, topic, payload="", retain=False):
        if self.dry:
            print("    [dry] PUB %-32s %r" % (topic, payload))
            return
        body = mp.s(topic) + str(payload).encode()
        hdr = 0x30 | (0x01 if retain else 0x00)          # PUBLISH, QoS 0
        self.sk.sendall(mp.pkt(hdr, body))

    # -- Empfang ---------------------------------------------------------
    def pump(self, seconds):
        """Alle eintreffenden PUBLISH-Pakete einsammeln: [(topic, payload)]."""
        out = []
        if self.dry:
            return out
        t0 = time.time()
        while time.time() - t0 < seconds:
            try:
                t, body = self.read_pkt()
            except socket.timeout:
                continue
            except (OSError, IOError):
                break
            if (t & 0xF0) != 0x30:                       # 不是 PUBLISH（比如 SUBACK）
                continue
            qos = (t >> 1) & 3
            tl = int.from_bytes(body[:2], "big")
            if len(body) < 2 + tl:
                continue
            topic = body[2:2 + tl].decode("utf-8", "replace")
            p = 2 + tl + (2 if qos else 0)
            out.append((topic, body[p:].decode("utf-8", "replace")))
        return out

    def wait_for(self, topic, seconds):
        """等某条特定消息；返回负载或 None。"""
        t0 = time.time()
        while time.time() - t0 < seconds:
            for t, p in self.pump(0.5):
                if t == topic:
                    return p
        return None

    # -- Ein Leistungsniveau --------------------------------------------
    def measure(self, dbm, addr, ep, n, pause):
        row = {"want": dbm, "eff": None, "ok": 0, "n": n, "lqi": None, "rssi": None}
        print("\n── %+d dBm %s" % (dbm, "─" * 46))
        self.pub(POWER_TOPIC, str(dbm))

        raw = self.wait_for(TX_TOPIC, 5.0)
        tx  = key_of(raw) if raw else None
        if tx:
            row["eff"] = tx.get("eff")
            if tx.get("eff") != tx.get("want"):
                print("   ! 期望 %s dBm，实际 %s dBm —— 调整没生效"
                      % (tx.get("want"), tx.get("eff")))
            else:
                print("   wirksam: %s dBm" % tx.get("eff"))
        elif not self.dry:
            print("   ! 没有 txpower 回执 —— 那边是不是还跑着老固件？")

        print("   等 %.0f s，让射频路径稳下来 …" % self.settle)
        self.pump(self.settle)

        target = state_topic(addr, ep)
        t0 = time.time()
        for i in range(n):
            self.pump(0.05)                              # Restfenster leeren
            self.pub(READ_TOPIC, "%s,%d" % (addr, ep))
            got = None
            tw = time.time()
            while time.time() - tw < self.timeout and got is None:
                for t, p in self.pump(0.25):
                    if t == target:
                        got = p
            if got is not None:
                row["ok"] += 1
            sys.stdout.write("   %2d/%d %s\r" % (i + 1, n, "OK" if got else "--"))
            sys.stdout.flush()
            time.sleep(pause)
        print("   %d/%d beantwortet (%.1f s)          " % (row["ok"], n, time.time() - t0))

        self.pub(LQI_TOPIC, "")
        raw = self.wait_for(LQI_UP, 4.0)
        if raw:
            row["lqi"], row["rssi"] = dev_row(key_of(raw), addr)
        return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default="0x747d", help="设备（短地址）")
    ap.add_argument("--ep", type=int, default=1, help="Endpunkt (Panel oft 2)")
    ap.add_argument("--levels", default="20,14,8,2", help="dBm-Stufen")
    ap.add_argument("--n", type=int, default=10, help="Versuche je Stufe")
    ap.add_argument("--pause", type=float, default=1.5, help="Pause zwischen Versuchen (s)")
    ap.add_argument("--settle", type=float, default=6.0, help="Einschwingzeit je Stufe (s)")
    ap.add_argument("--timeout", type=float, default=4.0, help="Antwortfenster (s)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    levels = [int(x) for x in a.levels.split(",") if x.strip()]
    r = Runner(dry=a.dry_run, settle=a.settle, timeout=a.timeout)
    print("Broker %s:%d   Basis %s" % (mp.HOST, mp.PORT, BASE))
    print("设备 %s ep%d，每档 %d 次尝试，档位 %s"
          % (a.addr, a.ep, a.n, levels))
    if a.dry_run:
        print("（dry-run：什么都不会发）")
    r.open()

    rows = [r.measure(d, a.addr, a.ep, a.n, 0.0 if a.dry_run else a.pause)
            for d in levels]

    print("\n%-9s %-8s %-9s %-5s %-8s" % ("期望", "实际", "应答", "LQI", "RSSI"))
    print("-" * 44)
    for x in rows:
        print("%-9s %-8s %-9s %-5s %-8s" % (
            "%+d dBm" % x["want"],
            ("%+d" % x["eff"]) if x["eff"] is not None else "–",
            "%d/%d" % (x["ok"], x["n"]),
            x["lqi"] if x["lqi"] is not None else "–",
            ("%d dBm" % x["rssi"]) if x["rssi"] is not None else "–"))

    # 评估：仍能把所有尝试都答上的最小一档。
    if a.dry_run:
        return
    full = [x for x in rows if x["ok"] == x["n"]]
    if full:
        best = min(full, key=lambda x: x["eff"] if x["eff"] is not None else x["want"])
        print("\n一次都没失败的最小档位：%+d dBm" % best["want"])
        better_high = [x for x in rows if x["want"] > best["want"] and x["ok"] < x["n"]]
        if better_high:
            print("更高的档位**并没有**更好 —— 再往上 PA 也没加分。")
        else:
            print("更高的档位至少一样好。只要干扰环境允许，"
                  "最高那一档就是稳妥的选择。")
    else:
        print("\n没有哪一档能过全部尝试。那问题就不是发射功率，"
              "而是距离/天线/干扰源。")
    if any(x["eff"] is not None and x["eff"] != x["want"] for x in rows):
        print("注意：至少有一档实际跑的不是期望值 —— "
              "表里写的是实际生效的值。")


if __name__ == "__main__":
    main()
