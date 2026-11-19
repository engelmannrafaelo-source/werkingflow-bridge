"""Atomare Reservierung fuer strict_monthly (oeffentlicher Schema-Generator, werking-schema-generator).

Root-Klarstellung 16.11.2026: Der 100-EUR-Monatstopf ist nur dann ein harter Deckel, wenn parallele Aufrufe
ihn NICHT ueberziehen koennen. Die reine Vorab-Pruefung (evaluate_budget) laesst n gleichzeitige Aufrufe
durch, die alle denselben Rest sehen. Deshalb reserviert das Tor fuer strict_monthly VOR dem Aufruf eine
Kostenobergrenze, atomar unter derselben Zeilensperre wie die Abbuchung (user_budgets FOR UPDATE):

    verbraucht + offene Reservierungen DIESER Periode + neue Obergrenze <= Monatslimit

Grundannahme (Umbau nach Pruefung R3, 16.11.2026): EINE RESERVIERUNG IST VERBRAUCH, bis die Abrechnung
sie durch den Ist-Betrag ersetzt. Die Abbuchung nach dem Aufruf (apply_budget_deduction mit reservation_id)
rechnet sie in DERSELBEN Transaktion ab (offen -> abgerechnet, Ist-Betrag in usedEur). Es gibt KEINE
Freigabe und KEIN Ablaufen: ob ein Aufruf beim Anbieter Kosten erzeugt hat, ist bei Fehlerenden
(Timeout, 502, abgebrochene Verbindung, Absturz) nicht entscheidbar — eine Freigabe koennte den Topf
ueberziehen. Eine nie abgerechnete Reservierung zaehlt deshalb mit ihrer Obergrenze bis zum Ende der
Periode, in der sie angelegt wurde (Periodenbeginn = resetAt - PERIOD_DAYS). Preis: Ein Fehler VOR dem
Versand blockiert bis Periodenende die eigene Obergrenze — Ueberblockung, nie Ueberziehung.
Eine spaete Abrechnung einer Reservierung aus einer frueheren Periode bucht in die aktuelle (konservativ).

TopUp zaehlt hier ausdruecklich NICHT (strict = nur der Monatstopf).

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
#: Periodenlaenge des Monatstopfs — gleich rollover_monthly_if_due (src/budget/calculator.py).
PERIOD_DAYS = 30
#: Hoechstzahl Ausgabe-Tokens NUR fuer werking-schema-generator (einzige Verwendung: main.py-Zweig dieser App
#: und cost_upper_bound_eur der strikten Reservierung; andere Apps unberuehrt). Das Tor setzt
#: max_tokens = min(angefragt, Grenze); das Anfragemodell hat sonst den Standard 64000 (src/models.py).
#: 16000 = Wert des Homepage-Clients (werking-tools schema-erzeugen/ki.ts KI_MAX_AUSGABE_TOKEN; 8000 schnitt
#: grosse Anlagen ab, Analyse 09.11.2026). Kanzler-Entscheid 17.11.2026 nach Weg-A-Pruefung R1 (K2).
#: G-5 (19.11.2026, Rafael ueber Kanzler): 32000 — mit 16000 endeten grosse Textanlagen (7 Unterlagen) samt Denken
#: zweimal mit finish_reason != stop. Wirkt erst, wenn der Client ebenfalls mehr als 16000 anfragt (min-Regel).
#: G-7 (19.11.2026, Rafael ueber Kanzler „Deckel auf 128k“): 128000; ersetzt den G-5-Wert.
MAX_TOKENS_CAP = 128000


@dataclass
class StrictReservation:
    reservation_id: str
    user_id: str
    plan_id: str
    amount_eur: float
    abgerechnet: bool = False


@dataclass
class StrictPending:
    """Im Tor geprueft, noch NICHT reserviert (Weg A: Reservierung direkt vor dem Versand)."""
    uid: Any
    app_id: str
    plan: Any
    amount_eur: float


#: Gesetzt vom Tor (Chat-Weg), verbraucht von gate.strict_reserve_pending() direkt vor dem Versand.
pending: contextvars.ContextVar[Optional[StrictPending]] = contextvars.ContextVar(
    "strict_monthly_pending", default=None,
)

#: Gesetzt vom Tor im Anfrage-Kontext; gelesen von der Abbuchung (ai_call_writer).
current: contextvars.ContextVar[Optional[StrictReservation]] = contextvars.ContextVar(
    "strict_monthly_reservation", default=None,
)


# ── Obergrenze (Worker) ──────────────────────────────────────────────────────

def cost_upper_bound_eur(model: Optional[str], request_body: Any, versuche: int = 1) -> float:
    """Obergrenze zu den hoechsten Saetzen des Modells: Eingabe = UTF-8-Bytes (>= Tokens) wie der
    Test-KI-Deckel, als Cache-Anlage bewertet (teuerster Eingabesatz); Ausgabe = max_tokens."""
    from src.budget.test_ki_deckel import CACHE_ANLAGE_MULT, eingabe_obergrenze_tokens
    from src.pricing import price_entry, usd_to_eur_rate

    # Dieselbe Preiszeile wie das Ledger (inkl. MODEL_PRICING_JSON-Override, Normalisierung).
    p = price_entry(model)
    if p is None:
        raise ValueError(f"kein Preis fuer Modell {model!r} — strict_monthly kann nicht reservieren")
    unten = p.get("bis_prompt") or {}
    ein = max(p["in"], unten.get("in", 0.0))
    aus = max(p["out"], unten.get("out", 0.0))
    eingabe = eingabe_obergrenze_tokens(request_body)
    if not request_body.max_tokens or request_body.max_tokens > MAX_TOKENS_CAP:
        raise ValueError(f"max_tokens {request_body.max_tokens!r} nicht begrenzt (Grenze {MAX_TOKENS_CAP})")
    ausgabe = int(request_body.max_tokens)
    if not isinstance(versuche, int) or versuche < 1:
        raise ValueError(f"versuche {versuche!r} ungueltig")
    # versuche: wie oft der Anbieter-Client denselben Aufruf hoechstens absetzt (jeder kann berechnet werden).
    usd = versuche * (eingabe * ein * CACHE_ANLAGE_MULT + ausgabe * aus) / 1_000_000.0
    return round(usd * usd_to_eur_rate(), 6)


def anbieter_versuche(boto_config: Any) -> int:
    """Wie oft botocore denselben Aufruf HOECHSTENS absetzt (jeder Versuch kann berechnet werden).
    botocore-Semantik: retries["total_max_attempts"] = Gesamtzahl; retries["max_attempts"] = Wiederholungen
    NACH dem ersten Versuch (gemessen 16.11.: max_attempts 2 -> 3 Verbindungen). Ohne ausdrueckliche
    Angabe gilt botocores Modus-Vorgabe — die nehmen wir nicht an: fail closed."""
    r = getattr(boto_config, "retries", None) or {}
    if "total_max_attempts" in r:
        n = int(r["total_max_attempts"])
    elif "max_attempts" in r:
        n = int(r["max_attempts"]) + 1
    else:
        raise ValueError("Bedrock-Konfiguration ohne ausdrueckliche Versuchszahl — strict_monthly reserviert nicht")
    if n < 1:
        raise ValueError(f"Versuchszahl {n} ungueltig")
    return n


# ── Datenbank (NUR platform-api) ─────────────────────────────────────────────

async def _lock_and_roll(conn, user_id: uuid.UUID, plan_id: str):
    """user_budgets-Zeile sperren, faelligen Monatstopf zuruecksetzen und PERSISTIEREN.
    Rueckgabe (limit_eur, used_eur, periodenbeginn) oder None, wenn der User keinen Topf fuer plan_id hat."""
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
    reset_at = datetime.fromisoformat(str(entry.reset_at).replace("Z", "+00:00"))
    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    return float(entry.limit_eur), float(entry.used_eur), reset_at - timedelta(days=PERIOD_DAYS)


async def _open_sum(conn, user_id: uuid.UUID, plan_id: str, periodenbeginn: datetime) -> float:
    """Alle nie abgerechneten Reservierungen der laufenden Periode — ohne Ablauf (Reservierung = Verbrauch)."""
    v = await conn.fetchval(
        "SELECT COALESCE(SUM(amount_eur), 0) FROM budget_reservations "
        "WHERE user_id = $1 AND plan_id = $2 AND status = 'offen' AND created_at >= $3",
        user_id, plan_id, periodenbeginn,
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
            limit, used, periodenbeginn = topf
            offen = await _open_sum(conn, user_id, plan_id, periodenbeginn)
            rest = round(limit - used - offen, 6)
            if amount_eur > rest:
                return {"ok": False, "duplicate": False, "reason": "strict_monthly_exhausted",
                        "limitEur": limit, "usedEur": used, "reservedEur": offen, "remainingEur": max(rest, 0.0)}
            await conn.execute(
                "INSERT INTO budget_reservations (reservation_id, user_id, plan_id, amount_eur) "
                "VALUES ($1, $2, $3, $4)",
                reservation_id, user_id, plan_id, amount_eur,
            )
    return {"ok": True, "duplicate": False, "limitEur": limit, "usedEur": used,
            "reservedEur": round(offen + amount_eur, 6), "remainingEur": round(rest - amount_eur, 6)}


async def settle_in_tx(conn, *, reservation_id: uuid.UUID, user_id: uuid.UUID, plan_id: str,
                       actual_eur: float) -> None:
    """In der Abbuchungs-Transaktion (Zeile ist dort schon gesperrt): offen -> abgerechnet.
    Bereits abgerechnet/unbekannt: Abbuchung gilt trotzdem (der Aufruf ist passiert), aber laut."""
    r = await conn.fetchrow(
        "UPDATE budget_reservations SET status = 'abgerechnet', settled_eur = $4, closed_at = now() "
        "WHERE reservation_id = $1 AND user_id = $2 AND plan_id = $3 AND status = 'offen' "
        "RETURNING amount_eur",
        reservation_id, user_id, plan_id, actual_eur,
    )
    if r is None:
        logger.error("[StrictReserve] Abrechnung ohne offene Reservierung id=%s user=%s plan=%s %.6f EUR "
                     "(abgerechnet/unbekannt) — Abbuchung gilt, Deckel nicht reservierungsgedeckt",
                     reservation_id, user_id, plan_id, actual_eur)
        return
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
