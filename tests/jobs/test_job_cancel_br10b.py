"""BR10b S1 — a DELETE that meets a RUNNING job is remembered.

Why: DELETE /v1/jobs/{id} answers 409 job_already_running for a job a worker
already has. Such a run can still park itself afterwards (defer_job: 429 in
the self-call, dependency wait) or lose its worker — and the watchdog then
started it again and billed it, although its owner had given it up. The
caller sends exactly one DELETE (energy job_abbrechen_bei_fristende) and
cannot catch that. The contract now:

  own + running                → 409 job_already_running (unchanged), plus
                                 error.cancel_requested = true; the wish is
                                 stored on the row (cancel_requested_at)
  … then the run parks itself  → 'cancelled', never resumed, never billed again
  … then its worker dies       → the watchdog closes it 'cancelled' instead of
                                 re-running it (also when its retries are spent)
  … then the run completes     → done/error as usual (the work was done)

The SQL runs against a real Postgres (BRIDGE_TEST_PG_URL) with the real
migrations including 063 — the row lock and the CASE in defer_job cannot be
shown with a mock.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from src.jobs import registry, routes, store, store_client
from src.jobs.executors import ExecutorHTTPError
from tests.jobs import test_job_cancel_br10 as br10
from tests.jobs.test_job_cancel_br10 import (
    HOME,
    JOB,
    OWNER_ATTR,
    OWNER_HEADERS,
    _delete,
    _err,
    _get,
    _insert,
    _status,
)

# BR10's fixtures, shared rather than re-typed (same store, same migrations).
mem = br10.mem
pg_store = br10.pg_store

# ---------------------------------------------------------------------------
# 1. The route
# ---------------------------------------------------------------------------


async def test_running_409_says_the_wish_is_recorded(mem, monkeypatch):
    mem.add(JOB, "running")
    monkeypatch.setattr(
        store_client,
        "cancel_job",
        AsyncMock(
            return_value={
                "status": "running",
                "changed": False,
                "cancel_requested": True,
            }
        ),
    )
    resp = await _delete(JOB, OWNER_HEADERS)
    err = _err(resp)
    assert resp.status_code == 409
    assert (err["code"], err["status"], err["retryable"]) == (
        "job_already_running",
        "running",
        False,
    )
    assert err["cancel_requested"] is True
    assert "will not be resumed" in err["message"]


async def test_running_409_behind_a_platform_api_without_the_wish(mem, monkeypatch):
    """A platform-api from before BR10b returns {status, changed} only. The
    body must not promise a wish nobody stored."""
    mem.add(JOB, "running")
    monkeypatch.setattr(
        store_client,
        "cancel_job",
        AsyncMock(return_value={"status": "running", "changed": False}),
    )
    resp = await _delete(JOB, OWNER_HEADERS)
    err = _err(resp)
    assert (resp.status_code, err["code"], err["cancel_requested"]) == (
        409,
        "job_already_running",
        False,
    )
    assert "resumed" not in err["message"]


@pytest.mark.parametrize("terminal", ["done", "error"])
async def test_terminal_409_carries_no_wish_field(mem, terminal):
    mem.add(JOB, terminal)
    err = _err(await _delete(JOB, OWNER_HEADERS))
    assert err["code"] == "job_terminal"
    assert "cancel_requested" not in err


# ---------------------------------------------------------------------------
# 2. The SQL
# ---------------------------------------------------------------------------


async def _wish(pool, job_id):
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT cancel_requested_at FROM ai_jobs WHERE job_id = $1", job_id
        )


async def _row(pool, job_id):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM ai_jobs WHERE job_id = $1", job_id)


async def test_sql_cancel_on_running_records_the_wish_once(pg_store):
    await _insert(pg_store, "j_run", "running")
    await _insert(pg_store, "j_done", "done")
    first = await store.cancel_job("j_run")
    assert first == {"status": "running", "changed": False, "cancel_requested": True}
    stamp = await _wish(pg_store, "j_run")
    assert stamp is not None
    await asyncio.sleep(0.05)
    assert await store.cancel_job("j_run") == first
    assert await _wish(pg_store, "j_run") == stamp  # idempotent, not re-stamped
    assert await _status(pg_store, "j_run") == "running"  # the run is not stopped
    await store.cancel_job("j_done")
    assert await _wish(pg_store, "j_done") is None  # only a running row records it


async def test_sql_defer_after_the_wish_cancels_instead_of_parking(pg_store):
    await _insert(pg_store, "j_w", "running")
    await store.cancel_job("j_w")
    await store.defer_job("j_w", 30, "429 no account capacity")
    assert await _status(pg_store, "j_w") == "cancelled"
    # Even once every timer has run out, nothing picks it up again.
    async with pg_store.acquire() as conn:
        await conn.execute(
            "UPDATE ai_jobs SET deferred_until = NOW() - interval '1 second', "
            "heartbeat_at = NOW() - interval '900 seconds'"
        )
    assert await store.claim_stale_job(90, 3) is None
    assert await store.mark_running("j_w") is False
    assert await store.find_abandoned(90, 3) == []
    active = await store.find_active()
    assert (active["active"], active["waiting"]) == (0, 0)
    assert await _status(pg_store, "j_w") == "cancelled"


async def test_sql_defer_without_a_wish_still_parks(pg_store):
    """Control: the ordinary capacity/dependency wait is unchanged."""
    await _insert(pg_store, "j_n", "running")
    await store.defer_job("j_n", 30, "429 no account capacity")
    row = await _row(pg_store, "j_n")
    assert (row["status"], row["defer_count"]) == ("pending", 1)
    assert row["deferred_until"] is not None


@pytest.mark.parametrize("attempts", [1, 3])
async def test_sql_dead_worker_with_the_wish_is_closed_not_resumed(pg_store, attempts):
    """Worker died mid-run (frozen heartbeat). Retries left (1) → it would be
    re-run; retries spent (3) → it would be failed 'error'. With the wish it
    is neither: the claim pass closes it 'cancelled'."""
    await _insert(pg_store, "j_dead", "running", heartbeat_age_s=900)
    async with pg_store.acquire() as conn:
        await conn.execute("UPDATE ai_jobs SET attempts = $1", attempts)
    await store.cancel_job("j_dead")
    assert await store.claim_stale_job(90, 3) is None
    assert await _status(pg_store, "j_dead") == "cancelled"
    assert await store.find_abandoned(90, 3) == []


async def test_sql_dead_worker_without_a_wish_is_still_resumed(pg_store):
    """Control: the watchdog's crash recovery is unchanged."""
    await _insert(pg_store, "j_dead", "running", heartbeat_age_s=900)
    claimed = await store.claim_stale_job(90, 3)
    assert claimed is not None and claimed["job_id"] == "j_dead"


