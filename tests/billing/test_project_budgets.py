"""
Tests für project_budgets_service — per-project API-Budget (interval='project').

Coverage:
- allocate_budget: neue Zeile (True), idempotent on conflict (False), leeres project_id → ValueError
- get_budget: remaining-Berechnung, None bei fehlender Zeile
- evaluate: exists/allowed bei vorhandenem/fehlendem Budget
- deduct: Cap auf remaining (used nie > limit), exists=False ohne Zeile, cost<=0 no-op
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
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
)

PG = "src.billing.project_budgets_service.get_pool"


def _mock_pool(fetchrow_result=None):
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)

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
    assert r == {"exists": False, "allowed": False, "remainingEur": 0.0}


@pytest.mark.asyncio
async def test_evaluate_allows_with_remaining():
    pool, _ = _mock_pool(fetchrow_result={"limit_eur": Decimal("100"), "used_eur": Decimal("99.9")})
    with patch(PG, return_value=pool):
        r = await evaluate(uuid.uuid4(), "energy-project", "Proj_1", 0.0)
    assert r["exists"] is True and r["allowed"] is True


@pytest.mark.asyncio
async def test_evaluate_blocks_when_exhausted():
    pool, _ = _mock_pool(fetchrow_result={"limit_eur": Decimal("100"), "used_eur": Decimal("100")})
    with patch(PG, return_value=pool):
        r = await evaluate(uuid.uuid4(), "energy-project", "Proj_1", 0.5)
    assert r["exists"] is True and r["allowed"] is False


# --- deduct ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_deduct_happy_path():
    pool, conn = _mock_pool(fetchrow_result={"limit_eur": Decimal("100"), "used_eur": Decimal("10")})
    with patch(PG, return_value=pool):
        r = await deduct(uuid.uuid4(), "energy-project", "Proj_1", 2.5)
    assert r["exists"] is True
    assert r["deductedEur"] == 2.5
    assert r["usedEur"] == 12.5
    assert r["remainingEur"] == 87.5


@pytest.mark.asyncio
async def test_deduct_caps_at_remaining_never_exceeds_limit():
    # 5 EUR remaining, call costs 8 EUR → only 5 deducted, used == limit (100).
    pool, conn = _mock_pool(fetchrow_result={"limit_eur": Decimal("100"), "used_eur": Decimal("95")})
    with patch(PG, return_value=pool):
        r = await deduct(uuid.uuid4(), "energy-project", "Proj_1", 8.0)
    assert r["deductedEur"] == 5.0
    assert r["usedEur"] == 100.0
    assert r["remainingEur"] == 0.0


@pytest.mark.asyncio
async def test_deduct_no_budget_row_is_noop():
    pool, _ = _mock_pool(fetchrow_result=None)
    with patch(PG, return_value=pool):
        r = await deduct(uuid.uuid4(), "energy-project", "Proj_1", 1.0)
    assert r == {"exists": False, "deductedEur": 0.0, "usedEur": 0.0, "remainingEur": 0.0}


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
    assert r == {"exists": False, "deductedEur": 0.0, "usedEur": 0.0, "remainingEur": 0.0}
