# Naht-Audit: Token-Tracking ↔ Billing

**Datum:** 2026-05-18
**Anlass:** Abschluss der Token-Tracking-Konsolidierung (Bridge self-log, Budget-Gate,
L0-Guard, App-Migration aller 5 Apps). Dieses Dokument hält die Befunde an der
**Naht zwischen Erfassung und Abrechnung** fest — sie gehören in den Billing-Workstream
(`feat/kundenbereich-self-service-endpoints`), nicht in die Tracking-Initiative.

**Status Tracking-Säule:** fertig. Jeder `/v1/chat/completions`-Call wird self-geloggt
(`ai_call_writer.persist_ai_call_activity`), der L0-Guard `ai_call_tracked` blockt
ungetrackte Calls. **Status Naht zur Abrechnung:** vier Befunde, einer kritisch.

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

## Reihenfolge für den Billing-Workstream

1. **Befund 3 zuerst** — ohne Deduction ist das gesamte Budget-/Gate-System
   funktional wirkungslos. Setzt Befund 2 (welches Pricing für die Kost?) und
   Befund 4 (welches Budget wird abgezogen?) als Entscheidung voraus.
2. **Befund 4** — Architektur-Entscheidung, welches Budget-System führt.
3. **Befund 2** — Pricing-SSoT, danach Gate/Metrics/Deduction darauf umstellen.
4. **Befund 1** — optional: Kost im self-log mitschreiben, sobald die SSoT steht
   (dann ist die Activity self-contained, kein Nachrechnen mehr nötig).