async def test_sql_live_run_with_the_wish_is_left_alone(pg_store):
    await _insert(pg_store, "j_live", "running", heartbeat_age_s=5)
    await store.cancel_job("j_live")
    assert await store.claim_stale_job(90, 3) is None
    assert await _status(pg_store, "j_live") == "running"


async def test_sql_parked_row_with_the_wish_is_closed_at_once(pg_store):
    """Mixed rollout: a pre-BR10b worker on its direct-DB fallback parks a
    wished row as 'pending'. The claim pass must close it, not start it —
    and not only after its wait."""
    await _insert(pg_store, "j_old", "pending", deferred_in_s=600)
    async with pg_store.acquire() as conn:
        await conn.execute("UPDATE ai_jobs SET cancel_requested_at = NOW()")
    assert await store.claim_stale_job(90, 3) is None
    assert await _status(pg_store, "j_old") == "cancelled"


async def test_sql_run_that_completes_ends_done(pg_store):
    await _insert(pg_store, "j_ok", "running")
    await store.cancel_job("j_ok")
    await store.mark_done("j_ok", {"text": "fertig"})
    job = await store.get_job("j_ok")
    assert (job["status"], job["result"]) == ("done", {"text": "fertig"})


async def test_sql_race_defer_holds_the_lock_then_cancel(pg_store):
    """The run parks itself (uncommitted) while the DELETE arrives: the
    cancel waits for the row lock, then sees 'pending' and cancels."""
    await _insert(pg_store, "j_r1", "running")
    async with pg_store.acquire() as runner:
        tx = runner.transaction()
        await tx.start()
        await runner.execute(
            "UPDATE ai_jobs SET status = CASE WHEN cancel_requested_at IS NULL "
            "THEN 'pending' ELSE 'cancelled' END, "
            "deferred_until = NOW() + interval '30 seconds' WHERE job_id = 'j_r1'"
        )
        cancel = asyncio.create_task(store.cancel_job("j_r1"))
        await asyncio.sleep(0.3)
        assert not cancel.done(), "cancel did not wait for the defer's row lock"
        await tx.commit()
        assert (await asyncio.wait_for(cancel, 5))["status"] == "cancelled"
    assert await _status(pg_store, "j_r1") == "cancelled"


