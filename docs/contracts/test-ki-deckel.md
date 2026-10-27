# Vertrag: Test-KI-Deckel (gedeckelte echte KI für Testkonten)

Stand: 27.10.2026, R2 nach Codex R1. Auftrag: KD 91624155, Grundlage Rafael 27.10. 16:36Z. Code: `src/budget/test_ki_deckel.py`, Migration `072_test_ki_deckel.sql`.

## Zweck

Tester von werking-works und werking-report prüfen mit echter KI: nur Haiku, nur gedeckelt.

Die Apps sperren Testaufrufe selbst (`KI_IM_TEST_GESPERRT`). Sie lassen nur Haiku-Chat in der **Preview** durch, und nur mit ihrem Schalter `KI_TEST_ECHT_GEDECKELT=1`. Dabei setzen sie `X-Test-Lauf`. Diese Bridge-Seite ist der Deckel dahinter.

## Konfiguration (nur Worker, `docker/docker-compose.yml` = Dev-Bridge)

| Variable | Wert |
|---|---|
| `BRIDGE_TEST_KI_DECKEL` | Bei `1` ist das Tor aktiv. Ist er nicht gesetzt oder ≠ `1`, ist der Code inert. Prod setzt ihn nie. |
| `BRIDGE_TEST_KI_PROD_PRINZIPALE` | Kommagetrennte `service_principals.name`. Aufrufe dieser Prinzipale sind Produktion und werden nie gedeckelt. Dev: `engelmann-vercel-dev`, weil Works-Prod über diese Bridge läuft. |

Kein Secret. Die Deckel (0,50 € je Lauf, 10 € je App und Tag, 20 € gesamt je Tag) sind Konstanten im Code. Voraussetzung ist `BRIDGE_PRINCIPALS_ENABLED=true`. Ohne aufgelösten Prinzipal greift keine Ausnahme, das Tor sperrt also.

## Wann das Tor greift

Alle Bedingungen müssen zutreffen, sonst bleibt der Aufruf unberührt:

1. Der Schalter steht auf `1`.
2. Die App (`X-App-ID` bzw. `X-Client-ID`) ist `werking-report`, `werking-works` oder `engelmann`.
3. Der Prinzipal des Aufrufs steht **nicht** in `BRIDGE_TEST_KI_PROD_PRINZIPALE`.
   - Maßgeblich ist nur der aus dem Token aufgelöste Prinzipal, nie ein Kopf.
   - **`X-App-Env` spielt keine Rolle.**
   - Sonderfall Job-Selbstaufruf (Worker-Schlüssel): Er übernimmt den Prinzipal, den `POST /v1/jobs` serverseitig mit dem Job gespeichert hat, aber nur für einen laufenden Job desselben Nutzers.
4. `tenants.account_type = 'test'` für den Nutzer (`X-User-ID`: UUID oder E-Mail). Geprüft wird über platform-api.

## Was dann gilt

| Fall | HTTP | `error.code` | Zusatz |
|---|---|---|---|
| Angefragtes Modell (nach Alias) ≠ `claude-haiku-5-5` | 403 | `KI_IM_TEST_GESPERRT` | `grund: modell_nicht_haiku` |
| `enable_tools=true` | 403 | `KI_IM_TEST_GESPERRT` | `grund: werkzeuge` |
| Direktweg mit Haiku nicht herstellbar | 403 | `KI_IM_TEST_GESPERRT` | `grund: weg` |
| `X-Test-Lauf` fehlt oder ist nicht 64 Hex | 403 | `KI_IM_TEST_GESPERRT` | `grund: ohne_lauf` (keine Buchung) |
| Deckel überschritten oder Lauf/Tag geschlossen | 402 | `KI_TEST_DECKEL_ERREICHT` | `ebene`, `grund`, `summe_eur`, `deckel_eur`, `reservierung_eur` |
| platform-api nicht erreichbar oder Antwort unklar | 503 | `KI_TEST_DECKEL_NICHT_LESBAR` | fail-closed |

**Weg.** Jeder gedeckelte Aufruf läuft über `claude-direct-notools` mit `claude-haiku-5-5`: ein Messages-API-Aufruf ohne Werkzeuge, `max_tokens` wirksam, Usage gemessen, Streaming als Minimal-SSE. Das kostet echtes Geld auf dem Prepaid-Schlüssel (KD D-1), daher greift zusätzlich die bestehende 90-€-Rolling-Grenze. Fallback-Ketten sind gesperrt.

**Reservierung (Obergrenze).**
- Eingabe ≤ UTF-8-Bytes aller Texte + 64 je Nachricht + 2.000 fest + 6.000 je Bild.
- Die Eingabe wird zum **Cache-Anlage-Satz 2×** bewertet, Ausgabe = `max_tokens`. Fehlt `max_tokens`, gilt 4.096, und dieser Wert wird auch dem Aufruf mitgegeben.
- Alle Sätze stammen aus der **höchsten** Preiszeile des Modells.

**Abrechnung.**
- Nur aus der **gemessenen** API-Usage, zu denselben pessimistischen Sätzen.
- Ohne Messung (Fehler, Abbruch) bleibt die volle Reservierung stehen.
- Liegt der Ist-Wert über der Reservierung, wird er gebucht, und **Lauf und App-Tag werden geschlossen**.

**Lauf.** Schlüssel aus UTC-Tag, Bridge-Nutzer-UUID und `X-Test-Lauf`. `X-Workflow-ID` bleibt unberührt und dient weiter den Projektbudgets.

**Idempotenz.**
- `reservierung_id` (uuid4 je Aufruf) ist Primärschlüssel in `test_ki_buchung`.
- Wiederholtes Reservieren gibt das gespeicherte Urteil zurück und bucht nichts.
- Abrechnen geht genau einmal (`offen` → `abgerechnet`).
- **Einziger Schreibweg ist platform-api; ein DB-Rückfall im Worker existiert nicht.**

**Protokoll.** `test-ki echt, gedeckelt app=… lauf=… reserviert|ist=… lauf_summe=x/0.50 tag_summe=y/10.00 gesamt=z/20.00 EUR` sowie `test-ki deckel erreicht …` und `test-ki gesperrt …`.

## Innen-API (platform-api, Service-Token)

`POST /v1/internal/test-ki-deckel/testkonto`, `…/reservieren` und `…/abrechnen`.

## Rollout-Reihenfolge

1. **Gegenprobe der Herkunft.** Aus den Worker-Logs bzw. dem Ledger feststellen, welche Prinzipale heute als engelmann/report mit einem Testmandanten KI aufrufen. Jeder produktive davon gehört in `BRIDGE_TEST_KI_PROD_PRINZIPALE`; sonst bekommt er laut 403.
2. Migration 072 per `bin/bridge-migrate.sh` auf die Dev-Bridge-DB.
3. Bridge-Deploy (Dev) mit beiden Variablen.
4. **Works:** Der Prinzipal muss geteilt sein. Works-Preview braucht einen **eigenen** Prinzipal, der nicht in der Liste steht (KD D-2). Erst danach `KI_TEST_ECHT_GEDECKELT=1` setzen, nur im Vercel-Scope `preview`.
5. **Report:** `KI_TEST_ECHT_GEDECKELT=1` nur `preview`. `report-vercel-dev` steht nicht in der Liste.
