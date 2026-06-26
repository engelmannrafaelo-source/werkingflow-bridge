"""Tests for GET /v1/jobs list endpoint and store.list_jobs.

DB-free: Postgres store and attribution extractor are mocked. Validates:
  - 400 when no scope filter provided
  - 400 on invalid status/limit
  - 403 on cross-app / cross-user attribution mismatch
  - 503 when feature flag or DB disabled
  - Happy-path: jobs returned with correct shape + elapsed
  - store.list_jobs raises ValueError on bad inputs
  - Attribution scope allows caller with no headers (service-to-service)
"""
from __future__ import annotations

import os

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.jobs import store


# ── store.list_jobs unit tests (pure logic, no DB) ──────────────────────────

def test_list_jobs_raises_without_scope():
    """Fail loud: ValueError when neither app_id nor user_id is given."""
    import asyncio

    async def _run():
        with patch("src.jobs.store.get_pool"):
            await store.list_jobs()

    with pytest.raises(ValueError, match="requires at least one"):
        asyncio.get_event_loop().run_until_complete(_run())


def test_list_jobs_raises_on_bad_limit():
    import asyncio

    async def _run():
        with patch("src.jobs.store.get_pool"):
            await store.list_jobs(app_id="x", limit=0)

    with pytest.raises(ValueError, match="limit must be"):
        asyncio.get_event_loop().run_until_complete(_run())


def test_list_jobs_raises_on_unknown_status():
    import asyncio

    async def _run():
        with patch("src.jobs.store.get_pool"):
            await store.list_jobs(app_id="x", status="bogus")

    with pytest.raises(ValueError, match="Unknown status filter"):
        asyncio.get_event_loop().run_until_complete(_run())


def test_row_to_list_item_extracts_model_and_usage():
    """_row_to_list_item pulls model/usage from result (chat-style)."""
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    import json

    row = {
        "job_id": "job_abc",
        "kind": "chat",
        "status": "done",
        "attribution": json.dumps({"app_id": "myapp", "user_id": "u1"}),
        "result": json.dumps({"model": "claude-sonnet-4-6", "usage": {"input_tokens": 10, "output_tokens": 5}}),
        "progress": None,
        "created_at": now,
        "updated_at": now,
    }
    item = store._row_to_list_item(row)
    assert item["model"] == "claude-sonnet-4-6"
    assert item["usage"]["input_tokens"] == 10
    assert item["attribution"]["app_id"] == "myapp"
    assert item["elapsed_seconds"] == 0.0
    assert item["created_at"] == now.isoformat()


def test_row_to_list_item_falls_back_to_progress_model():
    """model falls back to progress.model when result has no model."""
    import json
    from datetime import timedelta

    created = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    updated = created + timedelta(seconds=30)

    row = {
        "job_id": "job_xyz",
        "kind": "research",
        "status": "running",
        "attribution": None,
        "result": None,
        "progress": json.dumps({"phase": "research", "model": "claude-sonnet-4-6"}),
        "created_at": created,
        "updated_at": updated,
    }
    item = store._row_to_list_item(row)
    assert item["model"] == "claude-sonnet-4-6"
    assert item["usage"] is None
    # running jobs: elapsed measured to now(), so we can only check it's >= 0
    assert item["elapsed_seconds"] is not None and item["elapsed_seconds"] >= 0


# ── Route-level tests (FastAPI TestClient, mocked store + flag) ──────────────

def _make_app():
    """Build a minimal FastAPI app with the jobs router attached."""
    from fastapi import FastAPI
    from src.jobs import routes

    app = FastAPI()
    app.include_router(routes.router)
    return app


def _fake_list_jobs_result(n=2):
    now_iso = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc).isoformat()
    return [
        {
            "job_id": f"job_{i}",
            "kind": "ping",
            "status": "done",
            "created_at": now_iso,
            "elapsed_seconds": float(i),
            "model": None,
            "usage": None,
            "attribution": {"app_id": "myapp", "user_id": "u1"},
        }
        for i in range(n)
    ]


def _patch_enabled():
    """Patch both the feature flag and DB check to True."""
    return patch.multiple(
        "src.jobs.routes",
        _generic_jobs_enabled=MagicMock(return_value=True),
    )


@pytest.fixture()
def client():
    return TestClient(_make_app(), raise_server_exceptions=True)


def _auth_patch():
    """Patch verify_api_key to a no-op for route tests."""
    return patch("src.jobs.routes.verify_api_key", new=AsyncMock(return_value=True))


def test_list_400_no_scope(client):
    with (
        _auth_patch(),
        patch("src.jobs.routes._generic_jobs_enabled", return_value=True),
        patch("src.jobs.routes.is_db_enabled", return_value=True),
    ):
        resp = client.get("/v1/jobs")
    assert resp.status_code == 400
    assert "app_id" in resp.json()["detail"]


