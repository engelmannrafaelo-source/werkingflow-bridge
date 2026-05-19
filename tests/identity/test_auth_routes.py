"""
Tests for POST /v1/auth/login, POST /v1/auth/logout, GET /v1/auth/session.

Coverage:
- Successful login → 200 with jwt + user + appLicenses
- Wrong password → 401 (same message as unknown user — no enumeration)
- Unknown user → 401 (same message as wrong password)
- User without password_hash → 401
- Issued JWT accepted by verify_jwt (claims: sub, email, tenantId)
- Session row is inserted into DB on successful login
- Logout revokes session (expires_at = NOW())
- /v1/auth/session returns user data for valid session
- /v1/auth/session rejects expired/revoked session
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import os
os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import config
from src.identity.routes import router
from src.identity.password import hash_password
from src.identity.jwt_utils import verify_jwt


# ---------------------------------------------------------------------------
# App fixture — minimal FastAPI with only the identity router
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture(scope="module")
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Pool mock helpers
# ---------------------------------------------------------------------------

def _mock_pool(*fetchrow_results, fetch_result=None, execute_ok=True):
    """Minimal asyncpg pool mock. fetchrow_results consumed in order."""
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
# POST /v1/auth/login — happy path
# ---------------------------------------------------------------------------

class TestLoginSuccess:
    def test_returns_jwt_and_user(self, client: TestClient):
        uid = uuid.uuid4()
        row = _user_row(user_id=uid, email="alice@example.com", password="correct")
        pool, conn = _mock_pool(row, fetch_result=[])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post("/v1/auth/login", json={
                "email": "alice@example.com",
                "password": "correct",
            })

        assert resp.status_code == 200
        body = resp.json()
        assert "jwt" in body
        assert body["user"]["email"] == "alice@example.com"
        assert body["user"]["tenantId"] == "tenant-1"

    def test_issued_token_accepted_by_verify_jwt(self, client: TestClient):
        """Token issued by login must pass verify_jwt with correct claims."""
        uid = uuid.uuid4()
        row = _user_row(user_id=uid, email="bob@example.com", password="pass")
        pool, _ = _mock_pool(row, fetch_result=[])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post("/v1/auth/login", json={
                "email": "bob@example.com",
                "password": "pass",
            })

        token = resp.json()["jwt"]
        payload = verify_jwt(token)
        assert payload["sub"] == str(uid)
        assert payload["email"] == "bob@example.com"
        assert payload["tenantId"] == "tenant-1"

    def test_session_row_inserted(self, client: TestClient):
        """A session must be written to the sessions table on successful login."""
        uid = uuid.uuid4()
        row = _user_row(user_id=uid, password="pw")

        # Pool is called 3 times: fetchrow (user), fetch (licenses), execute (session)
        pool, conn = _mock_pool(row, fetch_result=[])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post("/v1/auth/login", json={
                "email": "user@example.com",
                "password": "pw",
            })

        assert resp.status_code == 200
        conn.execute.assert_awaited_once()
        call_args = conn.execute.call_args[0]
        assert "INSERT INTO sessions" in call_args[0]

    def test_app_licenses_included(self, client: TestClient):
        """app_licenses list is returned and mirrors what's in the DB."""
        uid = uuid.uuid4()
        row = _user_row(user_id=uid, password="pw")
        now = datetime.now(timezone.utc).date()
        license_row = {
            "app_id": "werking-report",
            "plan_id": "report-standard",
            "start_date": now,
            "end_date": None,
            "seats": 5,
        }
        pool, _ = _mock_pool(row, fetch_result=[license_row])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post("/v1/auth/login", json={
                "email": "user@example.com",
                "password": "pw",
            })

        body = resp.json()
        assert len(body["appLicenses"]) == 1
        assert body["appLicenses"][0]["appId"] == "werking-report"


# ---------------------------------------------------------------------------
# POST /v1/auth/login — failure paths (must all return identical 401)
# ---------------------------------------------------------------------------

_INVALID_CREDS_DETAIL = "Invalid credentials"


