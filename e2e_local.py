#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""e2e_local.py – 不带 Home Assistant，跑一遍生产链路的端到端测试。

    echter Broker (amqtt, 127.0.0.1:18830)
      + 真的 serial_gateway.py，当进程跑
      + echter ESP32-C6 auf COM9
      + 一个"假 HA"，做的事和 HA 的 MQTT 集成一模一样：
        Discovery-Config lesen, auf command_topic publizieren, state_topic lesen

这样就把 HA 主机上跑的那条链整个验过了 —— 只是搬到了本地：
  MQTT 命令 → add-on → UART → 设备 → 应答 → UART → add-on → state_topic

Aufruf:
    python e2e_local.py [--port COM9] [--addr 0x07da]
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ADDON = os.path.join(HERE, "host", "addon", "esp32c6_gateway", "serial_gateway.py")
VENV_PY = sys.executable

MQTT_PORT = 18830
WEB_PORT = 18082
BASE = "gw/coordinator"
HA_PREFIX = "homeassistant"

try:
    import paho.mqtt.client as mqtt
except ImportError:
    sys.exit("缺 paho-mqtt")

try:
    from amqtt.broker import Broker
except ImportError:
    sys.exit("缺 amqtt —— 已装到 --target ./.mqttenv（要设 PYTHONPATH）")


# ── Broker im Hintergrund-Thread ──────────────────────────────────────────
def start_broker(ready: threading.Event):
    config = {
        "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{MQTT_PORT}"}},
        "plugins": {
            "amqtt.plugins.authentication.AnonymousAuthPlugin": {"allow_anonymous": True},
            "amqtt.plugins.sys.broker.BrokerSysPlugin": {"sys_interval": 0},
        },
    }

    async def _run():
        broker = Broker(config)
        await broker.start()
        ready.set()
        while True:
            await asyncio.sleep(3600)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run())
    except Exception as e:
        print(f"[broker] {e}")
        ready.set()


# ── Fake-HA-Client ────────────────────────────────────────────────────────
class FakeHA:
    def __init__(self):
        self.msgs = []
        self.connected = threading.Event()
        self.c = mqtt.Client(client_id="fake-ha")
        self.c.on_connect = self._on_connect
        self.c.on_message = self._on_message

    def _on_connect(self, cl, ud, flags, rc):
        cl.subscribe([("homeassistant/#", 1), (BASE + "/#", 1)])
        self.connected.set()

    def _on_message(self, cl, ud, msg):
        try:
            pl = json.loads(msg.payload.decode())
        except Exception:
            pl = msg.payload.decode(errors="replace")
        self.msgs.append((msg.topic, pl, time.time()))

    def start(self):
        self.c.connect("127.0.0.1", MQTT_PORT, 30)
        self.c.loop_start()
        if not self.connected.wait(8):
            raise RuntimeError("假 HA 连不上")

    def stop(self):
        self.c.loop_stop()
        self.c.disconnect()

    def publish(self, topic, payload, retain=False):
        self.c.publish(topic, payload, qos=1, retain=retain)

    def find(self, topic, timeout=10.0, pred=None):
        end = time.time() + timeout
        while time.time() < end:
            for t, p, _ in reversed(self.msgs):
                if t == topic and (pred is None or pred(p)):
                    return p
            time.sleep(0.15)
        return None

    def all_on(self, topic):
        return [p for t, p, _ in self.msgs if t == topic]


def retained_value(topic, timeout=3.0):
    """一个全新客户端：用来证明这个值是以 retained 存在 broker 里的。"""
    got = {}

    def on_c(cl, ud, flags, rc):
        cl.subscribe(topic, 1)

    def on_m(cl, ud, msg):
        try:
            got["v"] = json.loads(msg.payload.decode())
        except Exception:
            got["v"] = msg.payload.decode(errors="replace")

    c = mqtt.Client(client_id="retain-check-%d" % int(time.time() * 1000))
    c.on_connect = on_c
    c.on_message = on_m
    c.connect("127.0.0.1", MQTT_PORT, 30)
    c.loop_start()
    end = time.time() + timeout
    while time.time() < end and "v" not in got:
        time.sleep(0.1)
    c.loop_stop()
    c.disconnect()
    return got.get("v")


