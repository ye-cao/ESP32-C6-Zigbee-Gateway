# -*- coding: utf-8 -*-
"""mqtt_probe.py – minimaler MQTT-3.1.1-Client (nur stdlib), um zu sehen,
was der Broker wirklich enthaelt.

    python mqtt_probe.py connect                 # nur CONNACK pruefen
    python mqtt_probe.py dump SECONDS [FILTER]   # '#' oder 'gw/#' …
"""
import socket, sys, time, json, os

HOST = os.environ.get("MQ_HOST", "192.168.101.5")
PORT = int(os.environ.get("MQ_PORT", "1883"))
USER = os.environ.get("MQ_USER") or None
PASS = os.environ.get("MQ_PASS") or None


def enc_len(n):
    out = bytearray()
    while True:
        d = n % 128
        n //= 128
        if n > 0:
            d |= 0x80
        out.append(d)
        if n == 0:
            return bytes(out)


def s(x):
    b = x.encode()
    return len(b).to_bytes(2, "big") + b


def pkt(t, body):
    return bytes([t]) + enc_len(len(body)) + body


class Cli:
    def __init__(self):
        self.sk = None

    def connect(self, cid="probe-py"):
        flags = 0x02                                   # clean session
        body = s("MQTT") + bytes([4]) + bytes([flags]) + (60).to_bytes(2, "big") + s(cid)
        if USER:
            flags |= 0x80
            if PASS:
                flags |= 0x40
            body = s("MQTT") + bytes([4]) + bytes([flags]) + (60).to_bytes(2, "big") + s(cid) + s(USER)
            if PASS:
                body += s(PASS)
        self.sk = socket.create_connection((HOST, PORT), timeout=10)
        self.sk.sendall(pkt(0x10, body))
        # Fixkopf = 1 Byte Typ + variable Laenge. Vorher wurden hier 2 Bytes
        # gelesen – dann ist das Laengenbyte schon weg und _varlen() liest
        # ein Byte zu viel (IndexError in body[1]).
        h = self._recv_exact(1)
        n = self._varlen()
        body = self._recv_exact(n)
        return h[0], body[0], body[1] if n > 1 else None   # typ, flags, rc

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            c = self.sk.recv(n - len(buf))
            if not c:
                raise IOError("Broker hat Verbindung geschlossen")
            buf += c
        return buf

    def _varlen(self):
        mult, val = 1, 0
        while True:
            b = self._recv_exact(1)[0]
            val += (b & 0x7F) * mult
            if not (b & 0x80):
                return val
            mult *= 128
            if mult > 128 ** 3:
                raise IOError("kaputte Laengenkodierung")

    def subscribe(self, topic, qos=1):
        body = (1).to_bytes(2, "big") + s(topic) + bytes([qos])
        self.sk.sendall(pkt(0x82, body))

    def read_pkt(self):
        t = self._recv_exact(1)[0]
        n = self._varlen()
        return t, self._recv_exact(n)


def do_connect():
    c = Cli()
    try:
        typ, fl, rc = c.connect()
        print("CONNECT → typ=0x%02x rc=%d  %s" % (
            typ, rc, {0: "angenommen (anonymous erlaubt)", 4: "bad user/pass",
                      5: "not authorized"}.get(rc, "?")))
        return rc == 0
    except Exception as e:
        print("CONNECT fehlgeschlagen:", type(e).__name__, e)
        return False
    finally:
        try:
            c.sk.close()
        except Exception:
            pass


def do_dump(secs, filt):
    c = Cli()
    typ, fl, rc = c.connect("probe-dump")
    if rc != 0:
        sys.exit("CONNACK rc=%d – keine Berechtigung" % rc)
    c.subscribe(filt, 0)
    print("abonniert %r – sammle %.1f s …\n" % (filt, secs))
    t0 = time.time()
    n = 0
    c.sk.settimeout(1.0)
    while time.time() - t0 < secs:
        try:
            t, body = c.read_pkt()
        except socket.timeout:
            continue
        if (t & 0xF0) != 0x30:
            continue
        qos = (t >> 1) & 3
        tl = int.from_bytes(body[:2], "big")
        topic = body[2:2 + tl].decode("utf-8", "replace")
        p = 2 + tl + (2 if qos else 0)
        payload = body[p:]
        n += 1
        print("[%6.2f] %s" % (time.time() - t0, topic))
        print("         %s" % payload.decode("utf-8", "replace")[:400])
    print("\n%d Nachrichten in %.1f s" % (n, secs))


def do_pub(topic, payload="", retain=True):
    """Retained-Nachricht setzen bzw. mit leerem Payload loeschen."""
    c = Cli()
    typ, fl, rc = c.connect("probe-pub")
    if rc != 0:
        sys.exit("CONNACK rc=%d – keine Berechtigung" % rc)
    body = s(topic) + payload.encode()
    hdr = 0x30 | (0x01 if retain else 0x00)          # PUBLISH, QoS 0
    c.sk.sendall(pkt(hdr, body))
    time.sleep(0.25)
    c.sk.close()
    print("%s %s  (%d B)" % ("geloescht" if retain and not payload else "gesetzt",
                             topic, len(payload)))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "connect"
    if cmd == "connect":
        sys.exit(0 if do_connect() else 1)
    if cmd == "pub":
        do_pub(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")
        sys.exit(0)
    do_dump(float(sys.argv[2]) if len(sys.argv) > 2 else 10,
            sys.argv[3] if len(sys.argv) > 3 else "#")
