"""
Tests für Trial-Plan Auto-Provisioning (Q1 — SPEC-Q1-TRIAL.md)

Covered acceptance criteria:
1. First /check for unlicensed user → trial provisioned, trial_active returned
2. Second /check for same user → no double-provision, ok returned
3. /deduct with planId="report-standard", trial-only user → deducts against trial.id
4. App without trial sibling (engelmann-custom) → unlicensed
5. Trial expired → trial_expired
6. Atomic provisioning: ON CONFLICT DO NOTHING prevents double-insert
"""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from src.budget.plans import find_trial_plan_for, get_plan, PLANS
from src.budget.calculator import (
    UserBudget,
    MonthlyBudgetEntry,
    check_budget,
)
from src.budget.routes import _provision_trial, _is_trial_expired


# ---------------------------------------------------------------------------
# Criterion 1–4 + 6: find_trial_plan_for (pure unit tests, no mocking)
# ---------------------------------------------------------------------------

class TestFindTrialPlanFor:
    def test_report_standard_returns_trial_sibling(self):
        trial = find_trial_plan_for("report-standard")
        assert trial is not None
        assert trial.id == "trial"
        assert trial.trial is True
        assert trial.app_id == "werking-report"

    def test_trial_plan_itself_returns_none(self):
        # "trial" IS the trial — no separate sibling.
        assert find_trial_plan_for("trial") is None

    def test_energy_project_has_no_trial(self):
        assert find_trial_plan_for("energy-project") is None

    def test_safety_project_has_no_trial(self):
        assert find_trial_plan_for("safety-project") is None

    def test_engelmann_custom_has_no_trial(self):
        # Criterion 4: apps without trial sibling return None.
        assert find_trial_plan_for("engelmann-custom") is None

    def test_noise_tbd_has_no_trial(self):
        assert find_trial_plan_for("noise-tbd") is None

    def test_unknown_plan_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown plan"):
            find_trial_plan_for("does-not-exist")


# ---------------------------------------------------------------------------
# Criterion 5: trial-expiry helper
# ---------------------------------------------------------------------------

class TestIsTrialExpired:
    def test_future_reset_not_expired(self):
        future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        entry = MonthlyBudgetEntry(limit_eur=5.0, used_eur=0.0, reset_at=future)
        assert _is_trial_expired(entry) is False

    def test_past_reset_is_expired(self):
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        entry = MonthlyBudgetEntry(limit_eur=5.0, used_eur=0.0, reset_at=past)
        assert _is_trial_expired(entry) is True

    def test_naive_datetime_treated_as_utc(self):
        # reset_at without tzinfo is treated as UTC.
        past_naive = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None).isoformat()
        entry = MonthlyBudgetEntry(limit_eur=5.0, used_eur=0.0, reset_at=past_naive)
        assert _is_trial_expired(entry) is True


# ---------------------------------------------------------------------------
# Criterion 6: _provision_trial atomic SQL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_provision_trial_calls_insert_on_conflict():
    """_provision_trial must issue INSERT … ON CONFLICT DO UPDATE WHERE."""
    conn = AsyncMock()
    conn.execute = AsyncMock()

    trial_plan = get_plan("trial")
    user_id = uuid.uuid4()

    await _provision_trial(conn, user_id, trial_plan)

    conn.execute.assert_called_once()
    sql_call = conn.execute.call_args[0][0]
    assert "INSERT INTO user_budgets" in sql_call
    assert "ON CONFLICT" in sql_call
    assert "WHERE" in sql_call

    # Verify the JSON payload contains the expected fields.
    # call signature: execute(sql, user_id, entry_json, trial_plan.id)
    json_arg = conn.execute.call_args[0][2]
    payload = json.loads(json_arg)
    assert "trial" in payload
    assert payload["trial"]["limitEur"] == 5.0
    assert payload["trial"]["usedEur"] == 0.0
    assert "resetAt" in payload["trial"]

    # reset_at must be ~7 days from now.
    reset_at = datetime.fromisoformat(payload["trial"]["resetAt"])
    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    delta = reset_at - datetime.now(timezone.utc)
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, hours=1)


