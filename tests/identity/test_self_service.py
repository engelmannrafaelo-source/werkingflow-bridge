"""
Tests for GDPR self-service endpoints.

POST   /v1/users/{user_id}/change-password
DELETE /v1/users/{user_id}
POST   /v1/users/{user_id}/anonymize
GET    /v1/users/{user_id}/export

Coverage:
- change-password: correct old password → 204
- change-password: wrong old password → 403
- change-password: operator bypasses old-password check
- change-password: require_self rejects different user → 403
- change-password: anonymized account → 410
- change-password: SSO-only user (no password_hash) → 409
- close_account: anonymizes PII, retains billing data
- close_account: idempotent on already-anonymized account
- close_account: require_self rejects different user → 403
- close_account: unknown user → 404
- anonymize (operator route): closes a user hard-delete would 409 on
- anonymize (operator route): idempotent on already-anonymized account
- anonymize (operator route): non-operator caller rejected → 403
- anonymize (operator route): unknown user → 404
- anonymize (operator route): DELETE never falls back to it (still 409)
- export: returns all data sections
- export: require_self rejects different user → 403
- export: unknown user → 404
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import os
import sys
os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

# admin_routes imports asyncpg unconditionally (for ForeignKeyViolationError on
# hard-delete); stub it if the C extension isn't built in this environment,
# mirroring tests/identity/test_role_support.py.
try:
    import asyncpg  # noqa: F401
except ImportError:
    _asyncpg_stub = MagicMock()
    _asyncpg_stub.UniqueViolationError = type("UniqueViolationError", (Exception,), {})
    _asyncpg_stub.ForeignKeyViolationError = type("ForeignKeyViolationError", (Exception,), {})
    _asyncpg_stub.PostgresError = type("PostgresError", (Exception,), {})
    _asyncpg_stub.Connection = MagicMock
    sys.modules["asyncpg"] = _asyncpg_stub

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import config
from src.db.admin_routes import router as admin_db_router
from src.identity.self_service import router
from src.identity.password import hash_password
from src.identity.jwt_utils import sign_jwt


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app() -> FastAPI:
    # Mirror the PRODUCTION composition (platform_main.py): admin_db_router is
    # registered BEFORE self_service_router. This matters: DELETE
    # /v1/users/{user_id} lives in admin_routes (delete_user) and delegates
    # non-operator callers to close_account. Mounting the self_service router
    # alone (as this fixture did until 2026-07-03) tested a composition that
    # never existed in production — close_account's own route was shadowed and
    # unreachable, yet its isolation tests were green while every real portal
    # "Konto löschen" died with 403 in the shadow handler.
    _app = FastAPI()
    _app.include_router(admin_db_router)
    _app.include_router(router)
    return _app


@pytest.fixture(scope="module")
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _jwt(user_id: uuid.UUID, tenant_id: str = "tenant-1") -> str:
    return sign_jwt(
        user_id=str(user_id),
        email="user@example.com",
        tenant_id=tenant_id,
        app_licenses=[],
    )


_SERVICE_HEADER = {"X-Bridge-Service-Token": config.service_token}


def _mock_pool(fetchrow_result=None, fetchval_side_effect=None, fetch_result=None):
    """
    Pool mock supporting fetchrow, fetchval (ordered side_effect), fetch,
    execute, and transaction context manager.
    """
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(return_value=fetchrow_result)
    conn.fetch = AsyncMock(return_value=fetch_result or [])

    if fetchval_side_effect is not None:
        conn.fetchval = AsyncMock(side_effect=fetchval_side_effect)
    else:
        conn.fetchval = AsyncMock(return_value=0)

    @asynccontextmanager
    async def _transaction():
        yield None

    conn.transaction = _transaction

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


def _mock_pool_multi(*fetchrow_results, fetchval_side_effect=None, fetch_result=None):
    """Pool mock where fetchrow is called multiple times — results consumed in order."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow_results))
    conn.fetch = AsyncMock(return_value=fetch_result or [])

    if fetchval_side_effect is not None:
        conn.fetchval = AsyncMock(side_effect=fetchval_side_effect)
    else:
        conn.fetchval = AsyncMock(return_value=0)

    @asynccontextmanager
    async def _transaction():
        yield None

    conn.transaction = _transaction

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


