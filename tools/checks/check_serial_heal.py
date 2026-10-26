"""临时检查：串口掉线自愈（Errno 5 / EIO 后必须换 fd，不能原地重试）。

复现的现象（2026-10-26 重建 add-on 后）：连续 100+ 行
    [Serial] Lese-Fehler: [Errno 5] I/O error – versuche weiter
网关一台设备都收不到。旧代码只 sleep 0.5 再读同一个 fd，永远不好。
"""
import sys, types, importlib.util

STATE = {"fail_open": False}
OPENED = {"n": 0}

fake_serial = types.ModuleType("serial")


class _FakeSer:
    def __init__(self, *a, **k):
        self.dtr = False
        self.rts = False

    def open(self):
        if STATE["fail_open"]:
            raise OSError(5, "Input/output error")
        OPENED["n"] += 1

    def close(self): pass
    def write(self, d): return len(d)
    def read(self, n): raise KeyboardInterrupt("test-stop")

    @property
    def in_waiting(self): raise KeyboardInterrupt("test-stop")


fake_serial.Serial = _FakeSer
sys.modules["serial"] = fake_serial
sys.modules["serial.serialutil"] = types.ModuleType("serial.serialutil")
sys.modules["pymysql"] = types.ModuleType("pymysql")
paho = types.ModuleType("paho"); mqttm = types.ModuleType("paho.mqtt")
clientm = types.ModuleType("paho.mqtt.client")


class _FakeMQTT:
    def __init__(self, *a, **k): pass
    def __getattr__(self, name): return lambda *a, **k: None


clientm.Client = _FakeMQTT
sys.modules["paho"] = paho
sys.modules["paho.mqtt"] = mqttm
sys.modules["paho.mqtt.client"] = clientm

sys.argv = ["serial_gateway.py", "--port", "/dev/null", "--baud", "115200",
            "--broker", "127.0.0.1", "--mqport", "1883",
            "--base", "gw/coordinator", "--topo-interval", "0"]
spec = importlib.util.spec_from_file_location("sg", "host/addon/esp32c6_gateway/serial_gateway.py")
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)          # in_waiting 抛 Ki 立即结束主循环
except KeyboardInterrupt:
    pass

# 后面要跑几十秒的指数退避，睡没了才测得动
mod.time.sleep = lambda s: None

fail = 0


def chk(cond, msg):
    global fail
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        fail += 1


print("== 开机端口打不开：必须给占位串口，不能抛异常")
STATE["fail_open"] = True
s = mod._serial_open("/dev/null", 115200, attempts=3, first_delay=0)
chk(isinstance(s, mod._NullSerial), "返回 _NullSerial（进程能继续起，不被 Supervisor 反复重启）")
try:
    s.in_waiting
    chk(False, "占位串口的 in_waiting 应该抛 OSError")
except OSError:
    chk(True, "占位串口的 in_waiting 抛 OSError")
try:
    s.read(1)
    chk(False, "占位串口的 read 应该抛 OSError")
except OSError:
    chk(True, "占位串口的 read 抛 OSError（主循环会走重开分支）")

print("== 设备回来后：_serial_reopen 换新 fd")
before = OPENED["n"]
STATE["fail_open"] = False
ok = mod._serial_reopen("模拟 EIO")
chk(ok is True, "_serial_reopen 返回 True")
chk(OPENED["n"] == before + 1, "确实新建了一个串口对象（换了 fd，不是原地重试）")
chk(isinstance(mod.ser, mod._CoordinatorSerialAlias) if hasattr(mod, "_CoordinatorSerialAlias")
    else mod.ser.__class__.__name__ == "CoordinatorSerial",
    "全局 ser 已换成真的 CoordinatorSerial")

print("== 重开也失败时：不能死循环卡死，要返回 False 让调用方下轮再来")
STATE["fail_open"] = True
ok2 = mod._serial_reopen("再模拟一次")
chk(ok2 is False, "6 次都失败 → 返回 False（主循环下轮再试）")

print("== 刷机期间（_serial_pause 置位）不许抢端口")
STATE["fail_open"] = False
mod._serial_pause.set()
ok3 = mod._serial_reopen("刷机中")
mod._serial_pause.clear()
chk(ok3 is False, "刷机中返回 False（不和 esptool 抢端口）")

print("== 写失败必须被吞掉（否则顺着 paho 回调炸穿 MQTT 线程）")


class _BadSer:
    def write(self, d): raise OSError(5, "Input/output error")
    def close(self): pass


real = mod.ser
real.ser = _BadSer()
r = real.write(b'{"cmd":"on"}')
chk(r == 0, "write 返回 0 而不是抛 SerialException（HA 开关命令不再拖死线程）")
chk(mod._write_err >= 1, "失败计数已 +1（日志限速用）")
real.ser = None

print("== 硬复位：RTS 拉低再放开，DTR 绝不能动（否则进下载模式）")


class _TogSer:
    log = []

    def __init__(self): self._rts = None

    @property
    def rts(self): return self._rts

    @rts.setter
    def rts(self, v):
        type(self).log.append(v)
        self._rts = v


real.ser = _TogSer()
ok_rst = mod._serial_kick_reset("测试")
chk(ok_rst is True, "_serial_kick_reset 返回 True")
chk(_TogSer.log == [True, False], f"RTS 序列 = {_TogSer.log}（拉低→放开）")
chk(not hasattr(real.ser, "dtr") or real.ser.dtr is None, "没碰 DTR")

print("RESULT:", "FAIL" if fail else "ALL OK")
sys.exit(1 if fail else 0)