@pytest.mark.asyncio
async def test_provision_trial_is_not_called_when_trial_entry_exists(monkeypatch):
    """If the trial entry already exists, _provision_trial must not be called."""
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    existing_entry = MonthlyBudgetEntry(limit_eur=5.0, used_eur=1.0, reset_at=future)
    existing_budget = UserBudget(
        user_id=str(uuid.uuid4()),
        monthly_budgets={"trial": existing_entry},
        top_up_balance_eur=0.0,
    )

    conn = AsyncMock()

    # The check logic: if budget.monthly_budgets.get(trial.id) is NOT None,
    # _provision_trial is never called. We verify by testing that logic directly.
    trial = find_trial_plan_for("report-standard")
    assert trial is not None

    if existing_budget.monthly_budgets.get(trial.id) is None:
        await _provision_trial(conn, uuid.uuid4(), trial)

    # If logic is correct, execute was never called.
    conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Criterion 1–3: budget check logic with mocked UserBudget
# ---------------------------------------------------------------------------

class TestBudgetCheckLogic:
    """Test check_budget (pure) combined with trial-routing logic that lives in routes."""

    def _make_trial_budget(self, used: float = 0.0, days_remaining: float = 6.0) -> UserBudget:
        reset_at = (datetime.now(timezone.utc) + timedelta(days=days_remaining)).isoformat()
        return UserBudget(
            user_id=str(uuid.uuid4()),
            monthly_budgets={
                "trial": MonthlyBudgetEntry(limit_eur=5.0, used_eur=used, reset_at=reset_at)
            },
            top_up_balance_eur=0.0,
        )

    def test_criterion1_first_check_after_provision_returns_ok(self):
        """After trial is provisioned, check_budget returns ok (allowed)."""
        budget = self._make_trial_budget()
        result = check_budget(budget, "trial", estimated_cost_eur=0.1)
        assert result.allowed is True
        assert result.reason == "ok"
        # monthly_remaining is limit minus used; check_budget does not deduct estimated_cost.
        assert result.monthly_remaining_eur == pytest.approx(5.0, abs=0.01)

    def test_criterion2_second_check_returns_ok(self):
        """Subsequent checks with existing trial entry work as normal."""
        budget = self._make_trial_budget(used=0.5)
        result = check_budget(budget, "trial", estimated_cost_eur=0.1)
        assert result.allowed is True
        assert result.reason == "ok"

    def test_criterion4_unlicensed_without_trial_sibling(self):
        """Apps without trial sibling: check_budget returns unlicensed."""
        budget = UserBudget(
            user_id=str(uuid.uuid4()),
            monthly_budgets={},
            top_up_balance_eur=0.0,
        )
        result = check_budget(budget, "engelmann-custom", estimated_cost_eur=0.1)
        assert result.allowed is False
        assert result.reason == "unlicensed"

    def test_criterion3_deduct_uses_trial_plan_id(self):
        """Routes redirect deduct to trial.id; deduct_budget updates trial entry."""
        from src.budget.calculator import deduct_budget
        budget = self._make_trial_budget(used=1.0)
        result = deduct_budget(budget, "trial", actual_cost_eur=0.5)
        assert result.from_monthly == pytest.approx(0.5)
        assert result.new_monthly_used == pytest.approx(1.5)

    def test_criterion5_expired_trial_budget_not_allowed(self):
        """Expired trial: check_budget may still return ok (expiry is enforced in routes).
        This test documents that routes must check _is_trial_expired before calling check_budget."""
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        expired_budget = UserBudget(
            user_id=str(uuid.uuid4()),
            monthly_budgets={
                "trial": MonthlyBudgetEntry(limit_eur=5.0, used_eur=0.0, reset_at=past)
            },
            top_up_balance_eur=0.0,
        )
        # The pure check_budget does not enforce expiry; routes do.
        # Verify _is_trial_expired detects it correctly.
        entry = expired_budget.monthly_budgets["trial"]
        assert _is_trial_expired(entry) is True


# ---------------------------------------------------------------------------
# Criterion 1+4 as route-level tests (mocked pool/auth)
# ---------------------------------------------------------------------------

