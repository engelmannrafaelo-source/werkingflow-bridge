# Pipeline-Aufrufe: Submit-Identität und Wiederaufnahme (v1)

Allgemeine Bridge-Schnittstelle für A1 und andere Aufrufer. Sie erweitert den
bestehenden atomaren Idempotency-Key-Vertrag; keine fachlichen Pipeline-Regeln
in der Bridge. Ohne die neuen Felder bleibt der bestehende Submit unverändert.

## Serverseitige Freischaltung (Default AUS)

`BRIDGE_JOB_ATTEMPT_IDENTITY_ENABLED=false` ist der Default in `src/config.py`
und den Env-Vorlagen. Der Worker liest den Schalter beim Prozessstart aus seinem
bestehenden Compose-`env_file`; eine Änderung benötigt einen freigegebenen
Worker-Rollout. Kein Live-Schalter und keine automatische Aktivierung beim Deploy.

Bei AUS ignoriert Submit `call_id`/`attempt_id`: keine Paarprüfung, keine Ableitung
oder Prüfung eines Attempt-Schlüssels, kein neuer Attempt-Identitätspfad.
Es gilt das Verhalten vor dieser Erweiterung. Der schon vorher vorhandene
`Idempotency-Key`-Header bleibt unverändert wirksam (einschließlich Validierung,
Replay und Konflikten); ohne Header ist Submit unkeyed. Die normalen
Request-Schematypen gelten weiterhin. AUS schaltet **nicht** die ältere allgemeine
Header-Idempotenz ab. Clients dürfen daraus keine verfügbare Attempt-Schnittstelle
ableiten und dürfen bei deaktiviertem Feature nicht auf unkeyed Submit ausweichen.

Ein authentifizierter, schemagültiger öffentlicher Lookup liefert bei AUS
`404 {reason:"feature_disabled",outcome:"unknown",job_id:null}` mit
`Cache-Control: no-store`, ohne Store-Zugriff. Das ist kein Beleg für Nichtannahme.
Authentifizierung und Request-Schema bleiben vorgeschaltet; nginx verlangt für
das Home-Routing weiterhin den passenden Header. Der interne, Service-Token-
geschützte Store-Leseweg bleibt Infrastruktur; die Freischaltung erfolgt am Worker.
Die folgenden Submit-/Lookup-Regeln gelten bei EIN.

## Identität vor dem ersten Senden speichern

Der Aufrufer persistiert `call_id`, `attempt_id`, Bridge-Basis-URL,
Owner-Kontext und unveränderliches `{kind,payload}` VOR dem ersten POST.
IDs sind opake technische Kennungen, jeweils 1–128 Zeichen aus
`A-Z a-z 0-9 . _ : -`, keine Kundennamen oder Inhalte. `call_id` bezeichnet
seinen fachlichen Aufruf; `attempt_id` dessen bewusst gestartete Ausführung.
Transport-Wiederholung, Workerwechsel und Resume ändern keine der beiden IDs.
Diese IDs sind nicht die internen Claim-Zähler des Jobrunners und nicht die
Remote-IDs des Dokumentdienstes in `document-attempts-design.md`.

Der Header MUSS schon am Lastverteiler vorhanden sein. Er lautet:

```python
import hashlib, json
key = "bridge-attempt-v1:" + hashlib.sha256(
    json.dumps([call_id, attempt_id], separators=(",", ":")).encode("ascii")
).hexdigest()
```

JavaScript: SHA-256 über UTF-8 von `JSON.stringify([call_id, attempt_id])`,
Hex klein geschrieben, gleicher Präfix. Das ASCII-Alphabet macht beide
Kodierungen identisch. Der Schlüssel allein verleiht keine Berechtigung.
Owner umfasst authentifizierten Principal bzw. Shared-Key-Modus, X-App-ID
und X-User-ID aus den kanonischen Headern. Auf Resume dieselben Werte senden;
`attribution` im Body ersetzt diese Identität nicht.

## Submit und Lookup

`POST /v1/jobs`, bestehende Auth-/Attributionsheader plus `Idempotency-Key`:

```json
{"call_id":"fiction-call-1","attempt_id":"attempt-1","kind":"chat","payload":{"model":"claude-sonnet-5-5","messages":[{"role":"user","content":"Sage hallo."}]}}
```

201 = neu gespeichert; 200 = bestehende Job-ID, kein neuer Submit/Dispatch.
Beide liefern `{job_id,status,kind}` und `Idempotent-Replayed: false|true`.
Gleiches Owner/Key-Paar mit anderem kind/payload: 422
`idempotency_key_reused`. Fehlendes ID-Paar oder unpassender Header: 400.
Die bestehende UNIQUE-Schranke in Postgres entscheidet konkurrierende Submits.
Das Vorab-Lesen allein ist keine Sperre. Auch terminale Jobs werden wiedergegeben.

