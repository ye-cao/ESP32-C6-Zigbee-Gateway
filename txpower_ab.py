# -*- coding: utf-8 -*-
"""txpower_ab.py – Sendeleistung des C6 messtechnisch vergleichen.

Frage, die das hier beantwortet: bringt +20 dBm an einer Keramikantenne
mehr Reichweite, oder verheizt die PA die obersten dB in Verzerrung?
Eine Vermutung ersetzt keine Messung – also wird gemessen.

WICHTIG zur Messgröße
---------------------
LQI und RSSI, die der C6 meldet, beschreiben den **Uplink** (Gerät → C6):
beides wird am empfangenen Frame gemessen (zb_gateway.c, aps_data_ind_cb →
ind.lqi / esp_ieee802154_get_recent_rssi). Die Sendeleistung des Gateways
ändert daran nichts. Wer hier LQI vorher/nachher vergleicht, misst Rauschen
und zieht den falschen Schluss.

Was sich mit der Sendeleistung ändert, ist der **Downlink** (C6 → Gerät).
Der einzige harte Beweis, dass ein Downlink-Frame angekommen ist, ist die
Antwort des Geräts. Also: N-mal Read Attributes auf On/Off schicken und
zählen, wie oft eine Antwort zurückkommt. Das ist die Messgröße.

    python txpower_ab.py                        # 20/14/8/2 dBm, 10 Versuche
    python txpower_ab.py --levels 20,17,14 --n 20
    python txpower_ab.py --dry-run              # nur zeigen, was gesendet würde

Umgebung: MQ_HOST / MQ_PORT / MQ_USER / MQ_PASS (wie mqtt_probe.py).
"""
import argparse
import json
import os
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mqtt_probe as mp                                    # noqa: E402

BASE = os.environ.get("BASE_TOPIC", "gw/coordinator")

POWER_TOPIC = f"{BASE}/cmd/set_tx_power"
LQI_TOPIC   = f"{BASE}/cmd/lqi"
READ_TOPIC  = f"{BASE}/cmd/read_onoff"
TX_TOPIC    = f"{BASE}/tx_power"
LQI_UP      = f"{BASE}/lqi"


# ── Topic-Hilfen ───────────────────────────────────────────────────────────
def state_topic(addr, ep):
    """Wohin die Antwort auf Read Attributes geht (siehe _ha_publish)."""
    sub = "on_off" if int(ep) <= 1 else "ep%d/on_off" % int(ep)
    return f"{BASE}/zigbee/{addr}/{sub}"


def key_of(payload):
    """JSON → dict, sonst None."""
    try:
        v = json.loads(payload)
    except Exception:
        return None
    return v if isinstance(v, dict) else None


def dev_row(lqi_msg, addr):
    """LQI/RSSI eines Geräts aus einer {"t":"lqi",…}-Meldung."""
    for d in (lqi_msg or {}).get("dev", []):
        if d.get("addr") == addr:
            return d.get("lqi"), d.get("rssi")
    return None, None


