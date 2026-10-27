# Vertrag: Test-KI-Deckel (gedeckelte echte KI für Testaufrufe, nur Haiku-Chat)

Stand: 27.10.2026, R4 nach Codex (`PRUEFUNG-ENTWURF-R4.md`) mit Umfangsentscheidung (a) des Vize: Test-KI heißt **nur Haiku-Chat**. Auftrag: KD 91624155, Grundlage Rafael 27.10. 16:36Z. Code: `src/budget/test_ki_deckel.py`, Migration `072_test_ki_deckel.sql`.

## Zweck

Tester von werking-works und werking-report prüfen mit echter KI, aber nur mit Haiku und nur gedeckelt.

Die Apps sperren Testaufrufe selbst (`KI_IM_TEST_GESPERRT`). Nur in der **Preview** und nur mit ihrem Schalter `KI_TEST_ECHT_GEDECKELT=1` lassen sie Haiku-Chat durch. Dabei setzen sie `X-Test-Lauf`, den sha256 einer geprüften Sitzungskennung. Diese Bridge-Seite ist der Deckel dahinter. Zugleich ist sie die Rückversicherung, falls ein Aufruf die App-Sperre umgeht.

## Vertrauensrichtung: Angaben können nur einschränken

Ein Aufruf ist ein **Testaufruf**, wenn mindestens eine der folgenden Bedingungen gilt:

- **(a)** Der vom Token aufgelöste Prinzipal steht in `BRIDGE_TEST_KI_PRINZIPALE`.
- **(b)** Es ist ein Job-Selbstaufruf, und der Job wurde von einem solchen Prinzipal angelegt. Das gilt auch, wenn der Job einen Marker trägt. Die Herkunft wird aus dem gespeicherten Job gelesen, nie aus einem Kopf.
- **(c)** Der Aufruf trägt `X-Test-Lauf`, egal mit welchem Prinzipal und welchem Mandanten.

Alle anderen Aufrufe bleiben unberührt: Legacy, Works-Prod (`engelmann-vercel-dev`), Workflow-Engine und Prod. Weder Kopf noch Mandant noch `account_type` befreien von etwas. `account_type` wird nicht gelesen.

**Der Umfang ist bewusst eng gefasst:**
- Report-Testläufe sind nur gedeckelt, wenn die Report-Sperre den Marker setzt. `report-vercel-dev` steht nicht in der Liste, weil über ihn auch Nicht-Testkonten der Preview mit Sonnet laufen.
- Der Marker wirkt **nur auf einer Bridge mit diesem Code** (R4). Ein älterer Dienst ignoriert ihn, ebenso ein fremder OpenAI-kompatibler Dienst (Local-Fallback, `baseUrl`-Override).
- Eine `fetch`-Referenz, die vor der App-Sperre gespeichert wurde und zugleich den alten geteilten Schlüssel nutzt, ist eine bestehende Grenze der Apps. R4 schließt sie nicht.

## Konfiguration (nur Worker, `docker/docker-compose.yml` = Dev-Bridge)

| Variable | Wert |
|---|---|
| `BRIDGE_TEST_KI_DECKEL` | `1` = Test-KI aktiv. Jeder andere Wert bedeutet: **jeder Testaufruf 403 `KI_TEST_DECKEL_INAKTIV`**. Ein Kopf allein öffnet also keinen ungedeckelten Weg. Prod setzt ihn nie. |
| `BRIDGE_TEST_KI_PRINZIPALE` | Kommagetrennte `service_principals.name`, deren Aufrufe alle Testaufrufe sind. Dev: **leer** (K67, 27.10.2026: Works und Report nur über (c); ein Eintrag sperrte jeden Nicht-Chat-Weg aller Nutzer dieses Prinzipals). Leer bedeutet: nur (c). |

Kein Secret. Die Deckel (0,50 € je Lauf, 10 € je App und Tag, 20 € gesamt je Tag) sind Konstanten im Code. Für (a) und (b) muss `BRIDGE_PRINCIPALS_ENABLED=true` gesetzt sein.

