# Naht-Audit: Token-Tracking ↔ Billing

**Datum:** 2026-05-18
**Anlass:** Abschluss der Token-Tracking-Konsolidierung (Bridge self-log, Budget-Gate,
L0-Guard, App-Migration aller 5 Apps). Dieses Dokument hält die Befunde an der
**Naht zwischen Erfassung und Abrechnung** fest — sie gehören in den Billing-Workstream
(`feat/kundenbereich-self-service-endpoints`), nicht in die Tracking-Initiative.

**Status Tracking-Säule:** fertig. Jeder `/v1/chat/completions`-Call wird self-geloggt
(`ai_call_writer.persist_ai_call_activity`), der L0-Guard `ai_call_tracked` blockt
ungetrackte Calls. **Status Naht zur Abrechnung:** ursprünglich vier Befunde, einer kritisch — siehe Status unten.

---

## Status 2026-05-19 — Befunde 1–4 aufgelöst

Abschluss-Review des Call-Pfads: alle vier ursprünglichen Befunde sind
adressiert. Die Detail-Abschnitte darunter bleiben als Historie stehen.

| Befund | Status | Durch |
|--------|--------|-------|
| B1 — kein EUR-Betrag im self-log | erledigt | `e223fe7` — `costEur` wird in die Activity geschrieben |
| B2 — keine Pricing-SSoT | erledigt | `e223fe7` — `src/pricing.py` als SSoT, Gate + Metrics lesen daraus |
| B3 — Deduction nicht verdrahtet | erledigt | `e223fe7` — `persist_ai_call_activity` → `_deduct_call_cost` → `apply_budget_deduction` |
| B4 — zwei parallele Budget-Systeme | entschärft | `d11e924` — toter per-Tenant-Gate aus dem Chat-Pfad entfernt; Chat hat einen Gate. `src/tenant`-Budget lebt nur noch read-only (`/v1/usage/status`) |

Derselbe Review fand zwei **neue** Restpunkte → Befund 5 + 6 unten. Beides
Genauigkeits-/Edge-Case-Themen, **kein Launch-Blocker**.

---

## Befund 1 — self-log erfasst Tokens, keinen EUR-Betrag

`src/activity/ai_call_writer.py` `persist_ai_call_activity()` schreibt
`promptTokens` / `completionTokens` / `totalTokens` — **kein Kostenfeld**.
EUR-Kosten werden überall nachgelagert aus den Tokens neu berechnet.

*Bewertung:* an sich vertretbar — **vorausgesetzt**, alle Stellen rechnen mit
derselben Preisquelle. Tun sie nicht (→ Befund 2).

---

## Befund 2 — keine Pricing-Single-Source-of-Truth (4 Tabellen)

Vier voneinander unabhängige Preisdefinitionen:

| Ort | Form | Werte (Sonnet-Klasse) |
|-----|------|-----------------------|
| `src/metrics/routes.py` `_DEFAULT_PRICING` | USD/1M | in 3.00 / out 15.00 |
| `src/providers/registry.py` `pricing_input/output` | USD/1M | 3.00 / 15.00 |
| `src/tenant/usage_tracker.py` `DEFAULT_PRICING` | USD/1M | (eigene Tabelle) |
| `src/main.py` Budget-Gate (~Z.1804) | EUR/1M, hardcoded inline | in 2.90 / out 14.50 |

Sie stimmen *grob* überein, driften aber bereits: das Gate rechnet 2.90/14.50 EUR,
die Registry impliziert nach USD→EUR-Kurs (0.92) 2.76/13.80 EUR.

*Empfehlung:* eine Pricing-SSoT (ein Modul, z.B. `src/pricing.py` oder die
`providers/registry`-Werte), aus der Gate, Metrics, usage_tracker und Billing lesen.
Keine vierfache Pflege bei Preisänderungen.

---

## Befund 3 — KRITISCH: Budget-Deduction ist nicht verdrahtet

`src/main.py` ruft **vor** dem LLM-Call `enforce_budget()` (den Gate, ~Z.1808).
**Nach** dem Call ruft es `persist_ai_call_activity()` (self-log, Z.1531/2621/2736/2793)
— aber **nirgends `deduct_budget()`**.

- `deduct_budget()` (`src/budget/calculator.py`) existiert.
- `/v1/budget/deduct` (`src/budget/routes.py:319`) existiert.
- **Kein Aufrufer im Call-Pfad** — verifiziert per grep über `src/` und `main.py`.

