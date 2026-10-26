import json, sys, time
import paho.mqtt.client as mqtt
got = {}
def on_connect(c, u, flags, rc, props=None):
    print("CONNACK rc =", rc, flush=True)
    c.subscribe("homeassistant/+/+/+/config", qos=0)
    c.subscribe("gw/coordinator/dev/+/status", qos=0)
def on_msg(c, u, m):
    got[m.topic] = m.payload.decode("utf-8", "replace")
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="dump%d" % int(time.time()))
c.on_connect = on_connect
c.on_message = on_msg
c.connect("192.168.101.5", 1883, keepalive=30)
t0 = time.time()
while time.time() - t0 < 5:
    c.loop(timeout=0.3)
print("msgs:", len(got))
for t in sorted(got):
    if t.startswith("homeassistant/"):
        try:
            d = json.loads(got[t])
        except Exception:
            continue
        av = d.get("availability") or []
        n = len(av)
        print(f"{n} src  {t}   av0={(av[0] if av else {}).get('topic')}")
    else:
        print(f"  avail {t} = {got[t]}")
