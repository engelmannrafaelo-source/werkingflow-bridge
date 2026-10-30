# Vertrag: Gate fail-closed bei unklarem Abrechnungstopf + gebundenes Auftragsbudget-Token (BA6)

Stand: 30.10.2026. Auftrag: KD 91624155 (BA6/B1, Entscheid F5 vom 29.10.2026 13:18Z). Grundlage: `s7e/ENTWURF-S7-360.md` (P10, H4, F5–F7) im Koordinationsordner der Energy-Lanes. Code: `src/budget/gate.py`, `src/api_auth/deps.py`, `src/config.py`, `src/billing/routes.py`.

## 1. Gate: kein Durchlass bei unklarem Abrechnungstopf

**Vorher (Befund B1):** `enforce_budget` warf `PlanResolutionError`/`AmbiguousPlanCatalog` bewusst weiter. Die Aufrufer (`main.py` Chat, Research, Doc-Agent, DOCX-Export) reichen aber nur `HTTPException` durch und schlucken alles andere mit `except Exception` („letting call through"). Dadurch lief genau der Aufruf ungemessen durch, den die Regel verhindern sollte. Bei der Sandbox-Lease wurde daraus ein 500.

**Jetzt:** Das Gate wandelt diese Fehler selbst in eine sichtbare Abweisung um:

| Ursache | HTTP | `error.type` | `reason` | `retryable` |
|---|---|---|---|---|
| Ein Projekt ist unter mehreren Plänen zugeteilt (`AmbiguousProjectBudget`) | 409 | `billing_plan_unresolved` | `ambiguous_project_budget` | false |
| Zuteilung zeigt auf einen Plan, der den Aufruf nicht bezahlen kann; App mit mehreren Projektplänen ohne Zuteilung | 409 | `billing_plan_unresolved` | `billing_plan_unresolved` | false |
| Plankatalog widersprüchlich (`AmbiguousPlanCatalog`, schon bei der Katalogprüfung) | 409 | `billing_plan_unresolved` | `ambiguous_plan_catalog` | false |
| Zuteilung unter einem Plan, der nicht (mehr) im aktiven Katalog steht | 409 | `billing_plan_unresolved` | `billing_plan_unresolved` | false |

Die Hülle mit `error.type` gilt für die Worker-Wege (Chat, Research, Doc-Agent, DOCX). Die Sandbox-Lease läuft in der platform-api mit dem Standard-Handler von FastAPI und antwortet dort mit `{"detail": {...}}`; Status 409 und `reason` sind gleich. Den Fehlertyp setzt der zentrale Fehlerhandler ausdrücklich. Ohne diese Setzung hieße ein 409 dort `bridge_throttle`, und Report wiederholt bei `bridge_throttle`. Die Abweisung erscheint im Log als `budget gate: REJECTED (409 …)`. nginx reicht 409 unverändert durch, abgefangen werden dort nur 429 und 5xx.

**Unverändert:** Infrastrukturfehler (DB/platform-api nicht erreichbar) bleiben fail-open, wie in `gate.py` beschlossen. Ausnahme ist ein bekanntes hard_cap-Projekt (Cache oder Kopf `X-Budget-Hard-Cap`): Dafür gilt seit 26.10. die Antwort 503 `budget_unavailable` mit `X-Bridge-Dependency-Unavailable: budget-state`. **Neu** fällt darunter auch der nicht geladene Plankatalog (`PlanCatalogUnavailable`). Er entkam dem Gate bisher vor jeder Prüfung, und ein hard_cap-Projekt lief dann trotz Kopf durch (Dev-Worker-Log 29.10. und 30.10.2026).

**Wen das trifft (Lesung Dev-Bridge, 30.10.2026):** Die Worker-`error.log` reichen bis zum 09.08.2026 zurück. Fehler der Planauflösung gab es nur am 16.08.2026 um 21:34 (Ortszeit): `werking-check` ohne `project_id` und ohne Zuteilung, 21 durchgelassene Chat-Aufrufe in einer Minute. Ab dieser Bridge bekämen solche Aufrufe eine 409 statt eines ungemessenen Durchlasses. Danach gab es keinen solchen Fall mehr. Belege liegen im BA6-Belegordner.

## 2. Gebundenes Auftragsbudget-Token

Das Operator-Token `X-Bridge-Service-Token` ist global. Es öffnet alle Billing-Routen, auch TopUp und `raise`. Energy darf es nie halten (P10/H4). Deshalb gibt es zwei gebundene Token:

| Variable (Bridge, platform-api) | Rechte | Gegenstück Energy (`env-contracts.json`, werking-energy) |
|---|---|---|
| `BRIDGE_AUFTRAGSBUDGET_TOKEN` | `anlegen`, `lesen` | `AI_BRIDGE_AUFTRAGSBUDGET_TOKEN` (nur API-Dienst) |
| `BRIDGE_AUFTRAGSBUDGET_LESETOKEN` | `lesen` | `AI_BRIDGE_AUFTRAGSBUDGET_LESETOKEN` (Worker) |

Kopf: `X-Bridge-Auftragsbudget-Token`. Bindung: `plan_id` muss `energy-project` sein. `X-App-ID` darf fehlen oder `werking-energy` lauten. Jeder andere Wert ergibt 403 `auftragsbudget_bindung`.

| Route | Operator-Token | Anlege-Token | Lese-Token |
|---|---|---|---|
| `POST /v1/users/{id}/auftrag-budgets` | ja | ja | 403 `auftragsbudget_recht_fehlt` |
| `GET /v1/users/{id}/auftrag-budgets/{project_id}` | ja | ja | ja |
| `GET /v1/users/{id}/auftrag-budgets?parent_project_id=` | ja | ja | ja |
| `POST /v1/users/{id}/auftrag-budgets/{project_id}/raise` | ja | **401** | **401** |
| jede andere Route | wie bisher | 401 (Kopf wird nicht gelesen) | 401 |

Regeln:
- Wird `X-Bridge-Service-Token` mitgeschickt, entscheidet allein dieses Token. Ist es ungültig, folgt 401 ohne Rückfall auf den anderen Kopf.
- Ungesetzte Variable = das Token existiert nicht, die Antwort ist 401. Der Operator-Weg bleibt unverändert.
- Beim Start wird geprüft: mindestens 32 Zeichen, die beiden Token verschieden und keines gleich `BRIDGE_SERVICE_TOKEN`. Sonst bricht der Start ab.
- Im Protokoll (`actor`) steht ein Aufruf mit gebundenem Token als `auftragsbudget-token:<X-App-ID>/<X-Agent-ID>`.

## 3. Ausrollen (nicht Teil dieses Commits; nach Mühl-Ende auf KD-Wort, Prod nur mit Rafaels Wort)

1. Werte je Bridge (Dev und Prod getrennt) erzeugen und in Infisical ablegen. Auf der Bridge kommen sie in `secrets/platform.env` der platform-api. `sync-infisical-to-bridge` (orchestrator) schreibt nur `docker/.env` der Worker und erfasst diese Variablen nicht. Den Eintrag in `platform.env` regelt deshalb der Rollout.
2. Dieselben Werte unter `werking-energy/<env>` als `AI_BRIDGE_AUFTRAGSBUDGET_TOKEN` (API) bzw. `AI_BRIDGE_AUFTRAGSBUDGET_LESETOKEN` (Worker) setzen.
3. Migration 071 ist Voraussetzung für die Routen (F7) und gehört nicht zu diesem Commit.
4. platform-api und Worker gemeinsam ausliefern: Das Gate läuft im Worker, die Token-Routen laufen in der platform-api.
