"""检查：Wi-Fi/TCP 传输通道（--link tcp）。

要验证的是「换一根线，协议不变」这一条：
  1. --link tcp 时开机走的是 CoordinatorTcp（不是串口）；
  2. 两个通道的接口一致（read/write/close/in_waiting/can_reset），
     主循环才能一个字都不用改；
  3. TCP 通道 can_reset=False —— 没有 RTS，绝不能去调「硬复位」；
  4. 真起一个监听，用真 socket 走一遍：accept → 读 → 写 → 断开 → 重新 accept。

★ 为什么第 3 条单独列出来：主循环里有个「45 s 没数据就复位芯片」的看门狗，
  如果不认 can_reset，TCP 模式下它会每 90 s 打一次「强制复位 C6」的日志，
  然后什么也没发生 —— 排查时会被这条假日志带偏。
"""
import sys, types, socket, importlib.util

STATE = {"recv_ki": True}


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ── 桩：pyserial / paho / pymysql ──────────────────────────────────────────
fake_serial = types.ModuleType("serial")
fake_serial.Serial = type("S", (), {"__init__": lambda self, *a, **k: None})
sys.modules["serial"] = fake_serial
sys.modules["serial.serialutil"] = types.ModuleType("serial.serialutil")
sys.modules["pymysql"] = types.ModuleType("pymysql")
paho = types.ModuleType("paho")
sys.modules["paho"] = paho
sys.modules["paho.mqtt"] = types.ModuleType("paho.mqtt")
_c = types.ModuleType("paho.mqtt.client")


class _FakeMQTT:
    def __init__(self, *a, **k): pass
    def __getattr__(self, name): return lambda *a, **k: None


_c.Client = _FakeMQTT
sys.modules["paho.mqtt.client"] = _c

# ── 桩：socket —— 让主循环第一次 recv 就抛 KeyboardInterrupt 结束导入 ──────
_real_socket = socket.socket


class _KiConn:
    def setsockopt(self, *a): pass
    def settimeout(self, *a): pass
    def close(self): pass
    def recv(self, n): raise KeyboardInterrupt("test-stop")
    def sendall(self, d): pass


class _FakeSrv:
    def __init__(self, *a, **k): pass
    def setsockopt(self, *a): pass
    def bind(self, *a): pass
    def listen(self, *a): pass
    def settimeout(self, *a): pass
    def accept(self): return _KiConn(), ("127.0.0.1", 5555)
    def close(self): pass


PORT = _free_port()
socket.socket = _FakeSrv

sys.argv = ["serial_gateway.py", "--link", "tcp", "--tcp-port", str(PORT),
            "--tcp-bind", "127.0.0.1", "--broker", "127.0.0.1", "--mqport", "1883",
            "--base", "gw/coordinator", "--topo-interval", "0"]
spec = importlib.util.spec_from_file_location(
    "sg", "host/addon/esp32c6_gateway/serial_gateway.py")
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
except KeyboardInterrupt:
    pass

socket.socket = _real_socket          # 后面的真 socket 测试要用真的
mod.time.sleep = lambda s: None

fail = 0


def chk(cond, msg):
    global fail
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        fail += 1


print("== 开机走的是 TCP 通道，不是串口")
chk(mod.args.link == "tcp", "--link tcp 生效")
chk(mod.ser.__class__.__name__ == "CoordinatorTcp",
    f"全局 ser 是 CoordinatorTcp（实际 {mod.ser.__class__.__name__}）")

print("== 接口必须和 CoordinatorSerial 一致（主循环不改一行）")
for name in ("read", "write", "close", "in_waiting"):
    chk(hasattr(mod.ser, name), f"有 {name}()")
chk(mod.ser.in_waiting == 0,
    "in_waiting 恒为 0 ⇒ 主循环走 read(1)，由 recv 自己等（不会空转）")

print("== TCP 通道不能「硬复位」")
chk(getattr(mod.ser, "can_reset", True) is False, "can_reset == False")
chk(mod._link_kick_reset("测试") is False,
    "_link_kick_reset 返回 False（没有 RTS，不能假装复位了）")
chk(getattr(mod.CoordinatorSerial, "can_reset", False) is True,
    "对照：串口通道 can_reset == True（看门狗仍然管用）")

print("== 真 socket 走一遍：accept → 读 → 写 → 断开 → 重新 accept")
port = _free_port()
srv = mod.CoordinatorTcp("127.0.0.1", port)
cli = _real_socket(socket.AF_INET, socket.SOCK_STREAM)
cli.connect(("127.0.0.1", port))
cli.sendall(b'{"t":"boot"}\n')
got = b""
for _ in range(20):                       # accept + recv 各需要一次 read()
    got += srv.read(1)
    if b"\n" in got:
        break
chk(got == b'{"t":"boot"}\n', f"收到的字节原样返回（{got!r}）")
chk(b"{" in got, "整行没有在 TCP 层被切开或改写 —— 拆行仍归主循环")

n = srv.write(b'{"cmd":"lqi"}\n')
cli.settimeout(2.0)
back = cli.recv(4096)
chk(n == len(b'{"cmd":"lqi"}\n'), f"write 返回写出的字节数（{n}）")
chk(back == b'{"cmd":"lqi"}\n', "对端原样收到命令行")

cli.close()
for _ in range(20):                       # 读到对端关闭 ⇒ 内部 drop
    srv.read(1)
    if srv.conn is None:
        break
chk(srv.conn is None, "对端断开后连接已释放（不会卡在一个死 fd 上）")

cli2 = _real_socket(socket.AF_INET, socket.SOCK_STREAM)
cli2.connect(("127.0.0.1", port))
cli2.sendall(b'{"cmd":"net_status"}\n')
got2 = b""
for _ in range(20):
    got2 += srv.read(1)
    if b"\n" in got2:
        break
chk(got2 == b'{"cmd":"net_status"}\n', "C6 重连后立刻又能收数据（无需重启进程）")
cli2.close()
srv.close()

print("RESULT:", "FAIL" if fail else "ALL OK")
sys.exit(1 if fail else 0)
