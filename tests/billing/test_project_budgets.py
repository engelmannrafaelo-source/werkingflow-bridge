"""
Tests für project_budgets_service — per-project API-Budget (interval='project').

Coverage:
- allocate_budget: neue Zeile (True), idempotent on conflict (False), leeres project_id → ValueError
- get_budget: remaining-Berechnung, None bei fehlender Zeile
- evaluate: exists/allowed bei vorhandenem/fehlendem Budget, TopUp-Fallback wenn Projekt-Budget leer
- deduct: Cap auf remaining (used nie > limit), TopUp-Fallback für den Rest, exists=False ohne Zeile,
  cost<=0 no-op
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import os
os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest

from src.billing.project_budgets_service import (
    allocate_budget,
    deduct,
    evaluate,
    get_budget,
    list_budgets,
)

PG = "src.billing.project_budgets_service.get_pool"
NOW = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)


def _topup_row(lot_id, amount_eur, purchased_days_ago=10, expires_in_days=300):
    # id must be a real UUID — persist_topup_lots() round-trips it through
    # uuid.UUID(lot.id) when writing the FIFO-reduced amount back.
    return {
        "id": lot_id,
        "amount_eur": Decimal(str(amount_eur)),
        "purchased_at": NOW - timedelta(days=purchased_days_ago),
        "expires_at": NOW + timedelta(days=expires_in_days),
    }


_LOT_1 = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _mock_pool(fetchrow_result=None, topup_rows=None):
    """Mock pool/conn shared by evaluate()/deduct() tests.

    `fetchrow_result` answers the project_budgets SELECT; any other
    `fetchrow` call (the legacy-topup-balance guard in topup_store) returns
    None — real code issues two DIFFERENT fetchrow queries once the TopUp
    fallback kicks in, so a single constant return value silently
    misattributes the project row to the legacy-balance check.
    `topup_rows` answers the user_topup_lots SELECT via `fetch` (defaults to
    no lots — i.e. TopUp contributes nothing).
    """
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)

    async def _fetchrow(query, *args, **kwargs):
        if "project_budgets" in query:
            return fetchrow_result
        return None  # user_topup_balances legacy guard — no legacy balance

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.fetch = AsyncMock(return_value=topup_rows or [])

    @asynccontextmanager
    async def _acquire():
        yield conn

    @asynccontextmanager
    async def _transaction():
        yield

    conn.transaction = _transaction
    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


# --- allocate_budget -------------------------------------------------------

@pytest.mark.asyncio
async def test_allocate_returns_true_on_insert():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": uuid.uuid4()})
    created = await allocate_budget(
        conn, user_id=uuid.uuid4(), tenant_id="t", plan_id="energy-project",
        project_id="Proj_1", limit_eur=100.0,
    )
    assert created is True


@pytest.mark.asyncio
async def test_allocate_idempotent_returns_false_on_conflict():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)  # ON CONFLICT DO NOTHING → no row
    created = await allocate_budget(
        conn, user_id=uuid.uuid4(), tenant_id="t", plan_id="energy-project",
        project_id="Proj_1", limit_eur=100.0,
    )
    assert created is False


@pytest.mark.asyncio
async def test_allocate_rejects_empty_project_id():
    conn = AsyncMock()
    with pytest.raises(ValueError):
        await allocate_budget(
            conn, user_id=uuid.uuid4(), tenant_id="t", plan_id="energy-project",
            project_id="", limit_eur=100.0,
        )


# --- get_budget ------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_budget_computes_remaining():
    pool, _ = _mock_pool(fetchrow_result={"limit_eur": Decimal("100"), "used_eur": Decimal("30")})
    with patch(PG, return_value=pool):
        b = await get_budget(uuid.uuid4(), "energy-project", "Proj_1")
    assert b == {"limitEur": 100.0, "usedEur": 30.0, "remainingEur": 70.0}


@pytest.mark.asyncio
async def test_get_budget_none_when_unallocated():
    pool, _ = _mock_pool(fetchrow_result=None)
    with patch(PG, return_value=pool):
        b = await get_budget(uuid.uuid4(), "energy-project", "Proj_1")
    assert b is None


# --- evaluate --------------------------------------------------------------

@pytest.mark.asyncio
async def test_evaluate_not_exists_blocks():
    pool, _ = _mock_pool(fetchrow_result=None)
    with patch(PG, return_value=pool):
        r = await evaluate(uuid.uuid4(), "energy-project", "Proj_1", 0.0)
    assert r == {"exists": False, "allowed": False, "remainingEur": 0.0, "topUpRemainingEur": 0.0}


@pytest.mark.asyncio
async def test_evaluate_allows_with_remaining():
    pool, _ = _mock_pool(fetchrow_result={"limit_eur": Decimal("100"), "used_eur": Decimal("99.9")})
    with patch(PG, return_value=pool):
        r = await evaluate(uuid.uuid4(), "energy-project", "Proj_1", 0.0)
    assert r["exists"] is True and r["allowed"] is True
    # Project budget alone covered it — TopUp was never consulted.
    assert r["topUpRemainingEur"] == 0.0


@pytest.mark.asyncio
async def test_evaluate_blocks_when_exhausted():
    # Project pot exhausted AND no TopUp lots (default _mock_pool) → truly blocked.
    pool, _ = _mock_pool(fetchrow_result={"limit_eur": Decimal("100"), "used_eur": Decimal("100")})
    with patch(PG, return_value=pool):
        r = await evaluate(uuid.uuid4(), "energy-project", "Proj_1", 0.5)
    assert r["exists"] is True and r["allowed"] is False
    assert r["topUpRemainingEur"] == 0.0


@pytest.mark.asyncio
async def test_evaluate_falls_back_to_topup_when_project_exhausted():
    # Regression for the KFR bug: project pot is 0/100 but the user holds
    # TopUp — evaluate() must allow the call instead of raising 402 while
    # 15.500 EUR of TopUp sits unused.
    pool, _ = _mock_pool(
        fetchrow_result={"limit_eur": Decimal("100"), "used_eur": Decimal("100")},
        topup_rows=[_topup_row(_LOT_1, 50.0)],
    )
    with patch(PG, return_value=pool):
        r = await evaluate(uuid.uuid4(), "energy-project", "Proj_1", 5.0)
    assert r["exists"] is True
    assert r["allowed"] is True
    assert r["topUpRemainingEur"] == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_evaluate_ignores_expired_topup_lot():
    pool, _ = _mock_pool(
        fetchrow_result={"limit_eur": Decimal("100"), "used_eur": Decimal("100")},
        topup_rows=[_topup_row("expired", 50.0, purchased_days_ago=400, expires_in_days=-1)],
    )
    with patch(PG, return_value=pool):
        r = await evaluate(uuid.uuid4(), "energy-project", "Proj_1", 5.0)
    assert r["allowed"] is False
    assert r["topUpRemainingEur"] == 0.0


# --- deduct ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_deduct_happy_path():
    pool, conn = _mock_pool(fetchrow_result={"limit_eur": Decimal("100"), "used_eur": Decimal("10")})
    with patch(PG, return_value=pool):
        r = await deduct(uuid.uuid4(), "energy-project", "Proj_1", 2.5)
    assert r["exists"] is True
    assert r["deductedEur"] == 2.5
    assert r["fromProjectEur"] == 2.5
    assert r["fromTopUpEur"] == 0.0
    assert r["usedEur"] == 12.5
    assert r["remainingEur"] == 87.5


@pytest.mark.asyncio
async def test_deduct_caps_at_remaining_when_no_topup_available():
    # 5 EUR remaining, call costs 8 EUR, no TopUp lots → 5 deducted total,
    # the 3 EUR shortfall is absorbed (logged), used == limit (100).
    pool, conn = _mock_pool(fetchrow_result={"limit_eur": Decimal("100"), "used_eur": Decimal("95")})
    with patch(PG, return_value=pool):
        r = await deduct(uuid.uuid4(), "energy-project", "Proj_1", 8.0)
    assert r["deductedEur"] == 5.0
    assert r["fromProjectEur"] == 5.0
    assert r["fromTopUpEur"] == 0.0
    assert r["usedEur"] == 100.0
    assert r["remainingEur"] == 0.0


@pytest.mark.asyncio
async def test_deduct_falls_back_to_topup_for_shortfall():
    # Regression for the KFR bug: project pot covers 5 of an 8 EUR call, the
    # remaining 3 EUR must be drawn from the user's TopUp lot instead of
    # being silently absorbed.
    pool, conn = _mock_pool(
        fetchrow_result={"limit_eur": Decimal("100"), "used_eur": Decimal("95")},
        topup_rows=[_topup_row(_LOT_1, 50.0)],
    )
    with patch(PG, return_value=pool):
        r = await deduct(uuid.uuid4(), "energy-project", "Proj_1", 8.0)
    assert r["fromProjectEur"] == 5.0
    assert r["fromTopUpEur"] == pytest.approx(3.0)
    assert r["deductedEur"] == pytest.approx(8.0)
    assert r["usedEur"] == 100.0  # project pot itself is still capped at its limit
    # The TopUp lot must actually be written back reduced (50 - 3 = 47).
    conn.execute.assert_any_call(
        "UPDATE user_topup_lots SET amount_eur = $2, updated_at = NOW() WHERE id = $1",
        _LOT_1,
        47.0,
    )


@pytest.mark.asyncio
async def test_deduct_shortfall_when_project_and_topup_both_insufficient():
    # Project covers 5, TopUp lot only has 1 more → total covered 6, cost 8.
    # Must NOT raise — the call already happened; shortfall is absorbed.
    pool, conn = _mock_pool(
        fetchrow_result={"limit_eur": Decimal("100"), "used_eur": Decimal("95")},
        topup_rows=[_topup_row(_LOT_1, 1.0)],
    )
    with patch(PG, return_value=pool):
        r = await deduct(uuid.uuid4(), "energy-project", "Proj_1", 8.0)
    assert r["fromProjectEur"] == 5.0
    assert r["fromTopUpEur"] == pytest.approx(1.0)
    assert r["deductedEur"] == pytest.approx(6.0)  # < cost_eur — shortfall absorbed, not raised


@pytest.mark.asyncio
async def test_deduct_no_budget_row_is_noop():
    pool, _ = _mock_pool(fetchrow_result=None)
    with patch(PG, return_value=pool):
        r = await deduct(uuid.uuid4(), "energy-project", "Proj_1", 1.0)
    assert r == {
        "exists": False, "deductedEur": 0.0, "fromProjectEur": 0.0,
        "fromTopUpEur": 0.0, "usedEur": 0.0, "remainingEur": 0.0,
    }


@pytest.mark.asyncio
async def test_deduct_zero_cost_noop_with_state():
    pool, _ = _mock_pool(fetchrow_result={"limit_eur": Decimal("100"), "used_eur": Decimal("40")})
    with patch(PG, return_value=pool):
        r = await deduct(uuid.uuid4(), "energy-project", "Proj_1", 0.0)
    assert r["exists"] is True and r["deductedEur"] == 0.0 and r["remainingEur"] == 60.0


@pytest.mark.asyncio
async def test_deduct_lazy_allocates_when_missing():
    # 1st FOR UPDATE → None (no budget yet); after the INSERT, 2nd FOR UPDATE
    # returns the freshly-allocated row → deduct draws from it.
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(
        side_effect=[None, {"limit_eur": Decimal("100"), "used_eur": Decimal("0")}]
    )

    @asynccontextmanager
    async def _acquire():
        yield conn

    @asynccontextmanager
    async def _transaction():
        yield

    conn.transaction = _transaction
    pool = MagicMock()
    pool.acquire = _acquire

    with patch(PG, return_value=pool):
        r = await deduct(
            uuid.uuid4(), "energy-project", "Proj_X", 3.0,
            allocate_limit_eur=100.0, tenant_id="t",
        )
    assert r["exists"] is True
    assert r["deductedEur"] == 3.0
    assert r["usedEur"] == 3.0
    assert r["remainingEur"] == 97.0
    assert conn.execute.await_count >= 1  # INSERT (alloc) + UPDATE (deduct)


@pytest.mark.asyncio
async def test_deduct_no_lazy_alloc_without_tenant():
    # Missing row + allocate_limit but no tenant_id → cannot allocate → noop.
    pool, _ = _mock_pool(fetchrow_result=None)
    with patch(PG, return_value=pool):
        r = await deduct(
            uuid.uuid4(), "energy-project", "Proj_X", 3.0,
            allocate_limit_eur=100.0, tenant_id=None,
        )
    assert r == {
        "exists": False, "deductedEur": 0.0, "fromProjectEur": 0.0,
        "fromTopUpEur": 0.0, "usedEur": 0.0, "remainingEur": 0.0,
    }


# --- list_budgets ----------------------------------------------------------

def _mock_pool_fetch(*fetch_results):
    """Pool whose conn.fetch returns each result in order (one per call)."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=list(fetch_results))

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


