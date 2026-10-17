#!/usr/bin/env python3
"""Ende-zu-Ende-Simulation des Serial-OTA gegen einen 256-Byte-fgets-C6.

Hintergrund (echtes Log vom 17.10.):
    [OTA] coordinator 7168/789804 Bytes gesendet      ← genau 1 Stoß
    danach nie wieder ein "write"-ACK

Ursache: der C6 liest Kommandos mit fgets(buf, BUF_SIZE). Bei BUF_SIZE=256
wird eine ~950 Zeichen lange fw_ota_data-Zeile in 255-Zeichen-Fragmente
zerhackt; das erste Fragment endet mitten im Hex-String, strchr(pd,'"')
findet kein Ende → Frame wird stillschweigend verworfen → kein ACK.

Dieser Test baut beide Seiten nach:
  * Gateway: die echten Funktionen aus serial_gateway.py (_fw_ota_start,
    _fw_ota_send_batch, _fw_ota_close)
  * C6: ein FakeSerial, das die fgets-Semantik exakt nachbildet
    (Zeilen werden bei buf_size-1 Zeichen zerschnitten)

Erwartung:
  FW_CHUNK=80  → Transfer läuft durch, Byte für Byte identisch, 0 Fragmente
  FW_CHUNK=448 → bricht nach dem ersten Stoß ab, 0 Bytes geschrieben
                 (= genau das beobachtete Verhalten)
"""
import ast
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

SRC = Path(__file__).parent / "host" / "addon" / "esp32c6_gateway" / "serial_gateway.py"
WANT = ("_js", "_j", "_env_int", "_fw_ota_close", "_fw_ota_watchdog",
        "_fw_ota_start", "_fw_ota_send_batch")
ACK_EVERY = 16          # serial_ota.c: FW_ACK_EVERY


# ── Gateway-Seite aus dem echten Quelltext laden ──────────────────────────
def load_gateway(chunk):
    src = SRC.read_text(encoding="utf-8")
    chunks = [ast.get_source_segment(src, n)
              for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name in WANT]
    assert len(chunks) == len(WANT), f"{len(chunks)}/{len(WANT)} Funktionen gefunden"

    class _NoThread:                        # Watchdog im Test nicht starten
        def __init__(self, *a, **k):
            pass
        def start(self):
            pass

    ns = {
        "os": os, "json": json, "threading": threading,
        "time": __import__("time"),
        "_fw_ota_file": None, "_fw_ota_size": 0, "_fw_ota_off": 0,
        "FW_CHUNK": chunk, "FW_BATCH": 16,
        "_state": {"fw_ota": {"status": "idle", "bin_path": "", "size": 0, "off": 0}},
        "_state_lock": threading.Lock(),
        "print": lambda *a, **k: None,     # Log ruhig halten
        "__builtins__": __builtins__,
    }
    exec("\n\n".join(chunks), ns)
    ns["threading"] = type("T", (), {"Thread": _NoThread})
    return ns


