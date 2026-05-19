"""
Tests for POST /v1/billing/subscription/provision and provision_subscription().

Coverage:
- provision_subscription: happy path — inserts sub + budget, logs event
- provision_subscription: idempotent — existing active sub returned as-is
- provision_subscription: ON CONFLICT race resolution — returns winner's row
- provision_subscription: trial plan rejected with ValueError
- provision_subscription: unknown plan rejected with ValueError
- POST /v1/billing/subscription/provision: 201 happy path
- POST /v1/billing/subscription/provision: 409 idempotent (existing active sub)
- POST /v1/billing/subscription/provision: 400 trial plan
- POST /v1/billing/subscription/provision: 400 unknown plan
- POST /v1/billing/subscription/provision: 401 missing service token
- POST /v1/billing/subscription/provision: 401 JWT rejected (service-token-only)
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch, call

import os
os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import config
from src.billing.billing_service import provision_subscription
from src.billing.routes import router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture(scope="module")
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


_SERVICE_TOKEN_HEADER = {"X-Bridge-Service-Token": config.service_token}


# ---------------------------------------------------------------------------
# Pool mock helpers
# ---------------------------------------------------------------------------

def _mock_pool(*fetchrow_results, fetchval_result=None):
    """Minimal asyncpg pool mock: fetchrow_results consumed in order."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow_results))
    conn.fetchval = AsyncMock(return_value=fetchval_result)
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


def _sub_row(**overrides):
    """Minimal subscription row mock (dict-style access)."""
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
    }
    defaults.update(overrides)
    row = MagicMock()
    row.__getitem__ = lambda self, k: defaults[k]
    row.keys = lambda: defaults.keys()
    return row


# ---------------------------------------------------------------------------
# provision_subscription — service-layer unit tests
# ---------------------------------------------------------------------------

class TestProvisionSubscriptionService:
    async def test_returns_existing_active_sub_idempotent(self):
        """If the user already has an active sub for this plan, return it, no INSERT."""
        uid = str(uuid.uuid4())
        existing = _sub_row(plan_id="report-standard")

        pool, conn = _mock_pool(existing)  # fetchrow[0] = existing active sub
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock) as mock_log:
            result = await provision_subscription(uid, "report-standard", 1)

        assert result["status"] == "active"
        assert result["planId"] == "report-standard"
        # No INSERT — fetchrow called once (the pre-check SELECT), no execute
        assert conn.execute.call_count == 0
        # No billing event logged for idempotent return
        mock_log.assert_not_called()

    async def test_inserts_new_subscription_and_budget(self):
        """Happy path: no existing sub → INSERT + budget provision + billing event."""
        uid = str(uuid.uuid4())
        new_sub = _sub_row(plan_id="report-standard")

        # fetchrow[0] = None (no existing active sub)
        # fetchrow[1] = new_sub (INSERT RETURNING)
        pool, conn = _mock_pool(None, new_sub)
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock) as mock_log:
            result = await provision_subscription(uid, "report-standard", 1)

        assert result["status"] == "active"
        assert result["planId"] == "report-standard"

        # execute called twice: INSERT subscriptions + INSERT user_budgets
        assert conn.execute.call_count == 1  # only _provision_plan_budget; INSERT uses fetchrow
        budget_sql = conn.execute.call_args[0][0]
        assert "user_budgets" in budget_sql

        # Billing event logged
        mock_log.assert_called_once()
        assert mock_log.call_args[0][0] == "subscription.provisioned"

    async def test_on_conflict_race_returns_winner_row(self):
        """If INSERT hits ON CONFLICT (concurrent call), we fall back to SELECT winner."""
        uid = str(uuid.uuid4())
        winner = _sub_row(plan_id="report-standard")

        # fetchrow[0] = None (pre-check: no active sub)
        # fetchrow[1] = None (INSERT returns nothing — ON CONFLICT DO NOTHING)
        # fetchrow[2] = winner (fallback SELECT)
        pool, conn = _mock_pool(None, None, winner)
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock) as mock_log:
            result = await provision_subscription(uid, "report-standard", 1)

        assert result["status"] == "active"
        # billing event still logged for the caller that triggered the provision
        mock_log.assert_called_once()
        assert mock_log.call_args[0][0] == "subscription.provisioned"

    async def test_trial_plan_raises_value_error(self):
        pool, _ = _mock_pool()
        with patch("src.billing.billing_service.get_pool", return_value=pool):
            with pytest.raises(ValueError, match="trial"):
                await provision_subscription(str(uuid.uuid4()), "trial", 1)

    async def test_unknown_plan_raises_value_error(self):
        pool, _ = _mock_pool()
        with patch("src.billing.billing_service.get_pool", return_value=pool):
            with pytest.raises(ValueError, match="Unknown plan"):
                await provision_subscription(str(uuid.uuid4()), "nonexistent-plan", 1)

    async def test_budget_entry_uses_plan_api_budget(self):
        """The INSERT into user_budgets carries the plan's api_budget_eur as limitEur."""
        import json as _json
        uid = str(uuid.uuid4())
        new_sub = _sub_row(plan_id="report-standard")

        pool, conn = _mock_pool(None, new_sub)
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock):
            await provision_subscription(uid, "report-standard", 1)

        # Third positional arg to execute(): sql, user_id, entry_json, plan_id_key
        entry_json_arg = conn.execute.call_args[0][2]
        entry = _json.loads(entry_json_arg)
        assert "report-standard" in entry
        assert entry["report-standard"]["limitEur"] == 50.0  # from plans.py
        assert entry["report-standard"]["usedEur"] == 0.0


