"""
Vorwarnung bei Monatsbudget-Verbrauch — an den BETREIBER, nicht an den Kunden.

Warum es das gibt (Rafael, 2026-08-11): Für die anonymen Gratis-Checks gilt ein
Monatsbudget von 1000 EUR. Ein Deckel, von dem niemand merkt, dass er näher
kommt, ist keine Bremse, sondern eine Überraschung — im schlechten Fall steht
der Gratis-Trichter still, ohne dass jemand es mitbekommt.

Abgrenzung zu `trial_warnings.py`: Das dort schreibt KUNDEN an ("dein Test
endet"). Dieses Modul schreibt ausschließlich an eine Betreiber-Adresse. Es
verschickt niemals Post an Kunden — Budget-Auslastung ist unsere Betriebs-
information, keine Kundenmitteilung.

Idempotenz: Tabelle `budget_warnings` mit UNIQUE (user_id, plan_id,
period_reset_at, threshold_pct). Der Periodenanker steckt IM Schlüssel — der
Monatsreset (`rollover_monthly_if_due`) schiebt `resetAt` weiter, damit ist die
Warnung in der neuen Periode automatisch wieder scharf, ohne Stempel-Pflege.

Defensiver Vertrag (wie trial_warnings):
  - RESEND_API_KEY fehlt UND es ist etwas zu senden -> lauter Fehler, kein
    stiller Übersprung. Nichts zu senden + fehlender Key ist unkritisch.
  - Ein fehlgeschlagener Versand bricht den Durchlauf NICHT ab; die anderen
    Konten werden weiter bearbeitet.
  - Gestempelt wird ausschließlich nach Resend-2xx, und der Stempel ist der
    LETZTE Schritt. Ein Absturz zwischen Versand und Stempel führt zu einer
    Dopplung beim nächsten Durchlauf — das ist bewusst dem umgekehrten Risiko
    (stempeln, dann nicht senden = stille Lücke) vorgezogen.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

import httpx

from src.db.client import get_pool

logger = logging.getLogger(__name__)

# Schwellen in Prozent. 50 = Rafaels "Warnung wenn die Hälfte verbraucht ist";
# 80 als Vorlauf zum Anschlag; 100, weil "Topf ist zu" die Meldung ist, die man
# am dringendsten braucht. Überschreibbar für Betreiber mit anderem Geschmack.
def _thresholds() -> List[int]:
    raw = os.environ.get("BUDGET_WARNING_THRESHOLDS_PCT", "50,80,100")
    werte = sorted({int(t.strip()) for t in raw.split(",") if t.strip()})
    if not werte or any(t <= 0 or t > 100 for t in werte):
        raise RuntimeError(
            f"budget_warnings: BUDGET_WARNING_THRESHOLDS_PCT ungueltig: {raw!r} "
            "(erwartet: Prozentwerte 1-100, komma-getrennt)"
        )
    return werte


def _recipient() -> str:
    return os.environ.get("BUDGET_WARNING_RECIPIENT", "office@werking.tools")


# ---------------------------------------------------------------------------
# Reine Logik — keine DB, kein Netz. Hier liegt das Verhalten, das Tests pruefen.
# ---------------------------------------------------------------------------

def _ist_trial(plan_id: str) -> bool:
    """Trial-Plan?

    Zwei Fehlerfaelle, absichtlich UNTERSCHIEDLICH behandelt — get_plan wirft
    dafuer eigene Typen:

    - RuntimeError = der Plan-Katalog ist LEER (reload_plans lief nie). Dann
      laesst sich Trial nicht von Echtplan unterscheiden, und ein pauschales
      "kein Trial" wuerde Meldungen fuer JEDEN erschoepften Testzugang
      verschicken und zugleich das Startproblem verdecken. Fliegt deshalb
      weiter: der Durchlauf scheitert laut, die Schleife loggt es, es geht
      keine falsche Post raus.
    - ValueError = DIESER eine Plan ist unbekannt. Das ist eine Anomalie, aber
      kein Grund, ein Konto mit Budget unbeobachtet zu lassen. Gilt als
      Nicht-Trial, mit lautem Log — eine Meldung zu viel ist besser als ein
      blinder Fleck.
    """
    from src.budget.plans import get_plan
    try:
        return bool(get_plan(plan_id).trial)
    except ValueError:
        logger.warning(
            "budget_warnings: Plan %r nicht im Katalog — als Nicht-Trial behandelt, "
            "Periodenangabe in der Meldung ist ungeprueft", plan_id,
        )
        return False


def erreichte_schwellen(
    used_eur: float,
    limit_eur: float,
    thresholds: List[int],
) -> List[int]:
    """Welche Schwellen sind beim aktuellen Verbrauch erreicht?

    Gibt ALLE erreichten zurueck, nicht nur die hoechste: bei einem Sprung von
    30 % auf 90 % zwischen zwei Durchlaeufen soll die 50er-Warnung nicht
    verschluckt werden — sie ist die Information "es geht steil". Was davon
    schon verschickt wurde, entscheidet der UNIQUE-Index, nicht diese Funktion.

    limit_eur <= 0 ergibt [] statt einer Division: ein Konto ohne Grenze hat
    keine Auslastung, und ein ZeroDivisionError im Sweep waere ein stiller
    Ausfall der ganzen Warnkette.
    """
    if limit_eur <= 0:
        return []
    pct = (used_eur / limit_eur) * 100.0
    return [t for t in thresholds if pct >= t]


def _fmt_eur(wert: float) -> str:
    """Deutsche Schreibweise: 1.234,56 — Punkt als Tausender, Komma als Dezimal.

    Bewusst von Hand statt ueber die ","-Formatspezifikation: die liefert je
    nach Umgebung ein schmales geschuetztes Leerzeichen (U+202F) als Trenner.
    Das ist in einer Mail unsichtbar und macht jeden Vergleich auf den Betrag
    unzuverlaessig (genau daran ist der erste Testlauf gescheitert).
    """
    ganz, _, dezimal = f"{abs(wert):.2f}".partition(".")
    gruppen = []
    while len(ganz) > 3:
        gruppen.insert(0, ganz[-3:])
        ganz = ganz[:-3]
    gruppen.insert(0, ganz)
    vorzeichen = "-" if wert < 0 else ""
    return vorzeichen + chr(46).join(gruppen) + chr(44) + dezimal


def render_warnung(
    *,
    konto: str,
    plan_id: str,
    used_eur: float,
    limit_eur: float,
    threshold_pct: int,
    period_reset_at: str,
) -> Tuple[str, str]:
    """(Betreff, HTML). Nennt Zahlen und den Zeitpunkt der Erholung."""
    pct = (used_eur / limit_eur) * 100.0 if limit_eur > 0 else 0.0
    voll = threshold_pct >= 100
    betreff = (
        f"[Budget] {konto} — Monatsbudget aufgebraucht ({_fmt_eur(limit_eur)} EUR)"
        if voll else
        f"[Budget] {konto} — {threshold_pct} % des Monatsbudgets verbraucht"
    )
    kopf = (
        "Das Monatsbudget ist aufgebraucht. Weitere Aufrufe auf dieses Konto "
        "werden abgelehnt, bis die Periode wechselt."
        if voll else
        f"Das Monatsbudget ist zu {pct:.0f} % verbraucht."
    )
    html = f"""<!DOCTYPE html>
