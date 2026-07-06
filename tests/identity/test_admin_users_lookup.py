"""
Tests for admin lookup-by-email + app_license assign endpoints.

Endpoints under test (src/db/admin_routes.py):
- GET  /v1/admin/users/lookup?email=...
- POST /v1/admin/users/{user_id}/app-licenses

Both are operator-only via require_admin. The drift-correction flow for the
app-side seed-users routes:
    register → 409 → lookup → optional license-assign.
"""
from __future__ import annotations

import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

# asyncpg is a C extension not present in the unit-test image; the admin_routes
# module imports it, so stub it to a no-op MagicMock with the exception types
# it references. Same pattern as test_role_support.py.
try:
    import asyncpg  # noqa: F401
except ImportError:
    _asyncpg_stub = MagicMock()
    _asyncpg_stub.UniqueViolationError = type("UniqueViolationError", (Exception,), {})
    _asyncpg_stub.ForeignKeyViolationError = type("ForeignKeyViolationError", (Exception,), {})
    _asyncpg_stub.PostgresError = type("PostgresError", (Exception,), {})
    sys.modules["asyncpg"] = _asyncpg_stub

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api_auth import require_admin
from src.api_auth.deps import AuthClaims
from src.db.admin_routes import router as admin_router


# ---------------------------------------------------------------------------
# Fixtures: app with overridable auth dep + pool-mock factory
# ---------------------------------------------------------------------------

def _operator_claims() -> AuthClaims:
    """Service-token without X-User-ID → operator (is_operator=True)."""
    return AuthClaims(
        kind="service", user_id=None, email=None, tenant_id=None,
        is_admin=True, acting_user_id=None,
    )


@pytest.fixture(autouse=True)
def _stub_list_subscriptions():
    """lookup now derives `entitlements` via billing_service.list_subscriptions
    (lazy-expiring SSoT read). Default to "no subscriptions" so every existing
    test keeps its focus; entitlement-specific tests override the mock."""
    with patch(
        "src.db.admin_routes.billing_service.list_subscriptions",
        new=AsyncMock(return_value=[]),
    ) as mock:
        yield mock


def _make_app(claims: AuthClaims) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[require_admin] = lambda: claims
    return app


def _make_pool(*, fetchrow_results=(), fetch_result=(), fetchval_result=None):
    """
    Build an asyncpg pool/conn mock.

    fetchrow_results consumed in order as side_effect.
    fetch_result returned from every conn.fetch().
    fetchval_result returned from every conn.fetchval().
    """
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow_results))
    conn.fetch = AsyncMock(return_value=list(fetch_result))
    conn.fetchval = AsyncMock(return_value=fetchval_result)
    conn.execute = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


def _user_row(
    user_id: uuid.UUID | None = None,
    email: str = "alice@example.com",
    name: str = "Alice",
    tenant_id: str = "tenant-alice",
    role: str = "user",
    email_verified: bool = True,
    anonymized_at=None,
) -> dict:
    uid = user_id or uuid.uuid4()
    now = datetime.now(timezone.utc)
    return {
        "id": uid,
        "email": email,
        "name": name,
        "tenant_id": tenant_id,
        "role": role,
        "email_verified": email_verified,
        "anonymized_at": anonymized_at,
        "created_at": now,
        "updated_at": now,
    }


def _license_row(app_id: str = "werking-energy", plan_id: str = "trial", seats: int = 1) -> dict:
    today = datetime.now(timezone.utc).date()
    return {
        "app_id": app_id,
        "plan_id": plan_id,
        "start_date": today,
        "end_date": None,
        "seats": seats,
    }


# ---------------------------------------------------------------------------
# GET /v1/admin/users/lookup
# ---------------------------------------------------------------------------

