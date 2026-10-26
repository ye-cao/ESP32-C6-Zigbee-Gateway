# Changelog

Alle nennenswerten Änderungen an diesem Projekt.
Format orientiert sich an *Keep a Changelog*, Versionierung an der Add-on-Version
(`host/addon/esp32c6_gateway/config.json`), weil das die einzige Versionsnummer ist,
die Nutzer in der Oberfläche sehen.

## [1.5.3] – 2026-10-26

Erste getaggte Version. Schwerpunkt: **Netzwerktopologie sichtbar machen,
OTA-Downloads zuverlässig machen und den seriellen Pfad unempfindlich gegen
einen toten USB-CDC-Filedescriptor**.

Stack: ESP-IDF 5.5.1 · `esp-zigbee-lib` 1.6.8 · `esp-zboss-lib` 1.6.4
Gerätekapazität: 64 Knoten · OTA image types `0x0001` (Endgerät) / `0x0002` (Repeater),
Hersteller-Code `0x131B`

### Added – Firmware (Koordinator, `main/`)

- **`zb_topo.c` / `zb_topo.h`**: echte Mesh-Topologie über ZDO `Mgmt_Lqi_Req`
  (`0x0031`) pro Knoten, inklusive Seiten-Fortsetzung und 15-s-Watchdog.
  Ausgabe als `{"t":"topo_mesh",…}`. Wichtig: Parameter **müssen** mit
  `ZB_BUF_GET_PARAM()` in den Buffer gelegt werden – `zb_buf_begin()` erhöht die
  Bufferlänge nicht, ZBOSS adressiert dann sich selbst (`0x0000`) und jedes Gerät
  meldet die Nachbarschaft des Koordinators.
- **`zb_ota_server.c`**: OTA-Server neu geschrieben. Vier Fehler, die den Download
  vorher unbrauchbar machten, sind behoben:
  1. `ota_data` wird jetzt mit `json_val()` geparst (das JSON enthält Leerzeichen
     nach dem Doppelpünktchen; ein fest verdrahtetes `strstr("\"data\":\"")` verwarf
     still jeden Block).
  2. Der **Datei-Offset des Clients** wird aus dem Image-Block-Request (ASDU)
     gelesen und dem Callback mitgeteilt. Der SDK-Callback liefert keinen Offset;
     ein blind hochgezählter Serverzeiger schob nach einem Neustart des Koordinators
     das gesamte Bild um die bereits übertragenen Bytes – der Grund, warum frühere
     Läufe bei „99,4 %" hingen und die Version nie aktiv wurde.
  3. Ein **2-KB-Fenster** wird vorab gefüllt, bevor `notify_req` läuft. Läuft der
     erste Block-Request ins Leere, bricht der SDK die Session ab (kein Retry).
  4. Statistik (`hit / miss / to / bad / served / sync`) plus `ota_stop`.
- **`zb_gateway.c`**: LQI/RSSI werden nur noch bei **gültigen** Werten überschrieben
  (`ind.lqi != 0`); neues Feld `lqia` (Sekunden seit der letzten gültigen Messung,
  `0xFFFFFFFF` = nie). Vorher setzte ein einziger Frame ohne LQI alle Geräte auf 0.
- **`main.c`**: `zb_lock()` / `zb_unlock()` um **jeden** SDK-Aufruf aus fremden Tasks
  (`uart_rx`, `btn`, `heartbeat`, `scan`). `port_uxCriticalNesting` ist global pro Kern,
  nicht pro Task – ungeschützte `esp_zb_*`-Aufrufe führen zu
  `assert failed: vPortExitCritical … (port_uxCriticalNesting[0] > 0)` und `rst:0xc`.
  Ausgenommen sind nur reine Flash-Pfade (`fw_ota_*`, `ota_data`).
  Puffer in `heartbeat_task` sind jetzt `static` (4 KB Task-Stack reichten nicht).
- **`ha_mqtt.c`**: `emit()` schreibt nicht mehr blockierend aus dem ZBOSS-Task.
  Zeile → Heap-Block → 16 tiefe Queue → eigener `tx_task`. Ein 4-KB-Topologie-Dump
  blockierte vorher ~1,4 s und riss den MAC-Layer in
  `Guru Meditation (Load access fault)` mit anschließendem Reboot.
- Geräteleistung schreibbar (`set_dev_power`, Basic `0x0016`).

### Added – Repeater (`router/`)

- **`zb_ota_client.c` / `zb_ota_client.h`**: OTA-Client, image type `0x0002`,
  `max_data_size = 223`, Version aus NVS, `esp_ota_set_boot_partition()` + Neustart.
- **Vendor-Cluster `0xFC00`**: `0x0001` = Sendeleistung (S8, dBm, **NVS-persistent**),
  `0x0002` = selbst gemeldeter Elternknoten.
- **BOOT-Taste kurz (<1 s)** schaltet die Leistung 8 → 14 → 20 → 8 dBm und speichert sie.

### Added – Add-on (`host/addon/esp32c6_gateway/`, v1.5.3)

- **Serieller Selbstheiler**: Nach einem Container-Neustart liefert der alte
  USB-CDC-Filedescriptor dauerhaft `[Errno 5] I/O error`; ein erneuter Versuch auf
  demselben fd erholt sich **nie**. Neu: `_NullSerial`, `_serial_open()` mit Retries,
  `_serial_reopen()` mit Backoff, `_serial_kick_reset()` (nur RTS, **nie** DTR –
  DTR ist der Strapping-Pin), verschluckte Schreibfehler und ein 45-s-Watchdog.
  Zusätzlich `POST /api/serial_reopen`.
