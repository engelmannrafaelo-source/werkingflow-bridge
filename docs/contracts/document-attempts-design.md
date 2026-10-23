# Dokument-Attempts: fehlende Ausführungsgrenze und Anschlussvertrag

Stand 23.10.2026. **Entwurf, nicht implementierte API. L4 bleibt ROT.**
Der Dokumentjob besitzt derzeit weder einen steuerbaren Remote-Prozess noch eine
persistente Remote-Attempt-Identität. Diesen Unterschied darf kein lokales
`task.cancel()` oder `cancelled` verdecken.

## Gemessener Quellstand und verbleibende Grenzen

| Stelle | Grenze / Verhalten | Urteil und Begründung |
|---|---|---|
| `jobs/document.py`, `main._proxy_document_endpoint` | Read/Write/Pool ohne Wanduhr; Connect über `PRIVACY_SERVICE_CONNECT_TIMEOUT_S` (Default 10 s) | Verbindungsaufbaufrist, keine Rechenfrist; Transportverlust nach möglicher Annahme bleibt unbekannter Ausgang. |
| `privacy_service/app.document_convert_endpoint` | `run_in_executor(None, chain.convert, …)`; gemeinsames Semaphore mit Default 2 | Nebenläufigkeitsgrenze, keine Stop-Funktion. Ein abgebrochener Await beendet den Thread nicht. Semaphore ist kein Nachweis für <2 GB. |
| `privacy_service/document_converter.py` | LibreOffice 300 s; Default maximal 600 Seiten | **L4 offen.** Seitenzahl ist aus einer alten Laufzeitannahme abgeleitet, kein belegtes Speichermaß. Beide Grenzen bleiben bis zum Ressourcenstopp bestehen. |
| `privacy_service/image_describer.py`, `image_triage.py` | `VisionProvider.analyze` mit 300-s-Default | **L4 offen.** Lokale Vision-Wanduhr bleibt bis zum Stop-/Reconcile-Umbau. Der HTTP-Aufruf an den Anbieter hat keinen hier implementierten Stop-/Statusvertrag. Socket-Ende belegt keinen Anbieter-Stopp. |
| `document_converter._docling_batch_pages`, `_render_max_bytes` | Default 25 Seiten pro Batch, 384 MiB kumulierte Bilder | Arbeitsmengen-/Speicherbegrenzung; 413 bei zu vielen Bildbytes bleibt fachlich sichtbar. Keine 2-GB-Garantie; Text, Modelle und Kopien kommen hinzu. |
| `image_describer`, `image_triage` | Ausgabeparameter `max_tokens=1500/1200` | Antwortformatbudget, keine Auftragslebensdauer. Abgeschnittene Beschreibungen werden markiert; vollständige Pixelabdeckung ist dadurch nicht belegt. |
| `jobs/store.prune_jobs` | Terminale Retention 2 h, mit Idempotenz mindestens 24 h | Keine aktive Dokumentarbeit wegen Erstellungsalter beenden. |
| `jobs/registry._defer_job` | Dokumentjobs ohne Max-Defers-Abbruch | Backoff bleibt; andere Jobarten behalten ihren Vertrag. |
| `jobs/store.claim_stale_job`, `find_abandoned` | Crash-Versuchszähler weiterhin begrenzt | **L4 offen.** Einfach entgrenzen würde ohne Remote-Reconcile mehrfach dieselbe Konvertierung starten. Erst Reconcile, danach diese lokale terminale Grenze entfernen. |
| `jobs/store.claim_stale_job` | Stale Running + Cancel-Wunsch wird geschlossen, Buchung ggf. unbekannt | **Remote-Stopp unbewiesen.** Der vorhandene Watchdog schließt die DB-Zeile, ohne eine entfernte Konvertierung zu stoppen. |
| `jobs/store_client` | Schreiben 10 s, Kontrolllesen mit eigenem Timeout | Technische Einzelversuche; große Payloads und Verluste beim Abschluss ungeprüft. Transportfehler dürfen keinen neuen fachlichen Auftrag auslösen. |
| nginx | 75 MiB JSON-Body; direkter Sync-Pfad mit Proxyfrist | Base64-Rohdatei <56,25 MiB abzüglich Envelope. Keine bewiesene E2E-Kapazität. Der Job-Lauf erfolgt als Self-Call ohne nginx, Submit/GET nicht. |

## Gemeinsamer L6-Vertrag nach B2

`call_id` bezeichnet genau eine fachliche Dokumentgeneration und bleibt über
Submit-Replay und Workerwechsel stabil. `attempt_id` bezeichnet eine tatsächliche
Remote-Ausführung. Eine neue Attempt-ID entsteht erst nach belegtem Ende oder
belegter Nichtannahme des alten Attempts. Transportverlust ist kein solcher Beleg.
Die IDs gehören in dedizierte, serverseitig gebundene Vertragsfelder, nicht in
beliebige Multipart-Felder. Owner/Home-Bridge, Quelldigest, Optionen,
Konverter-/Planversion und Generation gehören zur unveränderlichen Identität.

