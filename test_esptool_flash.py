#!/usr/bin/env python3
"""Test für den esptool-Notfall-Flash (Bootloader-Weg).

Hintergrund: die laufende C6-Firmware versteht fw_ota_data nicht (fgets mit
256-Byte-Puffer / starrer strstr), der App-Level-OTA bleibt nach dem ersten
Stoß bei 1280 Bytes stehen. Ausweg: esptool schreibt das Image direkt in
ota_0 – dafür muss die Leseschleife pausieren und der Port exklusiv sein.

Getestet wird ohne Hardware:
  1. Kommandozeile: Chip, Port, Reset-Modus, Offset 0x20000, write_flash
  2. flash_apply hält die Leseschleife an, schließt den Port, öffnet ihn wieder
  3. Status geht idle → running → done (bzw. error bei rc != 0)
  4. HTTP-Route /api/fw_flash existiert und startet einen Thread
  5. Dockerfile pinnt esptool <5 (dort heißt das Kommando noch write_flash)
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
    """Ersetzt subprocess.Popen – liefert vorgegebene Zeilen und Returncode."""
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
    ok &= check(cmd[1:3] == ["-m", "esptool"], "Aufruf über python -m esptool")
    ok &= check("--chip esp32c6" in joined, "Chip esp32c6")
    ok &= check("--port /dev/serial/by-id/usb-Espressif_USB_JTAG_test-if00" in joined,
                "Port wird durchgereicht")
    ok &= check("--before default_reset" in joined, "Reset vor dem Flashen")
    ok &= check("--after hard_reset" in joined, "Neustart nach dem Flashen")
    ok &= check("write_flash" in cmd, "Kommando write_flash (esptool 4.x)")
    ok &= check(cmd[-3:] == ["write_flash", "0x20000", "/data/fw_upload.bin"]
                or ("0x20000" in cmd and cmd[-1] == "/data/fw_upload.bin"),
                f"Offset 0x20000 + Datei (oea_0 laut boot-Log) → {cmd[-3:]}")

    print("\n[2] flash_apply: Pause, Port zu, Port wieder auf")
    old_ser = ns["ser"]
    FakePopen.rc = 0
    result = ns["flash_apply"]("/data/fw_upload.bin")
    ok &= check(result is True, "_flash_apply meldet Erfolg")
    ok &= check(old_ser.closed, "alter Serial-Port wurde geschlossen")
    ok &= check(len(ns["_created"]) >= 1, "CoordinatorSerial wurde neu geöffnet")
    ok &= check(not ns["_serial_pause"].is_set(),
                "Leseschleife läuft wieder (_serial_pause gelöscht)")
    ok &= check(ns["_flash_state"]["status"] == "done", "Status = done")
    ok &= check(len(ns["_flash_state"]["tail"]) >= 3,
                f"Ausgabe mitgeschnitten ({len(ns['_flash_state']['tail'])} Zeilen)")
    ok &= check(FakePopen.last_cmd[-1] == "/data/fw_upload.bin",
                "esptool bekam die richtige Datei")

    print("\n[3] Fehlerfall rc=1")
    FakePopen.rc = 1
    ok &= check(ns["flash_apply"]("/data/fw_upload.bin") is False, "Rückgabe False")
    ok &= check(ns["_flash_state"]["status"] == "error", "Status = error")
    ok &= check(not ns["_serial_pause"].is_set(), "Leseschleife trotzdem wieder frei")
    ok &= check(not ns["_flash_mutex"].locked(), "Mutex wieder freigegeben (kein Deadlock)")
    FakePopen.rc = 0

    print("\n[4] HTTP-Route und UI")
    src = SRC.read_text(encoding="utf-8")
    ok &= check('path == "/api/fw_flash"' in src, "Route /api/fw_flash im do_POST")
    ok &= check('action="/api/fw_flash"' in src, "Formular im Dashboard vorhanden")
    ok &= check("threading.Thread(target=flash_apply" in src,
                "Flash läuft im Hintergrund-Thread")
    ok &= check("_serial_pause.is_set()" in src, "Leseschleife beachtet die Pause")

    print("\n[5] Dockerfile")
    docker = (ADDON / "Dockerfile").read_text(encoding="utf-8")
    ok &= check("esptool" in docker, "esptool wird installiert")
    ok &= check("esptool>=4.7,<5" in docker, "auf <5 gepinnt (write_flash)")

    print()
    print("全部通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
