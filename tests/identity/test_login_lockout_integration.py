"""
Route-level integration test: POST /v1/auth/login actually enforces the
login_throttle lockout (security-audit-live-findings-20260818.md L10c/B.4).

Uses its own row fixture (including email_verified — the pre-existing
tests/identity/test_auth_routes.py fixture predates that column being read
by login() and is currently red for that unrelated reason; not touched
here to keep this fix's test surface isolated from that pre-existing drift).
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
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.identity import login_throttle
from src.identity.routes import router
from src.identity.password import hash_password


@pytest.fixture(autouse=True)
def _clean_throttle_state():
    login_throttle._reset_for_tests()
    yield
    login_throttle._reset_for_tests()


@pytest.fixture(autouse=True)
def _tight_limits(monkeypatch):
    monkeypatch.setenv("BRIDGE_LOGIN_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("BRIDGE_LOGIN_LOCKOUT_WINDOW_S", "300")


@pytest.fixture(scope="module")
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(router)
    return _app


@pytest.fixture(scope="module")
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _mock_pool(*fetchrow_results, fetch_result=None):
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow_results) * 20)  # reusable across repeated calls
    conn.fetch = AsyncMock(return_value=fetch_result or [])

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


def _user_row(email: str, password: str, email_verified: bool = True) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": uuid.uuid4(),
        "email": email,
        "name": "Test User",
        "tenant_id": "tenant-1",
        "role": "user",
        "provider_config": None,
        "password_hash": hash_password(password),
        "email_verified": email_verified,
        "created_at": now,
        "updated_at": now,
    }


class TestLockoutBlocksFurtherAttempts:
    def test_locked_out_after_max_wrong_attempts(self, client: TestClient):
        row = _user_row("victim@example.com", "correct")
        pool, _ = _mock_pool(row, fetch_result=[])

        with patch("src.identity.routes.get_pool", return_value=pool), \
             patch("src.identity.routes.billing_service.list_subscriptions", AsyncMock(return_value=[])):
            for _ in range(3):
                resp = client.post("/v1/auth/login", json={
                    "email": "victim@example.com", "password": "wrong",
                })
                assert resp.status_code == 401

            locked_resp = client.post("/v1/auth/login", json={
                "email": "victim@example.com", "password": "wrong",
            })

        assert locked_resp.status_code == 429
        assert locked_resp.json()["detail"]["code"] == "too_many_attempts"
        assert "Retry-After" in locked_resp.headers

    def test_lockout_blocks_even_the_correct_password(self, client: TestClient):
        """Once locked out, even the RIGHT password is rejected until the
        window clears — this is what makes it brute-force protection rather
        than just a wrong-password counter."""
        row = _user_row("victim2@example.com", "correct")
        pool, _ = _mock_pool(row, fetch_result=[])

        with patch("src.identity.routes.get_pool", return_value=pool), \
             patch("src.identity.routes.billing_service.list_subscriptions", AsyncMock(return_value=[])):
            for _ in range(3):
                client.post("/v1/auth/login", json={
                    "email": "victim2@example.com", "password": "wrong",
                })
            resp = client.post("/v1/auth/login", json={
                "email": "victim2@example.com", "password": "correct",
            })

        assert resp.status_code == 429

    def test_unrelated_email_not_affected(self, client: TestClient):
        """A brute-force sweep against one account must not lock out others."""
        attacked_row = _user_row("attacked@example.com", "correct")
        pool, _ = _mock_pool(attacked_row, fetch_result=[])

        with patch("src.identity.routes.get_pool", return_value=pool), \
             patch("src.identity.routes.billing_service.list_subscriptions", AsyncMock(return_value=[])):
            for _ in range(3):
                client.post("/v1/auth/login", json={
                    "email": "attacked@example.com", "password": "wrong",
                })

            bystander_row = _user_row("bystander@example.com", "pw")
            pool2, _ = _mock_pool(bystander_row, fetch_result=[])
            with patch("src.identity.routes.get_pool", return_value=pool2), \
                 patch("src.identity.routes.billing_service.list_subscriptions", AsyncMock(return_value=[])):
                resp = client.post("/v1/auth/login", json={
                    "email": "bystander@example.com", "password": "pw",
                })

        assert resp.status_code == 200


class TestSuccessfulLoginClearsThrottle:
    def test_correct_password_after_some_failures_still_succeeds(self, client: TestClient):
        """Below the lockout threshold, a typo-then-correct-password flow
        (the real, common legitimate case) must not be punished."""
        row = _user_row("typo@example.com", "correct")
        pool, _ = _mock_pool(row, fetch_result=[])

        with patch("src.identity.routes.get_pool", return_value=pool), \
             patch("src.identity.routes.billing_service.list_subscriptions", AsyncMock(return_value=[])):
            client.post("/v1/auth/login", json={"email": "typo@example.com", "password": "wroong"})
            resp = client.post("/v1/auth/login", json={"email": "typo@example.com", "password": "correct"})

        assert resp.status_code == 200

    def test_success_resets_counter_for_subsequent_typos(self, client: TestClient):
        row = _user_row("reset@example.com", "correct")
        pool, _ = _mock_pool(row, fetch_result=[])

        with patch("src.identity.routes.get_pool", return_value=pool), \
             patch("src.identity.routes.billing_service.list_subscriptions", AsyncMock(return_value=[])):
            client.post("/v1/auth/login", json={"email": "reset@example.com", "password": "wrong"})
            client.post("/v1/auth/login", json={"email": "reset@example.com", "password": "correct"})
            # Two more wrong attempts post-success must not trip the 3-strike lockout.
            r1 = client.post("/v1/auth/login", json={"email": "reset@example.com", "password": "wrong"})
            r2 = client.post("/v1/auth/login", json={"email": "reset@example.com", "password": "wrong"})

        assert r1.status_code == 401
        assert r2.status_code == 401  # not 429 — counter was cleared by the earlier success


class TestUnverifiedEmailNotCountedAsFailure:
    def test_correct_password_unverified_email_does_not_count_toward_lockout(self, client: TestClient):
        """The 403 email-not-verified gate proves the password was correct —
        it must not consume brute-force budget the way a wrong password does."""
        row = _user_row("unverified@example.com", "correct", email_verified=False)
        pool, _ = _mock_pool(row, fetch_result=[])

        with patch("src.identity.routes.get_pool", return_value=pool), \
             patch("src.identity.routes.billing_service.list_subscriptions", AsyncMock(return_value=[])):
            for _ in range(5):  # well past the 3-attempt threshold
                resp = client.post("/v1/auth/login", json={
                    "email": "unverified@example.com", "password": "correct",
                })
                assert resp.status_code == 403


class TestLoginStillWorksUnaffectedByThrottleBelowLimit:
    def test_first_time_correct_login_succeeds(self, client: TestClient):
        row = _user_row("newuser@example.com", "pw")
        pool, _ = _mock_pool(row, fetch_result=[])

        with patch("src.identity.routes.get_pool", return_value=pool), \
             patch("src.identity.routes.billing_service.list_subscriptions", AsyncMock(return_value=[])):
            resp = client.post("/v1/auth/login", json={
                "email": "newuser@example.com", "password": "pw",
            })

        assert resp.status_code == 200
        assert "jwt" in resp.json()