def _profile_row(uid: uuid.UUID, anonymized_at=None) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": uid,
        "email": "user@example.com",
        "name": "Test User",
        "tenant_id": "tenant-1",
        "role": "user",
        "created_at": now,
        "updated_at": now,
        "anonymized_at": anonymized_at,
    }


# ---------------------------------------------------------------------------
# POST /v1/users/{user_id}/change-password
# ---------------------------------------------------------------------------

class TestChangePassword:

    def test_correct_old_password_returns_204(self, client: TestClient):
        uid = uuid.uuid4()
        old_hash = hash_password("old-secret")
        row = {"password_hash": old_hash, "anonymized_at": None}
        pool, conn = _mock_pool(fetchrow_result=row)

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/users/{uid}/change-password",
                headers={"Authorization": f"Bearer {_jwt(uid)}"},
                json={"oldPassword": "old-secret", "newPassword": "new-secret-1234"},
            )

        assert resp.status_code == 204
        conn.execute.assert_awaited_once()
        sql = conn.execute.call_args[0][0]
        assert "UPDATE users" in sql
        assert "password_hash" in sql

    def test_wrong_old_password_returns_403(self, client: TestClient):
        uid = uuid.uuid4()
        old_hash = hash_password("real-secret")
        row = {"password_hash": old_hash, "anonymized_at": None}
        pool, conn = _mock_pool(fetchrow_result=row)

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/users/{uid}/change-password",
                headers={"Authorization": f"Bearer {_jwt(uid)}"},
                json={"oldPassword": "wrong-secret", "newPassword": "new-secret-1234"},
            )

        assert resp.status_code == 403
        assert "incorrect" in resp.json()["detail"].lower()
        conn.execute.assert_not_awaited()

    def test_require_self_rejects_other_user(self, client: TestClient):
        """A user JWT for uid1 may not change uid2's password."""
        uid1 = uuid.uuid4()
        uid2 = uuid.uuid4()
        pool, conn = _mock_pool()

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/users/{uid2}/change-password",
                headers={"Authorization": f"Bearer {_jwt(uid1)}"},
                json={"oldPassword": "x", "newPassword": "new-secret-1234"},
            )

        assert resp.status_code == 403

    def test_operator_bypasses_old_password(self, client: TestClient):
        """Service token (operator) skips old-password verification."""
        uid = uuid.uuid4()
        row = {"password_hash": hash_password("real-secret"), "anonymized_at": None}
        pool, conn = _mock_pool(fetchrow_result=row)

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/users/{uid}/change-password",
                headers=_SERVICE_HEADER,
                json={"oldPassword": "anything-at-all", "newPassword": "new-secret-1234"},
            )

        assert resp.status_code == 204
        conn.execute.assert_awaited_once()

    def test_anonymized_account_returns_410(self, client: TestClient):
        uid = uuid.uuid4()
        row = {
            "password_hash": hash_password("old"),
            "anonymized_at": datetime.now(timezone.utc),
        }
        pool, _ = _mock_pool(fetchrow_result=row)

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/users/{uid}/change-password",
                headers={"Authorization": f"Bearer {_jwt(uid)}"},
                json={"oldPassword": "old", "newPassword": "new-secret-1234"},
            )

        assert resp.status_code == 410

    def test_sso_only_user_returns_409(self, client: TestClient):
        """User without password_hash (SSO-only) cannot use this endpoint."""
        uid = uuid.uuid4()
        row = {"password_hash": None, "anonymized_at": None}
        pool, _ = _mock_pool(fetchrow_result=row)

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/users/{uid}/change-password",
                headers={"Authorization": f"Bearer {_jwt(uid)}"},
                json={"oldPassword": "anything", "newPassword": "new-secret-1234"},
            )

        assert resp.status_code == 409

    def test_unknown_user_returns_404(self, client: TestClient):
        uid = uuid.uuid4()
        pool, _ = _mock_pool(fetchrow_result=None)

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/users/{uid}/change-password",
                headers={"Authorization": f"Bearer {_jwt(uid)}"},
                json={"oldPassword": "x", "newPassword": "new-secret-1234"},
            )

        assert resp.status_code == 404

    def test_new_password_too_short_returns_422(self, client: TestClient):
        uid = uuid.uuid4()
        pool, _ = _mock_pool()

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/users/{uid}/change-password",
                headers={"Authorization": f"Bearer {_jwt(uid)}"},
                json={"oldPassword": "x", "newPassword": "short"},
            )

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /v1/users/{user_id} — close_account
# ---------------------------------------------------------------------------