OK = 0
FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  OK   " + name + ((": " + str(extra)) if extra else ""))
    else:
        FAIL += 1
        print("  FAIL " + name + ((": " + str(extra)) if extra else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM9")
    ap.add_argument("--addr", default="0x07da")
    ap.add_argument("--boot-wait", type=float, default=18.0)
    args = ap.parse_args()

    ep = 16
    state_topic = "%s/zigbee/%s/ep%d/on_off" % (BASE, args.addr, ep)
    cmd_topic = "%s/set/%s/ep%d/on_off" % (BASE, args.addr, ep)
    disc_topic = "%s/switch/c6_%s/ep%d_on_off/config" % (HA_PREFIX, args.addr, ep)

    print("== 1. Broker starten ==")
    ready = threading.Event()
    threading.Thread(target=start_broker, args=(ready,), daemon=True).start()
    if not ready.wait(10):
        sys.exit("broker 起不来")
    print("  Broker auf 127.0.0.1:%d" % MQTT_PORT)

    print("\n== 2. 把 serial_gateway.py 当进程启动 ==")
    env = dict(os.environ)
    env.update({"WEB_PORT": str(WEB_PORT), "DB_HOST": "", "HA_DISCOVERY": "1",
                "LOG_TZ": "Asia/Shanghai"})
    log = open(os.path.join(HERE, "e2e_addon.log"), "wb")
    proc = subprocess.Popen(
        [VENV_PY, "-u", ADDON, "--port", args.port, "--broker", "127.0.0.1",
         "--mqport", str(MQTT_PORT), "--base", BASE],
        cwd=os.path.dirname(ADDON), env=env, stdout=log, stderr=subprocess.STDOUT)
    print("  PID %d, Log: e2e_addon.log" % proc.pid)

    ha = FakeHA()
    try:
        ha.start()
        st = ha.find(BASE + "/status", timeout=15)
        check("add-on meldet online", st == "online", st)

        print("\n== 3. 等 %.0f s（Zigbee 网络要启动时间）==" % args.boot_wait)
        time.sleep(args.boot_wait)

        print("\n== 4. 通过 MQTT 问设备（probe）-> discovery ==")
        ha.publish(BASE + "/cmd/probe", "%s,%d" % (args.addr, ep))
        cfg = ha.find(disc_topic, timeout=15)
        check("Discovery-Config fuer ep%d angekommen" % ep, cfg is not None, disc_topic)
        if cfg:
            check("state_topic korrekt", cfg.get("state_topic") == state_topic, cfg.get("state_topic"))
            check("command_topic korrekt", cfg.get("command_topic") == cmd_topic, cfg.get("command_topic"))
            check("payload_on/off", (cfg.get("payload_on"), cfg.get("payload_off")) == ("1", "0"),
                  "%s/%s" % (cfg.get("payload_on"), cfg.get("payload_off")))
            check("unique_id", bool(cfg.get("unique_id")), cfg.get("unique_id"))

        print("\n== 5. HA schaltet EIN (set -> on) ==")
        ha.publish(cmd_topic, "1")
        v = ha.find(state_topic, timeout=8, pred=lambda p: p == {"v": 1})
        check("state_topic = {v:1}", v == {"v": 1}, v)
        rv = retained_value(state_topic)
        check("值是 retained（新客户端立刻就能看到）", rv == {"v": 1}, rv)

        print("\n== 6. HA schaltet AUS (set -> off) ==")
        ha.publish(cmd_topic, "0")
        v = ha.find(state_topic, timeout=8, pred=lambda p: p == {"v": 0})
        check("state_topic = {v:0}", v == {"v": 0}, v)

        print("\n== 7. toggle dreht weiter ==")
        ha.publish(cmd_topic, "toggle")
        v = ha.find(state_topic, timeout=8, pred=lambda p: p == {"v": 1})
        check("从 0 toggle -> {v:1}", v == {"v": 1}, v)

        print("\n== 8. 设备真的把命令都回执了 ==")
        # console 里的行是 JSON 字符串 —— FakeHA 里的 json.loads() 会把它们变成
        # dict。所以这里两种形态都还原成文本。
        # add-on 的状态回声立刻就到，设备的无线 ACK 要等几百 ms
        # 之后 -> 最多轮询 8 s。
        acked = []
        end = time.time() + 8
        while time.time() < end:
            console = [c if isinstance(c, str) else json.dumps(c, separators=(",", ":"))
                       for c in ha.all_on(BASE + "/console")]
            acked = [c for c in console
                     if '"cl":"0x0006"' in c and '"hex":"' in c and "0b0" in c]
            if len(acked) >= 3:
                break
            time.sleep(0.3)
        check("至少 3 帧 Default-Response 来自设备", len(acked) >= 3, len(acked))
        for c in acked[:3]:
            print("      " + c)

        print("\n== 9. 批量：三个端点都拿到实体 ==")
        ha.publish(BASE + "/cmd/probe", "%s,17" % args.addr)
        time.sleep(3)
        ha.publish(BASE + "/cmd/probe", "%s,18" % args.addr)
        time.sleep(4)
        cfgs = [t for t, _, _ in ha.msgs if t.endswith("_on_off/config")]
        check("3 Switch-Discovery-Configs", len(set(cfgs)) == 3,
              ", ".join(sorted(set(cfgs))))
    finally:
        ha.stop()
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        log.close()

    print("\n%d OK, %d FAIL" % (OK, FAIL))
    print("ALLE TESTS BESTANDEN" if FAIL == 0 else "TESTS FEHLGESCHLAGEN")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