class TestLookupHappyPath:
    def test_returns_user_with_licenses(self):
        uid = uuid.uuid4()
        row = _user_row(user_id=uid, email="alice@example.com")
        license_rows = [_license_row(app_id="werking-energy", plan_id="trial")]
        pool, _conn = _make_pool(fetchrow_results=[row], fetch_result=license_rows)

        client = TestClient(_make_app(_operator_claims()))
        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.get("/v1/admin/users/lookup?email=alice@example.com")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["user_id"] == str(uid)
        assert body["email"] == "alice@example.com"
        assert body["email_verified"] is True
        assert body["anonymized_at"] is None
        assert len(body["app_licenses"]) == 1
        assert body["app_licenses"][0]["app_id"] == "werking-energy"
        assert body["app_licenses"][0]["plan_id"] == "trial"

    def test_returns_empty_licenses_list_when_user_has_none(self):
        row = _user_row()
        pool, _ = _make_pool(fetchrow_results=[row], fetch_result=[])

        client = TestClient(_make_app(_operator_claims()))
        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.get("/v1/admin/users/lookup?email=alice@example.com")

        assert resp.status_code == 200
        assert resp.json()["app_licenses"] == []

    def test_exposes_anonymized_at_when_present(self):
        """Anonymized users typically have placeholder emails, but the field
        must be surfaced whenever non-NULL so the caller can detect the
        closed-account edge case (instead of assuming it never happens)."""
        when = datetime(2026, 1, 15, tzinfo=timezone.utc)
        # Use a valid-shaped email so EmailStr accepts the query param. The
        # interesting bit is anonymized_at: non-NULL must round-trip.
        row = _user_row(anonymized_at=when, email="closed@example.com")
        pool, _ = _make_pool(fetchrow_results=[row], fetch_result=[])

        client = TestClient(_make_app(_operator_claims()))
        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.get("/v1/admin/users/lookup?email=closed@example.com")

        assert resp.status_code == 200
        assert resp.json()["anonymized_at"] == when.isoformat()


class TestLookupFailures:
    def test_unknown_email_returns_404(self):
        pool, _ = _make_pool(fetchrow_results=[None])

        client = TestClient(_make_app(_operator_claims()))
        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.get("/v1/admin/users/lookup?email=ghost@example.com")

        assert resp.status_code == 404
        assert "ghost@example.com" in resp.json()["detail"]

    def test_missing_email_param_returns_422(self):
        client = TestClient(_make_app(_operator_claims()))
        resp = client.get("/v1/admin/users/lookup")
        assert resp.status_code == 422

    def test_malformed_email_param_returns_422(self):
        client = TestClient(_make_app(_operator_claims()))
        resp = client.get("/v1/admin/users/lookup?email=not-an-email")
        assert resp.status_code == 422

    def test_unauthenticated_returns_401(self):
        """No dependency override → require_admin enforces 401 on missing creds."""
        app = FastAPI()
        app.include_router(admin_router)
        client = TestClient(app)
        resp = client.get("/v1/admin/users/lookup?email=alice@example.com")
        # require_jwt_or_service returns 401 when no creds present.
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /v1/admin/users/{user_id}/app-licenses
# ---------------------------------------------------------------------------

