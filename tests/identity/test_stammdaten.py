"""
Tests for GET/PATCH /v1/tenants/{tenant_id}/stammdaten and
         GET/PATCH /v1/users/{user_id}/stammdaten.

Coverage:
- GET tenant stammdaten: 200 with default empty record when no row exists
- GET tenant stammdaten: 200 with stored data
- PATCH tenant stammdaten: 200 with merged update
- PATCH tenant stammdaten: non-admin member (non-tenant_admin role) → 403
- PATCH tenant stammdaten: operator bypasses role check
- GET user stammdaten: 200 with default empty record when no row exists
- GET user stammdaten: 200 with stored data (fields spread + updatedAt)
- PATCH user stammdaten: 200 with merged update
- PATCH user stammdaten: foreign user → 403 (require_self_or_admin)
"""
from __future__ import annotations

import json
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import os

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

# asyncpg is a C extension not available in the unit-test env — stub it out.
try:
    import asyncpg  # noqa: F401
except ImportError:
    _asyncpg_stub = MagicMock()
    _asyncpg_stub.UniqueViolationError = type("UniqueViolationError", (Exception,), {})
    _asyncpg_stub.PostgresError = type("PostgresError", (Exception,), {})
    _asyncpg_stub.Connection = MagicMock
    sys.modules["asyncpg"] = _asyncpg_stub

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api_auth.deps import AuthClaims
from src.api_auth import require_jwt_or_service, require_self_or_admin


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _operator_claims() -> AuthClaims:
    return AuthClaims(
        kind="service", user_id=None, email=None, tenant_id=None,
        is_admin=True, acting_user_id=None,
    )


def _member_claims(user_id: str, tenant_id: str, role: str = "member") -> AuthClaims:
    return AuthClaims(
        kind="user", user_id=user_id, email="u@example.com",
        tenant_id=tenant_id, is_admin=False, acting_user_id=user_id,
        role=role,
    )


def _admin_claims(user_id: str, tenant_id: str) -> AuthClaims:
    return _member_claims(user_id, tenant_id, role="tenant_admin")


def _make_pool(*fetchrow_results, fetchval_result=1):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow_results))
    conn.fetchval = AsyncMock(return_value=fetchval_result)
    conn.execute = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


def _now():
    return datetime.now(timezone.utc)


def _tenant_sd_row(
    firma=None,
    logo=None,
    style_settings=None,
    updated_at=None,
    updated_by=None,
):
    return {
        "firma": firma or {},
        "logo": logo,
        "style_settings": style_settings or {},
        "updated_at": updated_at or _now(),
        "updated_by": updated_by,
    }


def _user_sd_row(data=None, updated_at=None):
    return {
        "data": data or {},
        "updated_at": updated_at or _now(),
    }


def _make_app(claims: AuthClaims) -> FastAPI:
    from src.db.admin_routes import router as admin_router

    app = FastAPI()
    app.include_router(admin_router)
    # Override both auth dependencies used by stammdaten endpoints.
    app.dependency_overrides[require_jwt_or_service] = lambda: claims
    app.dependency_overrides[require_self_or_admin] = lambda user_id: claims
    return app


def _make_app_with_jwt_override(claims: AuthClaims) -> FastAPI:
    """Only overrides require_jwt_or_service — used for tenant endpoints."""
    from src.db.admin_routes import router as admin_router

    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[require_jwt_or_service] = lambda: claims
    return app


def _make_app_with_self_override(claims: AuthClaims) -> FastAPI:
    """Only overrides require_self_or_admin — used for user endpoints."""
    from src.db.admin_routes import router as admin_router

    app = FastAPI()
    app.include_router(admin_router)
    app.dependency_overrides[require_self_or_admin] = lambda user_id: claims
    return app


# ---------------------------------------------------------------------------
# GET /v1/tenants/{tenant_id}/stammdaten
# ---------------------------------------------------------------------------

