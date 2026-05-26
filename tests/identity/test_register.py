"""
Tests for POST /v1/auth/register.

Coverage:
- Success → 200 with jwt + user + appLicenses (auto-approve, auto-login)
- Duplicate email → 409 (UniqueViolationError translated)
- Weak password (< 8 chars) → 422 (Pydantic schema)
- Invalid email format → 422 (Pydantic schema)
- Missing appId → 422 (Pydantic schema)
- Unknown appId → 400 (allowlist guard)
- Tenant row is inserted (DB assertion via execute call_args)
- App-license row is inserted with default plan_id='trial'
- Session row is inserted (auto-login)
- DB constraint violation other than email → fail-loud 5xx, not silent 200
- Issued JWT validates via verify_jwt with correct claims
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.identity.routes import router
from src.identity.jwt_utils import verify_jwt


# ---------------------------------------------------------------------------
# App fixture — only the identity router
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
# Pool/conn mock — supports execute/fetchrow/fetch + transaction()
# ---------------------------------------------------------------------------

def _mock_pool(
    *,
    user_row: dict | None = None,
    license_rows: list[dict] | None = None,
    user_fetchrow_side_effect=None,
    execute_side_effect=None,
):
    """
    Build an asyncpg pool/conn mock for the register flow.

    Defaults to a happy-path:
      - INSERT tenants (execute)         → None
      - INSERT users RETURNING (fetchrow) → user_row
      - UPDATE tenants  (execute)         → None
      - INSERT app_licenses (execute)     → None
      - SELECT app_licenses (fetch)       → license_rows
      - INSERT sessions  (execute)        → None

    Override via:
      - user_fetchrow_side_effect: replace fetchrow with this side_effect
        (e.g. an asyncpg exception to simulate INSERT failure).
      - execute_side_effect: list/exception consumed by conn.execute in order.
    """
    conn = AsyncMock()
    if execute_side_effect is not None:
        conn.execute = AsyncMock(side_effect=execute_side_effect)
    else:
        conn.execute = AsyncMock(return_value=None)

    if user_fetchrow_side_effect is not None:
        conn.fetchrow = AsyncMock(side_effect=user_fetchrow_side_effect)
    else:
        conn.fetchrow = AsyncMock(return_value=user_row)

    conn.fetch = AsyncMock(return_value=license_rows or [])

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


def _user_row(
    user_id: uuid.UUID | None = None,
    email: str = "new@example.com",
    name: str = "New User",
    tenant_id: str = "tenant-new",
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
        "provider_config": None,
        "created_at": now,
        "updated_at": now,
    }


def _license_row(
    app_id: str = "werking-report",
    plan_id: str = "trial",
    seats: int = 1,
) -> dict:
    today = datetime.now(timezone.utc).date()
    return {
        "app_id": app_id,
        "plan_id": plan_id,
        "start_date": today,
        "end_date": None,
        "seats": seats,
    }


def _valid_body(**overrides) -> dict:
    body = {
        "email": "new@example.com",
        "password": "long-enough-password",
        "name": "New User",
        "appId": "werking-report",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------

class TestRegisterSuccess:
    def test_returns_jwt_user_and_licenses(self, client: TestClient):
        uid = uuid.uuid4()
        user_row = _user_row(user_id=uid, email="alice@example.com", name="Alice")
        license_rows = [_license_row(app_id="werking-report", plan_id="trial")]
        pool, _ = _mock_pool(user_row=user_row, license_rows=license_rows)

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/register",
                json=_valid_body(email="alice@example.com", name="Alice"),
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "jwt" in body
        assert body["user"]["email"] == "alice@example.com"
        assert body["user"]["name"] == "Alice"
        assert len(body["appLicenses"]) == 1
        assert body["appLicenses"][0]["appId"] == "werking-report"
        assert body["appLicenses"][0]["planId"] == "trial"

    def test_issued_jwt_validates_with_correct_claims(self, client: TestClient):
        uid = uuid.uuid4()
        user_row = _user_row(user_id=uid, email="bob@example.com", tenant_id="t-bob")
        pool, _ = _mock_pool(user_row=user_row, license_rows=[_license_row()])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/register",
                json=_valid_body(email="bob@example.com"),
            )

        assert resp.status_code == 200, resp.text
        payload = verify_jwt(resp.json()["jwt"])
        assert payload["sub"] == str(uid)
        assert payload["email"] == "bob@example.com"
        assert payload["tenantId"] == "t-bob"
        assert payload["role"] == "user"

    def test_tenant_row_is_inserted(self, client: TestClient):
        """DB assertion: INSERT INTO tenants ... must be issued."""
        user_row = _user_row()
        pool, conn = _mock_pool(user_row=user_row, license_rows=[_license_row()])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post("/v1/auth/register", json=_valid_body())

        assert resp.status_code == 200
        executed_sqls = [c.args[0] for c in conn.execute.await_args_list]
        assert any("INSERT INTO tenants" in s for s in executed_sqls), executed_sqls

    def test_app_license_row_is_inserted_with_trial_plan(self, client: TestClient):
        """DB assertion: INSERT INTO app_licenses with plan_id='trial'."""
        user_row = _user_row()
        pool, conn = _mock_pool(user_row=user_row, license_rows=[_license_row()])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post("/v1/auth/register", json=_valid_body())

        assert resp.status_code == 200
        license_calls = [
            c for c in conn.execute.await_args_list
            if "INSERT INTO app_licenses" in c.args[0]
        ]
        assert len(license_calls) == 1, license_calls
        # plan_id arg position: $3 in the SQL → index 2 in args after the SQL string
        args = license_calls[0].args
        assert "trial" in args, f"plan_id 'trial' not in args: {args}"

    def test_session_row_is_inserted(self, client: TestClient):
        """Auto-login means a session row must be written."""
        user_row = _user_row()
        pool, conn = _mock_pool(user_row=user_row, license_rows=[_license_row()])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post("/v1/auth/register", json=_valid_body())

        assert resp.status_code == 200
        sql_strings = [c.args[0] for c in conn.execute.await_args_list]
        assert any("INSERT INTO sessions" in s for s in sql_strings), sql_strings


# ---------------------------------------------------------------------------
# Validation failures — must NOT touch the DB
# ---------------------------------------------------------------------------

class TestRegisterValidation:
    def test_duplicate_email_returns_409(self, client: TestClient):
        """
        UniqueViolationError on the users INSERT → 409 Conflict.
        Anti-enumeration is a UI concern (per task brief), Bridge is explicit.
        """
        exc = asyncpg.UniqueViolationError(
            'duplicate key value violates unique constraint "users_email_key"'
        )
        pool, _ = _mock_pool(user_fetchrow_side_effect=[exc])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/register",
                json=_valid_body(email="taken@example.com"),
            )

        assert resp.status_code == 409
        assert "taken@example.com" in resp.json()["detail"]

    def test_weak_password_returns_422(self, client: TestClient):
        """Pydantic min_length=8 → 422 before any DB call."""
        pool, conn = _mock_pool(user_row=_user_row())

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/register",
                json=_valid_body(password="short"),
            )

        assert resp.status_code == 422
        conn.execute.assert_not_awaited()
        conn.fetchrow.assert_not_awaited()

    def test_invalid_email_returns_422(self, client: TestClient):
        pool, conn = _mock_pool(user_row=_user_row())

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/register",
                json=_valid_body(email="not-an-email"),
            )

        assert resp.status_code == 422
        conn.execute.assert_not_awaited()

    def test_missing_app_id_returns_422(self, client: TestClient):
        pool, conn = _mock_pool(user_row=_user_row())

        body = _valid_body()
        del body["appId"]
        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post("/v1/auth/register", json=body)

        assert resp.status_code == 422
        conn.execute.assert_not_awaited()

    def test_missing_name_returns_422(self, client: TestClient):
        pool, conn = _mock_pool(user_row=_user_row())

        body = _valid_body()
        del body["name"]
        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post("/v1/auth/register", json=body)

        assert resp.status_code == 422
        conn.execute.assert_not_awaited()

    def test_unknown_app_id_returns_400(self, client: TestClient):
        """
        appId not in the app_id enum → 400 Bad Request from the allowlist,
        BEFORE any DB call. Failing later with a Postgres enum error would
        be a 500 with PG noise — that's why we guard up-front.
        """
        pool, conn = _mock_pool(user_row=_user_row())

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/register",
                json=_valid_body(appId="unknown-app"),
            )

        assert resp.status_code == 400
        assert "unknown-app" in resp.json()["detail"].lower() or "appid" in resp.json()["detail"].lower()
        conn.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Fail-loud on DB errors — must NOT silently return 200
# ---------------------------------------------------------------------------

class TestRegisterFailLoud:
    def test_postgres_error_on_insert_returns_5xx(self, app: FastAPI):
        """A generic PG error during tenant INSERT must not silently pass."""
        pool, _ = _mock_pool(
            user_row=_user_row(),
            execute_side_effect=asyncpg.PostgresError("simulated constraint failure"),
        )

        no_raise_client = TestClient(app, raise_server_exceptions=False)
        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = no_raise_client.post("/v1/auth/register", json=_valid_body())

        # 500 from our explicit re-raise, never a silent 200.
        assert resp.status_code >= 500
        assert resp.status_code < 600

    def test_check_id_is_accepted_when_present(self, client: TestClient):
        """checkId is optional but accepted — werking-report relies on this."""
        user_row = _user_row()
        pool, _ = _mock_pool(user_row=user_row, license_rows=[_license_row()])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/register",
                json=_valid_body(checkId="abc-123"),
            )

        assert resp.status_code == 200

    def test_check_id_null_is_accepted(self, client: TestClient):
        """checkId=None (omitted) is valid — most apps don't supply it."""
        user_row = _user_row()
        pool, _ = _mock_pool(user_row=user_row, license_rows=[_license_row()])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post("/v1/auth/register", json=_valid_body())

        assert resp.status_code == 200
