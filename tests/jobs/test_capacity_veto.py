"""Placement veto on POST /v1/jobs — capacity-locked worker rejects BEFORE persist.

A generic job executes in-process on the receiving worker (spawn in
create_job_endpoint) and its LLM self-call pins to that worker's account.
A weekly-/session-locked account therefore may not ACCEPT jobs: the veto
answers the account_exhausted 429 envelope synchronously, nginx's /v1/jobs
location retries the POST on the next worker (proxy_next_upstream http_429).
Retry safety hinges on ONE invariant, tested here: the reject happens with
NOTHING persisted and NOTHING dispatched.
"""
from __future__ import annotations

import os

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _make_app():
    from fastapi import FastAPI
    from src.jobs import routes

    app = FastAPI()
    app.include_router(routes.router)
    return app


@pytest.fixture()
def client():
    return TestClient(_make_app(), raise_server_exceptions=True)


def _auth_patch():
    return patch("src.jobs.routes.verify_api_key", new=AsyncMock(return_value=True))


def _enabled_patches():
    return (
        patch("src.jobs.routes._generic_jobs_enabled", return_value=True),
        patch("src.jobs.routes.is_db_enabled", return_value=True),
    )


def _locked_cap_lock(remaining: int = 1234):
    lock = MagicMock()
    lock.is_locked.return_value = True
    lock.remaining_s.return_value = remaining
    return lock


def _open_cap_lock():
    lock = MagicMock()
    lock.is_locked.return_value = False
    lock.remaining_s.return_value = 0
    return lock


def test_locked_worker_rejects_429_without_persist_or_dispatch(client):
    en1, en2 = _enabled_patches()
    create = AsyncMock()
    with (
        _auth_patch(), en1, en2,
        patch("src.jobs.routes.get_executor", return_value=lambda: None),
        patch("src.middleware.capacity_lock.get_capacity_lock",
              return_value=_locked_cap_lock(remaining=900)),
        patch.object(__import__("src.jobs.store", fromlist=["create_job"]),
                     "create_job", create),
        patch("src.jobs.routes.spawn") as spawn,
    ):
        resp = client.post("/v1/jobs", json={"kind": "k", "payload": {}})

    assert resp.status_code == 429
    body = resp.json()
    from src.middleware.bridge_error import REASON_ACCOUNT_WEEKLY_EXHAUSTED
    assert body["error"]["reason"] == REASON_ACCOUNT_WEEKLY_EXHAUSTED
    assert resp.headers.get("Retry-After") == "900"
    create.assert_not_awaited()      # NOTHING persisted …
    spawn.assert_not_called()        # … NOTHING dispatched → 429-retry is safe


def test_locked_worker_retry_after_floor_is_60s(client):
    en1, en2 = _enabled_patches()
    with (
        _auth_patch(), en1, en2,
        patch("src.jobs.routes.get_executor", return_value=lambda: None),
        patch("src.middleware.capacity_lock.get_capacity_lock",
              return_value=_locked_cap_lock(remaining=3)),
        patch("src.jobs.routes.spawn"),
    ):
        resp = client.post("/v1/jobs", json={"kind": "k", "payload": {}})
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") == "60"


def test_unlocked_worker_accepts_and_dispatches(client):
    en1, en2 = _enabled_patches()
    create = AsyncMock()
    with (
        _auth_patch(), en1, en2,
        patch("src.jobs.routes.get_executor", return_value=lambda: None),
        patch("src.middleware.capacity_lock.get_capacity_lock",
              return_value=_open_cap_lock()),
        patch("src.jobs.store.create_job", create),
        patch("src.jobs.routes.spawn") as spawn,
    ):
        resp = client.post("/v1/jobs", json={"kind": "k", "payload": {"n": 1}})

    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    create.assert_awaited_once()
    spawn.assert_called_once()
