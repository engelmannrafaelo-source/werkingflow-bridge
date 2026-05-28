"""
Unit tests for POST /v1/users/{user_id}/app-licenses
            and DELETE /v1/users/{user_id}/app-licenses/{app_id}

These tests do NOT require a running database. They exercise:
  - Admin auth guard (no token → 401, non-operator → 403)
  - Input validation (unknown app_id, unknown plan_id, bad UUID, bad date, endDate < startDate)
  - Happy-path grant (insert → created=true, upsert → created=false)
  - Happy-path revoke (204 on success, 404 when not found)
"""
from __future__ import annotations

import os
import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Set env vars before any src imports so config is initialised with these values.
os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

# ---------------------------------------------------------------------------
# Minimal FastAPI app with only the admin_db_router mounted
# ---------------------------------------------------------------------------

from fastapi import FastAPI
from src.config import config
from src.db.admin_routes import router as admin_db_router

_app = FastAPI()
_app.include_router(admin_db_router)


@pytest.fixture
def client():
    return TestClient(_app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _operator_headers() -> dict:
    """Service token without X-User-ID — unrestricted operator (is_operator=True)."""
    return {"X-Bridge-Service-Token": config.service_token}


VALID_USER_ID = str(uuid.uuid4())
VALID_APP_ID = "werking-report"
VALID_PLAN_ID = "trial"
VALID_START = "2026-01-01"
VALID_END = "2026-12-31"


def _mock_pool(conn_mock: Any):
    """Return a pool mock whose acquire() context manager yields conn_mock."""
    pool = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn_mock)
    cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=cm)
    return pool


# ---------------------------------------------------------------------------
# POST /v1/users/{user_id}/app-licenses — auth guard
# ---------------------------------------------------------------------------

class TestGrantAppLicenseAuth:
    def test_no_credentials_returns_401(self, client):
        r = client.post(f"/v1/users/{VALID_USER_ID}/app-licenses", json={
            "appId": VALID_APP_ID, "planId": VALID_PLAN_ID,
            "startDate": VALID_START, "seats": 1,
        })
        assert r.status_code == 401

    def test_non_operator_service_proxy_returns_403(self, client):
        """Service token + X-User-ID is a customer proxy, not an operator → 403."""
        r = client.post(
            f"/v1/users/{VALID_USER_ID}/app-licenses",
            json={"appId": VALID_APP_ID, "planId": VALID_PLAN_ID, "startDate": VALID_START},
            headers={**_operator_headers(), "X-User-ID": str(uuid.uuid4())},
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /v1/users/{user_id}/app-licenses — input validation
# ---------------------------------------------------------------------------

class TestGrantAppLicenseValidation:
    def test_unknown_app_id_returns_400(self, client):
        r = client.post(
            f"/v1/users/{VALID_USER_ID}/app-licenses",
            json={"appId": "not-a-real-app", "planId": VALID_PLAN_ID, "startDate": VALID_START},
            headers=_operator_headers(),
        )
        assert r.status_code == 400
        assert "appId" in r.json()["detail"]

    def test_unknown_plan_id_returns_400(self, client):
        r = client.post(
            f"/v1/users/{VALID_USER_ID}/app-licenses",
            json={"appId": VALID_APP_ID, "planId": "not-a-plan", "startDate": VALID_START},
            headers=_operator_headers(),
        )
        assert r.status_code == 400
        assert "planId" in r.json()["detail"]

    def test_invalid_user_id_uuid_returns_400(self, client):
        r = client.post(
            "/v1/users/not-a-uuid/app-licenses",
            json={"appId": VALID_APP_ID, "planId": VALID_PLAN_ID, "startDate": VALID_START},
            headers=_operator_headers(),
        )
        assert r.status_code == 400
        assert "userId" in r.json()["detail"].lower() or "uuid" in r.json()["detail"].lower()

    def test_bad_start_date_format_returns_400(self, client):
        r = client.post(
            f"/v1/users/{VALID_USER_ID}/app-licenses",
            json={"appId": VALID_APP_ID, "planId": VALID_PLAN_ID, "startDate": "01/01/2026"},
            headers=_operator_headers(),
        )
        assert r.status_code == 400
        assert "startDate" in r.json()["detail"]

    def test_bad_end_date_format_returns_400(self, client):
        r = client.post(
            f"/v1/users/{VALID_USER_ID}/app-licenses",
            json={"appId": VALID_APP_ID, "planId": VALID_PLAN_ID,
                  "startDate": VALID_START, "endDate": "not-a-date"},
            headers=_operator_headers(),
        )
        assert r.status_code == 400
        assert "endDate" in r.json()["detail"]

    def test_end_date_before_start_date_returns_400(self, client):
        r = client.post(
            f"/v1/users/{VALID_USER_ID}/app-licenses",
            json={"appId": VALID_APP_ID, "planId": VALID_PLAN_ID,
                  "startDate": "2026-06-01", "endDate": "2026-01-01"},
            headers=_operator_headers(),
        )
        assert r.status_code == 400
        assert "endDate" in r.json()["detail"]

    def test_missing_start_date_returns_422(self, client):
        r = client.post(
            f"/v1/users/{VALID_USER_ID}/app-licenses",
            json={"appId": VALID_APP_ID, "planId": VALID_PLAN_ID},
            headers=_operator_headers(),
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# POST /v1/users/{user_id}/app-licenses — happy path (mocked DB)
# ---------------------------------------------------------------------------

def _license_db_row(created: bool) -> Any:
    uid = uuid.UUID(VALID_USER_ID)
    row = MagicMock()
    row.__getitem__ = lambda self, k: {
        "user_id": uid,
        "app_id": VALID_APP_ID,
        "plan_id": VALID_PLAN_ID,
        "start_date": date.fromisoformat(VALID_START),
        "end_date": date.fromisoformat(VALID_END),
        "seats": 1,
        "created": created,
    }[k]
    return row


class TestGrantAppLicenseHappyPath:
    def test_insert_returns_created_true(self, client):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=1)  # user_exists
        conn.fetchrow = AsyncMock(return_value=_license_db_row(created=True))

        with patch("src.db.admin_routes.get_pool", return_value=_mock_pool(conn)):
            r = client.post(
                f"/v1/users/{VALID_USER_ID}/app-licenses",
                json={"appId": VALID_APP_ID, "planId": VALID_PLAN_ID,
                      "startDate": VALID_START, "endDate": VALID_END, "seats": 1},
                headers=_operator_headers(),
            )
        assert r.status_code == 200
        body = r.json()
        assert body["created"] is True
        assert body["appId"] == VALID_APP_ID
        assert body["planId"] == VALID_PLAN_ID
        assert body["startDate"] == VALID_START
        assert body["endDate"] == VALID_END
        assert body["userId"] == VALID_USER_ID

    def test_upsert_returns_created_false(self, client):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=1)
        conn.fetchrow = AsyncMock(return_value=_license_db_row(created=False))

        with patch("src.db.admin_routes.get_pool", return_value=_mock_pool(conn)):
            r = client.post(
                f"/v1/users/{VALID_USER_ID}/app-licenses",
                json={"appId": VALID_APP_ID, "planId": VALID_PLAN_ID,
                      "startDate": VALID_START, "endDate": VALID_END, "seats": 1},
                headers=_operator_headers(),
            )
        assert r.status_code == 200
        assert r.json()["created"] is False

    def test_user_not_found_returns_404(self, client):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=None)  # user_exists = None → 404

        with patch("src.db.admin_routes.get_pool", return_value=_mock_pool(conn)):
            r = client.post(
                f"/v1/users/{VALID_USER_ID}/app-licenses",
                json={"appId": VALID_APP_ID, "planId": VALID_PLAN_ID, "startDate": VALID_START},
                headers=_operator_headers(),
            )
        assert r.status_code == 404

    def test_null_end_date_allowed(self, client):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=1)
        row = MagicMock()
        uid = uuid.UUID(VALID_USER_ID)
        row.__getitem__ = lambda self, k: {
            "user_id": uid,
            "app_id": VALID_APP_ID,
            "plan_id": VALID_PLAN_ID,
            "start_date": date.fromisoformat(VALID_START),
            "end_date": None,
            "seats": 2,
            "created": True,
        }[k]
        conn.fetchrow = AsyncMock(return_value=row)

        with patch("src.db.admin_routes.get_pool", return_value=_mock_pool(conn)):
            r = client.post(
                f"/v1/users/{VALID_USER_ID}/app-licenses",
                json={"appId": VALID_APP_ID, "planId": VALID_PLAN_ID,
                      "startDate": VALID_START, "seats": 2},
                headers=_operator_headers(),
            )
        assert r.status_code == 200
        assert r.json()["endDate"] is None


