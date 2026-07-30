"""The /v1/jobs placement veto must not block work that runs OFF the pool.

Companion to test_capacity_veto.py, which pins the veto itself. A `research`
job self-POSTs /v1/research (executors.research_executor) and inherits that
endpoint's pool-vs-cloud routing, so a capacity-locked worker CAN still serve
it via the research-cloud path. Vetoing it here would rebuild, one layer down,
the same gate that made the research-cloud overflow unreachable in exactly the
state it exists for (fixed 2026-07-30).

The veto stays conservative: anything not PROVABLY off-pool keeps it.
"""
from __future__ import annotations

import os

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


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


def _locked_cap_lock(remaining: int = 900):
    lock = MagicMock()
    lock.is_locked.return_value = True
    lock.remaining_s.return_value = remaining
    return lock


def _routing(result=None, raises=None):
    """Patch the shared routing decision the research handler also uses."""
    fn = AsyncMock(side_effect=raises) if raises else AsyncMock(return_value=result)
    return patch("src.research_cloud.routing.resolve_research_cloud_routing", fn), fn


# ---------------------------------------------------------------------------
# _job_runs_off_pool — the predicate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_non_research_kind_is_never_off_pool():
    from src.jobs.routes import JobCreateRequest, _job_runs_off_pool

    body = JobCreateRequest(kind="chat", payload={})
    assert await _job_runs_off_pool(body, {"user_id": "u1"}) is False


@pytest.mark.asyncio
async def test_research_job_routed_to_cloud_is_off_pool():
    from src.jobs.routes import JobCreateRequest, _job_runs_off_pool

    body = JobCreateRequest(kind="research", payload={"cloud_overflow": True})
    p, fn = _routing(result=True)
    with p:
        assert await _job_runs_off_pool(body, {"user_id": "u1"}) is True
    # Same inputs the executor's self-call will carry → same decision inside the job.
    fn.assert_awaited_once_with("u1", True)


@pytest.mark.asyncio
async def test_research_job_routed_to_pool_is_not_off_pool():
    from src.jobs.routes import JobCreateRequest, _job_runs_off_pool

    body = JobCreateRequest(kind="research", payload={})
    p, _ = _routing(result=False)
    with p:
        assert await _job_runs_off_pool(body, {"user_id": "u1"}) is False


@pytest.mark.asyncio
async def test_failed_routing_probe_keeps_the_veto():
    """A probe failure must never WIDEN admission."""
    from src.jobs.routes import JobCreateRequest, _job_runs_off_pool

    body = JobCreateRequest(kind="research", payload={"cloud_overflow": True})
    p, _ = _routing(raises=RuntimeError("db down"))
    with p:
        assert await _job_runs_off_pool(body, {"user_id": "u1"}) is False


# ---------------------------------------------------------------------------
# End-to-end through the endpoint, on a capacity-LOCKED worker
# ---------------------------------------------------------------------------
def test_locked_worker_accepts_a_cloud_bound_research_job(client):
    en1, en2 = _enabled_patches()
    create = AsyncMock()
    p, _ = _routing(result=True)
    with (
        _auth_patch(), en1, en2, p,
        patch("src.jobs.routes.get_executor", return_value=lambda: None),
        patch("src.middleware.capacity_lock.get_capacity_lock",
              return_value=_locked_cap_lock()),
        patch("src.jobs.store.create_job", create),
        patch("src.jobs.routes.spawn") as spawn,
    ):
        resp = client.post("/v1/jobs",
                           json={"kind": "research", "payload": {"cloud_overflow": True}})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    create.assert_awaited_once()
    spawn.assert_called_once()


def test_locked_worker_still_rejects_a_pool_bound_research_job(client):
    en1, en2 = _enabled_patches()
    create = AsyncMock()
    p, _ = _routing(result=False)
    with (
        _auth_patch(), en1, en2, p,
        patch("src.jobs.routes.get_executor", return_value=lambda: None),
        patch("src.middleware.capacity_lock.get_capacity_lock",
              return_value=_locked_cap_lock()),
        patch("src.jobs.store.create_job", create),
        patch("src.jobs.routes.spawn") as spawn,
    ):
        resp = client.post("/v1/jobs", json={"kind": "research", "payload": {}})

    assert resp.status_code == 429
    create.assert_not_awaited()
    spawn.assert_not_called()
