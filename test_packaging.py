#!/usr/bin/env python3
"""HA add-on 打包的回归测试。

起因：在 Windows 上编辑过之后 run.sh 变成了 CRLF 行尾。于是 shebang
成了 "#!/usr/bin/env bash\\r" 容器一启动就挂掉：

    env: can't execute 'bash
    ': No such file or directory

这个错误在构建时看不出来 —— 只有容器启动才暴露。
这个测试提前把它拦住。

检查内容：
  1. run.sh / *.py / *.json / Dockerfile 里没有 CRLF、没有 BOM
  2. run.sh 的 shebang 精确等于 "#!/usr/bin/env bash"
  3. run.sh 语法合法（bash -n）
  4. config.json 能解析，且版本与 Python 里的 GW_VERSION 一致
  5. Dockerfile 里每条 COPY 都指向一个存在的文件
  6. Dockerfile 里防 CRLF 的那道保险还在
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
    """找 Git-Bash。Windows 上光写 "bash" 往往只是个 WSL 壳，
    在这里会被沙箱挡住 —— 所以要定向去找。"""
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

    print("\n[1] 行尾和 BOM")
    for name in FILES:
        raw = (ADDON / name).read_bytes()
        cr = raw.count(b"\r")
        bom = raw.startswith(b"\xef\xbb\xbf")
        ok &= check(cr == 0, f"{name}：没有 CR（发现 {cr}）")
        ok &= check(not bom, f"{name}：无 UTF-8 BOM")

    print("\n[2] run.sh 的 shebang")
    first = (ADDON / "run.sh").read_bytes().split(b"\n", 1)[0]
    ok &= check(first == b"#!/usr/bin/env bash",
                f"Shebang exakt richtig: {first!r}")

    print("\n[3] run.sh Syntax")
    bash = _find_git_bash()
    if bash is None:
        print("  SKIP  没找到 Git-Bash（请手工执行 bash -n run.sh）")
    else:
        try:
            r = subprocess.run([bash, "-n", str(ADDON / "run.sh")],
                               capture_output=True)
            err = (r.stderr or b"").decode("utf-8", "replace").strip()
            ok &= check(r.returncode == 0,
                        "bash -n 无报错" + ("" if r.returncode == 0 else " → " + err))
        except OSError as e:
            print(f"  SKIP  bash 执行不了（{e}）")

    print("\n[4] 版本号一致性 config.json ↔ serial_gateway.py")
    cfg = json.loads((ADDON / "config.json").read_text(encoding="utf-8"))
    m = re.search(r'^GW_VERSION = "([^"]+)"',
                  (ADDON / "serial_gateway.py").read_text(encoding="utf-8"), re.M)
    ok &= check(m is not None, "Python 里找到 GW_VERSION")
    if m:
        ok &= check(cfg["version"] == m.group(1),
                    f"config.json={cfg['version']} == GW_VERSION={m.group(1)}")

    print("\n[5] Dockerfile 里 COPY 的源文件都存在")
    docker = (ADDON / "Dockerfile").read_text(encoding="utf-8")
    copies = re.findall(r"^COPY\s+(\S+)\s+\S+", docker, re.M)
    ok &= check(bool(copies), f"COPY 行数 ({len(copies)})")
    for src in copies:
        ok &= check((ADDON / src).is_file(), f"{src} 存在")

    print("\n[6] Dockerfile 会规范化 CRLF")
    ok &= check("s/\\r$//" in docker, "存在 sed 's/\\r$//' 保护")
    ok &= check("chmod a+x /run.sh" in docker, "run.sh 被设为可执行")

    print()
    print("全部通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