# ---------------------------------------------------------------------------
# DELETE /v1/users/{user_id}/app-licenses/{app_id} — auth guard
# ---------------------------------------------------------------------------

class TestRevokeAppLicenseAuth:
    def test_no_credentials_returns_401(self, client):
        r = client.delete(f"/v1/users/{VALID_USER_ID}/app-licenses/{VALID_APP_ID}")
        assert r.status_code == 401

    def test_non_operator_returns_403(self, client):
        r = client.delete(
            f"/v1/users/{VALID_USER_ID}/app-licenses/{VALID_APP_ID}",
            headers={**_operator_headers(), "X-User-ID": str(uuid.uuid4())},
        )
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# DELETE /v1/users/{user_id}/app-licenses/{app_id} — validation + happy path
# ---------------------------------------------------------------------------

class TestRevokeAppLicenseValidation:
    def test_unknown_app_id_returns_400(self, client):
        r = client.delete(
            f"/v1/users/{VALID_USER_ID}/app-licenses/unknown-app",
            headers=_operator_headers(),
        )
        assert r.status_code == 400
        assert "app_id" in r.json()["detail"]

    def test_invalid_user_id_returns_400(self, client):
        r = client.delete(
            f"/v1/users/not-a-uuid/app-licenses/{VALID_APP_ID}",
            headers=_operator_headers(),
        )
        assert r.status_code == 400


class TestRevokeAppLicenseHappyPath:
    def test_delete_existing_license_returns_204(self, client):
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="DELETE 1")

        with patch("src.db.admin_routes.get_pool", return_value=_mock_pool(conn)):
            r = client.delete(
                f"/v1/users/{VALID_USER_ID}/app-licenses/{VALID_APP_ID}",
                headers=_operator_headers(),
            )
        assert r.status_code == 204

    def test_delete_nonexistent_license_returns_404(self, client):
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="DELETE 0")

        with patch("src.db.admin_routes.get_pool", return_value=_mock_pool(conn)):
            r = client.delete(
                f"/v1/users/{VALID_USER_ID}/app-licenses/{VALID_APP_ID}",
                headers=_operator_headers(),
            )
        assert r.status_code == 404