class TestAssignLicenseHappyPath:
    def test_assigns_new_license_returns_created_true(self):
        uid = uuid.uuid4()
        today = datetime.now(timezone.utc).date()
        upsert_row = {
            "user_id": uid,
            "app_id": "werking-energy",
            "plan_id": "trial",
            "start_date": today,
            "end_date": None,
            "seats": 1,
            "created": True,
        }
        # fetchval (user_exists) → 1, fetchrow (UPSERT) → upsert_row.
        pool, conn = _make_pool(fetchrow_results=[upsert_row], fetchval_result=1)

        client = TestClient(_make_app(_operator_claims()))
        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/admin/users/{uid}/app-licenses",
                json={"app_id": "werking-energy", "plan_id": "trial", "seats": 1},
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["user_id"] == str(uid)
        assert body["app_id"] == "werking-energy"
        assert body["plan_id"] == "trial"
        assert body["seats"] == 1
        assert body["created"] is True
        # Confirm we hit the UPSERT — not a separate INSERT path.
        upsert_calls = [c for c in conn.fetchrow.await_args_list if "ON CONFLICT" in c.args[0]]
        assert len(upsert_calls) == 1

    def test_existing_license_returns_created_false(self):
        uid = uuid.uuid4()
        today = datetime.now(timezone.utc).date()
        upsert_row = {
            "user_id": uid,
            "app_id": "werking-energy",
            "plan_id": "energy-project",
            "start_date": today,
            "end_date": None,
            "seats": 3,
            "created": False,
        }
        pool, _ = _make_pool(fetchrow_results=[upsert_row], fetchval_result=1)

        client = TestClient(_make_app(_operator_claims()))
        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/admin/users/{uid}/app-licenses",
                json={"app_id": "werking-energy", "plan_id": "energy-project", "seats": 3},
            )

        assert resp.status_code == 200
        assert resp.json()["created"] is False
        assert resp.json()["plan_id"] == "energy-project"
        assert resp.json()["seats"] == 3

    def test_plan_id_defaults_to_trial(self):
        uid = uuid.uuid4()
        today = datetime.now(timezone.utc).date()
        upsert_row = {
            "user_id": uid,
            "app_id": "werking-energy",
            "plan_id": "trial",
            "start_date": today,
            "end_date": None,
            "seats": 1,
            "created": True,
        }
        pool, conn = _make_pool(fetchrow_results=[upsert_row], fetchval_result=1)

        client = TestClient(_make_app(_operator_claims()))
        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/admin/users/{uid}/app-licenses",
                json={"app_id": "werking-energy"},
            )

        assert resp.status_code == 200
        # The UPSERT call must carry 'trial' as the plan_id positional arg.
        upsert = [c for c in conn.fetchrow.await_args_list if "ON CONFLICT" in c.args[0]][0]
        # args: (sql, uid, app_id, plan_id, today, seats)
        assert "trial" in upsert.args