class TestGetTenantStammdaten:
    def test_empty_record_returns_200_with_defaults(self):
        tenant_id = "t-abc"
        pool, _ = _make_pool(None)  # no row in DB

        app = _make_app_with_jwt_override(_operator_claims())
        client = TestClient(app)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.get(f"/v1/tenants/{tenant_id}/stammdaten")

        assert resp.status_code == 200
        body = resp.json()
        assert body["firma"] == {}
        assert body["logo"] is None
        assert body["styleSettings"] == {}
        assert body["updatedAt"] is None
        assert body["updatedBy"] is None

    def test_existing_record_returned(self):
        tenant_id = "t-xyz"
        firma = {"name": "Acme GmbH", "rechtsform": "GmbH"}
        row = _tenant_sd_row(firma=firma, logo="data:image/png;base64,abc")
        pool, _ = _make_pool(row)

        app = _make_app_with_jwt_override(_operator_claims())
        client = TestClient(app)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.get(f"/v1/tenants/{tenant_id}/stammdaten")

        assert resp.status_code == 200
        body = resp.json()
        assert body["firma"]["name"] == "Acme GmbH"
        assert body["logo"] == "data:image/png;base64,abc"


# ---------------------------------------------------------------------------
# PATCH /v1/tenants/{tenant_id}/stammdaten
# ---------------------------------------------------------------------------

class TestPatchTenantStammdaten:
    def test_operator_can_patch(self):
        tenant_id = "t-1"
        result_row = _tenant_sd_row(firma={"name": "New Co"})
        # fetchval (tenant exists) → 1; fetchrow (role lookup skipped for operator) → result_row
        pool, conn = _make_pool(result_row, fetchval_result=1)

        app = _make_app_with_jwt_override(_operator_claims())
        client = TestClient(app)

        with (
            patch("src.db.admin_routes.get_pool", return_value=pool),
            patch("src.db.admin_routes._check_tenant_access", return_value=None),
        ):
            resp = client.patch(f"/v1/tenants/{tenant_id}/stammdaten", json={"firma": {"name": "New Co"}})

        assert resp.status_code == 200
        assert resp.json()["firma"]["name"] == "New Co"

    def test_tenant_admin_can_patch(self):
        uid = str(uuid.uuid4())
        tenant_id = "t-1"
        result_row = _tenant_sd_row(firma={"name": "Updated Co"})
        # User JWT: _check_tenant_admin_role reads claims.role, no DB fetchrow call.
        # fetchval (tenant exists check) → 1; fetchrow → UPSERT result.
        pool, conn = _make_pool(result_row, fetchval_result=1)

        app = _make_app_with_jwt_override(_admin_claims(uid, tenant_id))
        client = TestClient(app)

        with (
            patch("src.db.admin_routes.get_pool", return_value=pool),
            patch("src.db.admin_routes._check_tenant_access", return_value=None),
        ):
            resp = client.patch(
                f"/v1/tenants/{tenant_id}/stammdaten",
                json={"firma": {"name": "Updated Co"}},
            )

        assert resp.status_code == 200

    def test_non_admin_member_gets_403(self):
        uid = str(uuid.uuid4())
        tenant_id = "t-1"
        # role lookup returns "member" → should 403
        role_row = {"role": "member"}
        pool, _ = _make_pool(role_row, fetchval_result=1)

        app = _make_app_with_jwt_override(_member_claims(uid, tenant_id, role="member"))
        client = TestClient(app)

        with (
            patch("src.db.admin_routes.get_pool", return_value=pool),
            patch("src.db.admin_routes._check_tenant_access", return_value=None),
        ):
            resp = client.patch(
                f"/v1/tenants/{tenant_id}/stammdaten",
                json={"firma": {"name": "Sneaky"}},
            )

        assert resp.status_code == 403
        assert "tenant_admin" in resp.json()["detail"]

    def test_logo_patch_accepted(self):
        tenant_id = "t-2"
        result_row = _tenant_sd_row(logo="data:image/png;base64,xyz")
        pool, _ = _make_pool(result_row, fetchval_result=1)

        app = _make_app_with_jwt_override(_operator_claims())
        client = TestClient(app)

        with (
            patch("src.db.admin_routes.get_pool", return_value=pool),
            patch("src.db.admin_routes._check_tenant_access", return_value=None),
        ):
            resp = client.patch(
                f"/v1/tenants/{tenant_id}/stammdaten",
                json={"logo": "data:image/png;base64,xyz"},
            )

        assert resp.status_code == 200
        assert resp.json()["logo"] == "data:image/png;base64,xyz"