<html lang="de"><body style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; color: #111827;">
  <p>{kopf}</p>
  <table style="border-collapse: collapse; font-size: 14px;">
    <tr><td style="padding: 2px 12px 2px 0; color: #6b7280;">Konto</td><td><strong>{konto}</strong></td></tr>
    <tr><td style="padding: 2px 12px 2px 0; color: #6b7280;">Plan</td><td>{plan_id}</td></tr>
    <tr><td style="padding: 2px 12px 2px 0; color: #6b7280;">Verbraucht</td><td>{_fmt_eur(used_eur)} EUR von {_fmt_eur(limit_eur)} EUR</td></tr>
    <tr><td style="padding: 2px 12px 2px 0; color: #6b7280;">Neue Periode ab</td><td>{period_reset_at}</td></tr>
  </table>
  <p style="font-size: 13px; color: #6b7280;">
    Betriebsmeldung der AI-Bridge. Eine Meldung je Schwelle und Periode —
    nach dem Periodenwechsel wird erneut gewarnt.
  </p>
</body></html>"""
    return betreff, html


# ---------------------------------------------------------------------------
# Versand
# ---------------------------------------------------------------------------

async def _send_via_resend(
    *, recipient: str, subject: str, html_body: str, resend_key: str, sender: str,
) -> None:
    """POST an Resend. Wirft RuntimeError bei non-2xx."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {resend_key}",
                     "Content-Type": "application/json"},
            json={"from": sender, "to": [recipient],
                  "subject": subject, "html": html_body},
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Resend error {resp.status_code}: {resp.text[:200]}")


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

