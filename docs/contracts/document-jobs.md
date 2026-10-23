# Dokumentkonvertierung als Job

Stand 23.10.2026, Folgeauftrag Dokumentdienst. Implementierter Feature-Zweig,
**R1 geprüft, Mindestfixes zur letzten Runde R2; nicht integriert oder ausgeliefert**. Große Dokumente und
Postgres-CAS sind nicht Ende-zu-Ende abgenommen. Keine L4-/L6-Vollabnahme.

## Öffentlicher Auftrag

Authentifiziert `POST /v1/jobs`, `kind=document-convert`, bestehende
Owner-/App-/User-/Home-Bridge-Bindung und `Idempotency-Key` wie bei anderen Jobs:

```json
{
  "kind": "document-convert",
  "payload": {
    "filename": "example.csv",
    "content_type": "text/csv",
    "content_base64": "YSxiCngsMQo=",
    "fields": {"describe_images": "false"},
    "deadline_at": "2026-11-01T12:00:00+00:00"
  }
}
```

`deadline_at` ist optional, absolut und zeitzonengebunden. Es gehört zum
unveränderlichen Auftragsdigest; Retry verlängert es nicht. Ohne Frist gibt es
keine lokale Ersatzfrist. `fields` enthält nur Stringwerte; `file` ist reserviert.
Base64 ist strikt, Dateiname/Content-Type enthalten kein CR/LF/NUL. Fehler nennen
keine Eingabewerte. Gleicher Idempotenzschlüssel und Owner liefert dieselbe
Job-ID; anderer Inhalt weiterhin **422** gemäß generischem Submit-Vertrag.

`GET /v1/jobs/{id}` bleibt Ergebnisweg. Zustände bleiben
pending/running/done/error/cancelled; kein neues `expired`-Enum. Fristablauf wird
als cancelled mit `progress.abbruch.reason=deadline_expired` und Stop-Beleg
sichtbar. Ein erfolgreiches Resultat enthält `success`, `format`, `markdown`,
`metadata`, optionale Bilder sowie `call_id` und `attempt_id`.

## Remote-Identität und Fencing

Der Dokumentrunner benutzt die private authentifizierte Dienst-API
`PUT|GET|DELETE /document/calls/{call_id}`. Er fällt bei fehlender API nicht auf
den synchronen Endpunkt zurück. `call_id` wird aus Home-Bridge und serverseitiger
Job-ID gebunden; Workerwechsel ändern es nicht. Der Dienst vergibt `attempt_id`
bei persistenter Annahme. Quelle, Optionen, Reparaturmodus und Frist werden
vor dem Start atomar veröffentlicht. Wiederholtes PUT liefert denselben Attempt;
abweichender Digest dort **409**. GET liefert accepted/running/done/error/
cancelled/interrupted/unknown. Diese internen Zustände sind kein neuer öffentlicher
Jobstatus.

Bei Antwortverlust zuerst GET; bei unbekanntem Remote-Ausgang kein neuer Attempt.
Workerwechsel liest denselben laufenden Attempt oder sein gespeichertes Ergebnis.
Ein fehlender Guardian ohne Quittung wird anhand der gespeicherten Kernel-Boot-,
PID-Namespace- und PID-1-Startkennung aufgeklärt. Eine andere äußere Instanz
belegt den lokalen Prozessbaumverlust: interrupted mit
`reason=service_instance_gone`, `local_stopped=true` und `reaped=true`. Ein bloßer
API-Prozessneustart ist dafür kein Beleg. Gleiche/fehlende Identität bleibt
unknown; der öffentliche Fortschritt enthält dann
`diagnostic_code=DOCUMENT_RECONCILIATION_REQUIRED` und
`reconciliation_required_since`. Keine automatische erneute bezahlte Ausführung. Nach Dienstverlust quittiert der
unabhängige Guardian lokal interrupted. Dies ist ein technischer Fehler, kein
lokaler Zeit-/Versuchsabbruch. Es gibt noch keine Wiederaufnahme einzelner
Docling-/Vision-Teilschritte nach einem solchen Ende.