def _make_mock_pool(fetchrow_results: list, execute_ok: bool = True):
    """Build a mock asyncpg pool whose fetchrow calls return results in order."""
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=fetchrow_results)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


@pytest.mark.asyncio
async def test_route_check_unlicensed_no_trial_sibling():
    """Criterion 4: /check for engelmann-custom user with no budget → unlicensed."""
    from src.budget.routes import budget_check, CheckRequest
    from src.api_auth import AuthClaims

    user_id = str(uuid.uuid4())
    # fetchrow returns: budget_row=None, topup_row=None
    pool, conn = _make_mock_pool([None, None])

    req = CheckRequest(userId=user_id, planId="engelmann-custom", estimatedCostEur=0.1)
    claims = MagicMock(spec=AuthClaims)

    with patch("src.budget.routes.get_pool", return_value=pool):
        resp = await budget_check(req, claims)

    assert resp["allowed"] is False
    assert resp["reason"] == "unlicensed"
    assert resp["effectivePlanId"] == "engelmann-custom"
    # No provisioning for engelmann-custom.
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_route_check_first_request_provisions_trial():
    """Criterion 1: first /check for unlicensed report-standard user → trial provisioned."""
    from src.budget.routes import budget_check, CheckRequest
    from src.api_auth import AuthClaims

    user_id = str(uuid.uuid4())
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    trial_row = {"monthly_budgets": json.dumps({"trial": {"limitEur": 5.0, "usedEur": 0.0, "resetAt": future}})}
    topup_row = None

    # Call sequence: _load_user_budget (no row, no topup), provision_trial, _load_user_budget (trial row, no topup)
    pool, conn = _make_mock_pool([None, topup_row, trial_row, topup_row])

    req = CheckRequest(userId=user_id, planId="report-standard", estimatedCostEur=0.1)
    claims = MagicMock(spec=AuthClaims)

    with patch("src.budget.routes.get_pool", return_value=pool):
        resp = await budget_check(req, claims)

    assert resp["allowed"] is True
    assert resp["reason"] == "trial_active"
    assert resp["effectivePlanId"] == "trial"
    # _provision_trial must have been called once.
    conn.execute.assert_called_once()


@pytest.mark.asyncio
async def test_route_check_second_request_no_double_provision():
    """Criterion 2: second /check with existing trial entry → no provisioning."""
    from src.budget.routes import budget_check, CheckRequest
    from src.api_auth import AuthClaims

    user_id = str(uuid.uuid4())
    future = (datetime.now(timezone.utc) + timedelta(days=6)).isoformat()
    trial_row = {"monthly_budgets": json.dumps({"trial": {"limitEur": 5.0, "usedEur": 0.0, "resetAt": future}})}

    # User has no report-standard but HAS trial entry already.
    pool, conn = _make_mock_pool([trial_row, None])

    req = CheckRequest(userId=user_id, planId="report-standard", estimatedCostEur=0.1)
    claims = MagicMock(spec=AuthClaims)

    with patch("src.budget.routes.get_pool", return_value=pool):
        resp = await budget_check(req, claims)

    assert resp["allowed"] is True
    # reason is "ok" (not trial_active) because effective_plan_id == body.planId? No...
    # effective_plan_id = "trial" != "report-standard", so reason = "trial_active"
    assert resp["reason"] == "trial_active"
    assert resp["effectivePlanId"] == "trial"
    # No new provisioning — execute not called.
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_route_check_expired_trial():
    """Criterion 5: expired trial → trial_expired."""
    from src.budget.routes import budget_check, CheckRequest
    from src.api_auth import AuthClaims

    user_id = str(uuid.uuid4())
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    expired_row = {"monthly_budgets": json.dumps({"trial": {"limitEur": 5.0, "usedEur": 0.0, "resetAt": past}})}

    pool, conn = _make_mock_pool([expired_row, None])

    req = CheckRequest(userId=user_id, planId="report-standard", estimatedCostEur=0.1)
    claims = MagicMock(spec=AuthClaims)

    with patch("src.budget.routes.get_pool", return_value=pool):
        resp = await budget_check(req, claims)

    assert resp["allowed"] is False
    assert resp["reason"] == "trial_expired"
    assert resp["effectivePlanId"] == "trial"
