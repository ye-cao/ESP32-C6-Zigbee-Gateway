#!/usr/bin/env python3
"""从 Zigbee2MQTT 的转换器定义（zigbee-herdsman-converters）生成
给 HA 自动 discovery 用的紧凑型号库。

Quelle:  .zhc/src/devices/*.ts   (GitLab-Mirror, Tag v16.7.0)
Ziel:    host/addon/esp32c6_gateway/device_db.json

键 = ZCL Basic 属性 0x0005（ModelIdentifier）—— 正是协调器在配对时读的那个。
有了它，网关就能把刚入网的设备立刻归类正确（端点名、实体类型），
不用瞎猜。

只抽取声明性的部分 —— TS 的运行时逻辑（带代码的 fromZigbee 转换器）
没法一比一搬过来，但能力可以。
"""
import glob
import json
import os
import re
import sys

SRC_DIR = ".zhc/src/devices"
OUT = os.path.join("host", "addon", "esp32c6_gateway", "device_db.json")

# 数组里的一个定义对象：缩进正好 4 个空格。
# 注意：收尾必须用 lookahead —— 否则匹配会把换行一起吃掉，
# 结果每隔一块就被跳过。
BLOCK = re.compile(r"\n    \{\n(.*?)(?=\n    \},?\n)", re.S)

RE_ZBMODEL = re.compile(r"zigbeeModel:\s*\[", re.S)
RE_FP_ID   = re.compile(r"modelID:\s*")
# ★ v16.7.0 写的是 '单引号'，从 v26.x 起变成
# "双引号"（Prettier 改的）。两种都得允许，否则更新之后
# 生成器会产出一个空数据库 —— 而且不报错。
#
# ★★ 字符串不再用正则：有些厂商（例如 EcoSmart）
# 用的是**二进制** ModelID。ecosmart.ts 里就长这样：
#     '\u0000\u0002\u0000\u0004\u0003"…'
# —— 一个单引号字符串，里面含一个**字面**的 "。像
# ['"]([^'"]+)['"] 这样的模式会在字符串中间截断，
# 把 ',' 或 '1' 这种碎片当成 ModelID。所以改用真正的扫描器，
# 它能理解转义序列和两种引号。
RE_STR     = lambda k: re.compile(r"""\n\s*%s:\s*(?=['"%s])""" % (k, "`"))


