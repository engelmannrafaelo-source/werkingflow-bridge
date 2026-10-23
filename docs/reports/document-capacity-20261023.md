# Dokumentdienst — synthetischer Kapazitätsbefund, 23.10.2026

Messzeit 13:39:01 UTC. Codebasis `dcc54e695f6ac6fb6c567d6f83159aed6720ede1`.
Kein Deploy, keine Kundenunterlagen, keine bezahlte KI. Einmaliger lokaler
Prüfkontext, keine neue Unit-/Regressionstestsuite.

## Tatsächliche Messung

Zwei synthetische CSV-Payloads mit je **57.671.680 Bytes (55 MiB)**.
Die Datei enthält eine Spalte mit synthetischen Zeichen, keine Kundendaten.

| Teilweg | Gemessen |
|---|---:|
| JSON-Job-Envelope für eine Datei | 76.895.725 Bytes |
| nginx-Vertragslimit, nur Quellvergleich | 78.643.200 Bytes (75 MiB) |
| Zwei JSON-Parse-/Pydantic-Validierungen | 1,062 s |
| Peak-RSS nach paralleler Envelopehaltung | 541.851.648 Bytes (~516,8 MiB) |
| Zwei gleichzeitige Multipart-Annahmen durch echte FastAPI-Routen via ASGI | 0,536 s |
| Persistierte Quellen | zweimal exakt 57.671.680 Bytes |
| Peak-RSS des gesamten kombinierten Prüfprozesses | 541.851.648 Bytes |
| Harte Adressraumgrenze nur des Prüfprozesses | 1.887.436.800 Bytes (1800 MiB) |

Der Prüfprozess enthielt Client, Server-ASGI, Validierung und synthetisches
Material zusammen. Damit ist dies **kein RSS-Wert eines ausgelieferten Workers**.
Die Konverteraufnahme war gezielt gesperrt (Semaphore 0); beide angenommenen
Aufträge wurden vor Prozessstart storniert. Jeweils cancelled mit bestätigter
Nichtausführung, kein `started.json`. Keine Bildbeschreibung aufgerufen.

Annahme nutzte tatsächliche Multipart-Verarbeitung, Private-Volume-Dateien,
fsync/atomare Veröffentlichungen und die echten Remote-Kontrollrouten. Der
HTTP-Netzwerkstack und nginx waren nicht beteiligt. Die JSON-Job-Envelope-
Validierung war eine getrennte lokale Probe und lief nicht durch Platform-API/DB.

## Unbewiesen — keine Größenfreigabe

- Tatsächlich laufender nginx mit 75M und dessen Job-Pool-Routing; die
  theoretische Base64-Grenze bleibt <56,25 MiB abzüglich Envelope.
- Platform-API → Postgres → Watchdog-Claim, inklusive Rückstau mit bis zu
  50 Claims je Pass, großen JSONB-Zeilen und maximalem Prozess-RSS.
- Der vorhandene Store-Schreibtimeout von 10 s, verlorene Antworten,
  Ergebnisgröße und dauerhafter Abschluss großer bezahlter Resultate.
- PDF-/Docling-/GPU-Konvertierung und parallele Konverterprozesse unter 2 GB.
  Der bisherige Quellkommentar für 25-Seiten-Batches (~1,6 GB zusätzlich zu
  Modellen je Konvertierung) bleibt ein konkreter offener Kapazitätsbefund.
- Komprimierte große PDFs sowie kleine Dateien mit großer Renderausdehnung.
- Nachweis der persistenten Volume-Bindung auf dem tatsächlich verwendeten
  gemeinsamen Dev-/Prod-Dokumentdienst und ein begrenztes Retention-Verfahren.

Diese Werte erlauben keine Freigabe großer Kunden-PDFs. Es wurden keine
Produktionsdatenbanken oder Dienste zu Messzwecken verändert. Lokale Postgres-
Serverbinaries (initdb/postgres) fehlen; vorhandene Clientprogramme ersetzen
keinen Datenbank-Laufnachweis. Der Berater muss die verbleibenden Infrastruktur-
und Pyramidenbelege im autorisierten Umfeld organisieren.

Maschinenlesbarer Originalbefund im Auftragsordner:
`MESSUNG-BRIDGE-DOKUMENTDIENST-KAPAZITAET-20261023.json`.

## Parallelitätskorrektur im selben Abschnitt

Die Lektüre der Deadline-/DELETE-Pfade zeigte einen gemeinsamen festen `.new`-
Namen beim atomaren Schreiben. Er wurde durch ein privates temporäres Inode je
Schreiber ersetzt (0600, fsync, rename, Verzeichnis-fsync). 32 konkurrierende
synthetische Schreibvorgänge mit vier Threads ergaben einen vollständigen
Datensatz und keine temporären Reste. Zudem findet der Vision-Konfigurations-
Preflight nur bei neuer Annahme statt; ein bereits angenommener Replay oder
Cancellation-Tombstone hängt nicht von später veränderter Vision-Konfiguration ab.
