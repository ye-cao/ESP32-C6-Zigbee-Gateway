#!/usr/bin/env python3
"""esptool 应急刷写（bootloader 通道）的测试。

背景：当时跑着的 C6 固件不认 fw_ota_data（fgets 只有 256 字节缓冲 /
strstr 写死的），应用层 OTA 在第一批之后就停在 1280 字节。绕法：
让 esptool 把镜像直接写进 ota_0 —— 为此读取循环要暂停、端口要独占。

不接硬件就能测：
  1. Kommandozeile: Chip, Port, Reset-Modus, Offset 0x20000, write_flash
  2. flash_apply 会停读取循环、关端口、再重新打开
  3. 状态走 idle → running → done（rc != 0 时是 error）
  4. HTTP 路由 /api/fw_flash 存在，并且会起一个线程
  5. Dockerfile 把 esptool 钉在 <5（那个版本里命令还叫 write_flash）
"""
import ast
import sys
import threading
from pathlib import Path

ADDON = Path(__file__).parent / "host" / "addon" / "esp32c6_gateway"
SRC = ADDON / "serial_gateway.py"
WANT = ("_esptool_cmd", "flash_apply", "flash_status", "flash_is_running")


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    return bool(cond)


class FakePopen:
    """替换 subprocess.Popen —— 回放预设的行和返回码。"""
    last_cmd = None
    rc = 0

    def __init__(self, cmd, **kw):
        FakePopen.last_cmd = cmd
        self.stdout = iter(["esptool.py v4.8.1", "Chip is ESP32-C6",
                            "Wrote 789760 bytes", "Hash of data verified."])

    def wait(self):
        return FakePopen.rc


class FakeSerial:
    def __init__(self, *a, **kw):
        self.closed = False
        self.port = a[0] if a else None

    def close(self):
        self.closed = True


class FakeSubprocess:
    Popen  = FakePopen
    PIPE   = -1
    STDOUT = -2


def load():
    src = SRC.read_text(encoding="utf-8")
    chunks = [ast.get_source_segment(src, n)
              for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name in WANT]
    assert len(chunks) == len(WANT), f"{len(chunks)}/{len(WANT)} Funktionen"

    class Args:
        port = "/dev/serial/by-id/usb-Espressif_USB_JTAG_test-if00"
        baud = 115200

    created = []
    real_serial = FakeSerial

    class TrackedSerial(real_serial):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)

    ns = {
        "sys": sys, "subprocess": FakeSubprocess, "time": __import__("time"),
        "threading": threading,
        "args": Args, "APP_OFFSET": "0x20000", "FLASH_BAUD": 460800,
        "_serial_pause": threading.Event(),
        "_flash_mutex": threading.Lock(),
        "_flash_state_lock": threading.Lock(),
        "_flash_state": {"status": "idle", "bin": "", "tail": []},
        "CoordinatorSerial": TrackedSerial,
        "ser": TrackedSerial(Args.port, Args.baud),
        "print": lambda *a, **k: None,
        "__builtins__": __builtins__,
    }
    exec("\n\n".join(chunks), ns)
    ns["_created"] = created
    return ns


def main():
    ok = True
    ns = load()

    print("\n[1] esptool-Kommandozeile")
    cmd = ns["_esptool_cmd"]("/data/fw_upload.bin")
    joined = " ".join(cmd)
    ok &= check(cmd[1:3] == ["-m", "esptool"], "通过 python -m esptool 调用")
    ok &= check("--chip esp32c6" in joined, "Chip esp32c6")
    ok &= check("--port /dev/serial/by-id/usb-Espressif_USB_JTAG_test-if00" in joined,
                "端口被透传下去")
    ok &= check("--before default_reset" in joined, "刷写前先复位")
    ok &= check("--after hard_reset" in joined, "刷写后重启")
    ok &= check("write_flash" in cmd, "Kommando write_flash (esptool 4.x)")
    ok &= check(cmd[-3:] == ["write_flash", "0x20000", "/data/fw_upload.bin"]
                or ("0x20000" in cmd and cmd[-1] == "/data/fw_upload.bin"),
                f"偏移 0x20000 + 文件（boot 日志说是 oea_0）→ {cmd[-3:]}")

    print("\n[2] flash_apply：暂停、关端口、再打开")
    old_ser = ns["ser"]
    FakePopen.rc = 0
    result = ns["flash_apply"]("/data/fw_upload.bin")
    ok &= check(result is True, "_flash_apply meldet Erfolg")
    ok &= check(old_ser.closed, "旧的串口关掉了")
    ok &= check(len(ns["_created"]) >= 1, "CoordinatorSerial 重新打开了")
    ok &= check(not ns["_serial_pause"].is_set(),
                "读取循环恢复（_serial_pause 已清除）")
    ok &= check(ns["_flash_state"]["status"] == "done", "Status = done")
    ok &= check(len(ns["_flash_state"]["tail"]) >= 3,
                f"Ausgabe mitgeschnitten ({len(ns['_flash_state']['tail'])} Zeilen)")
    ok &= check(FakePopen.last_cmd[-1] == "/data/fw_upload.bin",
                "esptool 拿到的是对的文件")

    print("\n[3] Fehlerfall rc=1")
    FakePopen.rc = 1
    ok &= check(ns["flash_apply"]("/data/fw_upload.bin") is False, "Rückgabe False")
    ok &= check(ns["_flash_state"]["status"] == "error", "Status = error")
    ok &= check(not ns["_serial_pause"].is_set(), "不管怎样读取循环都放开了")
    ok &= check(not ns["_flash_mutex"].locked(), "锁已释放（没有死锁）")
    FakePopen.rc = 0

    print("\n[4] HTTP 路由和界面")
    src = SRC.read_text(encoding="utf-8")
    ok &= check('path == "/api/fw_flash"' in src, "Route /api/fw_flash im do_POST")
    ok &= check('action="/api/fw_flash"' in src, "Formular im Dashboard vorhanden")
    ok &= check("threading.Thread(target=flash_apply" in src,
                "刷写在后台线程里跑")
    ok &= check("_serial_pause.is_set()" in src, "Leseschleife beachtet die Pause")

    print("\n[5] Dockerfile")
    docker = (ADDON / "Dockerfile").read_text(encoding="utf-8")
    ok &= check("esptool" in docker, "会安装 esptool")
    ok &= check("esptool>=4.7,<5" in docker, "auf <5 gepinnt (write_flash)")

    print()
    print("全部通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
