# -*- coding: utf-8 -*-
"""Test der persistenten Knoten-Zuordnung aus serial_gateway.py.

Extracts the real code block from the shipped source (no copy!) and runs it
against a temp _NODES_FILE. Sequence mirrors the real event order:

  1. Geraet joint  -> {"t":"zigbee","sub":"join",...,"ieee":"..."}
                      dispatch ruft _ha_learn_ieee(addr, ieee)
  2. Erstes _ha_node(addr) bildet die Node-ID
  3. Geraet kommt mit NEUER Kurzadresse zurueck, join-Event wieder zuerst
  4. _ha_node(neue_addr) MUSS die alte Node-ID liefern
"""
import io, json, os, re, sys, tempfile

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "host", "addon", "esp32c6_gateway", "serial_gateway.py")

with io.open(SRC, "r", encoding="utf-8") as f:
    src = f.read()

a = src.index("_ha_devices = {}")
b = src.index("def _ha_state(")
block = src[a:b]
print("extrahiert: %d Zeichen" % len(block))

tmpdir = tempfile.mkdtemp(prefix="ndtest_")
nodes_file = os.path.join(tmpdir, "ha_nodes.json")

ns = {"os": os, "json": json, "__builtins__": __builtins__,
      # Modul-Globale, die VOR dem extrahierten Block stehen:
      "_noread": set(), "_ctrl": set()}
exec(compile(block, "<extract>", "exec"), ns)
ns["_NODES_FILE"] = nodes_file          # isolate from any real /data
ns["_DATA_DIR"] = tmpdir

ok = 0
total = 0


def check(label, cond, detail=""):
    global ok, total
    total += 1
    if cond:
        ok += 1
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s %s" % (label, detail))
        sys.exit(1)


IEEE = "00:12:4b:00:aa:bb:cc:dd"

print("\n1) Join 0x07da, IEEE gelernt, dann Node-ID bilden")
ns["_ha_learn_ieee"]("0x07da", IEEE)                    # <- was der Test vorher fehlte
n1 = ns["_ha_node"]("0x07da")
print("     c6_%s" % n1)
check("Node-ID wie bisher aus der Adresse (keine Migration)", n1 == "c6_0x07da",
      "got %r" % n1)

print("\n2) Zweiter Aufruf: Name bleibt eingefroren")
n1b = ns["_ha_node"]("0x07da")
check("stabil", n1b == n1, "%r != %r" % (n1b, n1))

print("\n3) Datei wurde geschrieben")
check("ha_nodes.json existiert", os.path.isfile(nodes_file))
with io.open(nodes_file, "r", encoding="utf-8") as f:
    saved = json.load(f)
check("addr_nodes hat 0x07da", saved["addr_nodes"].get("0x07da") == n1, str(saved["addr_nodes"]))
check("ieee_nodes hat die IEEE", saved["ieee_nodes"].get(IEEE) == n1, str(saved["ieee_nodes"]))
check("addr_ieee hat die Zuordnung", saved["addr_ieee"].get("0x07da") == IEEE, str(saved["addr_ieee"]))

print("\n4) Geraet kommt mit NEUER Adresse 0x08a1 zurueck (join-Event zuerst!)")
ns["_ha_learn_ieee"]("0x08a1", IEEE)
n2 = ns["_ha_node"]("0x08a1")
print("     c6_%s" % n2)
check("alte Node-ID uebernommen (keine Doppel-Entities)", n2 == n1, "%r != %r" % (n2, n1))

print("\n5) Ein ZWEITES Geraet darf nicht kollidieren")
ns["_ha_learn_ieee"]("0x0abc", "00:12:4b:11:22:33:44:55")
n3 = ns["_ha_node"]("0x0abc")
print("     c6_%s" % n3)
check("eigene Node-ID", n3 == "c6_0x0abc", "got %r" % n3)

print("\n6) Neustart simulieren: Datei neu laden, alte Adresse behaelt Namen")
ns2 = {"os": os, "json": json, "__builtins__": __builtins__,
       "_noread": set(), "_ctrl": set()}
exec(compile(block, "<extract2>", "exec"), ns2)
ns2["_NODES_FILE"] = nodes_file
ns2["_DATA_DIR"] = tmpdir
ns2["_nodes_load"]()
n4 = ns2["_ha_node"]("0x07da")
check("nach Neustart gleiche Node-ID", n4 == n1, "%r != %r" % (n4, n1))

print("\n7) Neustart + Geraet mit anderer Adresse (kein join-Event in dieser Sitzung)")
n5 = ns2["_ha_node"]("0x08a1")
check("trotzdem alte Node-ID (aus Datei)", n5 == n1, "%r != %r" % (n5, n1))

print("\n%d/%d Pruefungen bestanden" % (ok, total))
print("temp: %s" % nodes_file)