Vorhanden ist nur die atomare Submit-Idempotenz: `Idempotency-Key` unter
Owner-Scope, gleiche Eingabe liefert dieselbe Job-ID. Ein Konflikt antwortet
aktuell **422** (`idempotency_key_reused`), nicht die in B2 vorgeschlagene 409.
Den bestehenden generischen API-Vertrag nicht still ändern; ein künftiger
versionierter Dokumentvertrag muss die Abweichung ausdrücklich auflösen.
`deadline_at`, Abbruchgeneration, Artefaktreferenz und Attempt-Fencing sind noch
nicht implementiert. Clients dürfen diese Felder nicht als unterstützt behandeln.

Der nächste zusammenhängende Bau benötigt:

1. Authentifizierte, persistente Dokument-Artefakte und Remote-Attempt-Zeilen.
   Annahme speichert Digest und ID atomar vor Ausführung; gleicher Attempt und
   gleicher Digest liefert denselben Zustand, abweichender Digest einen Konflikt.
   Speicherausfall ist laut. Keine bloße In-Memory-Tabelle als Restart-Idempotenz.
2. Einen eigenen Prozess beziehungsweise eine isolierte Prozessgruppe je
   Konvertierung mit Reaper. Docling und LibreOffice gehören in diese Gruppe.
   Nutzerabbruch/Gesamtfrist setzt zuerst persistent den Wunsch. Stop-Quittung
   kommt erst nach Reaping aller zugehörigen Prozesse; Supervisor-Verlust darf
   keine unkontrollierten Kinder übriglassen. PID allein ist keine Identität.
3. Visionteiljobs mit eigenem Lebenszeichen und Ergebnis-/Stop-Reconcile. Solange
   der verwendete Anbieter keine Stop-Quittung bietet, bleibt dessen Ausgang
   ausdrücklich unbekannt oder wird bis zum dokumentierten Ende abgeglichen.
   Ein Prozessgruppen-Stopp im Dokumentdienst belegt nur lokale Freigabe, nicht
   das Ende bereits gestarteter externer Anbieterarbeit oder deren Abrechnung.
4. Bei Workerübernahme zuerst Remote-Status lesen; derselbe laufende Attempt
   wird übernommen, ein fertiges Ergebnis abgeholt. Unklarer Zustand bleibt
   sichtbar, ohne blindes erneutes Absenden. Claim-Fencing schützt Fortschritt
   und Abschluss davor, dass ein alter Worker einen neuen Claim überschreibt.
5. Stop-Wunsch, ursprüngliche absolute Gesamtfrist und Generation werden bis
   zum Teiljob propagiert. Terminale Writes müssen auf Claim-/Cancelgeneration
   bedingt sein. Das aktuelle unbedingte `store.mark_done` genügt dafür nicht.
   Eine Frist wird weder beim Retry verlängert noch lokal ersatzweise erfunden.

Damit sind Ressourcenstopp, Wiederaufnahme und spätes Done EIN Architekturpaket.
Nur neue IDs in die heutige synchrone Antwort zu schreiben löst keinen dieser
Lebenszyklusfehler. Dieses Paket ist im aktuellen Folgecommit noch offen.

## Kapazitätsnachweis vor Auslieferung

Nicht gemessen: nginx → Platform-API → DB → Watchdog-Claim, Store-Schreibtimeout,
Konverter-RSS, Ergebnisgröße, parallele große Dokumente und Gesamtprozess-RSS
unter 2 GB. Der vorhandene Quellkommentar nennt bereits rund 3,1 GB für zwei
Docling-Batches zusätzlich zu Modellen; das steht im Konflikt mit der geforderten
2-GB-Grenze und ist keine Freigabe. Auch Markdown/JSON und DB-Ergebnisgröße haben
keine bewiesene Grenze: ein Store-Schreibfehler nach vollständiger, bezahlter
Konvertierung ist möglich. Keine Live-/GPU-/DB-Messung in diesem Bau; deshalb keine Maximalgröße
als freigegeben ausweisen. Synthetische Dateien ohne Kundendaten sollen sowohl
komprimierte große PDFs als auch kleine Dateien mit großer Renderausdehnung
abdecken. Peak-RSS je beteiligtem Prozess und parallelem Lauf erfassen; erfolgreiches
Submit allein ist kein Nachweis. Bei einer verweigerten Ressourcenaufnahme bleibt
der Fehler sichtbar und darf nicht als vollständig konvertiertes Resultat gelten.

Pyramidenkontext: verlorene Submit-Antwort, konkurrierende gleiche Submits,
Digest-Konflikt, Worker-/Dienstneustart während Docling/LibreOffice/Vision,
Cancel vor und nach Remote-Annahme, verspätetes Done, verlorene Stop-Antwort,
Fristablauf und Wiederaufnahme derselben Generation, ausfallender Store beim
Abschluss. Kein neues Unit-Testpaket und kein grünes Urteil ohne diese Belege.

## Stand nach der ersten Zweitprüfung