# ── Messung ────────────────────────────────────────────────────────────────
class Runner(mp.Cli):
    """Erbt Verbindungsaufbau und Paketlesen von mqtt_probe.Cli."""

    def __init__(self, dry=False, settle=6.0, timeout=4.0):
        super().__init__()
        self.dry = dry
        # Im dry-run wird nichts gesendet, also gibt es auch nichts abzuwarten.
        self.settle = 0.0 if dry else settle
        self.timeout = 0.01 if dry else timeout

    def open(self):
        if self.dry:
            return
        typ, fl, rc = self.connect("txpower-ab")
        if rc != 0:
            sys.exit("CONNACK rc=%d – keine Berechtigung am Broker" % rc)
        for t in (f"{BASE}/zigbee/#", TX_TOPIC, LQI_UP):
            self.subscribe(t, 0)
        self.sk.settimeout(0.5)

    def pub(self, topic, payload="", retain=False):
        if self.dry:
            print("    [dry] PUB %-32s %r" % (topic, payload))
            return
        body = mp.s(topic) + str(payload).encode()
        hdr = 0x30 | (0x01 if retain else 0x00)          # PUBLISH, QoS 0
        self.sk.sendall(mp.pkt(hdr, body))

    # -- Empfang ---------------------------------------------------------
    def pump(self, seconds):
        """Alle eintreffenden PUBLISH-Pakete einsammeln: [(topic, payload)]."""
        out = []
        if self.dry:
            return out
        t0 = time.time()
        while time.time() - t0 < seconds:
            try:
                t, body = self.read_pkt()
            except socket.timeout:
                continue
            except (OSError, IOError):
                break
            if (t & 0xF0) != 0x30:                       # kein PUBLISH (z.B. SUBACK)
                continue
            qos = (t >> 1) & 3
            tl = int.from_bytes(body[:2], "big")
            if len(body) < 2 + tl:
                continue
            topic = body[2:2 + tl].decode("utf-8", "replace")
            p = 2 + tl + (2 if qos else 0)
            out.append((topic, body[p:].decode("utf-8", "replace")))
        return out

    def wait_for(self, topic, seconds):
        """Auf eine bestimmte Meldung warten; gibt Payload oder None."""
        t0 = time.time()
        while time.time() - t0 < seconds:
            for t, p in self.pump(0.5):
                if t == topic:
                    return p
        return None

    # -- Ein Leistungsniveau --------------------------------------------
    def measure(self, dbm, addr, ep, n, pause):
        row = {"want": dbm, "eff": None, "ok": 0, "n": n, "lqi": None, "rssi": None}
        print("\n── %+d dBm %s" % (dbm, "─" * 46))
        self.pub(POWER_TOPIC, str(dbm))

        raw = self.wait_for(TX_TOPIC, 5.0)
        tx  = key_of(raw) if raw else None
        if tx:
            row["eff"] = tx.get("eff")
            if tx.get("eff") != tx.get("want"):
                print("   ! Wunsch %s dBm, wirksam %s dBm – Anpassung griff nicht"
                      % (tx.get("want"), tx.get("eff")))
            else:
                print("   wirksam: %s dBm" % tx.get("eff"))
        elif not self.dry:
            print("   ! keine txpower-Rückmeldung – läuft dort eine alte Firmware?")

        print("   warte %.0f s bis sich der Funkpfad beruhigt hat …" % self.settle)
        self.pump(self.settle)

        target = state_topic(addr, ep)
        t0 = time.time()
        for i in range(n):
            self.pump(0.05)                              # Restfenster leeren
            self.pub(READ_TOPIC, "%s,%d" % (addr, ep))
            got = None
            tw = time.time()
            while time.time() - tw < self.timeout and got is None:
                for t, p in self.pump(0.25):
                    if t == target:
                        got = p
            if got is not None:
                row["ok"] += 1
            sys.stdout.write("   %2d/%d %s\r" % (i + 1, n, "OK" if got else "--"))
            sys.stdout.flush()
            time.sleep(pause)
        print("   %d/%d beantwortet (%.1f s)          " % (row["ok"], n, time.time() - t0))

        self.pub(LQI_TOPIC, "")
        raw = self.wait_for(LQI_UP, 4.0)
        if raw:
            row["lqi"], row["rssi"] = dev_row(key_of(raw), addr)
        return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", default="0x747d", help="Gerät (kurze Adresse)")
    ap.add_argument("--ep", type=int, default=1, help="Endpunkt (Panel oft 2)")
    ap.add_argument("--levels", default="20,14,8,2", help="dBm-Stufen")
    ap.add_argument("--n", type=int, default=10, help="Versuche je Stufe")
    ap.add_argument("--pause", type=float, default=1.5, help="Pause zwischen Versuchen (s)")
    ap.add_argument("--settle", type=float, default=6.0, help="Einschwingzeit je Stufe (s)")
    ap.add_argument("--timeout", type=float, default=4.0, help="Antwortfenster (s)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    levels = [int(x) for x in a.levels.split(",") if x.strip()]
    r = Runner(dry=a.dry_run, settle=a.settle, timeout=a.timeout)
    print("Broker %s:%d   Basis %s" % (mp.HOST, mp.PORT, BASE))
    print("Gerät %s ep%d, %d Versuche je Stufe, Stufen %s"
          % (a.addr, a.ep, a.n, levels))
    if a.dry_run:
        print("(dry-run: es wird nichts gesendet)")
    r.open()

    rows = [r.measure(d, a.addr, a.ep, a.n, 0.0 if a.dry_run else a.pause)
            for d in levels]

    print("\n%-9s %-8s %-9s %-5s %-8s" % ("Wunsch", "wirksam", "Antwort", "LQI", "RSSI"))
    print("-" * 44)
    for x in rows:
        print("%-9s %-8s %-9s %-5s %-8s" % (
            "%+d dBm" % x["want"],
            ("%+d" % x["eff"]) if x["eff"] is not None else "–",
            "%d/%d" % (x["ok"], x["n"]),
            x["lqi"] if x["lqi"] is not None else "–",
            ("%d dBm" % x["rssi"]) if x["rssi"] is not None else "–"))

    # Auswertung: kleinste Stufe, die noch alle Versuche beantwortet.
    if a.dry_run:
        return
    full = [x for x in rows if x["ok"] == x["n"]]
    if full:
        best = min(full, key=lambda x: x["eff"] if x["eff"] is not None else x["want"])
        print("\nKleinste Stufe ohne einen einzigen Fehlversuch: %+d dBm" % best["want"])
        better_high = [x for x in rows if x["want"] > best["want"] and x["ok"] < x["n"]]
        if better_high:
            print("Höhere Stufen waren NICHT besser – oben hat die PA nichts gutgemacht.")
        else:
            print("Höhere Stufen waren mindestens gleich gut. Solange die Störumgebung "
                  "es zulässt, ist die höchste Stufe die sichere Wahl.")
    else:
        print("\nKeine Stufe hat alle Versuche geschafft. Dann ist nicht die "
              "Sendeleistung das Problem, sondern Strecke/Antenne/Störer.")
    if any(x["eff"] is not None and x["eff"] != x["want"] for x in rows):
        print("Achtung: mindestens eine Stufe wurde anders gefahren als gewünscht – "
              "in der Tabelle steht der wirksame Wert.")


if __name__ == "__main__":
    main()
