#!/usr/bin/env python3
"""Packaging-Regressionstest für das HA-Add-on.

Anlass: nach einem Windows-Edit hatte run.sh CRLF-Zeilenenden. Das Shebang
lautete dann "#!/usr/bin/env bash\\r" und der Container starb beim Start mit

    env: can't execute 'bash
    ': No such file or directory

Der Fehler ist im Build nicht zu sehen – erst beim Start des Containers.
Dieser Test fängt ihn vorher ab.

Geprüft wird:
  1. keine CRLF und kein BOM in run.sh / *.py / *.json / Dockerfile
  2. Shebang von run.sh ist exakt "#!/usr/bin/env bash"
  3. run.sh ist syntaktisch gültig (bash -n)
  4. config.json parst, und die Version stimmt mit GW_VERSION im Python überein
  5. jede COPY-Zeile im Dockerfile zeigt auf eine existierende Datei
  6. der Dockerfile-Guard gegen CRLF ist vorhanden
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ADDON = Path(__file__).parent / "host" / "addon" / "esp32c6_gateway"
FILES = ["run.sh", "serial_gateway.py", "config.json", "Dockerfile", "device_db.json"]


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    return bool(cond)


def _find_git_bash():
    """Git-Bash suchen. Das nackte "bash" ist unter Windows oft ein WSL-Shim,
    der hier von der Sandbox blockiert wird – also gezielt suchen."""
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "usr" / "bin" / "bash.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "bash.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def main():
    ok = True

    print("\n[1] Zeilenenden und BOM")
    for name in FILES:
        raw = (ADDON / name).read_bytes()
        cr = raw.count(b"\r")
        bom = raw.startswith(b"\xef\xbb\xbf")
        ok &= check(cr == 0, f"{name}: kein CR (gefunden {cr})")
        ok &= check(not bom, f"{name}: kein UTF-8-BOM")

    print("\n[2] Shebang von run.sh")
    first = (ADDON / "run.sh").read_bytes().split(b"\n", 1)[0]
    ok &= check(first == b"#!/usr/bin/env bash",
                f"Shebang exakt richtig: {first!r}")

    print("\n[3] run.sh Syntax")
    bash = _find_git_bash()
    if bash is None:
        print("  SKIP  kein Git-Bash gefunden (prüfe manuell: bash -n run.sh)")
    else:
        try:
            r = subprocess.run([bash, "-n", str(ADDON / "run.sh")],
                               capture_output=True)
            err = (r.stderr or b"").decode("utf-8", "replace").strip()
            ok &= check(r.returncode == 0,
                        "bash -n ohne Fehler" + ("" if r.returncode == 0 else " → " + err))
        except OSError as e:
            print(f"  SKIP  bash nicht ausführbar ({e})")

    print("\n[4] Versionsgleichheit config.json ↔ serial_gateway.py")
    cfg = json.loads((ADDON / "config.json").read_text(encoding="utf-8"))
    m = re.search(r'^GW_VERSION = "([^"]+)"',
                  (ADDON / "serial_gateway.py").read_text(encoding="utf-8"), re.M)
    ok &= check(m is not None, "GW_VERSION im Python gefunden")
    if m:
        ok &= check(cfg["version"] == m.group(1),
                    f"config.json={cfg['version']} == GW_VERSION={m.group(1)}")

    print("\n[5] Dockerfile COPY-Ziele existieren")
    docker = (ADDON / "Dockerfile").read_text(encoding="utf-8")
    copies = re.findall(r"^COPY\s+(\S+)\s+\S+", docker, re.M)
    ok &= check(bool(copies), f"COPY-Zeilen gefunden ({len(copies)})")
    for src in copies:
        ok &= check((ADDON / src).is_file(), f"{src} existiert")

    print("\n[6] Dockerfile normalisiert CRLF")
    ok &= check("s/\\r$//" in docker, "sed 's/\\r$//' Guard vorhanden")
    ok &= check("chmod a+x /run.sh" in docker, "run.sh wird ausführbar gemacht")

    print()
    print("全部通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
