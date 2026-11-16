"""Atomare Reservierung fuer strict_monthly (oeffentlicher Schema-Generator, werking-schema-generator).

Root-Klarstellung 16.11.2026: Der 100-EUR-Monatstopf ist nur dann ein harter Deckel, wenn parallele Aufrufe
ihn NICHT ueberziehen koennen. Die reine Vorab-Pruefung (evaluate_budget) laesst n gleichzeitige Aufrufe
durch, die alle denselben Rest sehen. Deshalb reserviert das Tor fuer strict_monthly VOR dem Aufruf eine
Kostenobergrenze, atomar unter derselben Zeilensperre wie die Abbuchung (user_budgets FOR UPDATE):

    verbraucht + offene, nicht abgelaufene Reservierungen + neue Obergrenze <= Monatslimit

TopUp zaehlt hier ausdruecklich NICHT (strict = nur der Monatstopf). Die Abbuchung nach dem Aufruf
(apply_budget_deduction mit reservation_id) rechnet die Reservierung in DERSELBEN Transaktion ab
(offen -> abgerechnet). Endet die Anfrage ohne Abbuchung mit einem Fehler, gibt die Middleware frei
(offen -> freigegeben). Sicherheitsnetz: jede Reservierung laeuft nach TTL_SECONDS ab und zaehlt danach
nicht mehr. Bis dahin blockiert sie nur, ueberzieht nie.

Datenbank-Teil laeuft NUR in platform-api (hat die DB); der Worker spricht ueber call_platform und ist
fail-closed: keine klare Antwort -> 503.
Vorbild: src/budget/test_ki_deckel.py (Reservierung/Abrechnung/Obergrenze).
"""
from __future__ import annotations

import contextvars
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

STRICT_APP_ID = "werking-schema-generator"
#: Laenger als jeder einzelne Aufruf dauern darf (Chat-Weg, Bedrock). Danach zaehlt eine
#: haengengebliebene Reservierung nicht mehr; eine spaetere Abrechnung bucht trotzdem (laut geloggt).
TTL_SECONDS = 2 * 60 * 60
#: Ohne max_tokens setzt das Tor diese Ausgabegrenze und reserviert genau sie.
DEFAULT_MAX_TOKENS = 4096


@dataclass
class StrictReservation:
    reservation_id: str
    user_id: str
    plan_id: str
    amount_eur: float
    abgerechnet: bool = False
    freigegeben: bool = False


#: Gesetzt vom Tor im Anfrage-Kontext; gelesen von der Abbuchung (ai_call_writer).
current: contextvars.ContextVar[Optional[StrictReservation]] = contextvars.ContextVar(
    "strict_monthly_reservation", default=None,
)


# ── Obergrenze (Worker) ──────────────────────────────────────────────────────

def cost_upper_bound_eur(model: Optional[str], request_body: Any) -> float:
    """Obergrenze zu den hoechsten Saetzen des Modells: Eingabe = UTF-8-Bytes (>= Tokens) wie der
    Test-KI-Deckel, als Cache-Anlage bewertet (teuerster Eingabesatz); Ausgabe = max_tokens."""
    from src.budget.test_ki_deckel import CACHE_ANLAGE_MULT, eingabe_obergrenze_tokens
    from src.pricing import MODEL_PRICING, usd_to_eur_rate

    p = MODEL_PRICING.get(model or "")
    if p is None:
        raise ValueError(f"kein Preis fuer Modell {model!r} — strict_monthly kann nicht reservieren")
    unten = p.get("bis_prompt") or {}
    ein = max(p["in"], unten.get("in", 0.0))
    aus = max(p["out"], unten.get("out", 0.0))
    eingabe = eingabe_obergrenze_tokens(request_body)
    ausgabe = int(request_body.max_tokens or DEFAULT_MAX_TOKENS)
    usd = (eingabe * ein * CACHE_ANLAGE_MULT + ausgabe * aus) / 1_000_000.0
    return round(usd * usd_to_eur_rate(), 6)


# ── Datenbank (NUR platform-api) ─────────────────────────────────────────────

