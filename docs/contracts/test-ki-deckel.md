# Vertrag: Test-KI-Deckel (gedeckelte echte KI für Testkonten)

Stand: 27.10.2026. Auftrag: KD 91624155, Grundlage Rafael 27.10. 16:36Z. Code: `src/budget/test_ki_deckel.py`, Migration `072_test_ki_deckel.sql`.

## Zweck

Tester von werking-works und werking-report prüfen mit echter KI, aber nur mit Haiku und gedeckelt. Die Apps sperren Testaufrufe selbst (`KI_IM_TEST_GESPERRT`). Sie lassen nur `claude-haiku-5-5` durch, und das nur, wenn ihr eigener Schalter `KI_TEST_ECHT_GEDECKELT=1` gesetzt ist. Diese Bridge-Seite ist der Deckel dahinter. Den App-Schalter erst setzen, wenn Bridge-Rollout und Migration 072 durch sind.

## Konfiguration

| Variable | Wo | Wert |
|---|---|---|
| `BRIDGE_TEST_KI_DECKEL` | Worker, `docker/docker-compose.yml` (Dev-Bridge) | `1` = Tor aktiv. Fehlt oder ≠ `1`: Code inert, Verhalten unverändert. `docker-compose-prod.yml` trägt ihn nie. |

Kein Secret. Lauf- und Tagesdeckel (0,50 € / 10 €) stehen als Konstanten im Code. Eine Änderung braucht einen Commit, keinen Schalter.

## Wann das Tor greift

Alle vier Bedingungen müssen zutreffen, sonst bleibt der Aufruf unberührt:

1. `BRIDGE_TEST_KI_DECKEL=1`.
2. Die App (`X-App-ID` bzw. `X-Client-ID`) ist `werking-report`, `werking-works` oder `engelmann`. `engelmann` ist die Bridge-App-ID von Works; beide zählen als ein Tagesdeckel.
3. `X-App-Env` ist nicht `production`. Grund: Works-Prod ruft dieselbe Dev-Bridge auf, und der Works-Mandant `engelmann` hat `account_type='test'`. Ein fehlender Header zählt als Nicht-Prod, das Tor greift also.
4. In der Bridge-DB gilt `tenants.account_type = 'test'` für den Mandanten des Nutzers (`X-User-ID`: UUID oder E-Mail). Das Merkmal kommt nie aus einem Feld der Anfrage.

## Antworten

| Fall | HTTP | `error.code` | Zusatz |
|---|---|---|---|
| Geroutetes Modell ≠ `claude-haiku-5-5` oder Backend kein Claude-Weg (Alias, Tier, App-Regel, Pin) | 403 | `KI_IM_TEST_GESPERRT` | `grund: modell_nicht_haiku` |
| Kein `X-Workflow-ID` | 403 | `KI_IM_TEST_GESPERRT` | `grund: ohne_lauf` |
| Lauf- oder Tagesdeckel überschritten | 402 | `KI_TEST_DECKEL_ERREICHT` | `ebene` (`lauf`/`tag`), `summe_eur`, `deckel_eur` |
| DB/platform-api nicht les- oder schreibbar | 503 | `KI_TEST_DECKEL_NICHT_LESBAR` | fail-closed |

Die Fehlerhülle ist `{"detail": {"error": {...}}}`. Für Testaufrufe sind die Fallback-Ketten auf andere Anbieter abgeschaltet.

## Lauf und Kosten

- Ein Lauf ist der Schlüssel aus UTC-Tag, Bridge-Nutzer-UUID und `X-Workflow-ID`. Der Tag steckt im Schlüssel, weil Apps teils feste Workflow-Namen senden.
- Vor dem Aufruf wird eine Schätzung reserviert: Eingabe-Tokens plus `max_tokens` (ohne Angabe 8192), bewertet zum Listenpreis nach `src/pricing.py`. Lauf- und Tageszeile werden in einer Transaktion unter `FOR UPDATE` geprüft und gebucht.
- Nach dem Aufruf verrechnet `persist_ai_call_activity` die Schätzung auf die Ist-Kosten. Eine nie verrechnete Reservierung bleibt stehen: Sie zählt zu viel, nie zu wenig.
- Protokollzeilen: `test-ki echt, gedeckelt app=… lauf=… reserviert|ist=… lauf_summe=x/0.50 tag_summe=y/10.00 EUR`.

## Innen-API (platform-api, Service-Token)

`POST /v1/internal/test-ki-deckel/testkonto`, `…/reservieren` und `…/verrechnen`. Der Worker geht über platform-api. Bei `PlatformUnavailable` greift er direkt auf die DB zu, sofern er sie hat. Sonst antwortet er mit 503.

## Rollout-Reihenfolge

1. Migration 072 per `bin/bridge-migrate.sh` auf die Dev-Bridge-DB.
2. Bridge-Deploy (Dev) mit dem Schalter in `docker-compose.yml`.
3. Danach erst die App-Schalter `KI_TEST_ECHT_GEDECKELT=1` (Works/Report, Preview).
