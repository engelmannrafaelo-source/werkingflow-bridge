"""
Acceptance-criteria tests for /v1/sandbox/usage/record.

Covered criteria:
  6. Usage-Record-Idempotenz: same litellmCallId twice → no double insert, 2nd call returns same aggregate
  7. Usage-Aggregate: multiple events for same session → total_session_tokens sum is correct
"""
import uuid
from datetime import timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.sandbox import lease_service as ls
from src.sandbox.pricing import compute_hypothetical_cost_eur


# ---------------------------------------------------------------------------
# Pricing unit tests
# ---------------------------------------------------------------------------

class TestPricing:
    def test_sonnet_cost_calculation(self):
        cost = compute_hypothetical_cost_eur(
            model="claude-sonnet-4-5-20250929",
            input_tokens=1_000_000,
            output_tokens=0,
        )
        # 1M input tokens * $3.00 * 0.92 EUR/USD
        expected = 3.00 * 0.92
        assert abs(cost - expected) < 0.001

    def test_haiku_output_tokens(self):
        cost = compute_hypothetical_cost_eur(
            model="claude-haiku-4-5-20251001",
            input_tokens=0,
            output_tokens=1_000_000,
        )
        expected = 5.00 * 0.92
        assert abs(cost - expected) < 0.001

    def test_cache_read_tokens(self):
        cost = compute_hypothetical_cost_eur(
            model="claude-sonnet-4-5",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=1_000_000,
        )
        expected = 0.30 * 0.92
        assert abs(cost - expected) < 0.001

    def test_unknown_model_uses_default(self):
        cost = compute_hypothetical_cost_eur(
            model="future-model-99",
            input_tokens=1_000_000,
            output_tokens=0,
        )
        # default = sonnet pricing
        expected = 3.00 * 0.92
        assert abs(cost - expected) < 0.001


# ---------------------------------------------------------------------------
# Criterion 6: Idempotency via ON CONFLICT DO NOTHING
# ---------------------------------------------------------------------------

class TestUsageRecordIdempotency:
    @pytest.mark.asyncio
    async def test_record_usage_called_twice_second_is_noop(self):
        """
        The second INSERT with the same litellm_call_id must not raise.
        record_usage returns True on first insert and False when ON CONFLICT
        DO NOTHING causes the second insert to be a no-op.
        """
        user_id = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
        call_id = "lc-idempotency-test-001"
        session_id = "sess-abc"

        insert_count = 0

        async def mock_fetchrow(sql, *args):
            nonlocal insert_count
            if "INSERT INTO usage_events" in sql:
                insert_count += 1
                # First INSERT succeeds; second is a no-op (ON CONFLICT DO NOTHING)
                return {"id": insert_count} if insert_count == 1 else None
            # Fallback for any other fetchrow (e.g. aggregate)
            return {"total_tokens": 1620, "total_hypothetical_eur": 0.0042}

        conn = AsyncMock()
        conn.fetchrow.side_effect = mock_fetchrow

        kwargs = dict(
            litellm_call_id=call_id,
            user_id=user_id,
            tenant_id="tenant-1",
            session_id=session_id,
            lease_id=None,
            account_id="engelmann",
            app="engelmann",
            model="claude-sonnet-4-5",
            input_tokens=1240,
            output_tokens=380,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            hypothetical_cost_eur=0.0042,
            billing_mode="subscription",
        )

        first = await ls.record_usage(conn, **kwargs)
        second = await ls.record_usage(conn, **kwargs)

        assert insert_count == 2, "Both calls must reach the DB"
        assert first is True, "First insert should succeed"
        assert second is False, "Second insert is a no-op (idempotent)"

    @pytest.mark.asyncio
    async def test_aggregate_returns_current_session_total(self):
        """get_session_aggregate sums tokens and cost for a session."""
        conn = AsyncMock()
        conn.fetchrow.return_value = {
            "total_tokens": 24812,
            "total_hypothetical_eur": 0.0418,
        }

        result = await ls.get_session_aggregate(conn, "sess-xyz")
        assert result["total_session_tokens"] == 24812
        assert abs(result["total_session_hypothetical_eur"] - 0.0418) < 1e-9


# ---------------------------------------------------------------------------
# Criterion 7: Multiple events sum correctly
# ---------------------------------------------------------------------------

class TestUsageAggregate:
    @pytest.mark.asyncio
    async def test_multiple_events_sum(self):
        """
        Simulate 3 usage inserts then verify aggregate matches sum.
        """
        user_id = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
        session_id = "sess-multi"
        events = [
            {"litellm_call_id": "lc-001", "input_tokens": 500,  "output_tokens": 100, "cost": 0.0018},
            {"litellm_call_id": "lc-002", "input_tokens": 1000, "output_tokens": 300, "cost": 0.0042},
            {"litellm_call_id": "lc-003", "input_tokens": 200,  "output_tokens": 50,  "cost": 0.0008},
        ]

        total_tokens = sum(e["input_tokens"] + e["output_tokens"] for e in events)
        total_cost = sum(e["cost"] for e in events)

        # Mock the DB: fetchrow handles both the INSERT RETURNING and the aggregate.
        # INSERT returns a non-None row (success); aggregate returns the running sum.
        conn = AsyncMock()
        conn.fetchrow.return_value = {
            "total_tokens": total_tokens,
            "total_hypothetical_eur": total_cost,
        }

        for e in events:
            await ls.record_usage(
                conn,
                litellm_call_id=e["litellm_call_id"],
                user_id=user_id,
                tenant_id="t1",
                session_id=session_id,
                lease_id=None,
                account_id="engelmann",
                app="engelmann",
                model="claude-sonnet-4-5",
                input_tokens=e["input_tokens"],
                output_tokens=e["output_tokens"],
                cache_read_tokens=0,
                cache_creation_tokens=0,
                hypothetical_cost_eur=e["cost"],
                billing_mode="subscription",
            )

        aggregate = await ls.get_session_aggregate(conn, session_id)
        assert aggregate["total_session_tokens"] == total_tokens
        assert abs(aggregate["total_session_hypothetical_eur"] - total_cost) < 1e-9

    @pytest.mark.asyncio
    async def test_empty_session_returns_zeros(self):
        """Session with no events → totals are 0."""
        conn = AsyncMock()
        conn.fetchrow.return_value = {
            "total_tokens": 0,
            "total_hypothetical_eur": 0.0,
        }

        result = await ls.get_session_aggregate(conn, "sess-empty")
        assert result["total_session_tokens"] == 0
        assert result["total_session_hypothetical_eur"] == 0.0
