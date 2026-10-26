"""OTA 吞吐监视：轮询 /api/state 的 ota.transferred，输出 B/s 与停顿。"""
import json, sys, time, urllib.request

URL = "http://192.168.101.5:8082/api/state"
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0


def st():
    with urllib.request.urlopen(URL, timeout=5) as r:
        return json.load(r)


op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    with op.open(URL, timeout=5) as r:
        d = json.load(r)
except Exception as e:
    print("state nicht lesbar:", e)
    raise SystemExit(1)

t0 = time.time()
base = None
last_off = None
last_t = None
stall = 0.0
maxstall = 0.0
stop = 0
while time.time() - t0 < DUR:
    try:
        with op.open(URL, timeout=5) as r:
            d = json.load(r)
    except Exception as e:
        print(f"  ! state: {e}")
        time.sleep(2)
        continue
    o = d.get("ota") or {}
    off = int(o.get("transferred") or 0)
    size = int(o.get("size") or 0)
    now = time.time()
    if base is None:
        base, base_t = off, now
    if last_off is None:
        last_off, last_t = off, now
    elif off == last_off and off < size:
        st = now - last_t
        stop = st
        maxstall = max(maxstall, st)
    else:
        last_t, last_off = now, off
    el = now - base_t
    if el >= 5:
        print(f"[{time.strftime('%H:%M:%S')}] {off}/{size} "
              f"({100.0*off/size if size else 0:5.1f}%)  "
              f"avg {(off-base)/el:7.1f} B/s  "
              f"stall {stop:4.0f}s  max {maxstall:4.0f}s  st={o.get('status')}")
    if o.get("status") in ("done", "error"):
        print("状态:", o.get("status"))
        break
    time.sleep(3)
