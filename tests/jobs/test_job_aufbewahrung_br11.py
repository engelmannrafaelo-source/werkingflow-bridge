"""BR11 — job retention: never delete running or parked jobs, 2 h counted from
the terminal state, loud when a write finds its row gone (BR10R M-B).

Measured 10.10.2026 (BR10R §7, ttl-probe.txt) with the real SQL of
store.cleanup_old(7200): every job submitted 2 h 01 min ago was deleted —
done/error/cancelled that had finished 30 s earlier, a running job with a 5 s
old heartbeat, and a pending job parked for another 10 min. mark_done and
heartbeat on the deleted row then returned None: work done and booked, result
gone, poller told "unknown or expired".

Groups:
  (a)  only terminal rows are deleted, and their age counts from finished_at
  (b)  pending/running are never deleted by age; the 24 h backstop turns a
       still-parked job into a terminal error (not a deletion) and is loud
  (c)  mark_done / mark_error / heartbeat on a missing row raise, are counted,
       and the runner does not crash on it
  wire the worker never calls the old /cleanup route (its body on an old
       platform-api is the bug), and an old platform-api makes retention
       loud instead of silently deleting

SQL runs against a real Postgres when BRIDGE_TEST_PG_URL is set (same
convention as test_jobs_sofort_neu_vergeben.py), with the real migrations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest

from src.jobs import registry, store, store_client
from src.platform_client import PlatformResponse

REPO = Path(__file__).resolve().parents[2]
PG_URL = os.getenv("BRIDGE_TEST_PG_URL")
TTL = 2 * 60 * 60
MAX_AGE = 24 * 60 * 60

_JOBS_DDL = [
    REPO / "docker/migrations/031_ai_jobs.sql",
    REPO / "docker/migrations/044_ai_jobs_dependency_deferral.sql",
    REPO / "docker/migrations/063_ai_jobs_finished_at.sql",
    REPO / "docker/migrations/064_ai_jobs_cancel_requested.sql",
]


@pytest.fixture
async def pg():
    if not PG_URL:
        pytest.skip(
            "BRIDGE_TEST_PG_URL fehlt — Aufbewahrungs-SQL "
            "nicht gegen echten Postgres geprueft"
        )
    import asyncpg

    schema = f"br11_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(PG_URL)
    await admin.execute(f"CREATE SCHEMA {schema}")
    pool = await asyncpg.create_pool(
        PG_URL, min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    try:
        async with pool.acquire() as conn:
            for f in _JOBS_DDL:
                if f.exists():  # 063 is absent on the old code (red-against-old runs)
                    await conn.execute(f.read_text(encoding="utf-8"))
        with patch.object(store, "get_pool", return_value=pool):
            yield pool
    finally:
        await pool.close()
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()


async def _has_finished_at(pool) -> bool:
    async with pool.acquire() as c:
        return bool(
            await c.fetchval(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'ai_jobs' "
                "AND column_name = 'finished_at'"
            )
        )


async def _seed(pool, job_id, status, *, submit_ago, last_write_ago, deferred_in=None):
    """A row as the real store writes it, then moved back in time. For a
    terminal row last_write_ago is the moment it became terminal (updated_at
    and, where the column exists, finished_at)."""
    await store.create_job(job_id, "chat", {"x": 1}, {"app_id": "a"})
    terminal = status in ("done", "error", "cancelled")
    fin = (
        ", finished_at = NOW() - $4::interval"
        if terminal and await _has_finished_at(pool)
        else ""
    )
    dfr = "NOW() + $5::interval" if deferred_in else "NULL"
    async with pool.acquire() as c:
        await c.execute(
            f"""UPDATE ai_jobs SET status = $2,
                   created_at = NOW() - $3::interval,
                   updated_at = NOW() - $4::interval,
                   heartbeat_at = NOW() - $4::interval,
                   deferred_until = {dfr},
                   defer_count = CASE WHEN {dfr} IS NULL THEN 0 ELSE 1 END
                   {fin}
                 WHERE job_id = $1""",
            job_id,
            status,
            _iv(submit_ago),
            _iv(last_write_ago),
            *([_iv(deferred_in)] if deferred_in else []),
        )


def _iv(seconds: int):
    from datetime import timedelta

    return timedelta(seconds=seconds)


H = 3600
M = 60

# BR10R §7, row by row — plus the deletions that must still happen.
# (job, status, submit_ago, terminal/last-write ago, deferred_in, expected)
TABLE = [
    ("done_vor_30s_fertig_submit_vor_2h01", "done", 2 * H + M, 30, None, "bleibt"),
    ("error_vor_30s_fertig_submit_vor_2h01", "error", 2 * H + M, 30, None, "bleibt"),
    ("cancelled_vor_30s_submit_vor_2h01", "cancelled", 2 * H + M, 30, None, "bleibt"),
    (
        "running_heartbeat_frisch_submit_vor_2h01",
        "running",
        2 * H + M,
        5,
        None,
        "bleibt",
    ),
    (
        "pending_deferred_bis_in_10min_submit_vor_2h01",
        "pending",
        2 * H + M,
        5,
        10 * M,
        "bleibt",
    ),
    ("done_submit_vor_1h59_kontrolle", "done", 2 * H - M, 30, None, "bleibt"),
    # the retention still works: terminal for longer than 2 h -> gone
    ("done_fertig_vor_2h01", "done", 4 * H, 2 * H + M, None, "geloescht"),
    ("error_fertig_vor_2h01", "error", 4 * H, 2 * H + M, None, "geloescht"),
    ("cancelled_fertig_vor_2h01", "cancelled", 4 * H, 2 * H + M, None, "geloescht"),
    # a long-parked job (dependency patience is ~4 h) that finished just now
    ("done_nach_4h_warten_vor_30s_fertig", "done", 4 * H + 5 * M, 30, None, "bleibt"),
    # parked/running for 23 h: still below the backstop, never deleted
    ("pending_23h_geparkt", "pending", 23 * H, 60, 10 * M, "bleibt"),
    ("running_23h_alt_heartbeat_frisch", "running", 23 * H, 5, None, "bleibt"),
]


async def _apply_table(pg):
    for jid, st, sub, last, dfr, _ in TABLE:
        await _seed(pg, jid, st, submit_ago=sub, last_write_ago=last, deferred_in=dfr)


# ── (a)+(b): the BR10R §7 table, through the OLD route body ───────────────────
# store.cleanup_old is what POST /v1/internal/jobs-maintenance/cleanup runs —
# the route every worker from before BR11 still calls. Same name on old and new
# code, so this is the red-against-old measurement of M-B itself.


@pytest.mark.parametrize("row", TABLE, ids=[r[0] for r in TABLE])
async def test_table_cleanup_route(pg, row):
    await _apply_table(pg)
    await store.cleanup_old(TTL)
    jid, status, *_, expected = row
    job = await store.get_job(jid)
    if expected == "bleibt":
        assert job is not None, f"{jid} ({status}) was deleted by retention"
        assert job["status"] == status
    else:
        assert job is None, f"{jid} should have aged out"


# ── (a)+(b): the same table through the new retention pass ───────────────────


@pytest.mark.parametrize("row", TABLE, ids=[r[0] for r in TABLE])
async def test_table_prune(pg, row):
    await _apply_table(pg)
    r = await store.prune_jobs(TTL, MAX_AGE)
    jid, status, *_, expected = row
    job = await store.get_job(jid)
    if expected == "bleibt":
        assert job is not None and job["status"] == status
    else:
        assert job is None
    assert r["removed"] == 3 and r["expired"] == [] and r["running_over_max_age"] == 0


async def test_terminal_age_counts_from_finished_at_not_updated_at(pg):
    """A heartbeat tail after mark_done bumps updated_at; the clock is finished_at."""
    await store.create_job("j", "chat", {}, None)
    await store.mark_done("j", {"ok": True})
    async with pg.acquire() as c:
        await c.execute(
            "UPDATE ai_jobs SET finished_at = NOW() - interval '2 hours 1 minute', "
            "created_at = NOW() - interval '3 hours' WHERE job_id = 'j'"
        )
    await store.heartbeat("j")  # late beat: updated_at = now
    assert (await store.prune_jobs(TTL, MAX_AGE))["removed"] == 1
    assert await store.get_job("j") is None


async def test_terminal_transitions_stamp_finished_at(pg):
    await store.create_job("d", "chat", {}, None)
    await store.create_job("e", "chat", {}, None)
    await store.mark_done("d", {"ok": 1})
    await store.mark_error("e", "boom", code="X")
    for jid in ("d", "e"):
        assert (await store.get_job(jid))["finished_at"] is not None
    await store.create_job("p", "chat", {}, None)
    await store.heartbeat("p")
    assert (await store.get_job("p"))["finished_at"] is None


async def test_row_without_finished_at_ages_by_updated_at(pg):
    """Rows written before migration 063 / by an old platform-api."""
    await _seed(pg, "alt", "done", submit_ago=5 * H, last_write_ago=2 * H + M)
    async with pg.acquire() as c:
        await c.execute("UPDATE ai_jobs SET finished_at = NULL WHERE job_id = 'alt'")
    assert (await store.prune_jobs(TTL, MAX_AGE))["removed"] == 1


# ── BR10/BR10b x BR11: every way into 'cancelled' starts the retention clock ──


async def _cancelled_clock_is_fresh(pg, jid):
    job = await store.get_job(jid)
    assert job["status"] == "cancelled"
    assert job["finished_at"] is not None
    assert (await store.prune_jobs(TTL, MAX_AGE))["removed"] == 0
    async with pg.acquire() as c:
        await c.execute(
            "UPDATE ai_jobs SET finished_at = NOW() - interval '2 hours 1 minute' "
            "WHERE job_id = $1",
            jid,
        )
    assert (await store.prune_jobs(TTL, MAX_AGE))["removed"] == 1


async def test_cancel_of_pending_job_starts_the_clock(pg):
    await _seed(pg, "c1", "pending", submit_ago=3 * H, last_write_ago=60)
    await store.cancel_job("c1")
    await _cancelled_clock_is_fresh(pg, "c1")


async def test_cancel_wish_closed_by_defer_starts_the_clock(pg):
    await _seed(pg, "c2", "running", submit_ago=3 * H, last_write_ago=5)
    await store.cancel_job("c2")  # running: wish recorded
    await store.defer_job("c2", 60, "429")  # run parks itself -> cancelled
    await _cancelled_clock_is_fresh(pg, "c2")


async def test_cancel_wish_closed_by_watchdog_starts_the_clock(pg):
    await _seed(pg, "c3", "running", submit_ago=3 * H, last_write_ago=5)
    await store.cancel_job("c3")
    async with pg.acquire() as c:
        await c.execute(
            "UPDATE ai_jobs SET heartbeat_at = NOW() - interval '5 minutes' "
            "WHERE job_id = 'c3'"
        )
    assert await store.claim_stale_job(90, 3) is None  # closes it, claims nothing
    await _cancelled_clock_is_fresh(pg, "c3")


async def test_plain_defer_leaves_the_clock_unset(pg):
    await _seed(pg, "c4", "running", submit_ago=3 * H, last_write_ago=5)
    await store.defer_job("c4", 60, "429")
    job = await store.get_job("c4")
    assert job["status"] == "pending" and job["finished_at"] is None


# ── (b): the 24 h backstop is a loud terminal error, never a deletion ─────────


async def test_backstop_turns_parked_job_into_terminal_error(pg):
    await _seed(
        pg,
        "alt_geparkt",
        "pending",
        submit_ago=MAX_AGE + M,
        last_write_ago=60,
        deferred_in=10 * M,
    )
    await _seed(
        pg,
        "jung_geparkt",
        "pending",
        submit_ago=MAX_AGE - M,
        last_write_ago=60,
        deferred_in=10 * M,
    )
    await _seed(pg, "alt_laeuft", "running", submit_ago=MAX_AGE + M, last_write_ago=5)

    r = await store.prune_jobs(TTL, MAX_AGE)
    assert r["expired"] == ["alt_geparkt"]
    assert r["running_over_max_age"] == 1
    assert r["removed"] == 0

    job = await store.get_job("alt_geparkt")
    assert job["status"] == "error"
    assert job["error"]["code"] == "JOB_MAX_AGE_EXCEEDED"
    assert job["error"]["retryable"] is False
    assert job["finished_at"] is not None
    assert (await store.get_job("jung_geparkt"))["status"] == "pending"
    assert (await store.get_job("alt_laeuft"))["status"] == "running"

    # ... and it then ages out like any terminal row, 2 h after the backstop
    async with pg.acquire() as c:
        await c.execute(
            "UPDATE ai_jobs SET finished_at = NOW() - interval '2 hours 1 minute' "
            "WHERE job_id = 'alt_geparkt'"
        )
    assert (await store.prune_jobs(TTL, MAX_AGE))["removed"] == 1


async def test_backstop_never_takes_a_job_its_claimer_holds(pg):
    """Claim holds the row lock and flips pending -> running; the backstop's
    UPDATE waits, re-checks status='pending' and leaves it alone."""
    await _seed(pg, "rennen", "pending", submit_ago=MAX_AGE + M, last_write_ago=60)
    async with pg.acquire() as c:
        tx = c.transaction()
        await tx.start()
        await c.execute("SELECT 1 FROM ai_jobs WHERE job_id = 'rennen' FOR UPDATE")
        await c.execute(
            "UPDATE ai_jobs SET status = 'running', heartbeat_at = NOW() "
            "WHERE job_id = 'rennen'"
        )
        prune = asyncio.create_task(store.prune_jobs(TTL, MAX_AGE))
        await asyncio.sleep(0.5)
        assert not prune.done(), "backstop must wait for the claimer's row lock"
        await tx.commit()
    r = await asyncio.wait_for(prune, 5)
    assert r["expired"] == []
    assert (await store.get_job("rennen"))["status"] == "running"


# ── (c): writes on a missing row are loud ────────────────────────────────────


@pytest.mark.parametrize("op", ["mark_done", "mark_error", "heartbeat"])
async def test_write_on_deleted_row_raises(pg, op):
    """BR10R measured: old cleanup deleted a running job, then mark_done/heartbeat
    on it returned None. Now: JobRowMissing."""
    await _seed(pg, "weg", "running", submit_ago=2 * H + M, last_write_ago=5)
    async with pg.acquire() as c:
        await c.execute("DELETE FROM ai_jobs WHERE job_id = 'weg'")
    call = {
        "mark_done": lambda: store.mark_done("weg", {"bericht": "x"}),
        "mark_error": lambda: store.mark_error("weg", "boom", code="X"),
        "heartbeat": lambda: store.heartbeat("weg"),
    }[op]
    with pytest.raises(store.JobRowMissing) as exc:
        await call()
    assert exc.value.op == op and exc.value.job_id == "weg"


async def test_br10r_scenario_end_to_end(pg):
    """The exact BR10R sequence: retention pass, then the running job finishes."""
    await _seed(pg, "lauf", "running", submit_ago=2 * H + M, last_write_ago=5)
    await store.cleanup_old(TTL)
    await store.prune_jobs(TTL, MAX_AGE)
    await store.heartbeat("lauf")
    await store.mark_done("lauf", {"bericht": "x"})
    job = await store.get_job("lauf")
    assert job["status"] == "done" and job["result"] == {"bericht": "x"}


# ── (c) over the wire: internal route -> store_client ────────────────────────


def _internal_app():
    from fastapi import FastAPI

    from src.internal_routes import require_service_token, router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_service_token] = lambda: None
    return app


@pytest.mark.parametrize(
    "op,path,body",
    [
        ("mark_done", "/v1/internal/jobs/weg/done", {"result": {"a": 1}}),
        ("mark_error", "/v1/internal/jobs/weg/error", {"message": "m"}),
        ("heartbeat", "/v1/internal/jobs/weg/heartbeat", None),
    ],
)
def test_internal_route_answers_404_job_row_missing(op, path, body):
    from fastapi.testclient import TestClient

    with patch.object(
        store, op, new=AsyncMock(side_effect=store.JobRowMissing(op, "weg"))
    ):
        resp = TestClient(_internal_app()).post(path, json=body)
    assert resp.status_code == 404
    assert resp.json()["detail"] == {
        "reason": "job_row_missing",
        "op": op,
        "job_id": "weg",
    }


def test_internal_prune_route_and_bounds():
    from fastapi.testclient import TestClient

    client = TestClient(_internal_app())
    want = {"removed": 2, "expired": ["j"], "running_over_max_age": 0}
    with patch.object(store, "prune_jobs", new=AsyncMock(return_value=want)) as m:
        resp = client.post(
            "/v1/internal/jobs-maintenance/prune",
            json={"terminal_ttl_seconds": TTL, "max_age_seconds": MAX_AGE},
        )
    assert resp.status_code == 200 and resp.json() == want
    m.assert_awaited_once_with(TTL, MAX_AGE)
    # a backstop below every designed wait is a caller bug, refused
    assert (
        client.post(
            "/v1/internal/jobs-maintenance/prune",
            json={"terminal_ttl_seconds": TTL, "max_age_seconds": 3600},
        ).status_code
        == 422
    )


def _platform(status, body=None):
    return AsyncMock(return_value=PlatformResponse(status, body))


@pytest.mark.parametrize("op", ["mark_done", "mark_error", "heartbeat"])
async def test_store_client_row_missing_over_platform_is_loud_and_counted(op, caplog):
    args = {
        "mark_done": ("j", {"r": 1}),
        "mark_error": ("j", "m"),
        "heartbeat": ("j",),
    }[op]
    before = store_client.retention_snapshot()["row_missing_by_op"].get(op, 0)
    body = {"detail": {"reason": "job_row_missing", "op": op, "job_id": "j"}}
    with (
        patch.object(store_client, "call_platform", new=_platform(404, body)),
        caplog.at_level(logging.ERROR, logger="src.jobs.store_client"),
    ):
        with pytest.raises(store_client.JobRowMissing):
            await getattr(store_client, op)(*args)
    assert store_client.retention_snapshot()["row_missing_by_op"][op] == before + 1
    assert any("is GONE" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("op", ["mark_done", "mark_error", "heartbeat"])
async def test_store_client_row_missing_on_db_fallback_is_counted(op, monkeypatch):
    from src.platform_client import PlatformUnavailable

    monkeypatch.setattr(store_client, "is_db_enabled", lambda: True)
    args = {
        "mark_done": ("j", {"r": 1}),
        "mark_error": ("j", "m"),
        "heartbeat": ("j",),
    }[op]
    before = store_client.retention_snapshot()["row_missing_by_op"].get(op, 0)
    with (
        patch.object(
            store_client,
            "call_platform",
            new=AsyncMock(side_effect=PlatformUnavailable("down")),
        ),
        patch.object(
            store, op, new=AsyncMock(side_effect=store.JobRowMissing(op, "j"))
        ),
    ):
        with pytest.raises(store_client.JobRowMissing):
            await getattr(store_client, op)(*args)
    assert store_client.retention_snapshot()["row_missing_by_op"][op] == before + 1


async def test_store_client_bare_404_is_not_a_missing_row():
    """A 404 without the BR11 reason (e.g. a route that does not exist) stays a
    broken contract, not a silently reinterpreted datum."""
    with patch.object(
        store_client, "call_platform", new=_platform(404, {"detail": "Not Found"})
    ):
        with pytest.raises(store_client.JobStoreUnavailable):
            await store_client.mark_done("j", {"r": 1})


async def test_store_client_old_platform_204_stays_quiet():
    """platform-api from before BR11: 204 even for a missing row — degraded to
    the old behaviour, not worse, and no false alarm."""
    with patch.object(store_client, "call_platform", new=_platform(204)):
        assert await store_client.mark_done("j", {"r": 1}) is None


# ── wire: retention never reaches the old /cleanup body ──────────────────────


async def test_prune_calls_only_the_new_route():
    calls = []

    async def _api(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        return PlatformResponse(
            200, {"removed": 1, "expired": [], "running_over_max_age": 0}
        )

    with patch.object(store_client, "call_platform", new=_api):
        r = await store_client.prune_jobs(TTL, MAX_AGE)
    assert r["removed"] == 1
    assert calls == [
        (
            "POST",
            "/v1/internal/jobs-maintenance/prune",
            {"terminal_ttl_seconds": TTL, "max_age_seconds": MAX_AGE},
        )
    ]
    assert not hasattr(store_client, "cleanup_old"), (
        "a worker-side cleanup_old would reach the old deleting body "
        "on a pre-BR11 platform-api"
    )


@pytest.mark.parametrize("status", [404, 405])
async def test_prune_against_old_platform_api_deletes_nothing_and_is_loud(status):
    calls = []

    async def _api(method, path, **kw):
        calls.append(path)
        return PlatformResponse(status, {"detail": "Not Found"})

    before = store_client.retention_snapshot()["retention"].get(
        "prune_route_missing", 0
    )
    with patch.object(store_client, "call_platform", new=_api):
        with pytest.raises(store_client.RetentionRouteMissing):
            await store_client.prune_jobs(TTL, MAX_AGE)
    assert calls == ["/v1/internal/jobs-maintenance/prune"]  # no fallback to /cleanup
    assert (
        store_client.retention_snapshot()["retention"]["prune_route_missing"]
        == before + 1
    )


async def test_prune_malformed_answer_is_loud():
    with patch.object(
        store_client, "call_platform", new=_platform(200, {"removed": 1})
    ):
        with pytest.raises(store_client.JobStoreUnavailable):
            await store_client.prune_jobs(TTL, MAX_AGE)


# ── runner: a missing row never crashes the job task ─────────────────────────


async def test_run_body_mark_done_on_missing_row_does_not_raise_or_double_report():
    async def _exec(payload, attribution, report_progress):
        return {"ok": True}

    mark_error = AsyncMock()
    with (
        patch.dict(registry._EXECUTORS, {"t": _exec}),
        patch.object(
            store_client,
            "mark_done",
            new=AsyncMock(side_effect=store_client.JobRowMissing("mark_done", "j")),
        ),
        patch.object(store_client, "mark_error", new=mark_error),
        patch.object(store_client, "heartbeat", new=AsyncMock()),
    ):
        await registry._run_body("j", "t", {}, None)  # must not raise
    mark_error.assert_not_awaited()


async def test_run_body_mark_error_on_missing_row_does_not_raise():
    async def _exec(payload, attribution, report_progress):
        raise RuntimeError("boom")

    with (
        patch.dict(registry._EXECUTORS, {"t": _exec}),
        patch.object(
            store_client,
            "mark_error",
            new=AsyncMock(side_effect=store_client.JobRowMissing("mark_error", "j")),
        ),
        patch.object(store_client, "heartbeat", new=AsyncMock()),
    ):
        await registry._run_body("j", "t", {}, None)


async def test_heartbeat_loop_stops_on_missing_row(monkeypatch):
    monkeypatch.setattr(registry, "HEARTBEAT_INTERVAL_S", 0.01)
    hb = AsyncMock(side_effect=store_client.JobRowMissing("heartbeat", "j"))

    async def _exec(payload, attribution, report_progress):
        await asyncio.sleep(0.2)
        return {"ok": True}

    with (
        patch.dict(registry._EXECUTORS, {"t": _exec}),
        patch.object(store_client, "mark_done", new=AsyncMock()),
        patch.object(store_client, "heartbeat", new=hb),
    ):
        await registry._run_body("j", "t", {}, None)
    assert hb.await_count == 1


async def test_watchdog_continues_past_a_missing_row():
    abandoned = [{"job_id": "weg", "attempts": 3}, {"job_id": "da", "attempts": 3}]

    async def _mark_error(job_id, *a, **kw):
        if job_id == "weg":
            raise store_client.JobRowMissing("mark_error", job_id)

    with (
        patch.object(store_client, "claim_stale_job", new=AsyncMock(return_value=None)),
        patch.object(
            store_client, "find_abandoned", new=AsyncMock(return_value=abandoned)
        ),
        patch.object(
            store_client, "mark_error", new=AsyncMock(side_effect=_mark_error)
        ),
    ):
        counts = await registry.run_watchdog_pass(90, 3)
    assert counts == {"requeued": 0, "failed": 1}


# ── maintenance loop (main.py) ───────────────────────────────────────────────


def _main():
    try:
        import src.main as main
    except Exception as e:  # pragma: no cover — environment, not behaviour
        pytest.skip(f"src.main not importable here: {type(e).__name__}: {e}")
    return main


async def test_retention_pass_is_loud_about_expired_and_old_platform():
    main = _main()
    assert (
        main.GENERIC_JOB_TTL_SECONDS == TTL
        and main.GENERIC_JOB_MAX_AGE_SECONDS == MAX_AGE
    )
    snap = store_client.retention_snapshot()["retention"]
    exp0, run0 = (
        snap.get("expired_max_age", 0),
        snap.get("running_over_max_age_seen", 0),
    )
    r = {"removed": 2, "expired": ["alt"], "running_over_max_age": 1}
    # main's logger does not propagate to the root handler (logging_config) —
    # read what it was told directly.
    with (
        patch.object(store_client, "prune_jobs", new=AsyncMock(return_value=r)) as m,
        patch.object(main, "logger") as log,
    ):
        assert await main._generic_jobs_retention_pass(0) == 0
    m.assert_awaited_once_with(TTL, MAX_AGE)
    snap = store_client.retention_snapshot()["retention"]
    assert (
        snap["expired_max_age"] == exp0 + 1
        and snap["running_over_max_age_seen"] == run0 + 1
    )
    text = "\n".join(c.args[0] for c in log.error.call_args_list)
    assert "alt" in text and "JOB_MAX_AGE_EXCEEDED" in text and "running longer" in text

    with (
        patch.object(
            store_client,
            "prune_jobs",
            new=AsyncMock(side_effect=store_client.RetentionRouteMissing("old")),
        ),
        patch.object(main, "logger") as log,
    ):
        assert await main._generic_jobs_retention_pass(0) == 1
        assert await main._generic_jobs_retention_pass(1) == 2
    assert [
        c.args[0] for c in log.error.call_args_list if "retention skipped" in c.args[0]
    ] != []
    assert log.error.call_count == 1  # first one loud, then every 20th


async def test_watchdog_still_runs_when_retention_route_is_missing(monkeypatch):
    main = _main()
    monkeypatch.setattr(main, "GENERIC_JOB_MAINTENANCE_INTERVAL_S", 0)
    passes = []

    async def _watchdog(*a):
        passes.append(1)
        if len(passes) >= 2:
            raise asyncio.CancelledError
        return {"requeued": 0, "failed": 0}

    with (
        patch.object(main, "run_watchdog_pass", new=_watchdog),
        patch.object(
            store_client,
            "prune_jobs",
            new=AsyncMock(side_effect=store_client.RetentionRouteMissing("old")),
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await main._generic_jobs_maintenance_loop()
    assert len(passes) == 2


async def test_metrics_endpoint_exposes_counters():
    main = _main()
    snap = await main.get_jobs_retention_metrics(credentials=None)
    assert set(snap) == {"since_epoch", "row_missing_by_op", "retention"}
    json.dumps(snap)
