#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ota_test.py – 直接通过 COM9 驱动固件 OTA（不经过 MQTT/HA）。

Protokoll (identisch zu serial_gateway.py):
    {"cmd":"fw_ota_start","size":N}
    {"cmd":"fw_ota_data","off":O,"data":"<HEX>"}
    {"cmd":"fw_ota_end"}
Antworten:
    {"t":"fw_ota","st":"begin"|"write"|"done"|"error", ...}

这个测试先复现旧行为（80 B / 每批 16 帧 / 不带延时），再换别的节拍重跑，
以便把 1280 B 处中断的原因圈出来。

Beispiele:
    python ota_test.py --chunk 80  --batch 16 --delay 0
    python ota_test.py --chunk 240 --batch 8  --delay 0.005
"""
import argparse
import json
import os
import sys
import time

import serial

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BIN = os.path.join(HERE, "build", "zigbee-vital-sensor.bin")


def open_port(port, baud=115200):
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = port, baud, 0.05
    ser.rtscts = ser.dsrdtr = False
    ser.open()
    try:
        ser.dtr = False
        ser.rts = False
    except Exception:
        pass
    return ser


class Link:
    def __init__(self, ser):
        self.ser = ser
        self.buf = b""
        self.msgs = []

    def send(self, obj):
        self.ser.write(json.dumps(obj, separators=(",", ":")).encode() + b"\n")

    def pump(self, secs):
        """读 `secs` 秒，把新出现的 JSON 对象返回。"""
        out = []
        end = time.time() + secs
        while time.time() < end:
            try:
                chunk = self.ser.read(self.ser.in_waiting or 1)
            except Exception as e:
                print(f"[serial] {e}")
                break
            if not chunk:
                continue
            self.buf += chunk
            while b"\n" in self.buf:
                raw, self.buf = self.buf.split(b"\n", 1)
                txt = raw.decode("utf-8", errors="replace").strip()
                if not txt.startswith("{"):
                    continue
                try:
                    m = json.loads(txt)
                except Exception:
                    continue
                out.append(m)
                self.msgs.append(m)
        return out

    def wait_for(self, pred, timeout):
        end = time.time() + timeout
        while time.time() < end:
            for m in self.pump(0.1):
                if pred(m):
                    return m
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM9")
    ap.add_argument("--bin", default=DEFAULT_BIN)
    ap.add_argument("--chunk", type=int, default=80)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--delay", type=float, default=0.0, help="Pause pro Frame")
    ap.add_argument("--ack-timeout", type=float, default=6.0)
    ap.add_argument("--boot-wait", type=float, default=3.0)
    ap.add_argument("--verify-reboot", action="store_true",
                    help="fw_ota_end 之后核对启动日志（需要重新打开端口）")
    args = ap.parse_args()

    size = os.path.getsize(args.bin)
    frames = -(-size // args.chunk)
    print(f"Bin: {args.bin} ({size} B)")
    print(f"计划：{frames} 帧 × {args.chunk} B，每批 {args.batch} 帧，"
          f"delay={args.delay}s\n")

    ser = open_port(args.port)
    link = Link(ser)
    link.pump(args.boot_wait)          # Boot abwarten
    t0 = time.time()

    link.send({"cmd": "fw_ota_start", "size": size})
    m = link.wait_for(lambda x: x.get("t") == "fw_ota", 10)
    if not m:
        print("FAIL  C6 没回 'begin'")
        ser.close()
        return 1
    if m.get("st") == "error":
        print(f"FAIL  begin: {m.get('why')}")
        ser.close()
        return 1
    print(f"begin -> part={m.get('part')} size={m.get('size')}")

    off = 0
    last_ack = 0
    with open(args.bin, "rb") as f:
        while off < size:
            batch_end = min(off + args.chunk * args.batch, size)
            while off < batch_end:
                f.seek(off)
                data = f.read(min(args.chunk, size - off))
                link.send({"cmd": "fw_ota_data", "off": off, "data": data.hex()})
                off += len(data)
                if args.delay:
                    time.sleep(args.delay)

            if off >= size:
                # C6 只在每 FW_ACK_EVERY（16）帧回一次执。若总数
                # 不是 16 的整数倍，最后一小批就
                # 再也不会有 ACK -> 立刻转到收尾
                # 步骤。顺序由串口字节流本身
                # garantiert (rx_task verarbeitet strikt nacheinander).
                total_frames = -(-size // args.chunk)
                if total_frames % args.batch == 0:
                    ack = link.wait_for(
                        lambda x: x.get("t") == "fw_ota" and x.get("st") in
                                  ("write", "error", "done"), args.ack_timeout)
                    if ack and ack.get("st") == "error":
                        print(f"\nFAIL  C6 meldet error: {ack.get('why')}")
                        ser.close()
                        return 3
                else:
                    print(f"  最后一批没有 ACK "
                          f"（共 {total_frames} 帧，每批 {args.batch} 帧）-> 收尾")
                break

            ack = link.wait_for(
                lambda x: x.get("t") == "fw_ota" and x.get("st") in
                          ("write", "error", "done"), args.ack_timeout)
            if not ack:
                print(f"\nFAIL  Timeout: gesendet {off}/{size}, "
                      f"letzter ACK {last_ack} "
                      f"({last_ack * 100 // size} %)")
                print(f"      卡住了，停在 {time.time()-t0:.1f}s")
                ser.close()
                return 2
            if ack.get("st") == "error":
                print(f"\nFAIL  C6 meldet error: why={ack.get('why')} "
                      f"bei off={off}")
                ser.close()
                return 3
            last_ack = ack.get("off", last_ack)
            print(f"  ACK off={last_ack} ({ack.get('pct', '?')} %) "
                  f"[gesendet {off}]")

    print(f"\n全部字节发完，用时 {time.time()-t0:.1f}s -> fw_ota_end")
    link.send({"cmd": "fw_ota_end"})
    m = link.wait_for(lambda x: x.get("t") == "fw_ota" and
                      x.get("st") in ("done", "error"), 20)
    ser.close()
    if not m:
        print("FAIL  fw_ota_end 之后没有 'done'")
        return 4
    if m.get("st") == "error":
        print(f"FAIL  end: {m.get('why')}")
        return 5
    print(f"OK    done, written={m.get('written')}  Gesamt {time.time()-t0:.1f}s")

    if args.verify_reboot:
        time.sleep(3)
        ser = open_port(args.port)
        link = Link(ser)
        link.pump(6)
        ser.close()
        txt = json.dumps(link.msgs)
        boot_msgs = [x for x in link.msgs if x.get("t") == "boot"]
        print(f"Reboot: boot-Frame={'ja' if boot_msgs else 'nein'}, "
              f"{len(link.msgs)} Frames total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