def _ts_unescape(s):
    """把 TS 字符串字面量里的转义序列还原成真正的字符。"""
    out, i, n = [], 0, len(s)
    SIMPLE = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
              "v": "\v", "0": "\0", "'": "'", '"': '"', "`": "`", "\\": "\\"}
    while i < n:
        c = s[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        e = s[i + 1]
        if e == "u":
            if i + 2 < n and s[i + 2] == "{":                 # \u{1F600}
                j = s.find("}", i + 3)
                if j > 0:
                    try:
                        out.append(chr(int(s[i + 3:j], 16)))
                    except ValueError:
                        pass
                    i = j + 1
                    continue
            try:
                out.append(chr(int(s[i + 2:i + 6], 16)))      # \u00e9
                i += 6
                continue
            except ValueError:
                pass
        elif e == "x":
            try:
                out.append(chr(int(s[i + 2:i + 4], 16)))      # \x41
                i += 4
                continue
            except ValueError:
                pass
        out.append(SIMPLE.get(e, e))                          # \" \' \\ \n …
        i += 2
    return "".join(out)


def ts_string(text, pos=0):
    """从 pos（先跳过空白）开始读**一个**字符串字面量。

    返回：(值, 结束位置)，或 (None, pos)。
    支持 '…'、"…" 和 `…`，含反斜杠转义。
    """
    n = len(text)
    while pos < n and text[pos] in " \t\r\n":
        pos += 1
    if pos >= n or text[pos] not in "'\"`":
        return None, pos
    q = text[pos]
    i = pos + 1
    buf = []
    while i < n:
        c = text[i]
        if c == "\\":
            buf.append(text[i:i + 2])
            i += 2
            continue
        if c == q:
            return _ts_unescape("".join(buf)), i + 1
        buf.append(c)
        i += 1
    return None, pos


def ts_string_array(text, pos):
    """从 pos 开始读 `[ 'a', "b", … ]`。返回：(列表, 结束位置)。"""
    n = len(text)
    while pos < n and text[pos] in " \t\r\n":
        pos += 1
    if pos >= n or text[pos] != "[":
        return [], pos
    out = []
    i = pos + 1
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i < n and text[i] == "]":
            return out, i + 1
        s, nxt = ts_string(text, i)
        if s is None:
            return out, i                 # unerwartetes Konstrukt → abbrechen
        out.append(s)
        i = nxt
    return out, pos


def _id_ok(s):
    """Nur brauchbare ModelIDs durchlassen.

    EcoSmart 之类的厂商拿真正的二进制串（\u0000 …）当 ModelID。它们
    在 Basic 属性 0x0005 里根本不是合法 UTF-8，对归类毫无用处 ——
    只会把库撑大，日志里也永远匹配不上。
    有控制字符 ⇒ 直接丢掉。
    """
    if not s:
        return False
    if s.strip() != s:
        return False
    for ch in s:
        o = ord(ch)
        if o < 0x20 or o == 0x7F:        # Steuerzeichen / NUL / DEL
            return False
    return True


RE_ENDP    = re.compile(r"endpoints:\s*\{([^}]*)\}")


def ts_field(block, key):
    """在块里找 `key: "…"`，用扫描器读出它的值。

    同样的坑：`description: "16\" 吸顶灯"` 里有一个
    转义的 " —— 正则在这里会截断。
    """
    pad = "\n" + block                 # 前面补个 \n，好让 `\n\s*key` 匹配得上
    for m in RE_STR(key).finditer(pad):
        val, _ = ts_string(pad, m.end())
        if val is not None:
            return val
    return ""

# 能力识别：（在块文本里跑的正则）→ HA 组件 / 特征
CAPS = [
    # onOff() 和 onOff({...}) —— 不带参数也一样是开关
    (re.compile(r"\bonOff\(\s*[\{\)]|extend\.light_onoff_brightness|e\.switch\(\)|tz\.on_off"), "switch"),
    (re.compile(r"\blight\(\s*[\{\)]|extend\.light_onoff_brightness|m\.light\("),              "light"),
    (re.compile(r"ias_contact_alarm_1|e\.contact\(\)"),                        "contact"),
    (re.compile(r"ias_water_leak_alarm|e\.water_leak\(\)"),                    "moisture"),
    (re.compile(r"ias_smoke_alarm|e\.smoke\(\)"),                              "smoke"),
    (re.compile(r"ias_gas_alarm|e\.gas\(\)"),                                  "gas"),
    (re.compile(r"ias_vibration|e\.vibration\(\)"),                            "vibration"),
    (re.compile(r"\bfz\.temperature|e\.temperature\(\)"),                      "temperature"),
    (re.compile(r"\bfz\.humidity|e\.humidity\(\)"),                            "humidity"),
    (re.compile(r"\bfz\.illuminance|e\.illuminance\(\)"),                      "illuminance"),
    (re.compile(r"\bfz\.occupancy|e\.occupancy\(\)"),                          "occupancy"),
    (re.compile(r"\bfz\.battery|e\.battery\(\)"),                              "battery"),
    (re.compile(r"cover_position|e\.cover_position\(\)|windowCovering"),       "cover"),
    (re.compile(r"e\.lock\(\)|fz\.lock"),                                      "lock"),
    (re.compile(r"e\.action\(|command_recall|command_on\b"),                  "action"),
    (re.compile(r"fz\.metering|e\.power\(\)|e\.energy\(\)"),                   "power"),
]

# 每种识别出的特征对应的 HA 组件（用于 discovery 预置）
HA_COMP = {
    "switch": "switch", "light": "light", "contact": "binary_sensor",
    "moisture": "binary_sensor", "smoke": "binary_sensor", "gas": "binary_sensor",
    "vibration": "binary_sensor", "temperature": "sensor", "humidity": "sensor",
    "illuminance": "sensor", "occupancy": "binary_sensor", "battery": "sensor",
    "cover": "cover", "lock": "lock", "power": "sensor",
}


def parse_endpoints(block, caps):
    """onOff({endpoints:{top:16,center:17}}) → {"top":16,...}"""
    if "switch" not in caps and "light" not in caps:
        return {}
    for m in RE_ENDP.finditer(block):
        inner = m.group(1)
        eps = {}
        for part in inner.split(","):
            kv = part.split(":")
            if len(kv) == 2:
                k = kv[0].strip().strip("'\" ")
                v = kv[1].strip().strip("'\" ")
                if k and v.isdigit():
                    eps[k] = int(v)
        if eps:
            return eps
    return {}


def main():
    if not os.path.isdir(SRC_DIR):
        sys.exit(f"源目录不存在：{SRC_DIR}")

    db = {}
    stats = {"files": 0, "blocks": 0, "keys": 0, "no_model": 0, "bin_id": 0}

    for path in sorted(glob.glob(os.path.join(SRC_DIR, "*.ts"))):
        stats["files"] += 1
        txt = open(path, encoding="utf-8", errors="replace").read()
        for m in BLOCK.finditer(txt):
            stats["blocks"] += 1
            b = m.group(1)

            ids = []
            zm = RE_ZBMODEL.search(b)
            if zm:
                ids += ts_string_array(b, zm.end() - 1)[0]
            for fm in RE_FP_ID.finditer(b):
                s, _ = ts_string(b, fm.end())
                if s:
                    ids.append(s)
            raw_n = len(ids)
            ids = [i.strip() for i in ids if _id_ok(i)]
            stats["bin_id"] += raw_n - len(ids)
            if not ids:
                stats["no_model"] += 1
                continue

            model = ts_field(b, "model").strip()
            vendor = ts_field(b, "vendor").strip()
            desc = ts_field(b, "description").strip()

            caps = [name for rx, name in CAPS if rx.search(b)]
            entry = {
                "model": model or ids[0],
                "vendor": vendor,
                "description": desc,
                "caps": caps,
                "components": sorted({HA_COMP[c] for c in caps if c in HA_COMP}),
                "endpoints": parse_endpoints(b, caps),
                "src": os.path.basename(path),
            }
            for i in ids:
                db.setdefault(i, entry)
                stats["keys"] += 1

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=0, sort_keys=True)

    size = os.path.getsize(OUT)
    print(f"Dateien        : {stats['files']}")
    print(f"Definitionen   : {stats['blocks']}")
    print(f"条目           : {len(db)}  （含别名的键：{stats['keys']}）")
    print(f"没有 ModelID   : {stats['no_model']}")
    print(f"丢掉二进制 ID  : {stats['bin_id']}  （ModelID 里有控制字符）")
    print(f"Ausgabe        : {OUT}  ({size/1024:.0f} KiB)")
    return db


if __name__ == "__main__":
    main()
