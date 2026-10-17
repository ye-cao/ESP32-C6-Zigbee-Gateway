#!/usr/bin/env python3
"""回归测试：日志时间戳必须是本地时间，不能是容器 UTC。

背景：HA-Add-on-Container 默认 TZ=UTC，旧代码用 time.strftime("[%H:%M:%S]")
（即 time.localtime），于是日志显示 02:33，而墙上时钟是 10:33。

断言：
  1. LOG_TZ=Asia/Shanghai → 解析出 +0800，且 _ts() 与当前东八区时间一致
  2. LOG_TZ=Europe/Berlin → 解析出 +0200（夏令时）
  3. TZ=UTC 但没有 LOG_TZ → 不把 UTC 当成本地时区（回退系统时间）
  4. LOG_TZ 写成不存在的名字 → 不抛异常，回退系统时间
  5. _ts() 格式固定为 [HH:MM:SS]
"""
import ast
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SRC = Path(__file__).parent / "host" / "addon" / "esp32c6_gateway" / "serial_gateway.py"
WANT = ("_resolve_local_tz", "_ts")
TZ_KEYS = ("LOG_TZ", "TZ")


def load(path):
    src = path.read_text(encoding="utf-8")
    chunks = [ast.get_source_segment(src, n)
              for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name in WANT]
    assert len(chunks) == len(WANT), f"只找到 {len(chunks)}/{len(WANT)} 个函数"
    ns = {"os": os, "time": time, "datetime": datetime,
          "__builtins__": __builtins__}
    exec("\n\n".join(chunks), ns)
    return ns


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    return bool(cond)


def resolve_with(ns, **env):
    old = {k: os.environ.get(k) for k in TZ_KEYS}
    try:
        for k in TZ_KEYS:
            os.environ.pop(k, None)
        for k, v in env.items():
            os.environ[k] = v
        tz, name = ns["_resolve_local_tz"]()
        ns["_LOG_TZ"] = tz          # _ts() liest diese Modulglobale
        return tz, name, ns["_ts"]()
    finally:
        for k in TZ_KEYS:
            os.environ.pop(k, None)
            if old[k] is not None:
                os.environ[k] = old[k]


def offset_minutes(tz):
    if tz is None:
        return None
    return int(datetime.now(tz).utcoffset().total_seconds() // 60)


def main():
    ok = True
    ns = load(SRC)

    print("\n[1] LOG_TZ=Asia/Shanghai")
    tz, name, ts = resolve_with(ns, LOG_TZ="Asia/Shanghai")
    ok &= check(name == "Asia/Shanghai", f"解析名 = {name}")
    ok &= check(offset_minutes(tz) == 480, f"UTC 偏移 = {offset_minutes(tz)} min (+0800)")
    want = datetime.now(timezone(timedelta(hours=8))).strftime("[%H:%M:%S]")
    ok &= check(ts == want, f"时间戳 {ts} == 东八区 {want}")
    ok &= check(ts != time.strftime("[%H:%M:%S]") or offset_minutes(tz) == 480,
                "时间戳不取自容器 localtime(UTC)")

    print("\n[2] LOG_TZ=Europe/Berlin")
    tz, name, ts = resolve_with(ns, LOG_TZ="Europe/Berlin")
    off = offset_minutes(tz)
    ok &= check(off in (60, 120), f"UTC 偏移 = {off} min (冬令时 +0100 / 夏令时 +0200)")

    print("\n[3] 只有 TZ=UTC（容器默认）")
    tz, name, ts = resolve_with(ns, TZ="UTC")
    ok &= check(tz is None, f"未把 UTC 当成用户时区（回退系统时间，name={name}）")

    print("\n[4] LOG_TZ 是不存在的时区名")
    try:
        tz, name, ts = resolve_with(ns, LOG_TZ="Mars/Olympus")
        crashed = False
    except Exception as e:                       # noqa: BLE001
        crashed, name = True, f"异常 {e}"
    ok &= check(not crashed, f"不抛异常（结果 {name}）")

    print("\n[5] 时间戳格式")
    tz, name, ts = resolve_with(ns, LOG_TZ="Asia/Shanghai")
    ok &= check(bool(re.fullmatch(r"\[\d{2}:\d{2}:\d{2}\]", ts)), f"格式 {ts} 符合 [HH:MM:SS]")

    print("\n[6] 时区偏移与墙上时钟一致")
    now = datetime.now()
    tz, _, _ = resolve_with(ns, LOG_TZ="Asia/Shanghai")
    east8 = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    ok &= check(abs((now - east8).total_seconds()) < 120,
                f"本机时间与东八区相差 {int((now - east8).total_seconds())} s（应 < 120）")

    print()
    print("全部通过" if ok else "存在失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
