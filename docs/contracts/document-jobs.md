# Dokumentkonvertierung als Job

Stand 23.10.2026. Jobart `document-convert`; dieselbe Konvertierung wie
`POST /v1/document/convert`, einschließlich Multipart-Feldern und Attribution.
Der bestehende Multipart-Endpunkt bleibt verfügbar. Kein JSON-Proxy auf Multipart.

## Einreichen und Abholen

Authentifiziert `POST /v1/jobs`, mit denselben Owner-/App-/User-/Umgebungsheadern
wie andere Jobs. Voraussetzung: generische Jobs und ihr persistenter Store aktiv.

```json
{
  "kind": "document-convert",
  "payload": {
    "filename": "example.txt",
    "content_type": "text/plain",
    "content_base64": "SGFsbG8=",
    "fields": {"describe_images": "false"}
  }
}
```

`content_base64` enthält Standard-Base64 der Originaldatei, keinen Data-URI.
`fields` enthält ausschließlich Stringwerte; `file` ist reserviert.
Der Store enthält damit die Originalbytes, nicht eine öffentliche Downloadadresse.
Die Authentifizierung und Formatprüfung des Dokumentdienstes bleiben wirksam.
Base64 vergrößert den HTTP-/DB-Payload um ungefähr ein Drittel. Das bestehende
nginx-Limit von 75M gilt für das gesamte JSON: weniger als ca. 56 MiB Rohdatei
passen hinein (zusätzlich JSON-/Feld-Overhead), nicht 75 MiB wie bei Multipart.
Dies ist nur eine obere Transportgrenze, keine bestätigte End-to-end-Kapazität.
Der Platform-API-/DB-Pfad und sein vorhandener Schreibtimeout sind für große
Dokumente noch nicht vermessen; große Uploads sind vor Auslieferung offen.
Keine neue Umgebungsvariable. Ergebnis und Quelldaten nicht in Logs oder öffentliche Pfade legen.

Job-ID vor dem ersten Poll in der aufrufenden Arbeitsakte speichern.
`GET /v1/jobs/{id}` liefert pending/running/done/error/cancelled und Fortschritt;
bei done enthält dieselbe Antwort das Ergebnis.
Darin ist `result` die Dokumentantwort mit `success`, `format`, `markdown`,
`metadata` und gegebenenfalls Bildern. HTTP- und fehlerhafte JSON-Antworten werden
nicht als erfolgreiche Konvertierung gespeichert. Nicht-JSON und unvollständiges
Ergebnisschema sind terminale Fehler. Erneutes GET startet keine Konvertierung.

## Lebensdauer und Wiederaufnahme

Keine Gesamt-Wanduhrfrist im neuen Executor oder im Bridge-Konvertierungsproxy.
Der Runner hält den bestehenden Heartbeat; nach Worker-Verlust übernimmt der
bestehende Watchdog gemäß dessen Claim-/Versuchsregeln. Dies ist at-least-once:
ein Worker-Verlust kann eine zweite Remote-Konvertierung verursachen. Ein
Transportabbruch wird deshalb mit unbekanntem Remote-Ausgang als nicht automatisch
wiederholbarer Fehler gemeldet. Der Watchdog ist keine Remote-Ausführungsquittung.

Am gelesenen Basisstand 9be0986 beträgt die terminale Aufbewahrung **2 Stunden**
(`GENERIC_JOB_TTL_SECONDS`), nicht mehr 15 Minuten. Mit `Idempotency-Key` bleibt
der terminale Datensatz mindestens **24 Stunden** (`IDEMPOTENCY_RETENTION_SECONDS`).
Running-Jobs werden nicht wegen ihres Alters beendet oder gelöscht. Pending-Jobs
unterliegen bislang weiterhin dem generischen Alters-Backstop (weitere offene
Wanduhrgrenze für die vollständige Abnahme). Nach Ablauf liefert GET 404;
der Aufrufer muss fertige Ergebnisse rechtzeitig in seiner eigenen Ablage sichern.

Der bestehende `Idempotency-Key`-Vertrag gilt bereits: gleicher Owner, Schlüssel,
Jobart und Payload ergeben dieselbe Job-ID, ein abweichender Payload wird abgelehnt.
L6 `call_id`/`attempt_id` ist damit nicht implementiert: diese spätere Erweiterung
gehört in den gemeinsamen Jobvertrag samt Remote-Abgleich, nicht in freie
Multipart-Felder. Noch keine neue Semantik oder Exactly-once-Zusage für diese IDs.

## Abbruch: ausdrücklich begrenzter Vertrag

`DELETE /v1/jobs/{id}` nutzt die bestehende Ownerprüfung. Pending kann sofort
cancelled werden. Running vermerkt den Wunsch und antwortet zunächst 202;
der Statusfortschritt wird `cancel-requested-draining-conversion`.
Der entfernte Dienst führt synchrone Konverter in Threads aus und bietet noch
keine Stop-Quittung. Daher bleibt der Heartbeat aktiv, bis die Konvertierung eine
erfolgreiche vollständige Antwort geliefert hat. Erst dann wird cancelled mit
`mode=drained`, `remote_finished=true`, `ledger=vom_dokumentlauf` quittiert.
Dieser ausdrückliche Nutzerabbruch verwirft das später eingetroffene Markdown;
bereits entstandene Kosten werden dadurch nicht rückgängig gemacht.
`vom_dokumentlauf` bedeutet abgeschlossener Konvertierungslauf, keinen Parkvorgang
und keine zweite Buchung durch den Jobexecutor. Eine Antwort, die vor Beobachtung
des Abbruchwunschs fertig war, bleibt done. Fehler bleiben sichtbar als error,
ein Verbindungsverlust ist kein bestätigter Abbruch.

**Dies ist noch kein sofortiger Ressourcenabbruch beim Dokumentdienst.** Auch
eine Nutzer-/Gesamtfrist kann zunächst nur diesen Abbruchwunsch stellen. Für die
vollständige L4-Abnahme ist eine Remote-Stop-/Attempt-Schnittstelle erforderlich.
Ein hängender Remote-Dienst kann deshalb dauerhaft running/202 bleiben: Der
Heartbeat beweist den lebenden Runner, nicht den Fortschritt des Konverters.
Die Connection-Pool-Wartezeit ist ebenfalls nicht begrenzt. Ohne gebundenen
Owner (z.B. fehlender Attribution-Extractor) gilt der bestehende Legacy-Vertrag:
Running-DELETE antwortet 409; kein Drain-Abbruch wird versprochen.
Die vorhandenen internen Konverter-/Vision-Fristen (z.B. LibreOffice und
VisionProvider) sind hier ebenfalls noch nicht entfernt. Dieser Bauabschnitt
beseitigt die Bridge-Transport-Wanduhr, nicht alle internen KI-Fristen.

## Pyramiden-Prüfkontext (vor Auslieferung)

Synthetischer kalter Eingang; gleiche ID über Poll-/Workerwechsel; Lauf länger als
bisherige 600/1200 Sekunden; erfolgreiche Abholung; 415/413 und fehlerhaftes JSON;
Pending-/Running-Abbruch einschließlich Ergebnisrennen; Remote-Verlust ohne falsche
Stop-Quittung; Besitzerwechsel; Idempotency-Replay und abweichende Bytes; Aufbewahrung
2h/24h; große Base64-Payloads durch nginx, Platform-API, DB und Watchdog-Claim
bei Parallelität einschließlich Prozessspeicher (2-GB-Betriebsgrenze). Kein Live-Lauf und keine vollständige L4-Abnahme mit diesem Entwurf behauptet.
