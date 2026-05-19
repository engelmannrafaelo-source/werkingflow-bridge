"""
Tests for role support across Bridge identity layer.

Coverage:
- sign_jwt embeds role claim; verify_jwt returns it
- sign_jwt raises ValueError for invalid role
- Login response includes role field
- JWT issued by login carries the user's role claim
- Default role ('user') works for rows that have role='user' (migration default)
- AuthClaims.role is populated when decoding a JWT
- Admin (operator) can set role on create_user (role propagated to INSERT)
- Admin can update role via PATCH /v1/users/{user_id}
- Non-admin (self-caller) cannot update role → 403
- PATCH with only role (no name) works for admin
- PATCH with invalid role string returns 400
- create_user with invalid role returns 400
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

# asyncpg is a C extension not available in the unit-test env — stub it out
# so admin_routes.py can be imported without a compiled extension.
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

from src.identity.jwt_utils import sign_jwt, verify_jwt, VALID_ROLES
from src.identity.routes import router as identity_router
from src.identity.password import hash_password
from src.api_auth.deps import AuthClaims


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def identity_app() -> FastAPI:
    app = FastAPI()
    app.include_router(identity_router)
    return app


@pytest.fixture(scope="module")
def identity_client(identity_app: FastAPI) -> TestClient:
    return TestClient(identity_app)


def _mock_pool(*fetchrow_results, fetch_result=None):
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow_results))
    conn.fetch = AsyncMock(return_value=fetch_result or [])

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


def _user_row(
    user_id: uuid.UUID | None = None,
    email: str = "user@example.com",
    name: str = "Test User",
    tenant_id: str = "tenant-1",
    password: str = "secret",
    role: str = "user",
) -> dict:
    uid = user_id or uuid.uuid4()
    now = datetime.now(timezone.utc)
    return {
        "id": uid,
        "email": email,
        "name": name,
        "tenant_id": tenant_id,
        "role": role,
        "password_hash": hash_password(password),
        "created_at": now,
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# jwt_utils — sign / verify
# ---------------------------------------------------------------------------

class TestSignJwtRoleClaim:
    def test_role_included_in_payload(self):
        token = sign_jwt(
            user_id="u-1", email="a@b.com", tenant_id="t-1",
            app_licenses=[], role="admin",
        )
        payload = verify_jwt(token)
        assert payload["role"] == "admin"

    def test_default_role_is_user(self):
        token = sign_jwt(
            user_id="u-1", email="a@b.com", tenant_id="t-1", app_licenses=[],
        )
        payload = verify_jwt(token)
        assert payload["role"] == "user"

    def test_all_valid_roles_accepted(self):
        for r in VALID_ROLES:
            token = sign_jwt(
                user_id="u-1", email="a@b.com", tenant_id="t-1",
                app_licenses=[], role=r,
            )
            assert verify_jwt(token)["role"] == r

    def test_invalid_role_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid role"):
            sign_jwt(
                user_id="u-1", email="a@b.com", tenant_id="t-1",
                app_licenses=[], role="superuser",
            )


# ---------------------------------------------------------------------------
# AuthClaims — role propagation from JWT
# ---------------------------------------------------------------------------

class TestAuthClaimsRole:
    def test_role_populated_from_jwt(self):
        from src.api_auth.deps import _decode_user_jwt
        token = sign_jwt(
            user_id="u-1", email="a@b.com", tenant_id="t-1",
            app_licenses=[], role="tenant_admin",
        )
        claims = _decode_user_jwt(token)
        assert claims.role == "tenant_admin"

    def test_default_role_in_claims(self):
        from src.api_auth.deps import _decode_user_jwt
        token = sign_jwt(
            user_id="u-1", email="a@b.com", tenant_id="t-1", app_licenses=[],
        )
        claims = _decode_user_jwt(token)
        assert claims.role == "user"

    def test_service_token_has_no_role(self):
        claims = AuthClaims(
            kind="service", user_id=None, email=None, tenant_id=None,
            is_admin=True, acting_user_id=None,
        )
        assert claims.role is None


# ---------------------------------------------------------------------------
# POST /v1/auth/login — role in response + JWT
# ---------------------------------------------------------------------------

class TestLoginRoleSupport:
    def test_login_response_includes_role(self, identity_client: TestClient):
        row = _user_row(role="admin", password="pw")
        pool, _ = _mock_pool(row, fetch_result=[])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = identity_client.post("/v1/auth/login", json={
                "email": "user@example.com",
                "password": "pw",
            })

        assert resp.status_code == 200
        assert resp.json()["user"]["role"] == "admin"

    def test_login_jwt_carries_role(self, identity_client: TestClient):
        row = _user_row(role="tenant_admin", password="pw")
        pool, _ = _mock_pool(row, fetch_result=[])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = identity_client.post("/v1/auth/login", json={
                "email": "user@example.com",
                "password": "pw",
            })

        token = resp.json()["jwt"]
        payload = verify_jwt(token)
        assert payload["role"] == "tenant_admin"

    def test_default_role_user_via_login(self, identity_client: TestClient):
        """Migration default: existing users with role='user' pass through correctly."""
        row = _user_row(role="user", password="pw")
        pool, _ = _mock_pool(row, fetch_result=[])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = identity_client.post("/v1/auth/login", json={
                "email": "user@example.com",
                "password": "pw",
            })

        assert resp.status_code == 200
        body = resp.json()
        assert body["user"]["role"] == "user"
        assert verify_jwt(body["jwt"])["role"] == "user"


# ---------------------------------------------------------------------------
# Admin CRUD — create + update role
# ---------------------------------------------------------------------------

def _operator_claims() -> AuthClaims:
    return AuthClaims(
        kind="service", user_id=None, email=None, tenant_id=None,
        is_admin=True, acting_user_id=None,
    )


def _user_claims(user_id: str) -> AuthClaims:
    return AuthClaims(
        kind="user", user_id=user_id, email="u@example.com",
        tenant_id="t-1", is_admin=False, acting_user_id=user_id,
    )


def _db_row(role: str = "user", name: str = "Name") -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": uuid.uuid4(),
        "email": "u@example.com",
        "name": name,
        "tenant_id": "t-1",
        "role": role,
        "created_at": now,
        "updated_at": now,
    }


def _make_admin_app(auth_dep_name: str, claims: AuthClaims) -> FastAPI:
    """Create a minimal FastAPI app with admin_routes and a pre-resolved auth override."""
    from src.db.admin_routes import router as admin_router
    from src.api_auth import require_admin, require_self_or_admin

    app = FastAPI()
    app.include_router(admin_router)

    dep = require_admin if auth_dep_name == "require_admin" else require_self_or_admin
    app.dependency_overrides[dep] = lambda: claims
    return app


def _make_pool_with_row(row: dict):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row)
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


class TestAdminCreateUserRole:
    def test_create_with_explicit_role(self):
        row = _db_row(role="admin")
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=[None, row])  # tenant check → None, insert → row
        conn.execute = AsyncMock()

        @asynccontextmanager
        async def _acquire():
            yield conn

        pool = MagicMock()
        pool.acquire = _acquire

        app = _make_admin_app("require_admin", _operator_claims())
        client = TestClient(app)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.post("/v1/users", json={
                "email": "admin@example.com",
                "name": "Admin User",
                "role": "admin",
            })

        assert resp.status_code == 201
        assert resp.json()["role"] == "admin"

    def test_create_defaults_to_user_role(self):
        row = _db_row(role="user")
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=[None, row])
        conn.execute = AsyncMock()

        @asynccontextmanager
        async def _acquire():
            yield conn

        pool = MagicMock()
        pool.acquire = _acquire

        app = _make_admin_app("require_admin", _operator_claims())
        client = TestClient(app)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.post("/v1/users", json={
                "email": "newuser@example.com",
                "name": "New User",
            })

        assert resp.status_code == 201
        assert resp.json()["role"] == "user"

    def test_create_with_invalid_role_returns_400(self):
        app = _make_admin_app("require_admin", _operator_claims())
        client = TestClient(app)

        resp = client.post("/v1/users", json={
            "email": "x@example.com",
            "name": "X",
            "role": "superuser",
        })

        assert resp.status_code == 400
        assert "Invalid role" in resp.json()["detail"]


class TestAdminUpdateUserRole:
    def test_admin_can_update_role(self):
        uid = str(uuid.uuid4())
        row = _db_row(role="admin")
        pool = _make_pool_with_row(row)

        app = _make_admin_app("require_self_or_admin", _operator_claims())
        client = TestClient(app)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.patch(f"/v1/users/{uid}", json={"role": "admin"})

        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_admin_can_update_name_and_role_together(self):
        uid = str(uuid.uuid4())
        row = _db_row(role="owner", name="Updated")
        pool = _make_pool_with_row(row)

        app = _make_admin_app("require_self_or_admin", _operator_claims())
        client = TestClient(app)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.patch(f"/v1/users/{uid}", json={"name": "Updated", "role": "owner"})

        assert resp.status_code == 200

    def test_non_admin_cannot_update_role(self):
        uid = str(uuid.uuid4())
        app = _make_admin_app("require_self_or_admin", _user_claims(uid))
        client = TestClient(app)

        resp = client.patch(f"/v1/users/{uid}", json={"role": "admin"})

        assert resp.status_code == 403
        assert "admin" in resp.json()["detail"].lower()

    def test_non_admin_can_still_update_name(self):
        uid = str(uuid.uuid4())
        row = _db_row(name="New Name")
        pool = _make_pool_with_row(row)

        app = _make_admin_app("require_self_or_admin", _user_claims(uid))
        client = TestClient(app)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.patch(f"/v1/users/{uid}", json={"name": "New Name"})

        assert resp.status_code == 200

    def test_update_with_invalid_role_returns_400(self):
        uid = str(uuid.uuid4())
        app = _make_admin_app("require_self_or_admin", _operator_claims())
        client = TestClient(app)

        resp = client.patch(f"/v1/users/{uid}", json={"role": "not-a-role"})

        assert resp.status_code == 400
        assert "Invalid role" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Admin CRUD — update password (operator-only; used by the test-user seeder)
# ---------------------------------------------------------------------------

class TestAdminUpdateUserPassword:
    def test_admin_can_update_password(self):
        uid = str(uuid.uuid4())
        pool = _make_pool_with_row(_db_row())
        app = _make_admin_app("require_self_or_admin", _operator_claims())
        client = TestClient(app)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.patch(f"/v1/users/{uid}", json={"password": "NewSecret2026!"})

        assert resp.status_code == 200

    def test_password_is_bcrypt_hashed_never_stored_plain(self):
        uid = str(uuid.uuid4())
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=_db_row())
        conn.execute = AsyncMock()

        @asynccontextmanager
        async def _acquire():
            yield conn

        pool = MagicMock()
        pool.acquire = _acquire

        app = _make_admin_app("require_self_or_admin", _operator_claims())
        client = TestClient(app)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.patch(f"/v1/users/{uid}", json={"password": "PlaintextSecret2026!"})

        assert resp.status_code == 200
        sql = conn.fetchrow.call_args[0][0]
        args = conn.fetchrow.call_args[0][1:]
        assert "password_hash" in sql
        # The plaintext must never reach the DB; a bcrypt hash ($2...) does.
        assert "PlaintextSecret2026!" not in args
        assert any(isinstance(a, str) and a.startswith("$2") for a in args)

    def test_non_admin_cannot_update_password(self):
        uid = str(uuid.uuid4())
        app = _make_admin_app("require_self_or_admin", _user_claims(uid))
        client = TestClient(app)

        resp = client.patch(f"/v1/users/{uid}", json={"password": "selfservice-secret"})

        assert resp.status_code == 403
        assert "password" in resp.json()["detail"].lower()

    def test_password_and_name_together(self):
        uid = str(uuid.uuid4())
        pool = _make_pool_with_row(_db_row(name="Renamed"))
        app = _make_admin_app("require_self_or_admin", _operator_claims())
        client = TestClient(app)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.patch(
                f"/v1/users/{uid}",
                json={"name": "Renamed", "password": "Secret2026!"},
            )

        assert resp.status_code == 200

    def test_no_fields_returns_400(self):
        uid = str(uuid.uuid4())
        app = _make_admin_app("require_self_or_admin", _operator_claims())
        client = TestClient(app)

        resp = client.patch(f"/v1/users/{uid}", json={})

        assert resp.status_code == 400