`ai_jobs.attempts` ist der monoton steigende Claim-Fence. Der neue interne
`document-state`-Weg prüft Claim, Zustand und Abbruchwunsch unter derselben
Postgres-Zeilensperre. Heartbeat, Fortschritt, done, error und cancelled sind
bedingt. Veraltete generische Mutatoren dürfen Dokumentzeilen nicht schreiben.
Der Watchdog übernimmt stale Dokumente auch bei Abbruchwunsch zum Remote-Abgleich;
er schließt sie weder nach Versuchszähler noch ohne Remote-Quittung. Frische
Claims und Watchdog-Claims verwenden denselben Fence. Andere Jobarten behalten
ihren bestehenden Vertrag. Platform-API, Worker und Dokumentdienst müssen vor
Nutzung dieses Vertrags zusammenpassen; kein Upgrade laufender alter Dokumentjobs.

## Ressourcenstopp und Abbruch

`DELETE /v1/jobs/{id}` nutzt weiter die Ownerprüfung. Pending kann sofort enden;
Running registriert den Wunsch. Der Runner persistiert DELETE am Remote-Dienst.
DELETE vor dessen PUT erzeugt einen Tombstone, der auch verspätete alte PUTs
fenced. Erst nach lokaler Reap-Quittung darf der Job cancelled werden. Ein
Nutzerabbruch, der unter der DB-Zeilensperre vor done gewinnt, verhindert spätes
done. Fertige Remote-Ergebnisse bleiben auch bei verworfenem Job-Ergebnis erhalten.

Der Konverter läuft mit Docling/LibreOffice/Vision in einer eigenen Prozessgruppe.
Ein separater Linux-Subreaper beendet die Gruppe und adoptierte Nachkommen. Er
quittiert erst nach Reaping. Dienstverlust schließt seinen Kontrollkanal; die
ursprüngliche Gesamtfrist überwacht er zusätzlich selbst. LibreOffice hat ein
eigenes Profil, damit es nicht an einen fremden Prozess delegiert.

Die Quittung belegt **lokale** Ressourcenfreigabe. Vor jedem Vision-Aufruf wird
ein Pending-Beleg fsync-gesichert, erst eine empfangene Anbieterantwort entfernt
ihn. Nach lokalem Stop kann Anbieterarbeit unbekannt bleiben. Dann lautet der
Abbruchledger `unbekannt`, niemals bestätigte Anbieter-Stornierung oder Kosten-
Rücknahme. Guardian-SIGKILL in derselben äußeren Instanz bleibt ohne Quittung unbekannt.
Ein belegter Container-/Kernelwechsel quittiert nur den lokalen Verlust;
Provider-Pending-Belege bleiben dabei maßgeblich.
Ein lebender, hängender Konverter ohne Nutzerabbruch oder Gesamtfrist darf weiter
laufen; Heartbeat ist keine Aussage über fachlichen Fortschritt.

## Grenzen und Aufbewahrung

- Managed Konverter: keine 300-s-LibreOffice-/Vision-Frist, kein 600-Seiten-Abbruch.
- Legacy `/convert-pdf` und `/document/convert-and-anonymize`: noch Threads und
  bisherige Fristen. `/document/convert` nutzt ebenfalls den Reaper, ist aber
  synchron und ausdrücklich `managed=False`: die bisherigen 300-s-
  LibreOffice-/Vision-Fristen und der 600-Seiten-Deckel bleiben erhalten.
  Ein am Dienst erkannter Client-Abbruch fordert lokalen Stop; eine verlässliche
  Weiterleitung von Disconnects über die Bridge ist damit nicht belegt.
- Vision-Verbindungsaufbau 10 s; Remote-Kontrollversuche 30 s; Store-Schreiben 10 s.
  Das sind Einzelversuche. Remote-Ausgang und fertige Ergebnisse werden danach
  abgeglichen, kein neuer fachlicher Auftrag erzeugt.