- **Geräte-Availability**: jedes Gerät bekommt ein eigenes
  `{base}/dev/<addr>/status`-Topic, Discovery nutzt `availability_mode: "all"`.
  Vorher zeigten ausgefallene Schalter „aus" statt „nicht verfügbar", weil nur der
  Gateway-Zustand zählte. Bewertung über ein Punktesystem (APS-Frame, erfolgreicher
  `node_desc`-Probe, Nachbarschaftseintrag); ein Gerät gilt erst als verschwunden,
  wenn es im Gerätebestand steht, seit dem Start nichts gemeldet hat **und** in einer
  frischen Nachbarschaftstabelle fehlt (5 min Schonfrist).
- **Discovery-Migration**: `_disc_upgrade_avail()` ergänzt beim Start die zweite
  Availability-Quelle in bereits vorhandenen retained Configs. Ohne das behielten
  Geräte, die nach dem Update nie wieder etwas senden, für immer die alte Config.
- **Topologie-Auto-Sammlung** (`topo_interval`, Standard 60 s) und
  **15-s-Auto-Refresh** der Topologie-Seite; Rundenzähler, „zuletzt gesammelt"-Zeit
  und stabile Mesh-Daten (Geräte verschwinden nicht mehr kurzzeitig, nur weil ihre
  Antwort später eintrifft).
- **Geräteliste aus der Nachbarschaftstabelle** und aus rohen APS-Frames –
  reine Tasterpanels (die nie einen ZCL-Report senden) tauchen jetzt überhaupt erst
  in der Oberfläche auf.
- **Sendeleistung wird gemerkt** (`/data/gw_txpower.json`) und nach jedem
  Koordinator-Neustart erneut gesetzt, bis der Rücklesewert sie bestätigt.
  Die Firmware speichert sie **nicht** in NVS; ohne das lief das Gateway nach einem
  Stromausfall still mit der Standardleistung weiter (an `lqi`/`rssi` nicht erkennbar,
  weil die die Gegenrichtung beschreiben).
- Geräteseite: „zuletzt gemeldet" pro Gerät, LQI mit Altersangabe.
- Broadcast-Adressen (`0xfffc`–`0xffff`) werden bei `node_desc` verworfen; vorher
  erzeugte `0xffff` einen dauerhaften Fehleintrag „Gerät offline".
- Weitere Option `topo_interval` in `config.json` / `run.sh`.

### Added – Tests und Werkzeuge

- `tools/checks/run_all.py` – ein Aufruf prüft alles:
  Geräte-Availability/Discovery/Tx-Power, Topologie-Runden, serielle Selbstheilung,
  Web-UI-Rendering + i18n.
- `tools/ota_watch.py` – Durchsatz/Stillstands-Monitor für Zigbee-OTA.
- `tools/mq_dump.py` – retained Discovery-Configs und Availability-Topics auslesen.

### Changed

- Vorcompilierte Images `firmware/zigbee-vital-sensor-sdk16.bin` (663 616 B) und
  `firmware/zigbee-repeater.bin` (613 088 B) entsprechen jetzt exakt dem Stand der
  Quellen (Byte-Vergleich mit dem Build geprüft).
- `README.md` und `DEVICE_INTEGRATION_REPORT.md` um Topologie, OTA und
  Leistungsmessungen ergänzt; `OTA-调试记录-2026-10-26.md` dokumentiert die vier
  OTA-Server-Fehler im Detail.

### Fixed – Feldbefunde

- Repeater-OTA **an seinem Einsatzort** (LQI 40–51, RSSI −71 dBm): 613 088 B in
  **772 s**, ~920 B/s, **0 Stillstände**, Server `hit=2751 miss=0 to=0 bad=0`.
  Damit ist belegt: der frühere Einbruch auf 115 B/s war kein Funkproblem, sondern
  die vier OTA-Server-Fehler. Der Repeater muss zum Aktualisieren **nicht** neben
  den Koordinator gestellt werden.
- Obergrenze des Durchsatzes ist strukturell: 223 B pro Block überschreiten die
  APS-Nutzlast einer 802.15.4-Frame (~82 B), also drei Fragmente mit je einem ACK
  ⇒ ~4,4 Blöcke/s ≈ 950 B/s, unabhängig von der Linkqualität.

### Known limitations

- Netzwerke mit Geräten, die `Mgmt_Lqi_Req` nicht beantworten (z. B. Feibit-Schalter,
  `0x84 NOT_SUPPORTED`) und ESP32-C6-Repeater, deren ZBOSS `relationship` immer 2 und
  `depth` immer 0 liefert: Elternbeziehungen werden deshalb aus einem ungerichteten
  Kantengraph (Koordinator-Nachbarschaft + Selbstauskünfte) per BFS abgeleitet.
- Der Durchsatz ließe sich nur durch kleinere Blöcke (82/160 B) steigern; das
  erfordert ein USB-Flashen des Repeaters und ist nicht verifiziert.
- `dev_cmd`/`set_dev_power` wirken nur auf Geräte, die Basic `0x0016` beschreibbar
  anbieten; der Repeater speichert seine Leistung selbst in NVS.