class TestLoginFailures:
    def test_wrong_password_returns_401(self, client: TestClient):
        uid = uuid.uuid4()
        row = _user_row(user_id=uid, password="correct")
        pool, _ = _mock_pool(row)

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post("/v1/auth/login", json={
                "email": "user@example.com",
                "password": "wrong",
            })

        assert resp.status_code == 401
        assert resp.json()["detail"] == _INVALID_CREDS_DETAIL

    def test_unknown_user_returns_same_401(self, client: TestClient):
        """Unknown user must produce the same response as wrong password (no enumeration)."""
        pool, _ = _mock_pool(None)  # fetchrow returns None → user not found

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post("/v1/auth/login", json={
                "email": "nobody@example.com",
                "password": "whatever",
            })

        assert resp.status_code == 401
        assert resp.json()["detail"] == _INVALID_CREDS_DETAIL

    def test_user_without_password_hash_returns_same_401(self, client: TestClient):
        """A user row without password_hash (SSO-only) must not be logged in via password."""
        uid = uuid.uuid4()
        now = datetime.now(timezone.utc)
        row = {
            "id": uid,
            "email": "sso@example.com",
            "name": "SSO User",
            "tenant_id": "tenant-sso",
            "role": "user",
            "password_hash": None,
            "created_at": now,
            "updated_at": now,
        }
        pool, _ = _mock_pool(row)

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post("/v1/auth/login", json={
                "email": "sso@example.com",
                "password": "anything",
            })

        assert resp.status_code == 401
        assert resp.json()["detail"] == _INVALID_CREDS_DETAIL

    def test_wrong_and_unknown_have_same_response_body(self, client: TestClient):
        """Security: wrong-password and unknown-user responses are indistinguishable."""
        uid = uuid.uuid4()
        row = _user_row(user_id=uid, password="correct")
        pool_wrong, _ = _mock_pool(row)
        pool_unknown, _ = _mock_pool(None)

        with patch("src.identity.routes.get_pool", return_value=pool_wrong):
            resp_wrong = client.post("/v1/auth/login", json={
                "email": "user@example.com",
                "password": "wrong",
            })

        with patch("src.identity.routes.get_pool", return_value=pool_unknown):
            resp_unknown = client.post("/v1/auth/login", json={
                "email": "nobody@example.com",
                "password": "anything",
            })

        assert resp_wrong.status_code == resp_unknown.status_code == 401
        assert resp_wrong.json()["detail"] == resp_unknown.json()["detail"]

    def test_db_error_raises_5xx(self, app: FastAPI):
        """DB failure must propagate as 5xx, never silently pass login through."""
        pool = MagicMock()

        @asynccontextmanager
        async def _failing_acquire():
            raise RuntimeError("DB connection failed")
            yield  # unreachable — keeps this an async generator for asynccontextmanager

        pool.acquire = _failing_acquire

        # raise_server_exceptions=False so the 500 becomes a response, not a re-raised exception
        no_raise_client = TestClient(app, raise_server_exceptions=False)
        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = no_raise_client.post("/v1/auth/login", json={
                "email": "user@example.com",
                "password": "pw",
            })

        assert resp.status_code >= 500


# ---------------------------------------------------------------------------
# POST /v1/auth/logout
# ---------------------------------------------------------------------------

class TestLogout:
    def _valid_token(self) -> str:
        from src.identity.jwt_utils import sign_jwt
        return sign_jwt(
            user_id=str(uuid.uuid4()),
            email="x@example.com",
            tenant_id="t-1",
            app_licenses=[],
        )

    def test_logout_revokes_session(self, client: TestClient):
        token = self._valid_token()
        pool, conn = _mock_pool()

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/logout",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 204
        conn.execute.assert_awaited_once()
        sql = conn.execute.call_args[0][0]
        assert "UPDATE sessions" in sql
        assert "expires_at" in sql

    def test_logout_without_token_returns_401(self, client: TestClient):
        resp = client.post("/v1/auth/logout")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /v1/auth/session
# ---------------------------------------------------------------------------

