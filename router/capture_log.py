import sys, time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM14"
DUR  = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
# 第 3 个参数写 noreset 就不拉 RTS：用于观察正在运行的板子（例如下发命令后
# 看日志），不会把设备重启掉。
NO_RESET = len(sys.argv) > 3 and sys.argv[3].lower() in ("noreset", "n", "0")

s = serial.Serial(PORT, 115200, timeout=0.2)
if not NO_RESET:
    # 干净复位：Nano ESP32-C6 / USB-Serial-JTAG 上 RTS 拉低即拉低 EN
    s.setDTR(False)
    s.setRTS(True)
    time.sleep(0.15)
    s.setRTS(False)
    time.sleep(0.05)

t0 = time.time()
buf = bytearray()
while time.time() - t0 < DUR:
    d = s.read(4096)
    if d:
        buf.extend(d)
s.close()

sys.stdout.write(buf.decode("utf-8", "replace"))
print("\n--- captured %d bytes in %.0f s ---" % (len(buf), DUR))
