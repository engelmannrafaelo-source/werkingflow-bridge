"""
Tests für project_credits_service — Slot-Zähler für Projekt-Credits.

Coverage:
- get_available_credits: happy path, no rows (0)
- list_user_credits: grouped totals, empty
- consume_credit: happy path (decrements used), CreditsExhaustedError, FIFO order, SELECT FOR UPDATE
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import os
os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest

from src.billing.project_credits_service import (
    CreditsExhaustedError,
    consume_credit,
    get_available_credits,
    list_user_credits,
)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _mock_pool(fetchrow_result=None, fetch_result=None):
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetch = AsyncMock(return_value=fetch_result or [])

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


def _credit_row(**overrides):
    credit_id = uuid.uuid4()
    order_id = uuid.uuid4()
    defaults = {
        "id": credit_id,
        "plan_id": "energy-project",
        "quantity": 1,
        "used": 0,
        "granted_at": datetime.now(timezone.utc),
        "order_id": order_id,
    }
    defaults.update(overrides)
    row = MagicMock()
    row.__getitem__ = lambda self, k: defaults[k]
    return row


def _agg_row(plan_id="energy-project", total=1, used=0, available=1):
    row = MagicMock()
    row.__getitem__ = lambda self, k: {
        "plan_id": plan_id, "total": total, "used": used, "available": available
    }[k]
    return row


# ---------------------------------------------------------------------------
# get_available_credits
# ---------------------------------------------------------------------------

class TestGetAvailableCredits:
    async def test_returns_sum_of_available(self):
        user_id = uuid.uuid4()
        row = MagicMock()
        row.__getitem__ = lambda self, k: 3 if k == "available" else None

        pool, _ = _mock_pool(fetchrow_result=row)
        with patch("src.billing.project_credits_service.get_pool", return_value=pool):
            result = await get_available_credits(user_id, "energy-project")
        assert result == 3

    async def test_returns_zero_when_coalesce_returns_zero(self):
        user_id = uuid.uuid4()
        row = MagicMock()
        row.__getitem__ = lambda self, k: 0 if k == "available" else None

        pool, _ = _mock_pool(fetchrow_result=row)
        with patch("src.billing.project_credits_service.get_pool", return_value=pool):
            result = await get_available_credits(user_id, "energy-project")
        assert result == 0

    async def test_returns_zero_when_no_row(self):
        user_id = uuid.uuid4()
        pool, _ = _mock_pool(fetchrow_result=None)
        with patch("src.billing.project_credits_service.get_pool", return_value=pool):
            result = await get_available_credits(user_id, "energy-project")
        assert result == 0


# ---------------------------------------------------------------------------
# list_user_credits
# ---------------------------------------------------------------------------

class TestListUserCredits:
    async def test_returns_grouped_totals(self):
        user_id = uuid.uuid4()
        pool, _ = _mock_pool(fetch_result=[_agg_row(total=2, used=1, available=1)])
        with patch("src.billing.project_credits_service.get_pool", return_value=pool):
            result = await list_user_credits(user_id)

        assert len(result) == 1
        assert result[0]["planId"] == "energy-project"
        assert result[0]["available"] == 1
        assert result[0]["total"] == 2
        assert result[0]["used"] == 1

    async def test_empty_for_user_without_credits(self):
        user_id = uuid.uuid4()
        pool, _ = _mock_pool(fetch_result=[])
        with patch("src.billing.project_credits_service.get_pool", return_value=pool):
            result = await list_user_credits(user_id)
        assert result == []


# ---------------------------------------------------------------------------
# consume_credit
# ---------------------------------------------------------------------------

class TestConsumeCredit:
    async def test_happy_path_decrements_used(self):
        user_id = uuid.uuid4()
        credit = _credit_row(quantity=2, used=0)

        pool, conn = _mock_pool(fetchrow_result=credit)
        with patch("src.billing.project_credits_service.get_pool", return_value=pool):
            result = await consume_credit(user_id, "energy-project")

        assert result["planId"] == "energy-project"
        assert result["quantityBefore"] == 2
        assert result["usedBefore"] == 0
        conn.execute.assert_called_once()
        update_sql = conn.execute.call_args[0][0]
        assert "used + 1" in update_sql

    async def test_exhausted_raises_credits_exhausted_error(self):
        user_id = uuid.uuid4()
        pool, _ = _mock_pool(fetchrow_result=None)

        with patch("src.billing.project_credits_service.get_pool", return_value=pool):
            with pytest.raises(CreditsExhaustedError) as exc_info:
                await consume_credit(user_id, "energy-project")
        assert exc_info.value.plan_id == "energy-project"

    async def test_fifo_order_oldest_first(self):
        """consume_credit must pick the oldest credit row (ORDER BY granted_at ASC)."""
        user_id = uuid.uuid4()
        credit = _credit_row()
        pool, conn = _mock_pool(fetchrow_result=credit)

        with patch("src.billing.project_credits_service.get_pool", return_value=pool):
            await consume_credit(user_id, "energy-project")

        select_sql = conn.fetchrow.call_args[0][0]
        assert "granted_at ASC" in select_sql

    async def test_uses_select_for_update(self):
        """Transaction safety: SELECT FOR UPDATE must be used."""
        user_id = uuid.uuid4()
        credit = _credit_row()
        pool, conn = _mock_pool(fetchrow_result=credit)

        with patch("src.billing.project_credits_service.get_pool", return_value=pool):
            await consume_credit(user_id, "energy-project")

        select_sql = conn.fetchrow.call_args[0][0]
        assert "FOR UPDATE" in select_sql

    async def test_returns_credit_metadata(self):
        """Returned dict must contain creditId, planId, orderId, grantedAt for audit."""
        user_id = uuid.uuid4()
        credit = _credit_row(quantity=3, used=1)
        pool, _ = _mock_pool(fetchrow_result=credit)

        with patch("src.billing.project_credits_service.get_pool", return_value=pool):
            result = await consume_credit(user_id, "energy-project")

        assert "creditId" in result
        assert "orderId" in result
        assert "grantedAt" in result
        assert result["quantityBefore"] == 3
        assert result["usedBefore"] == 1