async def _lock_and_roll(conn, user_id: uuid.UUID, plan_id: str):
    """user_budgets-Zeile sperren, faelligen Monatstopf zuruecksetzen und PERSISTIEREN.
    Rueckgabe (limit_eur, used_eur) oder None, wenn der User keinen Topf fuer plan_id hat."""
    from src.budget.calculator import rollover_monthly_if_due
    from src.budget.routes import _build_monthly_budgets, _parse_raw_monthly

    row = await conn.fetchrow(
        "SELECT monthly_budgets FROM user_budgets WHERE user_id = $1 FOR UPDATE", user_id,
    )
    if row is None:
        return None
    raw = _parse_raw_monthly(row)
    entry = _build_monthly_budgets(raw).get(plan_id)
    if entry is None:
        return None
    rolled, did_roll = rollover_monthly_if_due(entry)
    if did_roll:
        upd = dict(raw)
        upd[plan_id] = {**upd[plan_id], "usedEur": rolled.used_eur, "resetAt": rolled.reset_at}
        await conn.execute(
            "UPDATE user_budgets SET monthly_budgets = $1::jsonb, updated_at = NOW() WHERE user_id = $2",
            json.dumps(upd), user_id,
        )
        logger.info("[StrictReserve] Monatstopf zurueckgesetzt user=%s plan=%s neuer_anker=%s",
                    user_id, plan_id, rolled.reset_at)
        entry = rolled
    return float(entry.limit_eur), float(entry.used_eur)


async def _open_sum(conn, user_id: uuid.UUID, plan_id: str) -> float:
    v = await conn.fetchval(
        "SELECT COALESCE(SUM(amount_eur), 0) FROM budget_reservations "
        "WHERE user_id = $1 AND plan_id = $2 AND status = 'offen' AND expires_at > now()",
        user_id, plan_id,
    )
    return float(v or 0)


async def db_reserve(*, reservation_id: uuid.UUID, user_id: uuid.UUID, plan_id: str,
                     amount_eur: float) -> Dict[str, Any]:
    """Atomar und idempotent. Antwort {ok, duplicate, reason?, limitEur, usedEur, reservedEur, remainingEur}."""
    from src.budget.plans import get_plan
    from src.db.client import get_pool

    plan = get_plan(plan_id)  # ValueError bei unbekanntem Plan
    if plan.trial or plan.interval != "month":
        raise ValueError(f"strict reservation nur fuer Monatsplaene (nicht {plan_id})")
    if not (amount_eur > 0):
        raise ValueError("amount_eur muss > 0 sein")
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            topf = await _lock_and_roll(conn, user_id, plan_id)
            vorhanden = await conn.fetchrow(
                "SELECT user_id, plan_id, amount_eur, status FROM budget_reservations WHERE reservation_id = $1",
                reservation_id,
            )
            if vorhanden is not None:
                if vorhanden["user_id"] != user_id or vorhanden["plan_id"] != plan_id:
                    raise ValueError("reservation_id gehoert zu einer anderen Reservierung")
                return {"ok": True, "duplicate": True, "status": vorhanden["status"]}
            if topf is None:
                return {"ok": False, "duplicate": False, "reason": "no_monthly_budget"}
            limit, used = topf
            offen = await _open_sum(conn, user_id, plan_id)
            rest = round(limit - used - offen, 6)
            if amount_eur > rest:
                return {"ok": False, "duplicate": False, "reason": "strict_monthly_exhausted",
                        "limitEur": limit, "usedEur": used, "reservedEur": offen, "remainingEur": max(rest, 0.0)}
            await conn.execute(
                "INSERT INTO budget_reservations (reservation_id, user_id, plan_id, amount_eur, expires_at) "
                "VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5))",
                reservation_id, user_id, plan_id, amount_eur, TTL_SECONDS,
            )
    return {"ok": True, "duplicate": False, "limitEur": limit, "usedEur": used,
            "reservedEur": round(offen + amount_eur, 6), "remainingEur": round(rest - amount_eur, 6)}