## Drei Prüfstellen, jede für alle Wege ihrer Art

1. **Wege-Schicht `TestKiWegeMiddleware`.** Reines ASGI vor jedem Worker-Endpunkt, vor jeder Kostenwirkung.
   - Ist ein `X-Test-Lauf` vorhanden, aber leer, ungültig (nicht 64 Hex) oder mehrfach gesetzt: 403 `lauf_ungueltig`, auf jeder Route.
   - Testaufruf bei Schalter aus: 403 `KI_TEST_DECKEL_INAKTIV`, auf jeder Route außer den kostenfreien.
   - Testaufruf auf einer Route außerhalb der **Erlaubnisliste**: 403 `KI_TEST_NUR_CHAT`.
   - Die Liste erlaubt `POST /v1/chat/completions` (Tor), `POST /v1/jobs` (Job-Prüfung) und kostenfreie Verwaltung: `GET`/`DELETE /v1/jobs/{id}`, `GET /v1/jobs`, `POST /v1/jobs/lookup`, `GET /v1/models`, `/v1/version`, `/health`, `/ready` und `OPTIONS`.
   - **Eine neue Route ist für Testaufrufe gesperrt, bis sie dort eingetragen wird.**
2. **Jobs.**
   - `POST /v1/jobs`: Ein Testaufruf legt nur `chat` oder `ping` an, `chat` nur mit gültigem `X-Test-Lauf`. Der geprüfte Marker und der Prinzipal werden serverseitig am Job gespeichert.
   - Der Job-Runner prüft vor **jedem** Executor dieselbe Regel anhand des gespeicherten Jobs. Das schließt Erkunder mit eigenem Transport ein, ebenso Jobs, die vor einer Schalter- oder Listenänderung angelegt wurden.
3. **Chat-Tor `tor`** (siehe unten).

## Was im Chat-Tor gilt

| Fall | HTTP | `error.code` | Zusatz |
|---|---|---|---|
| Angefragtes Modell (nach Alias) ≠ `claude-haiku-5-5` | 403 | `KI_IM_TEST_GESPERRT` | `grund: modell_nicht_haiku` |
| `enable_tools=true` | 403 | `KI_IM_TEST_GESPERRT` | `grund: werkzeuge` |
| Direktweg mit Haiku nicht herstellbar | 403 | `KI_IM_TEST_GESPERRT` | `grund: weg` |
| `X-Test-Lauf` fehlt (bei (a) oder (b)) | 403 | `KI_IM_TEST_GESPERRT` | `grund: ohne_lauf` (keine Buchung) |
| `X-Test-Lauf` leer, ungültig oder mehrfach | 403 | `KI_IM_TEST_GESPERRT` | `grund: lauf_ungueltig` |
| App ohne Test-Deckel | 403 | `KI_IM_TEST_GESPERRT` | `grund: app_unbekannt` |
| ohne `X-User-ID` bzw. Nutzer unbekannt | 403 | `KI_IM_TEST_GESPERRT` | `grund: ohne_nutzer` bzw. `nutzer_unbekannt` |
| Deckel überschritten oder Lauf/Tag geschlossen | 402 | `KI_TEST_DECKEL_ERREICHT` | `ebene`, `grund`, `summe_eur`, `deckel_eur`, `reservierung_eur` |
| platform-api, Job-Store oder Prinzipal nicht lesbar | 503 | `KI_TEST_DECKEL_NICHT_LESBAR` | `X-Bridge-Dependency-Unavailable: test-ki-deckel` (ein Job parkt) |
| Nicht-Chat-Weg | 403 | `KI_TEST_NUR_CHAT` | `testgrund`, `weg` |
| Schalter aus | 403 | `KI_TEST_DECKEL_INAKTIV` | `testgrund` |

Ab einem positiven Testgrund endet jeder Zweig mit einer Reservierung oder einer Abweisung, nie mit „kein Test“.

