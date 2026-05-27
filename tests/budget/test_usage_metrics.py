"""
Tests für GET /v1/budget/{user_id} — usage-metrics Erweiterung (P1).

Covered:
1. Sandbox + workflow tokens aus usage_events werden korrekt aggregiert
2. Fehlende Rows → alle Metriken sind 0 (kein Fehler)
3. Nur Sandbox, kein Workflow → workflowTokensUsed = 0
4. Nur Workflow, kein Sandbox → sandboxTokensUsed = 0, sandboxUsedEur = 0
5. monthlyTokensUsed = Summe aller sources
"""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

USER_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def _make_budget_row(monthly_budgets: dict | None = None) -> dict:
    return {
        "monthly_budgets": json.dumps(monthly_budgets or {}),
    }


def _make_topup_row(balance: float = 0.0) -> dict:
    return {"balance_eur": balance}


def _make_updated_at_row() -> dict:
    return {"updated_at": datetime(2026, 5, 1, tzinfo=timezone.utc)}


def _build_usage_rows(sandbox_tokens: int, sandbox_cost: float, workflow_tokens: int) -> list:
    """Build fake usage_events GROUP BY source result."""
    rows = []
    if sandbox_tokens:
        rows.append({"source": "sandbox", "tokens_used": sandbox_tokens, "cost_eur": sandbox_cost})
    if workflow_tokens:
        rows.append({"source": "workflow", "tokens_used": workflow_tokens, "cost_eur": 0.0})
    return rows


def _make_mock_pool(fetchrow_results: list, fetch_result: list):
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=fetchrow_results)
    conn.fetch = AsyncMock(return_value=fetch_result)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


# ---------------------------------------------------------------------------
# Helper: call get_budget with mocked pool + claims
# ---------------------------------------------------------------------------

async def _call_get_budget(fetchrow_results: list, fetch_result: list) -> dict:
    from src.budget.routes import get_budget
    from src.api_auth import AuthClaims

    pool, _ = _make_mock_pool(fetchrow_results, fetch_result)
    claims = MagicMock(spec=AuthClaims)

    with patch("src.budget.routes.get_pool", return_value=pool):
        return await get_budget(str(USER_ID), claims)


# ---------------------------------------------------------------------------
# Test 1: Both sources present
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_budget_sandbox_and_workflow():
    """Both sandbox and workflow rows → all four metric fields populated."""
    usage_rows = _build_usage_rows(
        sandbox_tokens=5_000, sandbox_cost=0.0250,
        workflow_tokens=20_000,
    )
    resp = await _call_get_budget(
        fetchrow_results=[
            _make_budget_row(),
            _make_topup_row(),
            _make_updated_at_row(),
        ],
        fetch_result=usage_rows,
    )

    assert resp["sandboxTokensUsed"] == 5_000
    assert resp["sandboxUsedEur"] == pytest.approx(0.0250, abs=1e-4)
    assert resp["workflowTokensUsed"] == 20_000
    assert resp["monthlyTokensUsed"] == 25_000


# ---------------------------------------------------------------------------
# Test 2: No usage_events rows at all → zeros
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_budget_no_usage_rows():
    """Empty usage_events → all metrics are 0, no crash."""
    resp = await _call_get_budget(
        fetchrow_results=[
            _make_budget_row(),
            _make_topup_row(),
            _make_updated_at_row(),
        ],
        fetch_result=[],
    )

    assert resp["sandboxTokensUsed"] == 0
    assert resp["sandboxUsedEur"] == 0.0
    assert resp["workflowTokensUsed"] == 0
    assert resp["monthlyTokensUsed"] == 0


# ---------------------------------------------------------------------------
# Test 3: Sandbox only
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_budget_sandbox_only():
    """Only sandbox rows → workflowTokensUsed stays 0."""
    usage_rows = _build_usage_rows(sandbox_tokens=3_000, sandbox_cost=0.015, workflow_tokens=0)
    resp = await _call_get_budget(
        fetchrow_results=[
            _make_budget_row(),
            _make_topup_row(),
            _make_updated_at_row(),
        ],
        fetch_result=usage_rows,
    )

    assert resp["sandboxTokensUsed"] == 3_000
    assert resp["workflowTokensUsed"] == 0
    assert resp["monthlyTokensUsed"] == 3_000


# ---------------------------------------------------------------------------
# Test 4: Workflow only
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_budget_workflow_only():
    """Only workflow rows → sandbox fields are 0."""
    usage_rows = _build_usage_rows(sandbox_tokens=0, sandbox_cost=0.0, workflow_tokens=10_000)
    resp = await _call_get_budget(
        fetchrow_results=[
            _make_budget_row(),
            _make_topup_row(),
            _make_updated_at_row(),
        ],
        fetch_result=usage_rows,
    )

    assert resp["sandboxTokensUsed"] == 0
    assert resp["sandboxUsedEur"] == 0.0
    assert resp["workflowTokensUsed"] == 10_000
    assert resp["monthlyTokensUsed"] == 10_000


# ---------------------------------------------------------------------------
# Test 5: Existing fields still present
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_budget_existing_fields_intact():
    """New fields don't break existing response shape."""
    from datetime import timedelta

    future = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()
    monthly = {"trial": {"limitEur": 5.0, "usedEur": 1.5, "resetAt": future}}

    resp = await _call_get_budget(
        fetchrow_results=[
            _make_budget_row(monthly),
            _make_topup_row(balance=10.0),
            _make_updated_at_row(),
        ],
        fetch_result=[],
    )

    assert resp["userId"] == str(USER_ID)
    assert "monthlyBudgets" in resp
    assert resp["topUpBalanceEur"] == pytest.approx(10.0)
    assert "updatedAt" in resp
    # New fields present
    assert "monthlyTokensUsed" in resp
    assert "sandboxUsedEur" in resp
    assert "sandboxTokensUsed" in resp
    assert "workflowTokensUsed" in resp