# ── C6-Seite: fgets-Semantik + serial_ota.c nachbauen ─────────────────────
class FakeC6:
    def __init__(self, buf_size=256, parser="old"):
        self.buf_size = buf_size
        self.parser = parser            # "old" = starres strstr, "new" = json_val
        self.raw = b""
        self.size = 0
        self.active = False
        self.written = 0
        self.chunks = 0
        self.acks = 0
        self.fragments = 0          # zerschnittene Zeilen
        self.dropped = 0            # Frames, die der Parser nicht versteht
        self.data = bytearray()
        self.ended = False
        self.maxline = 0

    # ---- Serial-Schnittstelle ----
    def write(self, payload):
        self.raw += payload
        while b"\n" in self.raw:
            line, self.raw = self.raw.split(b"\n", 1)
            self._feed(line.decode("utf-8", "replace"))

    # ---- fgets(buf, buf_size) ----
    def _feed(self, line):
        self.maxline = max(self.maxline, len(line))
        limit = self.buf_size - 1
        frags = [line[i:i + limit] for i in range(0, len(line), limit)] or [""]
        if len(frags) > 1:
            self.fragments += len(frags) - 1
        for frag in frags:
            self._handle(frag)

    # ---- rx_task() + serial_ota.c ----
    def _handle(self, frag):
        if "fw_ota_start" in frag:
            p = frag.find('"size"')
            if p >= 0:
                val = frag[p + 6:].lstrip(" \t:")
                self.size = int("".join(c for c in val.split(",")[0] if c.isdigit()) or 0)
            self.active = True
            self.written = self.chunks = 0
            return

        if "fw_ota_data" in frag:
            if not self.active:
                return
            if self.parser == "old":
                # strstr(buf, "\"data\":\"") – nur kompaktes JSON passt
                marker = '"data":"'
                k = frag.find(marker)
                if k < 0:
                    self.dropped += 1
                    return
                rest = frag[k + len(marker):]
                end = rest.find('"')
                if end < 0:
                    self.dropped += 1
                    return
                hexs = rest[:end]
                o = frag.find('"off":')
                off = int("".join(c for c in frag[o + 6:].split(",")[0]
                                  if c.isdigit()) or 0) if o >= 0 else -1
            else:
                # json_val() + strchr(pd,'"') – toleriert Leerzeichen
                key = frag.find('"data"')
                if key < 0:
                    return
                rest = frag[key + 6:].lstrip(" \t:")
                if not rest.startswith('"'):
                    self.dropped += 1
                    return
                rest = rest[1:]
                end = rest.find('"')
                if end < 0:
                    self.dropped += 1
                    return          # <- genau der stille Verlust
                hexs = rest[:end]
                off = self.written

            if off != self.written:                 # serial_ota_data: off != s_written
                return
            if len(hexs) % 2:
                hexs = hexs[:-1]
            raw = bytes.fromhex(hexs)
            if self.written + len(raw) > self.size + 16:
                return
            self.data += raw
            self.written += len(raw)
            self.chunks += 1
            if self.chunks % ACK_EVERY == 0:
                self.acks += 1              # -> {"t":"fw_ota","st":"write",...}
            return

        if "fw_ota_end" in frag:
            self.ended = True


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    return bool(cond)


def run(chunk, parser="old", size=20 * 1024):
    payload = bytes((i * 37 + 11) & 0xFF for i in range(size))
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(payload)
        path = f.name

    gw = load_gateway(chunk)
    c6 = FakeC6(buf_size=256, parser=parser)
    gw["ser"] = c6
    try:
        gw["_fw_ota_start"](path)
        gw["_fw_ota_send_batch"]()

        guard = 0
        while not c6.ended and guard < 20000:
            guard += 1
            if c6.acks:
                c6.acks = 0
                gw["_fw_ota_send_batch"]()
            else:
                break                       # C6 schweigt → Gateway wartet ewig
        return c6, payload, gw
    finally:
        gw["_fw_ota_close"]()               # Datei-Handle schließen, sonst WinError 32
        os.unlink(path)


def main():
    ok = True
    print("\n[A] chunk=80 + alter Parser (strstr mit \"data\":\") – der echte Fall")
    c6, payload, gw = run(80, parser="old")
    ok &= check(c6.fragments == 0, f"keine zerschnittenen Zeilen (ist {c6.fragments})")
    ok &= check(c6.dropped == 0, f"keine verworfenen Frames (ist {c6.dropped})")
    ok &= check(c6.ended, "fw_ota_end wurde empfangen")
    ok &= check(len(c6.data) == len(payload),
                f"{len(c6.data)}/{len(payload)} Bytes geschrieben")
    ok &= check(bytes(c6.data) == payload, "Binärinhalt Byte für Byte identisch")

    print("\n[B] chunk=448 + alter Parser – der beobachtete Bug")
    c6, payload, gw = run(448, parser="old")
    ok &= check(c6.fragments > 0, f"Zeilen werden zerschnitten (ist {c6.fragments})")
    ok &= check(c6.dropped > 0, f"Frames werden still verworfen (ist {c6.dropped})")
    ok &= check(not c6.ended, "Transfer endet NICHT (bleibt hängen)")
    ok &= check(c6.written == 0, f"kein einziges Byte geschrieben (ist {c6.written})")

    print("\n[C] chunk=80 + neuer Parser (json_val)")
    c6, payload, gw = run(80, parser="new")
    ok &= check(c6.ended and bytes(c6.data) == payload,
                f"ebenfalls vollständig ({len(c6.data)}/{len(payload)})")

    print("\n[D] tatsächliche Zeilenlänge (gemessen im Durchlauf)")
    for chunk, parser, expect_ok in ((80, "old", True), (96, "old", True), (448, "old", False)):
        c6, _, _ = run(chunk, parser=parser, size=4096)
        ok &= check((c6.maxline <= 255) is expect_ok,
                    f"chunk={chunk}: längste Zeile {c6.maxline} Zeichen "
                    f"({'passt in 255' if c6.maxline <= 255 else 'zu lang für 255'})")

    print()
    print("全部通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