- Docling-Batchgröße 25, Renderbudget 384 MiB, Vision-Ausgabe 1500/1200 Tokens,
  Semaphore gemäß `BRIDGE_MAX_CONCURRENT_CONVERSIONS`: Arbeits-/Antwort-/Speicher-
  grenzen. Renderüberschreitung bleibt 413. Keine Garantie vollständiger Abdeckung.
- nginx JSON 75 MiB: Base64-Datei weniger als ca. 56 MiB einschließlich
  Envelope-Reserve. Dokumentdienst selbst: 100 MiB Multipart. Dies sind
  Transportgrenzen, **keine nachgewiesene E2E-Größenfreigabe**.
- Terminaler Jobstore: 2 h, mit Idempotenz mindestens 24 h. Aktive Dokumentjobs
  enden nicht wegen ihres Alters oder Deferralzählers.
- Remote-Daten unter `/var/lib/bridge-document-jobs`, privates Docker-Volume,
  Verzeichnis 0700. Keine öffentliche URL. Volume darf bei Recreation nicht
  erneuert/gelöscht werden. ACK und Inhaltsaufbewahrung sind im folgenden
  Abschnitt beschrieben; ein vollständig begrenzter Speicher bei unbestätigt
  laufenden Prozessen bleibt vor Deploy nachzuweisen.

Keine neue Umgebungsvariable. Bestehende ActualText-Reparatureinstellung wird als
unveränderlicher Remote-Vertragswert übertragen und im isolierten Prozess
angewandt. Lokale Dokumentaktivität verwendet eine stabile Call-UID;
Attribution bleibt aus dem gespeicherten Auftrag. Best-effort-Metrik kann bei
Workerverlust nach Jobabschluss fehlen; der persistente Job/Remote-Beleg bleibt.

## Prüfkontext

Bereits lokal mit synthetischem Material geprüft: Annahme/Replay/Digestkonflikt,
CSV-Ergebnis mit Reap, Running-Abbruch, Tombstone vor verspätetem PUT,
Frist vor Start, verpflichtende Auth auch im Übergangsmodus, persistentes
Zurücklesen, fehlende Guardian-Quittung und Owner-Pipe-Verlust.
Offen: Postgres-CAS unter konkurrierenden Workern, voller nginx → Platform-API →
DB → Watchdog-Pfad mit großen Dateien, Store-Antwortverlust, GPU-/PDF-Peaks,
bezahlte Vision-Reconciliation und Opus-Abnahme. Keine neuen Unit-Tests.

Lokale Kapazitätsmessung und ihre Grenzen:
[synthetischer Befund vom 23.10.2026](../reports/document-capacity-20261023.md).

## R1-Korrekturen: Ablehnung, Erstumstellung und Volume-Verlust

HTTP 4xx außer 404/408/429 sowie die feste 503-Konfigurationsantwort
`document Vision is not configured` enden als Jobfehler
`DOCUMENT_REMOTE_REJECTED_<HTTP-Status>`, auch beim GET oder DELETE. Der Fehler
belegt keine bestätigte Ressourcenfreigabe; `remote_outcome=unconfirmed` bleibt
sichtbar. Transportfehler, 5xx ohne diesen Konfigurationsbefund und 429 werden
weiter abgeglichen, ohne neue Zeit-/Zählgrenze. Freie Antworttexte werden weder
in Jobfehler noch Logs übernommen.

Der erste neue Claim schreibt `call_id` atomar mit dem Wechsel nach running.
Heartbeat liefert den persistenten Fortschritt an den Besitzer zurück. Ein
Watchdog-Erstclaim (Fence 1, nie zuvor gestartet) sichert die Identität vor
dem ersten Remote-Aufruf; diese Erstaufnahme ist kein Protokoll-Upgrade. Ein
übernommener alter Job ohne passende call_id endet vor jedem Remote-Aufruf mit
`DOCUMENT_PROTOCOL_UPGRADE`. Das startet keine zweite Konvertierung, stoppt
aber auch keinen unbekannten alten synchronen Aufruf. Deshalb bei der
Erstumstellung Auftraggeber pausieren und alte Dokumentjobs/alte synchrone
Konvertierungen auslaufen lassen; dann Platform-API, Dokumentdienst mit
persistentem Volume, zuletzt Worker aktualisieren. Erst danach Annahme öffnen.
Kein GPU-/Prod-Deploy ist durch diese Beschreibung autorisiert.

