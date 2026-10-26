#!/usr/bin/env python3
"""Run every gateway regression check from the repository root.

The individual checks import `host/addon/esp32c6_gateway/serial_gateway.py`
by relative path, so they must be started with the repo root as working
directory. This runner takes care of that and prints one summary line per
check, so a single command decides whether a release is safe:

    python tools/checks/run_all.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

CHECKS = [
    ("设备可用性 / discovery / Tx-Power", os.path.join(HERE, "check_availability.py")),
    ("拓扑采集（轮次 / 剪枝 / 闪烁）", os.path.join(HERE, "check_topology.py")),
    ("串口自愈（死 fd / 复位 / 写异常）", os.path.join(HERE, "check_serial_heal.py")),
    ("Web UI 渲染 + i18n", os.path.join(ROOT, "test_i18n_render.py")),
]


def main() -> int:
    bad = []
    for name, path in CHECKS:
        if not os.path.exists(path):
            print(f"SKIP  {name}: {path} fehlt")
            bad.append(name)
            continue
        r = subprocess.run([sys.executable, path], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        ok = "RESULT: ALL OK" in (r.stdout or "")
        fails = [ln.strip() for ln in (r.stdout or "").splitlines()
                 if ln.strip().startswith("FAIL")]
        print(f"{'OK  ' if ok else 'FAIL'}  {name}")
        for ln in fails:
            print(f"        {ln}")
        if not ok:
            if not fails:
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-6:]
                for ln in tail:
                    print(f"        {ln}")
            bad.append(name)
    print()
    if bad:
        print(f"RESULT: {len(bad)} von {len(CHECKS)} Checks fehlgeschlagen")
        return 1
    print(f"RESULT: ALL OK ({len(CHECKS)} Checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