**Folge:** `used_eur` im User-/Plan-Budget wächst durch AI-Calls nie. Der
Hard-Budget-Gate prüft gegen ein Budget, das sich nie leert — der `402`-Fall
würde im Normalbetrieb nie natürlich auslösen. (Der seinerzeitige Smoke-Test
hat das Budget manuell auf erschöpft gesetzt; das verdeckte die Lücke.)

*Empfehlung:* Nach jedem erfolgreichen Call die tatsächliche Kost ermitteln
(echte `usage`-Tokens × Pricing-SSoT) und `deduct_budget()` aufrufen — analog
und direkt neben dem bestehenden `persist_ai_call_activity()`-Aufruf. Fail-open
bei Infra-Fehlern wie beim Gate. **Eingriff in den heißesten Bridge-Pfad —
sorgfältig + getestet.**

---

## Befund 4 — zwei parallele Budget-Systeme

- `src/budget/` — User-/Plan-basiert, monatliches Budget + Top-up-Pool
  (`calculator.py`, `gate.py`, `plans.py`). Der Gate an `main.py:1808` nutzt dieses.
- `src/tenant/` — Tenant-/Token-basiert, `billing_mode`, `current_tokens`
  (`check_budget`, `usage_tracker.py`). `main.py:1900` nutzt dieses.

Unklar, welches das maßgebliche ist. Solange beide existieren, ist nicht
eindeutig, wo „das Budget eines Kunden" lebt.

*Empfehlung:* bewusst entscheiden — ein System ist führend, das andere wird
abgebaut oder klar als andere Ebene abgegrenzt. Architektur-Entscheidung,
kein reiner Code-Fix.

---

## Befund 5 — Streaming-Deduction läuft auf Schätz-Tokens

Der Non-Streaming-Erfolgspfad (`main.py:2620`) übergibt `persist_ai_call_activity`
echte `usage`-Tokens. Der **Streaming-Pfad (`main.py:1532`) übergibt
`est_prompt_tokens`/`est_completion_tokens`** — Schätzwerte. Die Budget-Deduction
für Streaming-Calls beruht damit auf einer Schätzung, nicht auf der echten
Provider-Usage; `used_eur` driftet von den tatsächlichen Kosten ab.

*Empfehlung:* die echte `usage` aus dem letzten Streaming-Chunk greifen
(Anthropic liefert sie dort) und an `persist_ai_call_activity` durchreichen.
Präzisions-Verbesserung, kein Funktionsfehler.

---

## Befund 6 — `deduct_budget` ist all-or-nothing — im Post-Call-Pfad falsch

`src/budget/calculator.py:94`: übersteigt die Ist-Kost das Restbudget, wirft
`deduct_budget` `BUDGET_EXCEEDED` und zieht **gar nichts** ab. Für den
Pre-Call-Endpoint `/v1/budget/deduct` ist das korrekt — aber `e223fe7`
verwendet dieselbe Funktion über `apply_budget_deduction` **post-call** wieder,
nachdem der Call schon gelaufen ist.

**Folge:** passieren mehrere Calls den Gate gleichzeitig (jeder sieht noch
Restbudget), übersteigt der überzählige beim Abzug das Limit → `ValueError`
→ in `_deduct_call_cost` abgefangen, geloggt, **kein Abzug**. Der Kunde hat den
Call gratis; `used_eur` unterzählt. Begrenzt durch Concurrency (der Gate nutzt
eine konservative `max_tokens`-Schätzung und blockt die nächste Runde) — also
kein unbegrenztes Leck, aber real.

*Empfehlung:* im Post-Call-Pfad gedeckelt abziehen (`used_eur` aufs Limit,
Top-up auf 0 treiben) statt zu werfen. Der Call ist passiert — er muss bezahlt
werden, soweit Budget da ist. All-or-nothing nur im Pre-Call-Endpoint behalten.

---

## Reihenfolge für den Billing-Workstream

Befunde 1–4 erledigt (siehe Status oben). Offen, beide nicht dringend:

1. **Befund 6** — Post-Call-Deduction deckeln. Kleiner, abgegrenzter Fix.
2. **Befund 5** — echte Streaming-Usage greifen. Präzisions-Verbesserung.