class TestAssignLicenseFailures:
    def test_unknown_app_id_returns_400(self):
        uid = uuid.uuid4()
        # No DB needed — guard runs before DB.
        client = TestClient(_make_app(_operator_claims()))
        resp = client.post(
            f"/v1/admin/users/{uid}/app-licenses",
            json={"app_id": "not-an-app", "plan_id": "trial"},
        )
        assert resp.status_code == 400
        assert "not-an-app" in resp.json()["detail"]

    def test_unknown_plan_id_returns_400(self):
        uid = uuid.uuid4()
        client = TestClient(_make_app(_operator_claims()))
        resp = client.post(
            f"/v1/admin/users/{uid}/app-licenses",
            json={"app_id": "werking-energy", "plan_id": "platinum"},
        )
        assert resp.status_code == 400
        assert "platinum" in resp.json()["detail"]

    def test_invalid_user_id_returns_400(self):
        client = TestClient(_make_app(_operator_claims()))
        resp = client.post(
            "/v1/admin/users/not-a-uuid/app-licenses",
            json={"app_id": "werking-energy"},
        )
        assert resp.status_code == 400
        assert "UUID" in resp.json()["detail"]

    def test_user_not_found_returns_404(self):
        uid = uuid.uuid4()
        # fetchval returns None — user_exists check fails.
        pool, _ = _make_pool(fetchval_result=None)

        client = TestClient(_make_app(_operator_claims()))
        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/admin/users/{uid}/app-licenses",
                json={"app_id": "werking-energy"},
            )
        assert resp.status_code == 404
        assert str(uid) in resp.json()["detail"]

    def test_seats_must_be_positive(self):
        uid = uuid.uuid4()
        client = TestClient(_make_app(_operator_claims()))
        resp = client.post(
            f"/v1/admin/users/{uid}/app-licenses",
            json={"app_id": "werking-energy", "seats": 0},
        )
        # Pydantic ge=1 → 422
        assert resp.status_code == 422

    def test_unauthenticated_returns_401(self):
        """No override → require_admin returns 401."""
        app = FastAPI()
        app.include_router(admin_router)
        client = TestClient(app)
        resp = client.post(
            f"/v1/admin/users/{uuid.uuid4()}/app-licenses",
            json={"app_id": "werking-energy"},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Integration: lookup → assign chain matches the seed-users drift flow
# ---------------------------------------------------------------------------

class TestDriftCorrectionFlow:
    def test_lookup_then_assign_for_missing_app_license(self):
        """
        Simulates the app-side seed-users flow:
          1. /v1/auth/register returned 409 (verified elsewhere)
          2. /v1/admin/users/lookup?email=... → user has report+safety, NOT energy
          3. /v1/admin/users/{user_id}/app-licenses (app_id=energy) → assign
        """
        uid = uuid.uuid4()
        row = _user_row(user_id=uid, email="existing@example.com")
        existing_licenses = [
            _license_row(app_id="werking-report", plan_id="trial"),
            _license_row(app_id="werking-safety", plan_id="trial"),
        ]

        # Step 2: lookup
        lookup_pool, _ = _make_pool(fetchrow_results=[row], fetch_result=existing_licenses)
        client = TestClient(_make_app(_operator_claims()))
        with patch("src.db.admin_routes.get_pool", return_value=lookup_pool):
            lookup_resp = client.get("/v1/admin/users/lookup?email=existing@example.com")

        assert lookup_resp.status_code == 200
        held_apps = {lic["app_id"] for lic in lookup_resp.json()["app_licenses"]}
        assert "werking-energy" not in held_apps

        # Step 3: assign the missing license
        today = datetime.now(timezone.utc).date()
        assign_row = {
            "user_id": uid,
            "app_id": "werking-energy",
            "plan_id": "trial",
            "start_date": today,
            "end_date": None,
            "seats": 1,
            "created": True,
        }
        assign_pool, _ = _make_pool(fetchrow_results=[assign_row], fetchval_result=1)
        with patch("src.db.admin_routes.get_pool", return_value=assign_pool):
            assign_resp = client.post(
                f"/v1/admin/users/{uid}/app-licenses",
                json={"app_id": "werking-energy"},
            )

        assert assign_resp.status_code == 200
        assert assign_resp.json()["created"] is True


# ---------------------------------------------------------------------------
# Lookup: entitlements (subscription-derived verdicts)
# ---------------------------------------------------------------------------

class TestLookupEntitlements:
    def test_entitlements_come_from_billing_service(self, _stub_list_subscriptions):
        """The gate-relevant field: verdicts derive from subscriptions via the
        lazy-expiring billing read — NOT from app_licenses."""
        uid = uuid.uuid4()
        _stub_list_subscriptions.return_value = [
            {"appId": "werking-energy", "status": "active",
             "planId": "energy-project", "trialEndsAt": None,
             "mollieCustomerId": "seed-x", "seats": 1},
        ]
        pool, _conn = _make_pool(
            fetchrow_results=[_user_row(user_id=uid)], fetch_result=[]
        )
        client = TestClient(_make_app(_operator_claims()))
        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.get("/v1/admin/users/lookup?email=alice@example.com")

        assert resp.status_code == 200, resp.text
        ents = resp.json()["entitlements"]
        assert ents == [{"appId": "werking-energy", "status": "active",
                         "planId": "energy-project", "trialEndsAt": None}]

    def test_license_without_subscription_shows_empty_entitlements(self):
        """The exact 2026-07-06 trap: a valid license but NO active
        subscription → entitlements empty → operator sees at a glance why
        the app bounces with reason=no-license."""
        uid = uuid.uuid4()
        pool, _conn = _make_pool(
            fetchrow_results=[_user_row(user_id=uid)],
            fetch_result=[_license_row(app_id="werking-energy", plan_id="energy-project")],
        )
        client = TestClient(_make_app(_operator_claims()))
        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.get("/v1/admin/users/lookup?email=alice@example.com")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["app_licenses"][0]["app_id"] == "werking-energy"
        assert body["entitlements"] == []


# ---------------------------------------------------------------------------
# POST /v1/admin/users/{user_id}/subscriptions — grant access
# ---------------------------------------------------------------------------

def _subscription_dict(app_id: str = "werking-energy", plan_id: str = "energy-project") -> dict:
    return {
        "id": str(uuid.uuid4()),
        "userId": str(uuid.uuid4()),
        "appId": app_id,
        "planId": plan_id,
        "status": "active",
        "mollieCustomerId": "seed-x",
        "mollieSubscriptionId": None,
        "seats": 1,
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "cancelledAt": None,
        "suspendedAt": None,
        "expiredAt": None,
        "trialEndsAt": None,
    }


def _license_upsert_row(app_id: str = "werking-energy", plan_id: str = "energy-project",
                        created: bool = True) -> dict:
    today = datetime.now(timezone.utc).date()
    return {
        "user_id": uuid.uuid4(),
        "app_id": app_id,
        "plan_id": plan_id,
        "start_date": today,
        "end_date": None,
        "seats": 1,
        "created": created,
    }


class TestGrantSubscription:
    def _post(self, *, pool, grant_result, user_id=None, body=None):
        client = TestClient(_make_app(_operator_claims()))
        uid = user_id or str(uuid.uuid4())
        payload = body or {"app_id": "werking-energy", "plan_id": "energy-project"}
        with patch("src.db.admin_routes.get_pool", return_value=pool), patch(
            "src.db.admin_routes.billing_service.grant_subscription",
            new=AsyncMock(return_value=grant_result),
        ) as grant_mock:
            resp = client.post(f"/v1/admin/users/{uid}/subscriptions", json=payload)
        return resp, grant_mock

    def test_grant_creates_subscription_and_license(self):
        pool, _conn = _make_pool(
            fetchrow_results=[_license_upsert_row(created=True)], fetchval_result=1
        )
        resp, grant_mock = self._post(
            pool=pool, grant_result=(_subscription_dict(), True)
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["subscription_created"] is True
        assert body["license_created"] is True
        assert body["subscription"]["appId"] == "werking-energy"
        assert body["subscription"]["status"] == "active"
        assert body["app_license"]["plan_id"] == "energy-project"
        grant_mock.assert_awaited_once()

    def test_existing_active_subscription_is_not_mutated(self):
        """Idempotency: created=false from billing_service surfaces verbatim."""
        pool, _conn = _make_pool(
            fetchrow_results=[_license_upsert_row(created=False)], fetchval_result=1
        )
        resp, _ = self._post(pool=pool, grant_result=(_subscription_dict(), False))
        assert resp.status_code == 200, resp.text
        assert resp.json()["subscription_created"] is False

    def test_unknown_plan_fails_fast_before_any_write(self):
        pool, conn = _make_pool(fetchval_result=1)
        resp, grant_mock = self._post(
            pool=pool, grant_result=(_subscription_dict(), True),
            body={"app_id": "werking-energy", "plan_id": "does-not-exist"},
        )
        assert resp.status_code == 400
        assert "Unknown plan_id" in resp.json()["detail"]
        conn.fetchrow.assert_not_awaited()
        grant_mock.assert_not_awaited()

    def test_unknown_app_fails_fast_before_any_write(self):
        pool, conn = _make_pool(fetchval_result=1)
        resp, grant_mock = self._post(
            pool=pool, grant_result=(_subscription_dict(), True),
            body={"app_id": "not-an-app", "plan_id": "energy-project"},
        )
        assert resp.status_code == 400
        assert "Unknown app_id" in resp.json()["detail"]
        conn.fetchrow.assert_not_awaited()
        grant_mock.assert_not_awaited()

    def test_malformed_user_id_is_400(self):
        pool, _conn = _make_pool(fetchval_result=1)
        resp, _ = self._post(
            pool=pool, grant_result=(_subscription_dict(), True),
            user_id="not-a-uuid",
        )
        assert resp.status_code == 400
        assert "UUID" in resp.json()["detail"]

    def test_missing_user_is_404(self):
        pool, _conn = _make_pool(fetchval_result=None)
        resp, grant_mock = self._post(
            pool=pool, grant_result=(_subscription_dict(), True)
        )
        assert resp.status_code == 404
        grant_mock.assert_not_awaited()

    def test_plan_id_is_mandatory(self):
        """No trial default here — an operator grant must state the plan."""
        pool, _conn = _make_pool(fetchval_result=1)
        resp, _ = self._post(
            pool=pool, grant_result=(_subscription_dict(), True),
            body={"app_id": "werking-energy"},
        )
        assert resp.status_code == 422
