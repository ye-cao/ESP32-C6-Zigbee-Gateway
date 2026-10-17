#!/usr/bin/env python3
"""Erzeugt aus den Zigbee2MQTT-Converter-Definitionen (zigbee-herdsman-converters)
eine kompakte Modelldatenbank für die HA-Auto-Discovery.

Quelle:  .zhc/src/devices/*.ts   (GitLab-Mirror, Tag v16.7.0)
Ziel:    host/addon/esp32c6_gateway/device_db.json

Schlüssel = ZCL Basic-Attribut 0x0005 (ModelIdentifier) – genau das, was der
Koordinator im Interview liest. Damit kann der Gateway ein frisch beigetretenes
Gerät sofort richtig einordnen (Endpunkt-Namen, Entity-Typen) statt zu raten.

Nur deklarative Teile werden extrahiert – die TS-Runtime-Logik (fromZigbee-
Converter mit Code) kann nicht 1:1 übernommen werden, wohl aber die Fähigkeiten.
"""
import glob
import json
import os
import re
import sys

SRC_DIR = ".zhc/src/devices"
OUT = os.path.join("host", "addon", "esp32c6_gateway", "device_db.json")

# Ein Definitions-Objekt im Array: Einrückung genau 4 Leerzeichen.
# ACHTUNG: Abschluss als Lookahead – sonst frisst der Match den Zeilenumbruch
# und jeder zweite Block wird übersprungen.
BLOCK = re.compile(r"\n    \{\n(.*?)(?=\n    \},?\n)", re.S)

RE_ZBMODEL = re.compile(r"zigbeeModel:\s*\[", re.S)
RE_FP_ID   = re.compile(r"modelID:\s*")
# ★ v16.7.0 schrieb 'einfache' Anführungszeichen, ab v26.x sind es
# "doppelte" (Prettier-Umstellung). Beide zulassen, sonst liefert der
# Generator nach dem Update eine leere Datenbank – ohne Fehlermeldung.
#
# ★★ Kein Regex mehr für die Strings: einige Hersteller (z. B. EcoSmart)
# benutzen BINÄRE ModelIDs. In ecosmart.ts steht dann
#     '\u0000\u0002\u0000\u0004\u0003"…'
# – ein einfach-quotierter String, der ein LITERALES " enthält. Ein Muster
# wie ['"]([^'"]+)['"] schneidet dort mitten im String ab und liefert
# Bruchstücke wie ',' oder '1' als ModelID. Deshalb: echter Scanner, der
# Escape-Sequenzen und beide Quote-Arten versteht.
RE_STR     = lambda k: re.compile(r"""\n\s*%s:\s*(?=['"%s])""" % (k, "`"))


def _ts_unescape(s):
    """Wandelt die Escape-Sequenzen eines TS-String-Literals in echte Zeichen."""
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
    """Liest ab pos (nach Whitespace) EIN String-Literal.

    Rückgabe: (wert, endposition) oder (None, pos).
    Behandelt '…', "…" und `…` inkl. Backslash-Escapes.
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
    """Liest `[ 'a', "b", … ]` ab pos. Rückgabe: (liste, endposition)."""
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

    EcoSmart & Co. benutzen echte Binärstrings (\u0000 …) als ModelID. Die
    sind im Basic-Attribut 0x0005 nicht als UTF-8 lesbar und für die
    Zuordnung nutzlos – sie würden die DB nur aufblähen und beim Log-Match
    nie treffen. Steuerzeichen ⇒ verwerfen.
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
    """Sucht `key: "…"` im Block und liest den Wert mit dem Scanner.

    Gleiche Falle wie oben: `description: "16\" Deckenlampe"` enthält ein
    escaptes " – ein Regex würde dort abschneiden.
    """
    pad = "\n" + block                 # führender \n, damit `\n\s*key` greift
    for m in RE_STR(key).finditer(pad):
        val, _ = ts_string(pad, m.end())
        if val is not None:
            return val
    return ""

# Fähigkeitserkennung: (Regex im Blocktext) → HA-Komponente / Merkmal
CAPS = [
    # onOff() und onOff({...}) – ohne Argument genauso ein Schalter
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

# HA-Komponente je erkanntem Merkmal (für die Discovery-Vorbesetzung)
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
        sys.exit(f"Quelle fehlt: {SRC_DIR}")

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
    print(f"Einträge       : {len(db)}  (Schlüssel inkl. Aliase: {stats['keys']})")
    print(f"ohne ModelID   : {stats['no_model']}")
    print(f"Binär-IDs weg  : {stats['bin_id']}  (Steuerzeichen im ModelID)")
    print(f"Ausgabe        : {OUT}  ({size/1024:.0f} KiB)")
    return db


if __name__ == "__main__":
    main()