**Entscheidung P2-2:** Eine bereits im Jobfortschritt bekannte attempt_id darf
bei späterem GET-404 nicht durch einen neuen PUT ersetzt werden. Stattdessen
endet der Job mit `DOCUMENT_REMOTE_STATE_LOST`, ohne bestätigten Stop und ohne
automatische neue Konvertierung. Das gilt auch bei Abbruchwunsch. Ein 404 vor
jeder bestätigten Remote-Annahme erlaubt den ersten idempotenten PUT bzw.
den Abbruch-Tombstone weiterhin. Die Lücke „PUT-Antwort verloren und zugleich
Volume verloren, bevor attempt_id gespeichert wurde“ ist damit nicht
aufklärbar; eine vollständige Exactly-once-Garantie wird nicht behauptet.
Ein großzügigeres Wiederanlaufmodell mit möglicher doppelter Vision-Berechnung
gehört zur folgenden L4-Bauarbeit, nicht in diese Mindestfixes.

P2-1: ACK und Aufbewahrung für bestätigte Enden sind inzwischen gebaut (unten).
Unbestätigte Prozessenden bleiben eine **Deploy-Sperre** für vollständig
begrenzte Aufbewahrung. Diese
Runde baut keine ungeprüfte Retention nebenbei. L4 bleibt ROT: fachliche
Lebendigkeit, automatische Fortsetzung und die DB-/Container-/GPU-Laufbelege
stehen weiterhin aus.


## ACK und Inhaltsaufbewahrung (P2-1, Folgeabschnitt)

Ein terminales document_state(done|error|cancelled) mit Stop-Receipt schreibt
remote_ack=pending in derselben DB-Transaktion wie Ergebnis/Fehler und Endstatus.
Der Worker-Watchdog startet höchstens einen separaten ACK-Durchlauf je Worker;
die normale Jobübernahme wartet nicht auf den Dokumentdienst. Der Durchlauf
liest bis zu 100 offene terminale ACKs, sendet authentifiziert
POST /document/calls/{call_id}/ack mit {attempt_id,digest}, prüft die bestätigte
Identität und content_state=removed und markiert den Store-Eintrag als done.
Mehrere Worker dürfen dasselbe ACK senden. Der Store-Abschluss verändert
weder Ergebnis noch finished_at. Verlorene Antworten werden über dasselbe
dauerhafte ACK wiederholt, ohne PUT/DELETE oder einen neuen Attempt.

Der Dienst prüft Identität, freien Execution-Lock und reaped/local_stopped.
Fehlt eines davon: 409, keine Löschung. Er schreibt zuerst released.json
atomar und fsync, dann löscht er source, result.json, request.json (einschließlich
Dateiname/Prompts) und alle übrigen Inhaltsdateien. accepted.json mit technischem
Digest/IDs, receipt.json, released.json und execution.lock bleiben. Nach einem
Crash während der Löschung vollenden ACK-Replay oder der Janitor dieselbe
Löschung. GET meldet released/expired und den Receipt, niemals ein erfundenes
Ergebnis. Späte PUTs können den Tombstone nicht ersetzen und starten nichts.

