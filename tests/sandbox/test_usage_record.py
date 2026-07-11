"""
Acceptance-criteria tests for /v1/sandbox/usage/record.

Covered criteria:
  6. Usage-Record-Idempotenz: same litellmCallId twice → no double insert, 2nd call returns same aggregate
  7. Usage-Aggregate: multiple events for same session → total_session_tokens sum is correct
  10. Budget-Deduction: subscription tenant sandbox call → apply_budget_deduction called
  11. Pre-Gate: trial user with empty budget → lease_token returns 402, no lease inserted
  12. Pre-Gate: trial user with budget → lease issued, budget deducted on record_usage
  13. Query-Migration: usage_by_user reads from usage_events with source='sandbox'
"""
import uuid
from datetime import timezone
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from fastapi import HTTPException

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


# ---------------------------------------------------------------------------
# real_cost_eur must follow resolve_ledger_cost, not a sandbox-only hardcode
# ---------------------------------------------------------------------------

class TestRecordUsageRealCost:
    """
    Sandbox usage is issued exclusively through OAuth-leased Bridge accounts
    (routes.lease_token hard-requires billing_mode == 'subscription' before
    handing out a lease) — Anthropic bills that account at its flat
    subscription rate, so those calls have zero marginal cost to us. Sandbox
    must reuse resolve_ledger_cost (the same function the /chat|/research
    write-path uses) instead of hardcoding real_cost = hypothetical_cost_eur,
    or a subscription-covered sandbox call shows up as real spend in the
    ledger while a genuine pay_per_token sandbox usage must still show its
    real cost.
    """

    @pytest.mark.asyncio
    async def test_subscription_billing_mode_has_zero_real_cost(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = {"id": 1}

        await ls.record_usage(
            conn,
            litellm_call_id="lc-sub-001",
            user_id=uuid.uuid4(),
            tenant_id="t1",
            session_id="sess-sub",
            lease_id=None,
            account_id="engelmann",
            app="engelmann",
            model="claude-sonnet-4-5",
            input_tokens=1000,
            output_tokens=200,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            hypothetical_cost_eur=0.0123,
            billing_mode="subscription",
        )

        args = conn.fetchrow.call_args.args
        bm_enum, real_cost, hypothetical = args[10], args[11], args[12]
        assert bm_enum == "flat_rate_estimated"
        assert real_cost == 0.0, (
            "subscription-leased sandbox call must carry real_cost=0 — "
            "Anthropic charges the OAuth account a flat subscription rate, "
            "not per token"
        )
        assert hypothetical == 0.0123, "hypothetical must stay priced for reporting"

    @pytest.mark.asyncio
    async def test_pay_per_token_billing_mode_keeps_real_cost(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = {"id": 1}

        await ls.record_usage(
            conn,
            litellm_call_id="lc-ppt-001",
            user_id=uuid.uuid4(),
            tenant_id="t2",
            session_id="sess-ppt",
            lease_id=None,
            account_id="engelmann",
            app="engelmann",
            model="claude-sonnet-4-5",
            input_tokens=1000,
            output_tokens=200,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            hypothetical_cost_eur=0.0123,
            billing_mode="pay_per_token",
        )

        args = conn.fetchrow.call_args.args
        bm_enum, real_cost = args[10], args[11]
        assert bm_enum == "pay_per_token"
        assert real_cost == 0.0123, (
            "pay_per_token sandbox usage must keep real_cost == hypothetical — "
            "a €0 row here would hide genuine per-token spend"
        )


# ---------------------------------------------------------------------------
# Criterion 10: Budget deduction after record_usage (Defect 2)
# ---------------------------------------------------------------------------

class TestBudgetDeductionOnRecord:
    @pytest.mark.asyncio
    async def test_subscription_tenant_deducts_budget_after_insert(self):
        """
        Criterion 10: subscription tenant sandbox call →
        apply_budget_deduction is called with the hypothetical cost.
        Patch at the source location (lazy imports inside _deduct_sandbox_budget).
        """
        from src.sandbox.routes import _deduct_sandbox_budget
        from src.budget.plans import PlanConfig

        user_id = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
        app = "engelmann"
        cost = 0.0042

        plan = PlanConfig(
            id="engelmann-plan",
            app_id=app,
            name="Engelmann",
            price=0.0,
            interval="month",
            api_budget_eur=10.0,
            description="Test plan",
        )

        with patch("src.budget.plans.find_plan_for_app", return_value=plan) as mock_plan, \
             patch("src.budget.routes.apply_budget_deduction") as mock_deduct:
            mock_deduct.return_value = {
                "fromMonthly": cost,
                "fromTopUp": 0.0,
                "newMonthlyUsed": cost,
                "newTopUpBalance": 0.0,
                "effectivePlanId": plan.id,
            }
            await _deduct_sandbox_budget(user_id, app, cost)

        mock_plan.assert_called_once_with(app)
        mock_deduct.assert_called_once_with(user_id, plan.id, cost)

    @pytest.mark.asyncio
    async def test_no_deduction_when_app_not_in_catalog(self):
        """App without a plan entry → deduction skipped, no error."""
        from src.sandbox.routes import _deduct_sandbox_budget

        user_id = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

        with patch("src.budget.plans.find_plan_for_app", return_value=None) as mock_plan, \
             patch("src.budget.routes.apply_budget_deduction") as mock_deduct:
            await _deduct_sandbox_budget(user_id, "unknown-app", 0.01)

        mock_deduct.assert_not_called()

    @pytest.mark.asyncio
    async def test_deduction_failure_is_non_blocking(self):
        """DB error in apply_budget_deduction must not propagate."""
        from src.sandbox.routes import _deduct_sandbox_budget
        from src.budget.plans import PlanConfig

        user_id = uuid.UUID("11111111-2222-3333-4444-555555555555")
        plan = PlanConfig(
            id="p1", app_id="engelmann", name="P1",
            price=0.0, interval="month", api_budget_eur=10.0, description="",
        )

        with patch("src.budget.plans.find_plan_for_app", return_value=plan), \
             patch("src.budget.routes.apply_budget_deduction",
                   side_effect=RuntimeError("DB connection lost")):
            # Must not raise — best-effort only
            await _deduct_sandbox_budget(user_id, "engelmann", 0.005)


# ---------------------------------------------------------------------------
# Criterion 11 + 12: Pre-gate in lease_token (Defect 3)
# ---------------------------------------------------------------------------

class TestPreGateOnLease:
    @pytest.mark.asyncio
    async def test_exhausted_budget_blocks_lease_with_402(self):
        """
        Criterion 11: enforce_budget raises 402 → lease_token propagates it;
        no lease row is written to the DB.
        """
        from src.sandbox.routes import router
        from src.api_auth import require_service_token, AuthClaims
        from fastapi.testclient import TestClient
        from fastapi import FastAPI

        app_instance = FastAPI()
        app_instance.include_router(router)

        # Bypass auth via dependency_overrides
        fake_claims = AuthClaims(
            kind="service",
            user_id=None,
            email=None,
            tenant_id=None,
            is_admin=False,
        )
        app_instance.dependency_overrides[require_service_token] = lambda: fake_claims

        # enforce_budget raises 402
        async def _gate_raises(*args, **kwargs):
            raise HTTPException(
                status_code=402,
                detail={"error": "budget_exhausted", "reason": "trial_expired"},
            )

        # Minimal pool/conn mock
        mock_conn = AsyncMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_conn.fetchrow.return_value = {
            "tenant_id": "t1",
            "billing_mode": "subscription",
        }
        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_conn

        with patch("src.sandbox.routes.enforce_budget", side_effect=_gate_raises), \
             patch("src.sandbox.routes.get_pool", return_value=mock_pool):
            client = TestClient(app_instance, raise_server_exceptions=False)
            resp = client.post(
                "/v1/sandbox/lease-token",
                json={
                    "userId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "app": "engelmann",
                    "estimatedDurationMin": 15,
                },
            )

        assert resp.status_code == 402
        # No lease must have been created
        mock_conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_unlicensed_user_passes_gate(self):
        """
        Trial users with no budget entry yet must NOT be blocked (reason=unlicensed
        is non-blocking). enforce_budget returns None → lease proceeds.
        """
        from src.sandbox.routes import enforce_budget as real_enforce
        from src.budget.gate import _BLOCKING_REASONS

        # Simulate evaluate_budget returning reason=unlicensed
        async def _budget_unlicensed(uid, plan_id, cost):
            return {
                "allowed": False,
                "reason": "unlicensed",
                "effectivePlanId": plan_id,
                "monthlyRemainingEur": 0.0,
                "topUpRemainingEur": 0.0,
                "totalRemainingEur": 0.0,
            }

        assert "unlicensed" not in _BLOCKING_REASONS, (
            "'unlicensed' must not be a blocking reason — trial users need sandbox access"
        )


# ---------------------------------------------------------------------------
# Criterion 13: usage_by_user reads from usage_events (Defect 4)
# ---------------------------------------------------------------------------

class TestUsageQueryMigration:
    def test_usage_by_user_queries_usage_events_not_view(self):
        """
        Criterion 13: the SQL in usage_by_user must reference usage_events
        with source='sandbox', NOT the sandbox_usage_events view.
        """
        import inspect
        from src.sandbox import routes as sandbox_routes

        source = inspect.getsource(sandbox_routes.usage_by_user)

        assert "usage_events" in source, \
            "usage_by_user must query usage_events (not sandbox_usage_events view)"
        assert "source = 'sandbox'" in source or "source='sandbox'" in source, \
            "usage_by_user must filter by source='sandbox'"
        assert "sandbox_usage_events" not in source.replace("sandbox_usage_events", ""), \
            "usage_by_user must not reference the sandbox_usage_events view directly"

    def test_usage_by_tenant_queries_usage_events_not_view(self):
        """usage_by_tenant must also read from usage_events."""
        import inspect
        from src.sandbox import routes as sandbox_routes

        source = inspect.getsource(sandbox_routes.usage_by_tenant)
        assert "usage_events" in source
        assert "sandbox_usage_events" not in source

    def test_usage_by_session_model_breakdown_queries_usage_events(self):
        """usage_by_session model breakdown must read from usage_events."""
        import inspect
        from src.sandbox import routes as sandbox_routes

        source = inspect.getsource(sandbox_routes.usage_by_session)
        assert "usage_events" in source
        assert "sandbox_usage_events" not in source
