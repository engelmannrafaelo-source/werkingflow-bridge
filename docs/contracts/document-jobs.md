# Dokumentkonvertierung als Job

Stand 23.10.2026, Folgeauftrag Dokumentdienst. Implementierter Feature-Zweig,
**noch nicht zweitgeprüft, integriert oder ausgeliefert**. Große Dokumente und
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
Ein fehlender Guardian ohne Quittung bleibt unknown und braucht Aufklärung;
keine automatische erneute bezahlte Ausführung. Nach Dienstverlust quittiert der
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
Rücknahme. Guardian-SIGKILL/Hostverlust ohne Quittung bleibt ebenfalls unbekannt.
Ein lebender, hängender Konverter ohne Nutzerabbruch oder Gesamtfrist darf weiter
laufen; Heartbeat ist keine Aussage über fachlichen Fortschritt.

## Grenzen und Aufbewahrung

- Managed Konverter: keine 300-s-LibreOffice-/Vision-Frist, kein 600-Seiten-Abbruch.
- Legacy `/convert-pdf` und `/document/convert-and-anonymize`: noch Threads und
  bisherige Fristen. `/document/convert` nutzt ebenfalls den Reaper, ist aber
  synchron; ein Client-/nginx-Abbruch fordert lokalen Stop.
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
  erneuert/gelöscht werden. Noch kein ACK-/Retention-Cleanup: Original und Ergebnis
  bleiben bestehen. Betriebsfähiger begrenzter Speicher ist vor Deploy nachzuweisen.

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
