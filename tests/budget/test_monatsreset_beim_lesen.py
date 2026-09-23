"""
Monatsreset auch beim LESEN des Budgets.

rollover_monthly_if_due lief nur im Pruef- (/check, im Speicher) und im
Abbuchungspfad (persistiert). GET /v1/budget/{user_id} und die Admin-Liste
GET /v1/budget gaben usedEur/resetAt roh aus der Datenbank zurueck. Ein Konto
ohne Abbuchung seit dem Anker zeigte deshalb einen ueberfaelligen Topf: alter
Verbrauch, resetAt in der Vergangenheit — obwohl das naechste /check denselben
Topf als leer behandelt. Gemessen 23.09.2026 am Testkonto: Anker 01.08., seither
keine durchgekommene Abbuchung.

Der Lesepfad rechnet deshalb dieselbe Regel wie /check: nur im Speicher, nur
Nicht-Trials (bei Trials ist resetAt das Ablaufdatum).
"""
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

USER_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def _pool(fetchrow_results: list, fetch_results: list):
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=fetchrow_results)
    conn.fetch = AsyncMock(side_effect=fetch_results)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


def _plan(trial: bool):
    return MagicMock(trial=trial)


def _get_plan_fuer(trials: set):
    def _get(plan_id):
        if plan_id == "unbekannt":
            raise ValueError(f"[PlanManager] Unknown plan: {plan_id}")
        return _plan(plan_id in trials)
    return _get


async def _lies_einzeln(monthly: dict, trials: set = frozenset()) -> dict:
    from src.budget.routes import get_budget
    from src.api_auth import AuthClaims

    pool, conn = _pool(
        [
            {"monthly_budgets": json.dumps(monthly)},
            {"balance_eur": 0.0},
            {"updated_at": datetime(2026, 9, 1, tzinfo=timezone.utc)},
        ],
        [[], []],
    )
    with patch("src.budget.routes.get_pool", return_value=pool), \
         patch("src.budget.routes.get_plan", side_effect=_get_plan_fuer(set(trials))):
        resp = await get_budget(str(USER_ID), MagicMock(spec=AuthClaims))
    # Lesen schreibt nie: der neue Anker wird im Abbuchungspfad persistiert.
    conn.execute.assert_not_called()
    return resp


async def _lies_liste(monthly: dict, trials: set = frozenset()) -> dict:
    from src.budget.routes import list_budgets
    from src.api_auth import AuthClaims

    pool, conn = _pool([], [[{
        "user_id": USER_ID,
        "monthly_budgets": json.dumps(monthly),
        "updated_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "topup_balance": 0.0,
    }]])
    with patch("src.budget.routes.get_pool", return_value=pool), \
         patch("src.budget.routes.get_plan", side_effect=_get_plan_fuer(set(trials))):
        resp = await list_budgets(MagicMock(spec=AuthClaims))
    conn.execute.assert_not_called()
    return resp["items"][0]


def _jetzt() -> datetime:
    return datetime.now(timezone.utc)


def _faellig() -> dict:
    return {"limitEur": 10.0, "usedEur": 7.5, "resetAt": (_jetzt() - timedelta(days=53)).isoformat()}


@pytest.mark.asyncio
async def test_einzelsicht_zeigt_faelligen_topf_zurueckgesetzt():
    resp = await _lies_einzeln({"pro": _faellig()})
    topf = resp["monthlyBudgets"]["pro"]
    assert topf["usedEur"] == 0.0
    assert datetime.fromisoformat(topf["resetAt"]) > _jetzt()
    assert topf["limitEur"] == 10.0


@pytest.mark.asyncio
async def test_adminliste_zeigt_faelligen_topf_zurueckgesetzt():
    item = await _lies_liste({"pro": _faellig()})
    assert item["monthlyBudgets"]["pro"]["usedEur"] == 0.0
    assert datetime.fromisoformat(item["monthlyBudgets"]["pro"]["resetAt"]) > _jetzt()
    assert item["usedEur"] == 0.0
    assert item["remainingEur"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_nicht_faelliger_topf_bleibt_beim_lesen_unberuehrt():
    anker = (_jetzt() + timedelta(days=5)).isoformat()
    resp = await _lies_einzeln({"pro": {"limitEur": 10.0, "usedEur": 7.5, "resetAt": anker}})
    assert resp["monthlyBudgets"]["pro"] == {"limitEur": 10.0, "usedEur": 7.5, "resetAt": anker}


@pytest.mark.asyncio
async def test_trial_wird_beim_lesen_nie_gerollt():
    # Bei Trials ist resetAt das Ablaufdatum — ein Rollover machte den Trial unsterblich.
    abgelaufen = _faellig()
    resp = await _lies_einzeln({"trial": abgelaufen}, trials={"trial"})
    assert resp["monthlyBudgets"]["trial"] == abgelaufen
    item = await _lies_liste({"trial": abgelaufen}, trials={"trial"})
    assert item["monthlyBudgets"]["trial"] == abgelaufen


@pytest.mark.asyncio
async def test_unbekannter_plan_wird_nicht_gerollt_und_bricht_das_lesen_nicht(caplog):
    # Ohne Plan ist nicht entscheidbar, ob resetAt Anker oder Ablauf ist:
    # nicht raten, roh zeigen, laut loggen.
    roh = _faellig()
    resp = await _lies_einzeln({"unbekannt": roh})
    assert resp["monthlyBudgets"]["unbekannt"] == roh
    assert any("unbekannt" in r.getMessage() for r in caplog.records)