class TestCloseAccount:

    def test_anonymizes_pii_and_retains_billing(self, client: TestClient):
        uid = uuid.uuid4()
        user_row = {"id": uid, "anonymized_at": None}
        # fetchval side_effect: invoices=2, subscriptions=1, credit_purchases=0, billing_events=3
        pool, conn = _mock_pool_multi(
            user_row,
            fetchval_side_effect=[2, 1, 0, 3],
        )

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.delete(
                f"/v1/users/{uid}",
                headers={"Authorization": f"Bearer {_jwt(uid)}"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["userId"] == str(uid)
        assert "anonymizedAt" in body
        assert body["retained"]["invoices"] == 2
        assert body["retained"]["subscriptions"] == 1
        assert body["retained"]["creditPurchases"] == 0
        assert body["retained"]["billingEvents"] == 3

        # Verify the anonymization UPDATE was executed.
        update_calls = [
            c for c in conn.execute.call_args_list
            if "UPDATE users" in c[0][0]
        ]
        assert len(update_calls) == 1
        update_sql = update_calls[0][0][0]
        assert "password_hash" in update_sql
        assert "anonymized_at" in update_sql

        # Verify sessions were deleted.
        session_calls = [
            c for c in conn.execute.call_args_list
            if "DELETE FROM sessions" in c[0][0]
        ]
        assert len(session_calls) == 1

        # Verify invoices were NOT touched.
        invoice_calls = [
            c for c in conn.execute.call_args_list
            if "invoices" in c[0][0].lower() and "update" in c[0][0].lower()
        ]
        assert len(invoice_calls) == 0

    def test_idempotent_on_already_anonymized(self, client: TestClient):
        uid = uuid.uuid4()
        ts = datetime.now(timezone.utc)
        user_row = {"id": uid, "anonymized_at": ts}
        pool, conn = _mock_pool(fetchrow_result=user_row)

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.delete(
                f"/v1/users/{uid}",
                headers={"Authorization": f"Bearer {_jwt(uid)}"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["alreadyAnonymized"] is True
        # No DB mutations should have been made.
        conn.execute.assert_not_awaited()

    def test_require_self_rejects_other_user(self, client: TestClient):
        uid1 = uuid.uuid4()
        uid2 = uuid.uuid4()
        pool, _ = _mock_pool()

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.delete(
                f"/v1/users/{uid2}",
                headers={"Authorization": f"Bearer {_jwt(uid1)}"},
            )

        assert resp.status_code == 403

    def test_unknown_user_returns_404(self, client: TestClient):
        uid = uuid.uuid4()
        pool, _ = _mock_pool(fetchrow_result=None)

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.delete(
                f"/v1/users/{uid}",
                headers={"Authorization": f"Bearer {_jwt(uid)}"},
            )

        assert resp.status_code == 404

    def test_operator_delete_is_hard_delete(self, client: TestClient):
        # Operator (service token WITHOUT X-User-ID) on DELETE /v1/users/{id}
        # takes the HARD-DELETE branch in admin_routes.delete_user — it never
        # reaches close_account. The previous version of this test
        # ("operator_can_close_any_account") asserted the operator got the
        # anonymize path — true only in the isolated single-router fixture,
        # never in production. Composition semantics: operator → hard-delete
        # (204/404/409), non-operator → GDPR anonymize via delegation.
        uid = uuid.uuid4()
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="DELETE 1")
        acquire_cm = MagicMock()
        acquire_cm.__aenter__ = AsyncMock(return_value=conn)
        acquire_cm.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_cm)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.delete(
                f"/v1/users/{uid}",
                headers=_SERVICE_HEADER,
            )

        assert resp.status_code == 204
        sql = conn.execute.call_args_list[0][0][0]
        assert "DELETE FROM users" in sql

    def test_scoped_service_token_delegates_to_anonymize(self, client: TestClient):
        # Service token WITH X-User-ID (the portal proxy's credential shape) is
        # NOT an operator — delete_user must delegate to close_account (GDPR
        # anonymize). THIS is the exact call shape that returned 403 for every
        # portal customer until 2026-07-03 (require_admin on the shadow route).
        uid = uuid.uuid4()
        ts = datetime.now(timezone.utc)
        pool, conn = _mock_pool(fetchrow_result={"id": uid, "anonymized_at": ts})

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.delete(
                f"/v1/users/{uid}",
                headers={**_SERVICE_HEADER, "X-User-ID": str(uid)},
            )

        assert resp.status_code == 200
        assert resp.json()["alreadyAnonymized"] is True


# ---------------------------------------------------------------------------
# POST /v1/users/{user_id}/anonymize — operator-only escape valve
# ---------------------------------------------------------------------------

class TestOperatorAnonymize:

    def test_operator_can_anonymize_user_with_billing_history(self, client: TestClient):
        """
        Exactly the scenario that left the four e2e test accounts stuck:
        operator DELETE 409s on retained subscriptions/credit_purchases, and
        until this route existed there was no other way to close the account.
        """
        uid = uuid.uuid4()
        user_row = {"id": uid, "anonymized_at": None}
        # fetchval side_effect: invoices=0, subscriptions=2, credit_purchases=1, billing_events=0
        pool, conn = _mock_pool_multi(
            user_row,
            fetchval_side_effect=[0, 2, 1, 0],
        )

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/users/{uid}/anonymize",
                headers=_SERVICE_HEADER,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["userId"] == str(uid)
        assert body["retained"]["subscriptions"] == 2
        assert body["retained"]["creditPurchases"] == 1
        assert body.get("alreadyAnonymized") is None

        update_calls = [c for c in conn.execute.call_args_list if "UPDATE users" in c[0][0]]
        assert len(update_calls) == 1

    def test_idempotent_on_already_anonymized(self, client: TestClient):
        uid = uuid.uuid4()
        ts = datetime.now(timezone.utc)
        pool, conn = _mock_pool(fetchrow_result={"id": uid, "anonymized_at": ts})

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/users/{uid}/anonymize",
                headers=_SERVICE_HEADER,
            )

        assert resp.status_code == 200
        assert resp.json()["alreadyAnonymized"] is True
        conn.execute.assert_not_awaited()

    def test_non_operator_user_jwt_rejected(self, client: TestClient):
        """A plain user JWT (not is_admin) has no path to this operator route."""
        uid = uuid.uuid4()
        pool, _ = _mock_pool()

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/users/{uid}/anonymize",
                headers={"Authorization": f"Bearer {_jwt(uid)}"},
            )

        assert resp.status_code == 403

    def test_scoped_service_token_rejected(self, client: TestClient):
        """
        Service token WITH X-User-ID is a customer proxy, not an operator —
        it must not reach the operator-only anonymize route either (it already
        has its own path to the same effect via DELETE, scoped to itself).
        """
        uid = uuid.uuid4()
        pool, _ = _mock_pool()

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/users/{uid}/anonymize",
                headers={**_SERVICE_HEADER, "X-User-ID": str(uid)},
            )

        assert resp.status_code == 403

    def test_unknown_user_returns_404(self, client: TestClient):
        uid = uuid.uuid4()
        pool, _ = _mock_pool(fetchrow_result=None)

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.post(
                f"/v1/users/{uid}/anonymize",
                headers=_SERVICE_HEADER,
            )

        assert resp.status_code == 404

    def test_delete_never_falls_back_to_anonymize(self, client: TestClient):
        """
        Operator hard-DELETE on a user with billing history must still 409 —
        this route existing must not become an implicit fallback inside
        delete_user. The 409 detail should point the caller at the explicit
        anonymize endpoint instead of just saying "cancel/refund first" (which
        does not actually unblock hard-delete, since RESTRICT fires on row
        existence, not subscription status).
        """
        uid = uuid.uuid4()
        conn = AsyncMock()
        conn.execute = AsyncMock(
            side_effect=asyncpg.ForeignKeyViolationError(
                "update or delete on table \"users\" violates foreign key constraint"
            )
        )
        acquire_cm = MagicMock()
        acquire_cm.__aenter__ = AsyncMock(return_value=conn)
        acquire_cm.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_cm)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.delete(
                f"/v1/users/{uid}",
                headers=_SERVICE_HEADER,
            )

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "billing" in detail.lower()
        assert f"/v1/users/{uid}/anonymize" in detail