class TestGetSession:
    def _valid_token(self, user_id: uuid.UUID) -> str:
        from src.identity.jwt_utils import sign_jwt
        return sign_jwt(
            user_id=str(user_id),
            email="sess@example.com",
            tenant_id="t-sess",
            app_licenses=[],
        )

    def test_valid_session_returns_user(self, client: TestClient):
        uid = uuid.uuid4()
        token = self._valid_token(uid)
        future = datetime.now(timezone.utc) + timedelta(hours=8)
        session_row = {"expires_at": future}
        now = datetime.now(timezone.utc)
        user_row = {
            "id": uid,
            "email": "sess@example.com",
            "name": "Session User",
            "tenant_id": "t-sess",
            "role": "user",
            "created_at": now,
            "updated_at": now,
        }
        # /session: first fetchrow = session, second fetchrow = user, fetch = licenses
        pool, conn = _mock_pool(session_row, user_row, fetch_result=[])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.get(
                "/v1/auth/session",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        assert resp.json()["user"]["email"] == "sess@example.com"

    def test_expired_session_returns_401(self, client: TestClient):
        uid = uuid.uuid4()
        token = self._valid_token(uid)
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        session_row = {"expires_at": past}
        pool, _ = _mock_pool(session_row)

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.get(
                "/v1/auth/session",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 401

    def test_missing_session_returns_401(self, client: TestClient):
        uid = uuid.uuid4()
        token = self._valid_token(uid)
        pool, _ = _mock_pool(None)  # session not found

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.get(
                "/v1/auth/session",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 401

    def test_no_token_returns_401(self, client: TestClient):
        resp = client.get("/v1/auth/session")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /v1/auth/test-token
# ---------------------------------------------------------------------------

# Send the actually-configured service token so the test is self-consistent
# regardless of which BRIDGE_SERVICE_TOKEN the environment provides.
_SERVICE_TOKEN_HEADER = {"X-Bridge-Service-Token": config.service_token}


def _test_token_row(
    user_id: uuid.UUID | None = None,
    email: str = "test-user@test.werkingflow.com",
    name: str = "Test User",
    tenant_id: str = "werking-report-test",
    role: str = "user",
    account_type: str = "test",
) -> dict:
    """User row as returned by the test-token JOIN (carries account_type)."""
    uid = user_id or uuid.uuid4()
    now = datetime.now(timezone.utc)
    return {
        "id": uid,
        "email": email,
        "name": name,
        "tenant_id": tenant_id,
        "role": role,
        "created_at": now,
        "updated_at": now,
        "account_type": account_type,
    }


class TestTestToken:
    def test_test_tenant_user_gets_token(self, client: TestClient):
        uid = uuid.uuid4()
        row = _test_token_row(user_id=uid, email="t@test.werkingflow.com")
        pool, _ = _mock_pool(row, fetch_result=[])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/test-token",
                json={"email": "t@test.werkingflow.com"},
                headers=_SERVICE_TOKEN_HEADER,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["user"]["email"] == "t@test.werkingflow.com"
        payload = verify_jwt(body["jwt"])
        assert payload["sub"] == str(uid)

    def test_session_row_inserted(self, client: TestClient):
        row = _test_token_row()
        pool, conn = _mock_pool(row, fetch_result=[])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/test-token",
                json={"email": row["email"]},
                headers=_SERVICE_TOKEN_HEADER,
            )

        assert resp.status_code == 200
        conn.execute.assert_awaited_once()
        assert "INSERT INTO sessions" in conn.execute.call_args[0][0]

    def test_missing_service_token_returns_401(self, client: TestClient):
        resp = client.post(
            "/v1/auth/test-token",
            json={"email": "t@test.werkingflow.com"},
        )
        assert resp.status_code == 401

    def test_unknown_user_returns_404(self, client: TestClient):
        pool, _ = _mock_pool(None)
        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/test-token",
                json={"email": "nobody@test.werkingflow.com"},
                headers=_SERVICE_TOKEN_HEADER,
            )
        assert resp.status_code == 404

    def test_customer_tenant_refused_403(self, client: TestClient):
        """The wall: a service token can never mint a token for a customer."""
        row = _test_token_row(account_type="customer")
        pool, _ = _mock_pool(row, fetch_result=[])
        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/test-token",
                json={"email": row["email"]},
                headers=_SERVICE_TOKEN_HEADER,
            )
        assert resp.status_code == 403

    def test_internal_tenant_refused_403(self, client: TestClient):
        row = _test_token_row(account_type="internal")
        pool, _ = _mock_pool(row, fetch_result=[])
        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/test-token",
                json={"email": row["email"]},
                headers=_SERVICE_TOKEN_HEADER,
            )
        assert resp.status_code == 403
