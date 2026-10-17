#!/usr/bin/env python3
"""回归测试：页面自动刷新不得清空 <input type="file"> 的已选文件。

背景：旧实现用 <meta http-equiv="refresh" content="10">，每 10 秒无条件整页重载，
用户选完固件还没点上传就被刷回「未选择任何文件」。

断言：
  1. 两个页面都不再出现 http-equiv="refresh"
  2. 页面里存在条件刷新脚本，且 busy() 会检查 input[type=file].files
  3. 存在 id="rl" 的状态提示元素
  4. 渲染出的 JS 大括号配平（f-string 转义没写错）
  5. 渲染出的 HTML 仍然包含文件上传表单与路径表单
"""
import ast
import io
import json
import sys
import threading
from pathlib import Path

SRC = Path(__file__).parent / "host" / "addon" / "esp32c6_gateway" / "serial_gateway.py"

WANT = ("_autoreload_js", "_html", "_device_html")


def load_funcs(path):
    """只 exec 需要的三个函数，避免模块级副作用（串口/MQTT 连接）。"""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    chunks = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT:
            chunks.append(ast.get_source_segment(src, node))
    assert len(chunks) == len(WANT), f"只找到 {len(chunks)}/{len(WANT)} 个函数"
    ns = {
        "json": json,
        "threading": threading,
        "_state_lock": threading.Lock(),
        "_state": {
            "status": "online", "uptime": 123, "channel": 15, "pan": "0x1a62",
            "permit_join": {"open": False, "seconds": 0},
            "devices": {
                "0x07da": {
                    "ieee": "00:12:4b:00:1c:aa:bb:cc",
                    "clusters": {"on_off": {"payload": {"v": 1}, "ts": "12:00:00"}},
                    "lqi": 200, "rssi": -55,
                }
            },
            "ota": {"status": "idle", "bin_path": None, "transferred": 0, "size": 0},
            "fw_ota": {"status": "idle", "off": 0, "size": 0},
        },
        "BASE": "zigbee2mqtt/esp32c6gw",
        "DB_HOST": "",
        "DB_NAME": "wagodb",
        "FW_CHUNK": 80,
        "FW_BATCH": 16,
        "GW_VERSION": "1.1.6",
        "APP_OFFSET": "0x20000",
        "flash_status": lambda: {"status": "idle", "bin": "", "tail": []},
        "_ha_db": {},
        "_ha_devices": {},
        "_ha_ep_label": lambda addr, epn: f"EP{epn}",
        "_ha_device": lambda addr: {},
        "__builtins__": __builtins__,
        "__file__": str(path),
    }
    exec("\n\n".join(chunks), ns)
    return ns


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    return bool(cond)


def main():
    ok = True
    ns = load_funcs(SRC)

    for name, render in (("主页 /", ns["_html"]), ("设备页 /device/0x07da", lambda: ns["_device_html"]("0x07da"))):
        print(f"\n[{name}]")
        html = render()
        assert isinstance(html, str) and html, "渲染结果为空"

        ok &= check('http-equiv="refresh"' not in html,
                    '不再使用 http-equiv="refresh"')
        ok &= check("location.reload()" in html,
                    "存在脚本驱动的 location.reload()")
        ok &= check("input[type=file]" in html and ".files" in html,
                    "busy() 会检查 input[type=file].files")
        ok &= check('id="rl"' in html, '存在 id="rl" 刷新状态提示')
        ok &= check("{{" not in html and "}}" not in html,
                    "f-string 大括号已正确转义（无残留 {{ }}）")
        ok &= check(html.count("{") == html.count("}"),
                    f"大括号配平 ({{={html.count('{')}, }}={html.count('}')})")

    print("\n[主页表单完整性]")
    html = ns["_html"]()
    ok &= check('enctype="multipart/form-data"' in html, "文件上传表单仍在")
    ok &= check('type="file" name="file"' in html, "file input 仍在")
    ok &= check('name="path"' in html, "路径表单仍在")
    ok &= check("/api/fw_ota" in html, "提交地址指向 /api/fw_ota")

    print("\n[脚本片段]")
    js = ns["_autoreload_js"](10)
    ok &= check("SECS = 10" in js, "秒数参数已注入")
    ok &= check(js.count("{") == js.count("}"), f"JS 大括号配平 ({{={js.count('{')}, }}={js.count('}')})")
    ok &= check("'file'" in js and "'focus'" in js and "'dirty'" in js,
                "三种暂停条件齐全（已选文件 / 有焦点 / 内容被改）")
    ok &= check("addEventListener('change'" in js, "选中文件后立即更新提示文案")

    print()
    if ok:
        print("全部通过")
        return 0
    print("存在失败项")
    return 1


if __name__ == "__main__":
    sys.exit(main())
