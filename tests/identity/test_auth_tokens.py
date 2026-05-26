"""
Tests for the password-reset + email-verification endpoints (migration 018).

Endpoints under test:
- POST /v1/auth/forgot-password
- POST /v1/auth/reset-password-with-token
- POST /v1/auth/resend-verification
- POST /v1/auth/verify-email

Coverage per endpoint:
- Mint endpoints (forgot-password, resend-verification):
  * Happy-path: token issued, cleartext logged, 204
  * Anti-enumeration: unknown user → 204 silent, no token issued
  * Anti-enumeration: anonymized user → 204 silent
  * Resend-verification: already-verified user → 204 silent
  * Rate-limit: >= 3 tokens in last hour → 204 silent
  * Invalidates prior unused token before INSERT

- Consume endpoints (reset-password-with-token, verify-email):
  * Happy-path: valid token consumed, side-effect applied, 204
  * Invalid token (no row) → 400
  * Used token (used_at set) → 400
  * Expired token (expires_at in past) → 400
  * Anonymized account → 400 (closed accounts cannot be reset/verified)
  * Reset-password: sessions revoked, password rotated
  * Verify-email: users.email_verified set true

- Token-helper invariants:
  * _hash_token is deterministic, 64-char hex
  * _token_expiry_hours: env-overridable, fail-loud on garbage
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.identity.routes import (
    router,
    _hash_token,
    _token_expiry_hours,
)


# ---------------------------------------------------------------------------
# App fixture
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
# Pool / connection mock
#
# Routes use:
#   async with pool.acquire() as conn:
#       row = await conn.fetchrow(...)
#       async with conn.transaction(): ...
#       count = await conn.fetchval(...)
#       await conn.execute(...)
# ---------------------------------------------------------------------------

def _mock_pool(
    *,
    fetchrow_results: List[Any] | None = None,
    fetchval_results: List[Any] | None = None,
):
    """
    Build a pool/conn mock. `fetchrow_results` and `fetchval_results` are
    ordered side_effects — each await pops the next value. Defaults yield
    None / 0 respectively.
    """
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(
        side_effect=fetchrow_results if fetchrow_results is not None else [None],
    )
    conn.fetchval = AsyncMock(
        side_effect=fetchval_results if fetchval_results is not None else [0],
    )

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


def _user_lookup_row(
    *,
    user_id: uuid.UUID | None = None,
    anonymized: bool = False,
    email_verified: bool = False,
) -> dict:
    return {
        "id": user_id or uuid.uuid4(),
        "anonymized_at": datetime.now(timezone.utc) if anonymized else None,
        "email_verified": email_verified,
    }


def _token_row(
    *,
    user_id: uuid.UUID | None = None,
    used: bool = False,
    expired: bool = False,
    anonymized: bool = False,
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": uuid.uuid4(),
        "user_id": user_id or uuid.uuid4(),
        "expires_at": now - timedelta(hours=1) if expired else now + timedelta(hours=24),
        "used_at": now if used else None,
        "anonymized_at": now if anonymized else None,
    }


# ===========================================================================
# Token-helper invariants
# ===========================================================================

class TestTokenHelpers:
    def test_hash_token_is_deterministic_and_hex(self):
        h1 = _hash_token("abc123")
        h2 = _hash_token("abc123")
        assert h1 == h2
        assert len(h1) == 64
        int(h1, 16)  # must parse as hex

    def test_hash_token_differs_per_input(self):
        assert _hash_token("a") != _hash_token("b")

    def test_token_expiry_hours_defaults(self, monkeypatch):
        monkeypatch.delenv("TOKEN_EXPIRES_HOURS_RESET", raising=False)
        monkeypatch.delenv("TOKEN_EXPIRES_HOURS_VERIFY", raising=False)
        assert _token_expiry_hours("password_reset") == 24
        assert _token_expiry_hours("email_verification") == 72

    def test_token_expiry_hours_env_override(self, monkeypatch):
        monkeypatch.setenv("TOKEN_EXPIRES_HOURS_RESET", "6")
        monkeypatch.setenv("TOKEN_EXPIRES_HOURS_VERIFY", "12")
        assert _token_expiry_hours("password_reset") == 6
        assert _token_expiry_hours("email_verification") == 12

    def test_token_expiry_hours_fail_loud_on_garbage(self, monkeypatch):
        monkeypatch.setenv("TOKEN_EXPIRES_HOURS_RESET", "not-a-number")
        with pytest.raises(RuntimeError):
            _token_expiry_hours("password_reset")

    def test_token_expiry_hours_fail_loud_on_non_positive(self, monkeypatch):
        monkeypatch.setenv("TOKEN_EXPIRES_HOURS_RESET", "0")
        with pytest.raises(RuntimeError):
            _token_expiry_hours("password_reset")

    def test_token_expiry_hours_unknown_type(self):
        with pytest.raises(ValueError):
            _token_expiry_hours("not-a-real-type")


# ===========================================================================
# POST /v1/auth/forgot-password
# ===========================================================================

class TestForgotPassword:
    def test_happy_path_issues_token_and_logs_cleartext(
        self, client: TestClient, caplog
    ):
        uid = uuid.uuid4()
        pool, conn = _mock_pool(
            fetchrow_results=[_user_lookup_row(user_id=uid)],
            fetchval_results=[0],  # rate-limit count
        )

        with patch("src.identity.routes.get_pool", return_value=pool):
            with caplog.at_level("INFO", logger="src.identity.routes"):
                resp = client.post(
                    "/v1/auth/forgot-password",
                    json={"email": "alice@example.com"},
                )

        assert resp.status_code == 204
        # INSERT into auth_tokens must have happened
        executed = [c.args[0] for c in conn.execute.await_args_list]
        assert any("INSERT INTO auth_tokens" in s for s in executed), executed
        # Cleartext token logged for the mailer
        assert any("forgot_password: token issued" in r.message for r in caplog.records)

    def test_unknown_user_returns_204_silently(self, client: TestClient):
        """Anti-enumeration: same response shape for unknown emails."""
        pool, conn = _mock_pool(fetchrow_results=[None])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/forgot-password",
                json={"email": "nobody@example.com"},
            )

        assert resp.status_code == 204
        # No token issuance side-effects: no INSERT, no rate-limit count
        executed = [c.args[0] for c in conn.execute.await_args_list]
        assert not any("INSERT INTO auth_tokens" in s for s in executed), executed

    def test_anonymized_user_returns_204_silently(self, client: TestClient):
        pool, conn = _mock_pool(
            fetchrow_results=[_user_lookup_row(anonymized=True)],
        )

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/forgot-password",
                json={"email": "closed@example.com"},
            )

        assert resp.status_code == 204
        executed = [c.args[0] for c in conn.execute.await_args_list]
        assert not any("INSERT INTO auth_tokens" in s for s in executed)

    def test_rate_limit_suppresses_token_issuance(self, client: TestClient, caplog):
        pool, conn = _mock_pool(
            fetchrow_results=[_user_lookup_row()],
            fetchval_results=[3],  # already at limit
        )

        with patch("src.identity.routes.get_pool", return_value=pool):
            with caplog.at_level("INFO", logger="src.identity.routes"):
                resp = client.post(
                    "/v1/auth/forgot-password",
                    json={"email": "busy@example.com"},
                )

        assert resp.status_code == 204
        executed = [c.args[0] for c in conn.execute.await_args_list]
        assert not any("INSERT INTO auth_tokens" in s for s in executed), executed
        # Operator-visible log entry
        assert any("rate-limited" in r.message for r in caplog.records)

    def test_invalidates_prior_unused_token_before_insert(self, client: TestClient):
        """A new mint must mark old unused tokens used_at=NOW() first."""
        pool, conn = _mock_pool(
            fetchrow_results=[_user_lookup_row()],
            fetchval_results=[0],
        )

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/forgot-password",
                json={"email": "alice@example.com"},
            )

        assert resp.status_code == 204
        sqls = [c.args[0] for c in conn.execute.await_args_list]
        # Two writes: invalidate (UPDATE) then INSERT, in that order
        invalidate_idx = next(
            (i for i, s in enumerate(sqls) if "UPDATE auth_tokens" in s and "used_at" in s),
            -1,
        )
        insert_idx = next(
            (i for i, s in enumerate(sqls) if "INSERT INTO auth_tokens" in s),
            -1,
        )
        assert invalidate_idx >= 0 and insert_idx >= 0, sqls
        assert invalidate_idx < insert_idx, (invalidate_idx, insert_idx)

    def test_invalid_email_returns_422(self, client: TestClient):
        pool, conn = _mock_pool()
        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/forgot-password",
                json={"email": "not-an-email"},
            )
        assert resp.status_code == 422
        conn.execute.assert_not_awaited()


# ===========================================================================
# POST /v1/auth/reset-password-with-token
# ===========================================================================

class TestResetPasswordWithToken:
    def test_happy_path_rotates_password_and_revokes_sessions(
        self, client: TestClient
    ):
        uid = uuid.uuid4()
        pool, conn = _mock_pool(
            fetchrow_results=[_token_row(user_id=uid)],
        )

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/reset-password-with-token",
                json={"token": "real-token-hex", "newPassword": "new-strong-pw"},
            )

        assert resp.status_code == 204, resp.text
        sqls = [c.args[0] for c in conn.execute.await_args_list]
        assert any("UPDATE users" in s and "password_hash" in s for s in sqls), sqls
        assert any("UPDATE auth_tokens" in s and "used_at" in s for s in sqls), sqls
        assert any("DELETE FROM sessions" in s for s in sqls), sqls

    def test_invalid_token_returns_400(self, client: TestClient):
        pool, conn = _mock_pool(fetchrow_results=[None])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/reset-password-with-token",
                json={"token": "ghost", "newPassword": "new-strong-pw"},
            )

        assert resp.status_code == 400
        sqls = [c.args[0] for c in conn.execute.await_args_list]
        assert not any("UPDATE users" in s for s in sqls), sqls

    def test_used_token_returns_400(self, client: TestClient):
        pool, conn = _mock_pool(fetchrow_results=[_token_row(used=True)])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/reset-password-with-token",
                json={"token": "already-used", "newPassword": "new-strong-pw"},
            )

        assert resp.status_code == 400

    def test_expired_token_returns_400(self, client: TestClient):
        pool, conn = _mock_pool(fetchrow_results=[_token_row(expired=True)])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/reset-password-with-token",
                json={"token": "expired", "newPassword": "new-strong-pw"},
            )

        assert resp.status_code == 400

    def test_anonymized_user_returns_400(self, client: TestClient):
        """Closed accounts cannot be password-reset, even with a live token."""
        pool, conn = _mock_pool(fetchrow_results=[_token_row(anonymized=True)])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/reset-password-with-token",
                json={"token": "ghost-on-closed", "newPassword": "new-strong-pw"},
            )

        assert resp.status_code == 400

    def test_weak_password_returns_422(self, client: TestClient):
        pool, conn = _mock_pool(fetchrow_results=[_token_row()])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/reset-password-with-token",
                json={"token": "ok", "newPassword": "short"},
            )

        assert resp.status_code == 422
        conn.execute.assert_not_awaited()

    def test_empty_token_returns_422(self, client: TestClient):
        pool, conn = _mock_pool(fetchrow_results=[_token_row()])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/reset-password-with-token",
                json={"token": "", "newPassword": "new-strong-pw"},
            )

        assert resp.status_code == 422


# ===========================================================================
# POST /v1/auth/resend-verification
# ===========================================================================

class TestResendVerification:
    def test_happy_path_issues_verification_token(self, client: TestClient, caplog):
        pool, conn = _mock_pool(
            fetchrow_results=[_user_lookup_row(email_verified=False)],
            fetchval_results=[0],
        )

        with patch("src.identity.routes.get_pool", return_value=pool):
            with caplog.at_level("INFO", logger="src.identity.routes"):
                resp = client.post(
                    "/v1/auth/resend-verification",
                    json={"email": "unverified@example.com"},
                )

        assert resp.status_code == 204
        sqls = [c.args[0] for c in conn.execute.await_args_list]
        assert any("INSERT INTO auth_tokens" in s for s in sqls), sqls
        # The INSERT carries the email_verification token_type — assert it
        insert_call = next(c for c in conn.execute.await_args_list
                           if "INSERT INTO auth_tokens" in c.args[0])
        assert "email_verification" in insert_call.args, insert_call.args
        assert any("resend_verification: token issued" in r.message
                   for r in caplog.records)

    def test_unknown_user_returns_204_silently(self, client: TestClient):
        pool, conn = _mock_pool(fetchrow_results=[None])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/resend-verification",
                json={"email": "ghost@example.com"},
            )

        assert resp.status_code == 204
        sqls = [c.args[0] for c in conn.execute.await_args_list]
        assert not any("INSERT INTO auth_tokens" in s for s in sqls)

    def test_already_verified_user_returns_204_silently(self, client: TestClient):
        pool, conn = _mock_pool(
            fetchrow_results=[_user_lookup_row(email_verified=True)],
        )

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/resend-verification",
                json={"email": "already@example.com"},
            )

        assert resp.status_code == 204
        sqls = [c.args[0] for c in conn.execute.await_args_list]
        assert not any("INSERT INTO auth_tokens" in s for s in sqls), sqls

    def test_anonymized_user_returns_204_silently(self, client: TestClient):
        pool, conn = _mock_pool(
            fetchrow_results=[_user_lookup_row(anonymized=True)],
        )

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/resend-verification",
                json={"email": "closed@example.com"},
            )

        assert resp.status_code == 204
        sqls = [c.args[0] for c in conn.execute.await_args_list]
        assert not any("INSERT INTO auth_tokens" in s for s in sqls)

    def test_rate_limit_suppresses_issuance(self, client: TestClient):
        pool, conn = _mock_pool(
            fetchrow_results=[_user_lookup_row()],
            fetchval_results=[3],
        )

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/resend-verification",
                json={"email": "spammy@example.com"},
            )

        assert resp.status_code == 204
        sqls = [c.args[0] for c in conn.execute.await_args_list]
        assert not any("INSERT INTO auth_tokens" in s for s in sqls)


# ===========================================================================
# POST /v1/auth/verify-email
# ===========================================================================

class TestVerifyEmail:
    def test_happy_path_marks_email_verified(self, client: TestClient):
        uid = uuid.uuid4()
        pool, conn = _mock_pool(fetchrow_results=[_token_row(user_id=uid)])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/verify-email",
                json={"token": "ok-token"},
            )

        assert resp.status_code == 204, resp.text
        sqls = [c.args[0] for c in conn.execute.await_args_list]
        assert any("UPDATE users" in s and "email_verified" in s for s in sqls), sqls
        assert any("UPDATE auth_tokens" in s and "used_at" in s for s in sqls), sqls

    def test_invalid_token_returns_400(self, client: TestClient):
        pool, conn = _mock_pool(fetchrow_results=[None])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/verify-email",
                json={"token": "ghost"},
            )

        assert resp.status_code == 400
        sqls = [c.args[0] for c in conn.execute.await_args_list]
        assert not any("UPDATE users" in s for s in sqls)

    def test_used_token_returns_400(self, client: TestClient):
        pool, conn = _mock_pool(fetchrow_results=[_token_row(used=True)])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/verify-email",
                json={"token": "burnt"},
            )

        assert resp.status_code == 400

    def test_expired_token_returns_400(self, client: TestClient):
        pool, conn = _mock_pool(fetchrow_results=[_token_row(expired=True)])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/verify-email",
                json={"token": "stale"},
            )

        assert resp.status_code == 400

    def test_anonymized_user_returns_400(self, client: TestClient):
        pool, conn = _mock_pool(fetchrow_results=[_token_row(anonymized=True)])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/verify-email",
                json={"token": "on-closed-acct"},
            )

        assert resp.status_code == 400

    def test_empty_token_returns_422(self, client: TestClient):
        pool, conn = _mock_pool(fetchrow_results=[_token_row()])

        with patch("src.identity.routes.get_pool", return_value=pool):
            resp = client.post(
                "/v1/auth/verify-email",
                json={"token": ""},
            )

        assert resp.status_code == 422