@pytest.mark.asyncio
async def test_list_budgets_aggregates_pot_and_joins_tokens():
    # 2 projects × 100 = 500 pot; tokens joined by project_id; P2 has no tokens.
    budget_rows = [
        {"project_id": "P1", "limit_eur": Decimal("100"), "used_eur": Decimal("40")},
        {"project_id": "P2", "limit_eur": Decimal("100"), "used_eur": Decimal("0")},
    ]
    token_rows = [{"project_id": "P1", "tokens_used": 12345}]
    pool, _ = _mock_pool_fetch(budget_rows, token_rows)
    with patch(PG, return_value=pool):
        out = await list_budgets(uuid.uuid4(), "energy-project")

    assert out["totals"] == {
        "limitEur": 200.0,
        "usedEur": 40.0,
        "remainingEur": 160.0,
        "tokensUsed": 12345,
        "projectCount": 2,
    }
    p1, p2 = out["projects"]
    assert p1 == {
        "projectId": "P1", "limitEur": 100.0, "usedEur": 40.0,
        "remainingEur": 60.0, "tokensUsed": 12345,
    }
    assert p2["tokensUsed"] == 0  # budget row without usage rows


@pytest.mark.asyncio
async def test_list_budgets_empty_when_no_projects():
    pool, _ = _mock_pool_fetch([], [])
    with patch(PG, return_value=pool):
        out = await list_budgets(uuid.uuid4(), "energy-project")
    assert out["projects"] == []
    assert out["totals"]["projectCount"] == 0
    assert out["totals"]["limitEur"] == 0.0