async def test_sql_race_cancel_holds_the_lock_then_defer(pg_store):
    """The other order: the DELETE holds the row (wish written, uncommitted)
    while the run parks itself. defer_job waits, then sees the wish."""
    await _insert(pg_store, "j_r2", "running")
    async with pg_store.acquire() as canceller:
        tx = canceller.transaction()
        await tx.start()
        await canceller.fetchval(
            "SELECT status FROM ai_jobs WHERE job_id = 'j_r2' FOR UPDATE"
        )
        await canceller.execute(
            "UPDATE ai_jobs SET cancel_requested_at = NOW() WHERE job_id = 'j_r2'"
        )
        defer = asyncio.create_task(store.defer_job("j_r2", 30, "429"))
        await asyncio.sleep(0.3)
        assert not defer.done(), "defer did not wait for the cancel's row lock"
        await tx.commit()
        await asyncio.wait_for(defer, 5)
    assert await _status(pg_store, "j_r2") == "cancelled"


# ---------------------------------------------------------------------------
# 3. End to end: route + runner + watchdog over the real store
# ---------------------------------------------------------------------------


@pytest.fixture
def wired(pg_store, monkeypatch):
    from src.main import extract_attribution_context

    monkeypatch.setenv("BRIDGE_ORIGIN_ID", HOME)
    monkeypatch.setattr(routes, "verify_api_key", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(routes, "_attribution_extractor", extract_attribution_context)
    for name in (
        "get_job", "cancel_job", "mark_running", "defer_job", "heartbeat",
        "update_progress", "mark_done", "mark_error", "claim_stale_job",
        "find_abandoned",
    ):
        monkeypatch.setattr(store_client, name, getattr(store, name))
    return pg_store


async def test_running_job_cancelled_then_refused_429_is_never_resumed(
    wired, caplog, monkeypatch
):
    """The S1 scenario as it happens: the worker runs the job, the owner's
    DELETE arrives (409), then the self-call is refused 429 and the runner
    parks the job. Before BR10b the watchdog re-ran it once the wait was
    over — a second executor call, a second bill."""
    seen = []

    async def executor(payload, attribution, report_progress):
        resp = await _delete(JOB, OWNER_HEADERS)
        seen.append((resp.status_code, _err(resp).get("cancel_requested")))
        raise ExecutorHTTPError(429, "no account capacity", retry_after_s=30)

    calls = AsyncMock(side_effect=executor)
    # The row's kind ('chat') is what a watchdog re-run would look up.
    monkeypatch.setitem(registry._EXECUTORS, "chat", calls)
    await _insert(wired, JOB, "pending")

    with caplog.at_level(logging.WARNING, logger=registry.logger.name):
        await registry.run_generic_job(JOB, "chat", {}, OWNER_ATTR)
    assert seen == [(409, True)]
    assert await _status(wired, JOB) == "cancelled"
    assert "cancelled instead of deferred" in caplog.text

    # Every timer runs out — the watchdog still finds nothing to start.
    async with wired.acquire() as conn:
        await conn.execute(
            "UPDATE ai_jobs SET deferred_until = NOW() - interval '1 second', "
            "heartbeat_at = NOW() - interval '900 seconds'"
        )
    out = await registry.run_watchdog_pass(90, 3)
    assert out == {"requeued": 0, "failed": 0}
    assert calls.await_count == 1
    assert (await _get(JOB, OWNER_HEADERS)).json()["status"] == "cancelled"


async def test_running_job_without_delete_is_still_resumed_after_429(
    wired, monkeypatch
):
    """Control: no DELETE → the parked job is re-run once its wait is over."""
    calls = AsyncMock(
        side_effect=[ExecutorHTTPError(429, "no account capacity"), {"ok": True}]
    )
    monkeypatch.setitem(registry._EXECUTORS, "chat", calls)
    await _insert(wired, JOB, "pending")
    await registry.run_generic_job(JOB, "chat", {}, OWNER_ATTR)
    assert await _status(wired, JOB) == "pending"
    async with wired.acquire() as conn:
        await conn.execute(
            "UPDATE ai_jobs SET deferred_until = NOW() - interval '1 second'"
        )
    spawned = []
    monkeypatch.setattr(registry, "spawn", lambda coro: spawned.append(coro))
    assert await registry.run_claim_pass(90, 3) == 1
    await spawned[0]
    assert calls.await_count == 2
    assert await _status(wired, JOB) == "done"