# ---------------------------------------------------------------------------
# GET /v1/users/{user_id}/stammdaten
# ---------------------------------------------------------------------------

class TestGetUserStammdaten:
    def test_empty_record_returns_200_with_no_data(self):
        uid = str(uuid.uuid4())
        pool, _ = _make_pool(None)

        app = _make_app_with_self_override(_member_claims(uid, "t-1"))
        client = TestClient(app)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.get(f"/v1/users/{uid}/stammdaten")

        assert resp.status_code == 200
        body = resp.json()
        assert body == {"updatedAt": None}

    def test_existing_record_fields_spread_into_response(self):
        uid = str(uuid.uuid4())
        data = {"gutachter": {"vorname": "Max", "nachname": "Muster"}, "signatur": "sig"}
        row = _user_sd_row(data=data)
        pool, _ = _make_pool(row)

        app = _make_app_with_self_override(_member_claims(uid, "t-1"))
        client = TestClient(app)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.get(f"/v1/users/{uid}/stammdaten")

        assert resp.status_code == 200
        body = resp.json()
        assert body["gutachter"]["vorname"] == "Max"
        assert body["signatur"] == "sig"
        assert "updatedAt" in body


# ---------------------------------------------------------------------------
# PATCH /v1/users/{user_id}/stammdaten
# ---------------------------------------------------------------------------

class TestPatchUserStammdaten:
    def test_user_can_patch_own_stammdaten(self):
        uid = str(uuid.uuid4())
        stored = {"gutachter": {"vorname": "Max"}, "updatedAt": None}
        result_row = _user_sd_row(data={"gutachter": {"vorname": "Max"}})
        pool, conn = _make_pool(result_row)

        app = _make_app_with_self_override(_member_claims(uid, "t-1"))
        client = TestClient(app)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.patch(
                f"/v1/users/{uid}/stammdaten",
                json={"gutachter": {"vorname": "Max"}},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["gutachter"]["vorname"] == "Max"
        assert "updatedAt" in body

    def test_merge_upsert_sql_called(self):
        """Verify UPSERT with merge semantics is issued, not a plain INSERT."""
        uid = str(uuid.uuid4())
        result_row = _user_sd_row(data={"signatur": "Dr. Muster"})
        pool, conn = _make_pool(result_row)

        app = _make_app_with_self_override(_operator_claims())
        client = TestClient(app)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.patch(
                f"/v1/users/{uid}/stammdaten",
                json={"signatur": "Dr. Muster"},
            )

        assert resp.status_code == 200
        sql = conn.fetchrow.call_args[0][0]
        assert "ON CONFLICT" in sql
        assert "COALESCE" in sql

    def test_foreign_user_blocked_by_require_self_or_admin(self):
        """require_self_or_admin rejects a non-self, non-admin caller."""
        uid_owner = str(uuid.uuid4())
        uid_other = str(uuid.uuid4())

        from src.db.admin_routes import router as admin_router
        from src.api_auth import require_self_or_admin as real_dep

        app = FastAPI()
        app.include_router(admin_router)
        # Use the REAL require_self_or_admin with a non-matching user claim.
        claims = _member_claims(uid_other, "t-1")
        app.dependency_overrides[require_jwt_or_service] = lambda: claims

        client = TestClient(app)
        # The path user_id is uid_owner but the claims identify uid_other → 403.
        resp = client.patch(
            f"/v1/users/{uid_owner}/stammdaten",
            json={"signatur": "attacker"},
            headers={"Authorization": "Bearer ignored"},
        )
        assert resp.status_code == 403