_SELECT_BUDGETS_SQL = """
SELECT b.user_id, u.email, b.monthly_budgets
FROM user_budgets b
JOIN users u ON u.id = b.user_id
"""

_ALREADY_SENT_SQL = """
SELECT 1 FROM budget_warnings
WHERE user_id = $1 AND plan_id = $2 AND period_reset_at = $3 AND threshold_pct = $4
"""

_STAMP_SQL = """
INSERT INTO budget_warnings
  (user_id, plan_id, period_reset_at, threshold_pct, used_eur, limit_eur, recipient)
VALUES ($1, $2, $3, $4, $5, $6, $7)
ON CONFLICT (user_id, plan_id, period_reset_at, threshold_pct) DO NOTHING
"""


async def _faellige_warnungen() -> List[Dict[str, Any]]:
    """Alle noch nicht verschickten, erreichten Schwellen — ueber alle Konten."""
    thresholds = _thresholds()
    pool = get_pool()
    faellig: List[Dict[str, Any]] = []

    async with pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_BUDGETS_SQL)
        for row in rows:
            raw = row["monthly_budgets"]
            if isinstance(raw, str):
                raw = json.loads(raw)
            if not isinstance(raw, dict):
                continue
            for plan_id, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                try:
                    used = float(entry.get("usedEur", 0.0))
                    limit = float(entry.get("limitEur", 0.0))
                    anker = str(entry["resetAt"])
                except (KeyError, TypeError, ValueError):
                    # Kaputter Eintrag: ueberspringen, aber SICHTBAR — ein
                    # stiller Skip hier heisst "Konto wird nie ueberwacht".
                    logger.warning(
                        "budget_warnings: Eintrag unlesbar user=%s plan=%s entry=%r",
                        row["user_id"], plan_id, entry,
                    )
                    continue
                # Trials ueberspringen — DERSELBE Fallstrick wie beim
                # Monatsreset: bei Trials ist resetAt das ABLAUFdatum, nicht
                # der Periodenanker. Die Zeile "Neue Periode ab" waere dort
                # sachlich falsch, und da ein Trial nie rollt, bliebe der
                # Schluessel fuer immer gleich (eine Dauer-Meldung pro Trial).
                # Erschoepfte Trials sind ausserdem der Normalfall und Sache
                # von trial_warnings, das den KUNDEN informiert.
                if _ist_trial(plan_id):
                    continue
                for pct in erreichte_schwellen(used, limit, thresholds):
                    schon = await conn.fetchval(
                        _ALREADY_SENT_SQL, row["user_id"], plan_id, anker, pct
                    )
                    if schon:
                        continue
                    faellig.append({
                        "user_id": row["user_id"],
                        "konto": row["email"],
                        "plan_id": plan_id,
                        "period_reset_at": anker,
                        "threshold_pct": pct,
                        "used_eur": used,
                        "limit_eur": limit,
                    })
    return faellig