`POST /v1/jobs/lookup`, dieselben Header, nur diese beiden Felder im Body:

```json
{"call_id":"fiction-call-1","attempt_id":"attempt-1"}
```

- 200: `{job_id,status,kind}`; dann `GET /v1/jobs/{job_id}` weiterpollen.
- 404: `{reason:"job_identity_unknown",outcome:"unknown",job_id:null}`.
  Das bedeutet NICHT „nicht angenommen“. Keine automatische Neueinreichung.
- 503/Netzfehler: Lookup nicht möglich; dieselbe Identität später nachschlagen.
- 400/422: Identitäts-/Schemafehler; korrigieren, keine Ersatz-ID erfinden.

Lookup liest ausschließlich; kein Capacity-Veto, kein Executor, kein Claim und
keine Anlage. Der Client braucht keinen Prompt/Dateilink mehr zur Abfrage.
Nginx nutzt für Lookup und keyed Submit denselben festen Home-Store und weicht
bei dessen Ausfall nicht auf eine andere Datenbank aus. Die erhaltene Job-ID
enthält die Home-Bridge; nach Erhalt ausschließlich diese ID pollen.

## Verlorene Antwort, Retention und unbekannter Ausgang

Nach unbekanntem Submit-Ausgang zunächst ausschließlich Lookup. Ein 404 kann
mit einem noch laufenden ursprünglichen Submit zusammenfallen oder nach
Retention auftreten. Es erlaubt weder einen neuen Schlüssel noch einen neuen
POST. Auch ein Poll-Budget-Ende oder Neustart erlaubt keine Neueinreichung.
Die Pipeline speichert den offenen Aufruf und übernimmt ihn auf Resume.
Ein neuer fachlicher Attempt benötigt eine bewusste Entscheidung mit belegtem
Ende/Nichtannahme; der Lookup liefert diesen Beleg bei 404 gerade nicht.

Bestehende Store-Grenze: aktive Jobs werden nicht bereinigt; terminale keyed
Jobs bleiben mindestens 24 Stunden nach Terminalzustand. Danach kann die Zeile
verschwinden. Es gibt noch kein unbegrenzt persistentes Identity-Tombstone.
Deshalb keine unbegrenzte Exactly-once-Garantie und kein blindes POST-Replay
nach längerer Unterbrechung. Ergebnis und Job-ID rechtzeitig im Aufrufer sichern;
404 nach Verlust bleibt laut ungeklärt. Diese Änderung startet keine neue Arbeit
zur vermeintlichen Reparatur eines unbekannten Ausgangs.

## Auslieferung und Abnahme

1. Client-Nutzung ausgeschaltet lassen. Auf beiden beteiligten Bridges zuerst
   Platform-API ausrollen (neuer interner `POST /v1/internal/jobs/identity-lookup`,
   Service-Token geschützt). Migration 065 muss vorhanden sein; keine neue Migration.
2. Alle Worker beider Bridges mit neuem Code und Schalter AUS ausrollen, danach
   nginx beider Bridges mit dem keyed Home-Weg für `/v1/jobs/lookup`.
   Auch Peer-/Backup-Worker müssen diesen Stand haben. Neuer Worker hinter altem
   LB genügt nicht; alte Platform-API führt zu 503, niemals zu Submit-Fallback.
3. Vollständigen Stand je Dienst prüfen; bei AUS die `feature_disabled`-Antwort
   sowie unveränderte Legacy-Submits abnehmen. Erst danach den Schalter über die
   verwaltete Worker-Umgebung auf EIN setzen und alle beteiligten Worker ausrollen.
   Keine Client-Aktivierung während eines gemischten Worker-Rollouts.
4. Lookup und Submit mit synthetischer Identität durch beide nginx/Home-Wege
   abnehmen. Erst dann die neue A1-Nutzung einschalten.

Rücknahme: zuerst neue Client-Submits pausieren, vorhandene Job-IDs weiterpollen;
ungeklärte Aufrufe behalten ihre Identität. Dann Worker-Schalter AUS ausrollen.
`feature_disabled` erlaubt weder eine Ersatz-ID noch automatische Neueinreichung.
Push/Deploy separat durch den Berater; dieser Vertrag ist keine Deploy-Freigabe.

Pyramiden-Szenarien: konkurrierender Submit; verlorene Submit-Antwort;
Resume ohne Job-ID; Ownerwechsel; abweichender Payload; voller Pool bei Lookup;
Home-Ausfall; alte Platform-API; terminaler Replay; nach Retention unbekannter
Ausgang ohne neue Modellarbeit. Keine neuen Unit-/Regressionstestdateien.

Zusätzlicher Pyramiden-Prüfkontext: Default AUS, Lookup ohne Store-Zugriff,
ignorierte Body-Identitäten bei AUS, unveränderte Legacy-Header-Idempotenz,
Freischaltung erst nach vollständigem Rollout und Rücknahme mit offenen Aufrufen.