async def db_release(*, reservation_id: uuid.UUID) -> Dict[str, Any]:
    """offen -> freigegeben, hoechstens einmal. Antwort {released: bool, status}."""
    from src.db.client import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "UPDATE budget_reservations SET status = 'freigegeben', closed_at = now() "
            "WHERE reservation_id = $1 AND status = 'offen' RETURNING status", reservation_id,
        )
        if r is not None:
            return {"released": True, "status": "freigegeben"}
        s = await conn.fetchval("SELECT status FROM budget_reservations WHERE reservation_id = $1", reservation_id)
        if s is None:
            raise LookupError("reservation_id unbekannt")
        return {"released": False, "status": s}


async def settle_in_tx(conn, *, reservation_id: uuid.UUID, user_id: uuid.UUID, plan_id: str,
                       actual_eur: float) -> None:
    """In der Abbuchungs-Transaktion (Zeile ist dort schon gesperrt): offen -> abgerechnet.
    Bereits freigegeben/abgelaufen: Abbuchung gilt trotzdem (der Aufruf ist passiert), aber laut."""
    r = await conn.fetchrow(
        "UPDATE budget_reservations SET status = 'abgerechnet', settled_eur = $4, closed_at = now() "
        "WHERE reservation_id = $1 AND user_id = $2 AND plan_id = $3 AND status = 'offen' "
        "RETURNING amount_eur, expires_at < now() AS abgelaufen",
        reservation_id, user_id, plan_id, actual_eur,
    )
    if r is None:
        logger.error("[StrictReserve] Abrechnung ohne offene Reservierung id=%s user=%s plan=%s %.6f EUR "
                     "(freigegeben/abgerechnet/unbekannt) — Abbuchung gilt, Deckel nicht reservierungsgedeckt",
                     reservation_id, user_id, plan_id, actual_eur)
        return
    if r["abgelaufen"]:
        logger.error("[StrictReserve] Abrechnung NACH Ablauf der Reservierung id=%s %.6f EUR — Deckel kann "
                     "ueberschritten sein", reservation_id, actual_eur)
    if actual_eur > float(r["amount_eur"]) + 1e-9:
        logger.error("[StrictReserve] Ist %.6f > Reservierung %.6f EUR id=%s — Obergrenze verletzt",
                     actual_eur, float(r["amount_eur"]), reservation_id)


# ── Worker-Seite (fail-closed) ───────────────────────────────────────────────

async def reserve_via_platform(*, user_id: uuid.UUID, plan_id: str, amount_eur: float) -> Dict[str, Any]:
    """Eine Wiederholung (idempotent per ID). Jede Antwort ausser 200/JSON wirft."""
    from src.platform_client import call_platform

    rid = str(uuid.uuid4())
    resp = await call_platform(
        "POST", "/v1/budget/strict/reserve",
        json={"reservationId": rid, "userId": str(user_id), "planId": plan_id, "amountEur": amount_eur},
        retries=1, domain="user",
    )
    if resp.status_code != 200 or not isinstance(resp.json, dict):
        raise RuntimeError(f"strict reserve answered {resp.status_code}")
    return {**resp.json, "reservationId": rid}


async def release_via_platform(reservation_id: str) -> None:
    """Wirft nie; scheitert die Freigabe, laeuft die Reservierung per TTL ab."""
    try:
        from src.platform_client import call_platform

        resp = await call_platform(
            "POST", "/v1/budget/strict/release", json={"reservationId": reservation_id},
            retries=1, domain="user",
        )
        if resp.status_code != 200:
            logger.error("[StrictReserve] Freigabe id=%s antwortete %s — laeuft per TTL ab",
                         reservation_id, resp.status_code)
    except Exception as e:  # noqa: BLE001
        logger.error("[StrictReserve] Freigabe id=%s gescheitert (%s) — laeuft per TTL ab", reservation_id, e)


async def release_if_unsettled(request: Any, status_code: int) -> None:
    """Middleware-Haken: Anfrage endete mit Fehler und ohne Abrechnung -> freigeben."""
    r = getattr(getattr(request, "state", None), "strict_reservation", None)
    if r is None or r.abgerechnet or r.freigegeben or status_code < 400:
        return
    r.freigegeben = True
    await release_via_platform(r.reservation_id)