async def run_budget_warning_sweep() -> Dict[str, Any]:
    """Ein Durchlauf. Idempotent — mehrfacher Aufruf sendet nicht doppelt."""
    faellig = await _faellige_warnungen()
    if not faellig:
        logger.info("budget_warnings: Durchlauf fertig — nichts faellig")
        return {"sent": 0, "failed": 0, "due": 0}

    resend_key = os.environ.get("RESEND_API_KEY")
    sender = os.environ.get(
        "RESEND_BUDGET_WARNING_SENDER",
        os.environ.get("RESEND_INVOICE_SENDER", "billing@werking.tools"),
    )
    if not resend_key:
        raise RuntimeError(
            f"budget_warnings: RESEND_API_KEY nicht konfiguriert — "
            f"{len(faellig)} faellige Warnungen koennen nicht raus"
        )

    empfaenger = _recipient()
    pool = get_pool()
    sent = failed = 0

    for w in faellig:
        betreff, html = render_warnung(
            konto=w["konto"], plan_id=w["plan_id"], used_eur=w["used_eur"],
            limit_eur=w["limit_eur"], threshold_pct=w["threshold_pct"],
            period_reset_at=w["period_reset_at"],
        )
        try:
            await _send_via_resend(
                recipient=empfaenger, subject=betreff, html_body=html,
                resend_key=resend_key, sender=sender,
            )
        except Exception as exc:
            # Ein Konto darf den Durchlauf nicht kippen.
            failed += 1
            logger.error(
                "budget_warnings: Versand fehlgeschlagen konto=%s plan=%s schwelle=%s: %s",
                w["konto"], w["plan_id"], w["threshold_pct"], exc,
            )
            continue
        async with pool.acquire() as conn:
            await conn.execute(
                _STAMP_SQL, w["user_id"], w["plan_id"], w["period_reset_at"],
                w["threshold_pct"], w["used_eur"], w["limit_eur"], empfaenger,
            )
        sent += 1
        logger.info(
            "budget_warnings: gesendet konto=%s plan=%s schwelle=%s%% (%.2f/%.2f EUR)",
            w["konto"], w["plan_id"], w["threshold_pct"], w["used_eur"], w["limit_eur"],
        )

    logger.info("budget_warnings: Durchlauf fertig — gesendet=%s fehlgeschlagen=%s", sent, failed)
    return {"sent": sent, "failed": failed, "due": len(faellig)}


# ---------------------------------------------------------------------------
# Taeglicher Scheduler — gleiche Bauweise wie trial_warnings
# ---------------------------------------------------------------------------

_WARNING_HOUR_UTC = int(os.environ.get("BUDGET_WARNING_HOUR_UTC", "7"))


async def _budget_warning_loop() -> None:
    while True:
        now = datetime.now(timezone.utc)
        next_run = now.replace(hour=_WARNING_HOUR_UTC, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run = next_run + timedelta(days=1)
        delay = (next_run - now).total_seconds()
        logger.info(
            "budget_warnings: naechster Durchlauf %s UTC (in %.0f Minuten)",
            next_run.isoformat(), delay / 60,
        )
        await asyncio.sleep(delay)
        try:
            await run_budget_warning_sweep()
        except Exception as exc:
            # Konfigurationsfehler (z. B. fehlender Resend-Key) darf die
            # Schleife nicht beenden — der naechste Durchlauf holt alles
            # idempotent nach, sobald der Betreiber es richtet.
            logger.error("budget_warnings: Durchlauf fehlgeschlagen: %s", exc)


def start_budget_warning_loop() -> asyncio.Task:
    """Taeglichen Durchlauf einplanen. Einmal aus dem lifespan-Startup rufen."""
    return asyncio.create_task(_budget_warning_loop())