def test_list_400_invalid_status(client):
    with (
        _auth_patch(),
        patch("src.jobs.routes._generic_jobs_enabled", return_value=True),
        patch("src.jobs.routes.is_db_enabled", return_value=True),
    ):
        resp = client.get("/v1/jobs?app_id=myapp&status=invalid")
    assert resp.status_code == 400
    assert "status" in resp.json()["detail"].lower()


def test_list_503_flag_off(client):
    with (
        _auth_patch(),
        patch("src.jobs.routes._generic_jobs_enabled", return_value=False),
    ):
        resp = client.get("/v1/jobs?app_id=myapp")
    assert resp.status_code == 503


def test_list_503_db_disabled(client):
    with (
        _auth_patch(),
        patch("src.jobs.routes._generic_jobs_enabled", return_value=True),
        patch("src.jobs.routes.is_db_enabled", return_value=False),
    ):
        resp = client.get("/v1/jobs?app_id=myapp")
    assert resp.status_code == 503


def test_list_403_cross_app_scope(client):
    """Caller sends X-App-ID: real-app but asks for app_id=other-app → 403."""
    from src.jobs import routes

    def fake_extractor(req):
        return {"app_id": "real-app", "user_id": None}

    with (
        _auth_patch(),
        patch("src.jobs.routes._generic_jobs_enabled", return_value=True),
        patch("src.jobs.routes.is_db_enabled", return_value=True),
        patch.object(routes, "_attribution_extractor", fake_extractor),
    ):
        resp = client.get("/v1/jobs?app_id=other-app", headers={"X-App-ID": "real-app"})
    assert resp.status_code == 403
    assert "other-app" in resp.json()["detail"]


def test_list_403_cross_user_scope(client):
    """Caller sends X-User-ID: real-user but asks for user_id=other-user → 403."""
    from src.jobs import routes

    def fake_extractor(req):
        return {"app_id": None, "user_id": "real-user"}

    with (
        _auth_patch(),
        patch("src.jobs.routes._generic_jobs_enabled", return_value=True),
        patch("src.jobs.routes.is_db_enabled", return_value=True),
        patch.object(routes, "_attribution_extractor", fake_extractor),
    ):
        resp = client.get("/v1/jobs?user_id=other-user", headers={"X-User-ID": "real-user"})
    assert resp.status_code == 403
    assert "other-user" in resp.json()["detail"]


def test_list_happy_path_returns_jobs(client):
    """Happy path: correct scope, store returns 2 jobs, response has correct shape."""
    from src.jobs import routes

    def fake_extractor(req):
        return {"app_id": "myapp", "user_id": "u1"}

    expected = _fake_list_jobs_result(2)

    with (
        _auth_patch(),
        patch("src.jobs.routes._generic_jobs_enabled", return_value=True),
        patch("src.jobs.routes.is_db_enabled", return_value=True),
        patch.object(routes, "_attribution_extractor", fake_extractor),
        patch.object(routes.store, "list_jobs", AsyncMock(return_value=expected)),
    ):
        resp = client.get("/v1/jobs?app_id=myapp&user_id=u1&limit=10")

    assert resp.status_code == 200
    body = resp.json()
    assert "jobs" in body
    assert len(body["jobs"]) == 2
    job = body["jobs"][0]
    for field in ("job_id", "kind", "status", "created_at", "elapsed_seconds", "model", "usage", "attribution"):
        assert field in job, f"missing field: {field}"


def test_list_no_attribution_headers_passes_scope(client):
    """Service-to-service: no X-App-ID header → extractor returns None → filter
    param is accepted as-is (no mismatch, no 403)."""
    from src.jobs import routes

    def fake_extractor(req):
        return {"app_id": None, "user_id": None}

    expected = _fake_list_jobs_result(1)

    with (
        _auth_patch(),
        patch("src.jobs.routes._generic_jobs_enabled", return_value=True),
        patch("src.jobs.routes.is_db_enabled", return_value=True),
        patch.object(routes, "_attribution_extractor", fake_extractor),
        patch.object(routes.store, "list_jobs", AsyncMock(return_value=expected)),
    ):
        resp = client.get("/v1/jobs?app_id=any-app")

    assert resp.status_code == 200


def test_list_passes_correct_params_to_store(client):
    """Verify store.list_jobs is called with the correct arguments."""
    from src.jobs import routes

    def fake_extractor(req):
        return {"app_id": "myapp", "user_id": None}

    mock_list = AsyncMock(return_value=[])

    with (
        _auth_patch(),
        patch("src.jobs.routes._generic_jobs_enabled", return_value=True),
        patch("src.jobs.routes.is_db_enabled", return_value=True),
        patch.object(routes, "_attribution_extractor", fake_extractor),
        patch.object(routes.store, "list_jobs", mock_list),
    ):
        resp = client.get("/v1/jobs?app_id=myapp&status=done&limit=25")

    assert resp.status_code == 200
    mock_list.assert_awaited_once_with(app_id="myapp", user_id=None, status="done", limit=25)
