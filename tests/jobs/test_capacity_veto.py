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
# ADR-0012: POST /v1/jobs mints an id carrying this bridge's marker and fails
# CLOSED without one — a job whose id cannot be routed is a job the peer bridge
# can never find. These tests are about the capacity veto, so they give the
# worker an identity and leave the id logic to tests/jobs/test_job_id_home.py.
os.environ.setdefault("BRIDGE_ORIGIN_ID", "test-bridge")

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
        patch("src.jobs.store_client.is_store_available", return_value=True),
    )


def _locked_cap_lock(remaining: int = 1234, reason: str = "session_window"):
    lock = MagicMock()
    lock.is_locked.return_value = True
    lock.remaining_s.return_value = remaining
    # The real CapacityLock records WHICH window closed when it locks; the veto
    # passes that on so the 429 names the limit the caller actually hit instead
    # of always saying "weekly" (see bridge_error.account_exhausted_error).
    lock.get_lock_info.return_value = {
        "worker": "test-worker",
        "locked_until_ts": 0.0,
        "reason": reason,
        "set_at_ts": 0.0,
    }
    return lock


def _open_cap_lock():
    lock = MagicMock()
    lock.is_locked.return_value = False
    lock.remaining_s.return_value = 0
    lock.get_lock_info.return_value = None
    return lock


def test_locked_worker_rejects_429_without_persist_or_dispatch(client):
    en1, en2 = _enabled_patches()
    create = AsyncMock()
    with (
        _auth_patch(), en1, en2,
        patch("src.jobs.routes.get_executor", return_value=lambda: None),
        patch("src.middleware.capacity_lock.get_capacity_lock",
              return_value=_locked_cap_lock(remaining=900)),
        patch.object(__import__("src.jobs.store_client", fromlist=["create_job"]),
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
        patch("src.jobs.store_client.create_job", create),
        patch("src.jobs.routes.spawn") as spawn,
    ):
        resp = client.post("/v1/jobs", json={"kind": "k", "payload": {"n": 1}})

    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"
    create.assert_awaited_once()
    spawn.assert_called_once()
