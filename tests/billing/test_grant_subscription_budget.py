"""
Tests for grant_subscription() — budget pot + audit trail.

Until 2026-08-26 the admin grant path created subscription + license but neither
a monthly budget pot nor a billing event. The result was access on paper only:
login 200, /dashboard 200, and the app still showing "Kein KI-Guthaben" because
the pot every AI call is metered against did not exist. It was found on a demo
account whose grant had also left no trace in the audit trail.

Coverage:
- paid monthly plan → budget pot provisioned + 'subscription.granted' logged
- project-interval plan (energy-project) → NO monthly pot (project_budgets_service owns it)
- trial plan → NO monthly pot (reset_at is the expiry; evaluate_budget provisions it)
- existing active sub → self-heals a missing pot, returns created=False, logs nothing
- unknown plan → ValueError (a plan we cannot price is one we cannot budget)
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

from src.billing.billing_service import grant_subscription
from src.budget.plans import PLANS, PlanConfig


# The catalog is a runtime cache filled from the plans table at Bridge startup;
# unit tests seed it directly so they need no database. Mirrors the rows of
# migration 020_plans_table.sql.
_TEST_PLANS = {
    "trial": PlanConfig("trial", "werking-report", "7-Tage-Test", 0, "month", 5,
                        "", trial=True),
    "report-standard": PlanConfig("report-standard", "werking-report", "Standard",
                                  250, "month", 50, ""),
    "energy-project": PlanConfig("energy-project", "werking-energy", "Energy-Projekt",
                                 1000, "project", 100, ""),
}


@pytest.fixture(autouse=True)
def _seeded_plan_catalog():
    original = dict(PLANS)
    PLANS.clear()
    PLANS.update(_TEST_PLANS)
    yield
    PLANS.clear()
    PLANS.update(original)


def _mock_pool(*fetchrow_results):
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow_results))

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


def _sub_row(**overrides):
    uid = uuid.uuid4()
    defaults = {
        "id": uuid.uuid4(),
        "user_id": uid,
        "app_id": "werking-report",
        "plan_id": "report-standard",
        "status": "active",
        "mollie_customer_id": f"seed-{uid}",
        "mollie_subscription_id": None,
        "seats": 1,
        "started_at": datetime.now(timezone.utc),
        "cancelled_at": None,
        "suspended_at": None,
        "expired_at": None,
        "trial_ends_at": None,
    }
    defaults.update(overrides)
    row = MagicMock()
    row.__getitem__ = lambda self, k: defaults[k]
    row.keys = lambda: defaults.keys()
    return row


def _budget_sql(conn):
    return [c[0][0] for c in conn.execute.call_args_list if "user_budgets" in c[0][0]]


class TestGrantProvisionsBudget:
    async def test_paid_monthly_plan_gets_pot_and_audit_event(self):
        """The regression this file exists for: pot written, grant auditable."""
        uid = str(uuid.uuid4())
        pool, conn = _mock_pool(None, _sub_row(plan_id="report-standard"))

        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event",
                   new_callable=AsyncMock) as mock_log:
            result, created = await grant_subscription(
                uid, "werking-report", "report-standard", 1, granted_by="operator",
            )

        assert created is True
        assert result["status"] == "active"
        assert len(_budget_sql(conn)) == 1

        mock_log.assert_called_once()
        assert mock_log.call_args[0][0] == "subscription.granted"
        assert mock_log.call_args[1]["source"] == "admin"
        assert mock_log.call_args[1]["payload"]["planId"] == "report-standard"
        assert mock_log.call_args[1]["payload"]["grantedBy"] == "operator"

    async def test_project_plan_gets_no_monthly_pot(self):
        """energy-project is billed per project — a monthly lane would never be read."""
        uid = str(uuid.uuid4())
        pool, conn = _mock_pool(
            None, _sub_row(app_id="werking-energy", plan_id="energy-project"),
        )

        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event",
                   new_callable=AsyncMock) as mock_log:
            _, created = await grant_subscription(
                uid, "werking-energy", "energy-project", 1,
            )

        assert created is True
        assert _budget_sql(conn) == []
        mock_log.assert_called_once()

    async def test_trial_plan_gets_no_monthly_pot(self):
        """A trial's reset_at IS its expiry — writing a 30-day anchor here would
        silently stretch the trial. evaluate_budget provisions trials itself."""
        uid = str(uuid.uuid4())
        pool, conn = _mock_pool(None, _sub_row(plan_id="trial"))

        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event",
                   new_callable=AsyncMock):
            await grant_subscription(uid, "werking-report", "trial", 1)

        assert _budget_sql(conn) == []

    async def test_existing_sub_self_heals_missing_pot_without_event(self):
        """Re-granting repairs the pot of an already-active subscription — that is
        how grants issued before the fix get healed — but creates no new row and
        therefore no audit event."""
        uid = str(uuid.uuid4())
        pool, conn = _mock_pool(_sub_row(plan_id="report-standard"))

        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event",
                   new_callable=AsyncMock) as mock_log:
            _, created = await grant_subscription(
                uid, "werking-report", "report-standard", 1,
            )

        assert created is False
        assert len(_budget_sql(conn)) == 1
        mock_log.assert_not_called()

    async def test_unknown_plan_raises(self):
        """safety-project still sits in the DB enum but not in the active catalog.
        Granting it now fails loudly instead of minting a subscription that could
        never serve an AI call."""
        uid = str(uuid.uuid4())
        pool, _ = _mock_pool(None, _sub_row(plan_id="safety-project"))

        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event",
                   new_callable=AsyncMock):
            with pytest.raises(ValueError, match="Unknown plan"):
                await grant_subscription(uid, "werking-safety", "safety-project", 1)