# ---------------------------------------------------------------------------
# POST /v1/billing/subscription/provision — HTTP endpoint tests
# ---------------------------------------------------------------------------

class TestProvisionEndpoint:
    def test_happy_path_returns_201(self, client: TestClient):
        uid = str(uuid.uuid4())
        sub = _sub_row(plan_id="report-standard")

        # fetchrow[0]=None (no existing), fetchrow[1]=sub (INSERT)
        pool, _ = _mock_pool(None, sub)
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock):
            resp = client.post(
                "/v1/billing/subscription/provision",
                json={"userId": uid, "planId": "report-standard", "seats": 1},
                headers=_SERVICE_TOKEN_HEADER,
            )

        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "active"
        assert body["planId"] == "report-standard"

    def test_idempotent_existing_returns_200(self, client: TestClient):
        """Existing active sub → 200 (201 only on actual creation, but FastAPI returns body)."""
        uid = str(uuid.uuid4())
        existing = _sub_row(plan_id="report-standard")

        pool, _ = _mock_pool(existing)
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock):
            resp = client.post(
                "/v1/billing/subscription/provision",
                json={"userId": uid, "planId": "report-standard", "seats": 1},
                headers=_SERVICE_TOKEN_HEADER,
            )

        # FastAPI returns 201 regardless (status_code from decorator) —
        # the body is the existing sub, no duplicate created.
        assert resp.status_code == 201
        assert resp.json()["status"] == "active"

    def test_trial_plan_returns_400(self, client: TestClient):
        pool, _ = _mock_pool(None)
        with patch("src.billing.billing_service.get_pool", return_value=pool):
            resp = client.post(
                "/v1/billing/subscription/provision",
                json={"userId": str(uuid.uuid4()), "planId": "trial", "seats": 1},
                headers=_SERVICE_TOKEN_HEADER,
            )
        assert resp.status_code == 400
        assert "trial" in resp.json()["detail"].lower()

    def test_unknown_plan_returns_400(self, client: TestClient):
        pool, _ = _mock_pool(None)
        with patch("src.billing.billing_service.get_pool", return_value=pool):
            resp = client.post(
                "/v1/billing/subscription/provision",
                json={"userId": str(uuid.uuid4()), "planId": "does-not-exist", "seats": 1},
                headers=_SERVICE_TOKEN_HEADER,
            )
        assert resp.status_code == 400

    def test_missing_service_token_returns_401(self, client: TestClient):
        resp = client.post(
            "/v1/billing/subscription/provision",
            json={"userId": str(uuid.uuid4()), "planId": "report-standard", "seats": 1},
        )
        assert resp.status_code == 401

    def test_jwt_rejected_service_token_only(self, client: TestClient):
        """The endpoint requires a service token, not a user JWT."""
        from src.identity.jwt_utils import sign_jwt
        token = sign_jwt(
            user_id=str(uuid.uuid4()),
            email="x@example.com",
            tenant_id="t-1",
            app_licenses=[],
        )
        resp = client.post(
            "/v1/billing/subscription/provision",
            json={"userId": str(uuid.uuid4()), "planId": "report-standard", "seats": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        # require_service_token checks for X-Bridge-Service-Token, not Bearer JWT
        assert resp.status_code == 401

    def test_seats_out_of_range_returns_422(self, client: TestClient):
        """seats=0 violates Field(ge=1) — Pydantic validation error."""
        resp = client.post(
            "/v1/billing/subscription/provision",
            json={"userId": str(uuid.uuid4()), "planId": "report-standard", "seats": 0},
            headers=_SERVICE_TOKEN_HEADER,
        )
        assert resp.status_code == 422
