"""Tests für das generische Async-Job-System (src/jobs/).

DB-frei: der Postgres-Store wird gemockt. Geprüft wird die LOGIK —
Runner-Pfade (done/error/no-executor), Watchdog (requeue/fail-loud),
Digest-Stabilität, Flag-Gating. Der echte Store braucht Postgres und wird
separat (Integration) abgedeckt.
"""
from __future__ import annotations

import os
os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

from unittest.mock import AsyncMock, patch

import pytest

from src.jobs import registry, store, store_client


# ── Digest ──────────────────────────────────────────────────────────────────

def test_payload_digest_is_order_stable():
    a = store.payload_digest({"x": 1, "y": 2})
    b = store.payload_digest({"y": 2, "x": 1})
    assert a == b
    assert a.startswith("sha256:")


def test_payload_digest_differs_on_content():
    assert store.payload_digest({"x": 1}) != store.payload_digest({"x": 2})
    assert store.payload_digest(None) == store.payload_digest({})


# ── Registry ────────────────────────────────────────────────────────────────

async def test_register_and_get_executor():
    async def fake(payload, attribution, report_progress):
        return {"ok": True}

    registry.register_executor("unit-test-kind", fake)
    assert registry.get_executor("unit-test-kind") is fake
    assert "unit-test-kind" in registry.registered_kinds()
    assert registry.get_executor("does-not-exist") is None


# ── Runner ──────────────────────────────────────────────────────────────────

async def test_run_generic_job_done_path():
    async def good(payload, attribution, report_progress):
        await report_progress({"phase": "mid", "percent": 50})
        return {"result": payload["n"] * 2}

    registry.register_executor("good", good)
    with patch.multiple(
        store_client,
        mark_running=AsyncMock(),
        heartbeat=AsyncMock(),
        update_progress=AsyncMock(),
        mark_done=AsyncMock(),
        mark_error=AsyncMock(),
    ):
        await registry.run_generic_job("job_1", "good", {"n": 21}, {"app_id": "x"})
        store_client.mark_running.assert_awaited_once_with("job_1")
        store_client.mark_done.assert_awaited_once_with("job_1", {"result": 42})
        store_client.update_progress.assert_awaited()  # report_progress flowed through
        store_client.mark_error.assert_not_awaited()


async def test_run_generic_job_error_path():
    async def boom(payload, attribution, report_progress):
        raise RuntimeError("kaboom")

    registry.register_executor("boom", boom)
    with patch.multiple(
        store_client, mark_running=AsyncMock(), heartbeat=AsyncMock(),
        update_progress=AsyncMock(), mark_done=AsyncMock(), mark_error=AsyncMock(),
    ):
        await registry.run_generic_job("job_2", "boom", {}, None)
        store_client.mark_error.assert_awaited_once()
        args = store_client.mark_error.await_args
        assert args.args[0] == "job_2"
        assert "kaboom" in args.args[1]
        store_client.mark_done.assert_not_awaited()


async def test_run_generic_job_unknown_kind_fails_loud():
    with patch.multiple(store_client, mark_running=AsyncMock(), mark_error=AsyncMock()):
        await registry.run_generic_job("job_3", "no-such-kind", {}, None)
        store_client.mark_error.assert_awaited_once()
        assert store_client.mark_error.await_args.kwargs.get("code") == "NO_EXECUTOR"


# ── Watchdog ────────────────────────────────────────────────────────────────

async def test_watchdog_requeues_stale_and_fails_exhausted():
    import asyncio

    stale_job = {"job_id": "j_stale", "kind": "ping", "payload": {}, "attribution": None, "attempts": 2}
    dead_job = {"job_id": "j_dead", "kind": "ping", "payload": {}, "attribution": None, "attempts": 3}

    # claim returns the stale job once (already bumped to running), then None.
    with patch.multiple(
        store_client,
        claim_stale_job=AsyncMock(side_effect=[stale_job, None]),
        find_abandoned=AsyncMock(return_value=[dead_job]),
        mark_error=AsyncMock(),
    ), patch.object(registry, "_run_body", new=AsyncMock()) as run_body:
        counts = await registry.run_watchdog_pass(stale_seconds=90, max_attempts=3)
        assert counts == {"requeued": 1, "failed": 1}
        # _run_body for the claimed job is spawned → give the loop a tick.
        await asyncio.sleep(0)
        run_body.assert_awaited_once()
        assert run_body.await_args.args[0] == "j_stale"  # NO re-mark_running (already claimed)
        store_client.mark_error.assert_awaited_once()
        assert store_client.mark_error.await_args.kwargs.get("code") == "REQUEUE_EXHAUSTED"


# ── Flag gating ─────────────────────────────────────────────────────────────

def test_flag_helper_reflects_env(monkeypatch):
    from src.jobs import routes
    monkeypatch.setenv("BRIDGE_GENERIC_JOBS_ENABLED", "true")
    assert routes._generic_jobs_enabled() is True
    monkeypatch.setenv("BRIDGE_GENERIC_JOBS_ENABLED", "false")
    assert routes._generic_jobs_enabled() is False
    monkeypatch.delenv("BRIDGE_GENERIC_JOBS_ENABLED", raising=False)
    assert routes._generic_jobs_enabled() is False