Die zunächst vorbereitete Entfernung von LibreOffice-/Vision-Fristen und
Seitendeckel wurde nach Opus R1 zurückgenommen. Sie war ohne Ersatz für den
Ressourcenstopp nicht integrationsfähig und hätte alle synchronen Dokumentaufrufer
betroffen. Im Folgecommit bleiben nur die Pending-/Deferral-Ausnahmen, die
Log-/Docstring-Korrekturen und dieser Vertrag. Es ist keine neue technische
Freigabe-Sperre gebaut; der Bericht weist den vollständigen Auftrag als offen aus.

## Ergänzungen aus Opus R2

Die Pending-Ausnahme liegt in der Platform-API (`store.prune_jobs`), die
Deferral-Ausnahme im Worker. Erst beide aktualisierten Komponenten erfüllen
diesen Teilvertrag. Ein gemischter Stand mit alter Platform-API behält den
Altersabbruch. Dies ist eine Auslieferungsnotiz, kein erfolgter Deploy.

Der Watchdog übernimmt bis zu 50 fällige Jobs pro Pass einschließlich Payload
und startet sie als Hintergrundaufgaben. Bei einem großen Rückstau kommen
Base64-Kopien und Dekodierung gleichzeitig zusammen; das erhöht das ungeprüfte
2-GB-Risiko. Die Zahl 50 ist eine Passgröße, keine belegte Speicherzulassung.
Auch veränderliche Fehlertexte können weiterhin neue WARNING-Meldungen erzeugen.

Deploy-Gate im Quellstand geprüft: `scripts/platform_api_job_gate.py` setzt
`busy` ausschließlich bei `active > 0`. `store.find_active` führt zukünftige
Deferrals getrennt als `waiting`; diese allein sperren den Gate nicht. Fällige
Pending-Jobs und Running-Jobs zählen dagegen als aktiv. Kein Live-Gate gestartet.

Opus R2 nennt Unit-Tests als Integrationsbedingung. Maßgeblich bleibt der
explizite Auftrag: keine neuen Unit-Tests, stattdessen Architektur-/Pyramidenbelege
für Prune-Ausnahme, Deferral nach bisheriger Zählgrenze, fehlende Cancel-Route und
Poll-Fehler/Erholung. Solche Laufbelege sind mit diesem Teilbau nicht erbracht.

## Bauabschnitt Ressourcenstopp (23.10.2026, Folgeauftrag)

`/document/convert` verwendet jetzt einen separaten Konverterprozess und einen
Linux-Subreaper. Docling, LibreOffice, Triage und Bildbeschreibung gehören zum
Kindprozess. Nach Clientverlust oder Abbruch des Dienst-Tasks schließt der
Kontrollkanal; der Reaper beendet die Prozessgruppe und sammelt auch adoptierte
Nachkommen ein. Erst danach schreibt er eine fsync-gesicherte lokale Quittung.
Dies ersetzt den Thread für diesen Endpunkt; `/convert-pdf` und
`/document/convert-and-anonymize` behalten vorerst ihren bisherigen Vertrag.

Die Quittung bestätigt ausschließlich lokale Ressourcen. Für gestartete Vision
kann nach Abbruch der Anbieter-Ausgang unbekannt sein. Ein getöteter Reaper,
Hostverlust oder fehlende Quittung ist niemals ein bestätigter Stop. Ein
Dienst-/Worker-Neustart braucht zusätzlich den persistenten Attempt-Abgleich
(Bauabschnitt 3). Die internen Konverterfristen bleiben in Abschnitt 1 bestehen.
Synthetischer CSV-Lauf: Ergebnis 200, Konverter-Exit 0, Reap-Quittung vorhanden;
keine KI-Anfrage. Noch keine Opus-Prüfung dieses Folgeabschnitts.

## Bauabschnitt Fristen

Nur im reaper-eigenen Konverterprozess gilt jetzt: keine 300-s-LibreOffice-Frist,
kein 600-Seiten-Abbruch, keine 300-s-Vision-Frist. Legacy-Aufrufer in Threads
behalten diese Schutzgrenzen. Der Ausführungskontext ist nicht durch Eingabefelder
oder eine Umgebungseinstellung aktivierbar. LibreOffice erhält ein eigenes Profil
im Konverter-Verzeichnis; es darf sich nicht an einen fremden Prozess anhängen.

Verbleibend: Vision-TCP-Aufbau 10 s (Verbindungsaufbau), Renderbudget 384 MiB
(Speicherverweigerung, 413), Docling-Batchgröße 25 (Arbeitsportion), Vision-Ausgabe
1500/1200 Tokens (Antwortbudget mit Unvollständigkeitsmarker), gemeinsames
Konverter-Semaphore (Parallelität). Keine dieser Grenzen belegt vollständige
Kapazität unter 2 GB. Der Sync-Pfad hat weiterhin nginx-/Aufruferfristen; dessen
Verbindungsende fordert lokalen Stop. Ohne Nutzerabbruch oder Gesamtfrist kann
ein lebender, aber hängender Konverter weiterhin laufen. Anbieterarbeit nach
lokalem Stop bleibt unbekannt; es existiert keine Anbieter-Stop-API.
