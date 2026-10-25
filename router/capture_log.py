import sys, time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM14"
DUR  = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0

s = serial.Serial(PORT, 115200, timeout=0.2)
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
