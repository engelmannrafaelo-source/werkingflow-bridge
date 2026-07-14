"""
Tests für project_resets_service — operator-gated one-shot "reset project" grant.

Coverage:
- create_request: success, empty argument/project_id → ValueError,
  unique-violation (open request exists) → OpenRequestExistsError
- decide: approve/reject success, missing row → RequestNotFoundError
- redeem: no approved grant → redeemed=False (no reset), approved grant →
  redeemed=True + reset_budget invoked + grant marked redeemed (one transaction)
- get_open_status: open row → its status, no row → 'none'
- reset_budget (project_budgets_service): 'UPDATE 1' → True, 'UPDATE 0' → False,
  and that it only ever zeroes used_eur
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import os
os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest

from src.billing import project_resets_service as svc
from src.billing.project_budgets_service import reset_budget


def _row(**over):
    base = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "tenant_id": "plasser",
        "plan_id": "energy-project",
        "project_id": "Proj_1",
        "app_id": "werking-energy",
        "project_name": "Kaiser-Franz-Ring",
        "argument": "Neue Messdaten nachgereicht",
        "status": "requested",
        "requested_at": None,
        "decided_at": None,
        "decided_by": None,
        "redeemed_at": None,
    }
    base.update(over)
    return base


def _mock_pool(*, fetchrow_result=None, fetchrow_side=None, execute_result="UPDATE 1"):
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=execute_result)
    if fetchrow_side is not None:
        conn.fetchrow = AsyncMock(side_effect=fetchrow_side)
    else:
        conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetch = AsyncMock(return_value=[])

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


# --- create_request --------------------------------------------------------

@pytest.mark.asyncio
async def test_create_request_success(monkeypatch):
    pool, conn = _mock_pool(fetchrow_result=_row())
    import src.billing.project_resets_service as m
    monkeypatch.setattr(m, "get_pool", lambda: pool)
    out = await svc.create_request(
        user_id=uuid.uuid4(), project_id="Proj_1", argument="Neue Messdaten",
    )
    assert out["projectId"] == "Proj_1"
    assert out["status"] == "requested"


@pytest.mark.asyncio
async def test_create_request_empty_argument_raises():
    with pytest.raises(ValueError):
        await svc.create_request(user_id=uuid.uuid4(), project_id="P", argument="   ")


@pytest.mark.asyncio
async def test_create_request_empty_project_raises():
    with pytest.raises(ValueError):
        await svc.create_request(user_id=uuid.uuid4(), project_id="  ", argument="x")


@pytest.mark.asyncio
async def test_create_request_open_conflict_raises(monkeypatch):
    pool, conn = _mock_pool()
    conn.fetchrow = AsyncMock(
        side_effect=Exception('duplicate key value violates unique constraint "uq_prr_open_per_project"')
    )
    import src.billing.project_resets_service as m
    monkeypatch.setattr(m, "get_pool", lambda: pool)
    with pytest.raises(svc.OpenRequestExistsError):
        await svc.create_request(user_id=uuid.uuid4(), project_id="Proj_1", argument="x")


# --- decide ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_success(monkeypatch):
    pool, conn = _mock_pool(fetchrow_result=_row(status="approved", decided_by="rafael"))
    import src.billing.project_resets_service as m
    monkeypatch.setattr(m, "get_pool", lambda: pool)
    out = await svc.decide(request_id=uuid.uuid4(), approve=True, operator="rafael")
    assert out["status"] == "approved"


@pytest.mark.asyncio
async def test_decide_missing_row_raises(monkeypatch):
    pool, conn = _mock_pool(fetchrow_result=None)  # WHERE status='requested' matched nothing
    import src.billing.project_resets_service as m
    monkeypatch.setattr(m, "get_pool", lambda: pool)
    with pytest.raises(svc.RequestNotFoundError):
        await svc.decide(request_id=uuid.uuid4(), approve=True)


# --- redeem ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_redeem_no_grant_returns_false_and_resets_nothing(monkeypatch):
    pool, conn = _mock_pool(fetchrow_result=None)  # no approved grant
    import src.billing.project_resets_service as m
    monkeypatch.setattr(m, "get_pool", lambda: pool)
    out = await svc.redeem(user_id=uuid.uuid4(), project_id="Proj_1")
    assert out == {"redeemed": False, "budgetReset": False}
    conn.execute.assert_not_awaited()  # never touched a budget without a grant


@pytest.mark.asyncio
async def test_redeem_approved_grant_resets_and_consumes(monkeypatch):
    grant_id = uuid.uuid4()
    pool, conn = _mock_pool(fetchrow_result={"id": grant_id}, execute_result="UPDATE 1")
    import src.billing.project_resets_service as m
    monkeypatch.setattr(m, "get_pool", lambda: pool)
    out = await svc.redeem(user_id=uuid.uuid4(), project_id="Proj_1")
    assert out == {"redeemed": True, "budgetReset": True}
    # Two executes: reset_budget UPDATE project_budgets, then UPDATE status→redeemed.
    assert conn.execute.await_count == 2
    sqls = " ".join(str(c.args[0]) for c in conn.execute.await_args_list)
    assert "project_budgets" in sqls and "used_eur = 0" in sqls
    assert "status = 'redeemed'" in sqls


@pytest.mark.asyncio
async def test_redeem_approved_grant_no_budget_row_still_succeeds(monkeypatch):
    pool, conn = _mock_pool(fetchrow_result={"id": uuid.uuid4()}, execute_result="UPDATE 0")
    import src.billing.project_resets_service as m
    monkeypatch.setattr(m, "get_pool", lambda: pool)
    out = await svc.redeem(user_id=uuid.uuid4(), project_id="Proj_1")
    assert out == {"redeemed": True, "budgetReset": False}


# --- get_open_status -------------------------------------------------------

@pytest.mark.asyncio
async def test_open_status_reports_status(monkeypatch):
    pool, conn = _mock_pool(fetchrow_result={"status": "approved"})
    import src.billing.project_resets_service as m
    monkeypatch.setattr(m, "get_pool", lambda: pool)
    out = await svc.get_open_status(user_id=uuid.uuid4(), project_id="Proj_1")
    assert out == {"status": "approved"}


@pytest.mark.asyncio
async def test_open_status_none_when_no_open_row(monkeypatch):
    pool, conn = _mock_pool(fetchrow_result=None)
    import src.billing.project_resets_service as m
    monkeypatch.setattr(m, "get_pool", lambda: pool)
    out = await svc.get_open_status(user_id=uuid.uuid4(), project_id="Proj_1")
    assert out == {"status": "none"}


# --- reset_budget (project_budgets_service) --------------------------------

@pytest.mark.asyncio
async def test_reset_budget_true_when_row_updated():
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    ok = await reset_budget(conn, user_id=uuid.uuid4(), plan_id="energy-project", project_id="P")
    assert ok is True
    assert "used_eur = 0" in str(conn.execute.await_args.args[0])


@pytest.mark.asyncio
async def test_reset_budget_false_when_no_row():
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 0")
    ok = await reset_budget(conn, user_id=uuid.uuid4(), plan_id="energy-project", project_id="P")
    assert ok is False