Der Janitor läuft beim Dienststart und danach etwa jede Minute. Inhaltsfrist:
7 Tage seit bestätigtem Ende; ohne dauerhaften Start seit Annahme bzw. bei
Teilannahmen seit ältester Datei. Das ist Datenschutzaufbewahrung, keine
Konvertierungsfrist. Ausführungen mit gehaltenem Execution-Lock bleiben
unangetastet. Nie gestartete Altannahmen werden unter beiden Locks mit
not_started-Receipt und expired-Tombstone abgeschlossen; verspätete Starts
bleiben ausgeschlossen. Bestätigt beendete unbestätigte Ergebnisse werden
nach der Frist entfernt, auch wenn der Auftraggeber verschwunden ist.

Offene Grenze: started ohne Receipt in derselben Prozessinstanz bleibt unknown,
wird NICHT als beendet ausgegeben und wird laut mit unconfirmed-Zähler gemeldet.
Bei belegtem Verlust der äußeren Instanz wird zunächst ein Stop-Receipt
geschrieben; erst ab diesem Nachweis läuft die Inhaltsfrist. Ein alter unbekannter
Prozess wird nicht durch sein Alter still beendet. Vollständige Aufbewahrung
für diese Fälle benötigt die noch offene L4-Prozessklärung.

Die bestehende öffentliche Jobretention bleibt unverändert (2h bzw. keyed
mindestens 24h). Bei langer Nichtverfügbarkeit kann der ACK-Auftrag mit dieser
Zeile verschwinden; der unabhängige 7-Tage-Janitor begrenzt trotzdem bestätigte
Remote-Enden. Keine unbegrenzte Ergebnisverfügbarkeit versprochen. ACK bestätigt
dauerhafte Speicherung im Jobstore, nicht schon die Abholung durch die App.

Rollout: neue Platform-API (ACK-Abfragen/Bestätigung) vor neuen Workern; neuer
Dokumentdienst für ACK/Janitor erforderlich. Kein neuer Env-Schalter. Der
Privacy-/GPU-Deploy bleibt bis Kapazitätsnachweis und L4-Freigabe gesperrt.


## Kapazitätsmessung (P2-6)

scripts/document_capacity_probe.py führt auf dem GPU-Host isolierte synthetische
PDF-Konvertierungen mit dem echten document_process/document_worker-Pfad aus.
Parameter: --image (lokal vorhandenes Image), --source-root (expliziter Checkout),
--source-sha, --pages, --parallel 1|2, --output (privater Berichtspfad).
Das Image wird per ID fixiert; ausschließlich der angegebene src-Baum wird
read-only darübergelegt. Das ist eine Quellstandmessung mit diesen Image-
Abhängigkeiten, kein Nachweis eines frisch gebauten Release-Images.

Kein Dienststart/Deploy, kein Netz, keine Token, keine Vision, keine Kundendaten.
Je eigener Container: 2 GiB RAM einschließlich Swap, zwei CPUs, 256 PIDs.
Das Werkzeug sammelt hostseitig RSS des eigenen Prozessbaums sowie
nvidia-smi-Compute-Speicher nur seiner eigenen PIDs. Gemeldet werden je Prozess
und Konvertierung die beobachteten Peaks, der gemeinsame gleichzeitige Peak,
Dauer, Exit/OOM, Reap-Receipt und tatsächliches docling_parsed. Abtastung etwa
250 ms plus Messkosten; RSS summiert gemeinsame Seiten mehrfach und VRAM ist
ein beobachteter Prozesswert, keine lückenlose Allokations-Höchstmarke.

Die Probe beginnt nur mit mindestens 8 GiB freiem VRAM und beendet ihre eigenen
Container bei weniger als 2 GiB Rest. Das sind Schutzbedingungen dieser
Messung, keine neue Laufzeitregel für Kundenkonvertierungen. Ein OOM, fehlendes
Ergebnis oder Docling-Fallback wird rot ausgegeben, nie als erfolgreicher
Kapazitätsnachweis. Fremde Prozesse werden weder beendet noch verändert.
Reale Messberichte, nicht dieser Werkzeugbau, entscheiden P2-6. Große Dateien,
Parallelität, Dienst-Grundlast und der endgültige Release-Build bleiben Teil
der Abnahme. L4 bleibt bis dahin ROT.