# ---------------------------------------------------------------------------
# GET /v1/users/{user_id}/export
# ---------------------------------------------------------------------------

class TestExportUserData:

    def _full_mock_pool(self, uid: uuid.UUID):
        """Return a pool whose conn answers all 9 queries the export makes."""
        now = datetime.now(timezone.utc)

        profile = {
            "id": uid,
            "email": "user@example.com",
            "name": "Test User",
            "tenant_id": "tenant-1",
            "role": "user",
            "created_at": now,
            "updated_at": now,
            "anonymized_at": None,
        }
        stammdaten = {"data": {"firma": "ACME"}, "updated_at": now}
        budget = {"monthly_budgets": {"2026-05": "10.00"}, "updated_at": now}
        balance = {"balance_eur": Decimal("5.00"), "updated_at": now}

        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=None)
        # fetchrow is called 4 times: profile, stammdaten, budget, balance
        conn.fetchrow = AsyncMock(side_effect=[profile, stammdaten, budget, balance])
        # fetch is called 5 times: licenses, subs, invoices, billing_events, activities
        conn.fetch = AsyncMock(return_value=[])
        conn.fetchval = AsyncMock(return_value=0)

        @asynccontextmanager
        async def _transaction():
            yield None

        conn.transaction = _transaction

        @asynccontextmanager
        async def _acquire():
            yield conn

        pool = MagicMock()
        pool.acquire = _acquire
        return pool, conn

    def test_returns_all_sections(self, client: TestClient):
        uid = uuid.uuid4()
        pool, _ = self._full_mock_pool(uid)

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.get(
                f"/v1/users/{uid}/export",
                headers={"Authorization": f"Bearer {_jwt(uid)}"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert "exportedAt" in body
        assert body["schemaVersion"] == "1"
        assert "profile" in body
        assert body["profile"]["email"] == "user@example.com"
        assert "appLicenses" in body
        assert "stammdaten" in body
        assert "subscriptions" in body
        assert "invoices" in body
        assert "billingEvents" in body
        assert "activities" in body
        assert "budget" in body
        assert "topupBalance" in body

    def test_profile_fields_present(self, client: TestClient):
        uid = uuid.uuid4()
        pool, _ = self._full_mock_pool(uid)

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.get(
                f"/v1/users/{uid}/export",
                headers={"Authorization": f"Bearer {_jwt(uid)}"},
            )

        profile = resp.json()["profile"]
        assert profile["id"] == str(uid)
        assert profile["tenantId"] == "tenant-1"
        assert "createdAt" in profile
        assert "anonymizedAt" in profile

    def test_require_self_rejects_other_user(self, client: TestClient):
        uid1 = uuid.uuid4()
        uid2 = uuid.uuid4()
        pool, _ = _mock_pool()

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.get(
                f"/v1/users/{uid2}/export",
                headers={"Authorization": f"Bearer {_jwt(uid1)}"},
            )

        assert resp.status_code == 403

    def test_unknown_user_returns_404(self, client: TestClient):
        uid = uuid.uuid4()
        pool, _ = _mock_pool(fetchrow_result=None)

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.get(
                f"/v1/users/{uid}/export",
                headers={"Authorization": f"Bearer {_jwt(uid)}"},
            )

        assert resp.status_code == 404

    def test_no_auth_returns_401(self, client: TestClient):
        uid = uuid.uuid4()
        resp = client.get(f"/v1/users/{uid}/export")
        assert resp.status_code == 401

    def test_operator_can_export_any_user(self, client: TestClient):
        uid = uuid.uuid4()
        pool, _ = self._full_mock_pool(uid)

        with patch("src.identity.self_service.get_pool", return_value=pool):
            resp = client.get(
                f"/v1/users/{uid}/export",
                headers=_SERVICE_HEADER,
            )

        assert resp.status_code == 200
        assert resp.json()["profile"]["id"] == str(uid)