**Weg.**
- Jeder gedeckelte Aufruf läuft über `claude-direct-notools` mit `claude-haiku-5-5`: ein Messages-API-Aufruf ohne Werkzeuge, `max_tokens` wirksam, Streaming als Minimal-SSE.
- Das kostet echtes Geld auf dem Prepaid-Schlüssel (KD D-1). Deshalb greift zusätzlich die bestehende 90-€-Rolling-Grenze.
- Fallback-Ketten sind gesperrt.

**Reservierung (Obergrenze).**
- Eingabe ≤ UTF-8-Bytes aller Texte + 64 je Nachricht + 2.000 fest + 6.000 je Bild.
- Die Eingabe wird zum **Cache-Anlage-Satz 2×** bewertet, die Ausgabe mit `max_tokens`. Fehlt `max_tokens`, gilt 4.096, und dieser Wert wird auch dem Aufruf mitgegeben.
- Alle Sätze kommen aus der **höchsten** Preiszeile des Modells.

**Abrechnung (`UsageMessung`).**
- `vision_provider._vision_response_from` leitet die Messung aus der **unveränderten** Rohantwort ab.
- „Vollständig“ heißt: `usage` ist ein dict, und alle vier Felder (`input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`) sind `type(x) is int` und ≥ 0.
- Nur der Torzweig übergibt `call_anthropic_direct` ein **frisches**, nur diesem Aufruf gehörendes `MessungZiel`. Die Messung erscheint deshalb nie in JSON, SSE, Job-Ergebnis oder Fallback-Antworten.
- Erstattet wird nur bei `quelle=anthropic_messages_api` und `vollstaendig`. In jedem anderen Fall, auch bei Fehler oder Abbruch, bleibt die volle Reservierung stehen.
- Liegt der Ist-Wert über der Reservierung, wird er gebucht, und **Lauf und App-Tag werden geschlossen**.
- Die Ledger-`usage` des Direktwegs bleibt wie bisher ohne Cache-Kategorien. Repariert wird die Deckelabrechnung, nicht das Ledger.

**Lauf.** Der Schlüssel besteht aus UTC-Tag, Bridge-Nutzer-UUID und `X-Test-Lauf`. `X-Workflow-ID` bleibt unberührt.

**Idempotenz.**
- `reservierung_id` (uuid4 je Aufruf) ist Primärschlüssel in `test_ki_buchung`.
- Wiederholtes Reservieren gibt das gespeicherte Urteil zurück und bucht nichts.
- Abrechnen geht genau einmal (`offen` → `abgerechnet`).
- **Einziger Schreibweg ist platform-api.**

**Protokoll.**
- `test-ki echt, gedeckelt testgrund=… prinzipal=<Name> app=… lauf=… reserviert|ist=… lauf_summe=x/0.50 tag_summe=y/10.00 gesamt=z/20.00 EUR`
- `test-ki deckel erreicht …`, `test-ki gesperrt …`, `test-ki nur chat …`, `test-ki inaktiv …`
- Bei verlorener Antwort: `Abrechnung Ausgang unklar …`

## Innen-API (platform-api, Service-Token)

`POST /v1/internal/test-ki-deckel/reservieren` und `…/abrechnen`. Die Testkonto-Abfrage aus R2 ist entfallen.

## Rückweg

- Zuerst den **App-Schalter** löschen, danach den Bridge-Schalter.
- Wird nur der Bridge-Schalter gelöscht, sperrt die Bridge jeden Testaufruf selbst mit 403 `KI_TEST_DECKEL_INAKTIV`.
- Ein **Image-Rückbau** hinter diesen Code hebt die Kopplung auf, denn ein alter Code kennt den Marker nicht. Gespeicherte Jobs mit Marker liefen dann ungedeckelt. Deshalb gilt vor jedem Rückbau: App-Schalter löschen, Leerlauf der Testjobs abwarten (keine `pending`/`running`-Jobs mit `test_lauf` oder Test-Prinzipal) oder diese Jobs definiert abbrechen (`DELETE /v1/jobs/{id}`). Erst dann das Image tauschen.
