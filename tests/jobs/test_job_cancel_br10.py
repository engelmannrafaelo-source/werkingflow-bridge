"""BR10 — DELETE /v1/jobs/{id}: the owner withdraws a job no worker has started.

Why: a caller that gives up at its deadline (energy RETRY-2) left a 'pending'
or parked job behind, which the bridge started later anyway and billed for a
result nobody read. The contract, line by line:

  own + pending/deferred         → 200 {job_id, status:'cancelled'}, atomically,
                                   and no worker starts it afterwards
  own + already cancelled        → 200, the same body (idempotent)
  own + running (race lost too)  → 409 job_already_running, retryable:false
  own + done/error               → 409 job_terminal, retryable:false, status
  foreign or unknown             → 404 job_not_found, the SAME body, and the
                                   same store work in front of it
  cancelled                      → never billed, listed as 'cancelled'

Three layers, three groups:
  1. the route (in-memory store with the store's semantics, real attribution
     extractor from main.py),
  2. the SQL against a real Postgres (BRIDGE_TEST_PG_URL, the convention of
     tests/jobs/test_jobs_sofort_neu_vergeben.py) — the atomicity and the race
     are properties of the row lock and cannot be shown with a mock,
  3. the worker → platform-api seam (store_client + internal_routes) and the
     runner (registry).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import httpx
import pytest

from src.jobs import registry, routes, store, store_client

REPO = Path(__file__).resolve().parents[2]
PG_URL = os.getenv("BRIDGE_TEST_PG_URL")
HEX32 = "0123456789abcdef" * 2
HOME = "dev"
JOB = f"job_{HOME}_{HEX32}"
OWNER_HEADERS = {"X-App-ID": "werking-energy", "X-User-ID": "user-a"}
OWNER_ATTR = {"app_id": "werking-energy", "user_id": "user-a", "bridge_origin": "dev"}


# ---------------------------------------------------------------------------
# 1. The route
# ---------------------------------------------------------------------------


class MemStore:
    """get_job / cancel_job / mark_running with store.py's semantics, in memory.

    Counts every call so a test can show that a foreign id and an unknown id
    cost the route exactly the same store work."""

    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.calls: List[tuple] = []

    def add(self, job_id: str, status: str, attribution=None, **over) -> None:
        now = datetime.now(timezone.utc)
        self.rows[job_id] = {
            "job_id": job_id,
            "kind": "chat",
            "status": status,
            "attribution": OWNER_ATTR if attribution is None else attribution,
            "created_at": now - timedelta(minutes=2),
            "updated_at": now,
            "progress": None,
            "result": None,
            "error": None,
            "deferred_until": None,
            "defer_count": 0,
            "defer_reason": None,
            **over,
        }

    async def get_job(self, job_id):
        self.calls.append(("get_job", job_id))
        row = self.rows.get(job_id)
        return dict(row) if row else None

    async def cancel_job(self, job_id):
        self.calls.append(("cancel_job", job_id))
        row = self.rows.get(job_id)
        if row is None:
            return None
        if row["status"] != store.JOB_STATUS_PENDING:
            return {"status": row["status"], "changed": False}
        row["status"] = store.JOB_STATUS_CANCELLED
        return {"status": store.JOB_STATUS_CANCELLED, "changed": True}

    async def mark_running(self, job_id):
        self.calls.append(("mark_running", job_id))
        row = self.rows.get(job_id)
        if row is None or row["status"] != store.JOB_STATUS_PENDING:
            return False
        row["status"] = store.JOB_STATUS_RUNNING
        return True


@pytest.fixture
def mem(monkeypatch):
    from src.main import extract_attribution_context

    m = MemStore()
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", HOME)
    monkeypatch.setattr(routes, "verify_api_key", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "_require_enabled", lambda: None)
    # The real extractor — ownership must agree with how the submit attributed.
    monkeypatch.setattr(routes, "_attribution_extractor", extract_attribution_context)
    for name in ("get_job", "cancel_job", "mark_running"):
        monkeypatch.setattr(store_client, name, getattr(m, name))
    return m


def _app():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(routes.router)
    return app


async def _delete(job_id: str, headers=None) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as c:
        return await c.delete(f"/v1/jobs/{job_id}", headers=headers or {})


async def _get(job_id: str, headers=None) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as c:
        return await c.get(f"/v1/jobs/{job_id}", headers=headers or {})


def _err(resp: httpx.Response) -> Dict[str, Any]:
    body = resp.json()
    assert set(body) == {"error"}, body
    return body["error"]


def _comparable(resp: httpx.Response) -> Dict[str, Any]:
    """The body minus the wall-clock second it was built in."""
    err = dict(_err(resp))
    err.pop("timestamp")
    return {
        "status": resp.status_code,
        "error": err,
        "headers": {k: v for k, v in resp.headers.items() if k != "date"},
    }


async def test_pending_job_of_the_owner_is_cancelled(mem):
    mem.add(JOB, "pending")
    resp = await _delete(JOB, OWNER_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"job_id": JOB, "status": "cancelled"}
    assert mem.rows[JOB]["status"] == "cancelled"


async def test_deferred_job_is_cancellable(mem):
    """A parked job keeps status 'pending' with deferred_until in the future —
    exactly the job that would otherwise start later and be billed."""
    mem.add(
        JOB,
        "pending",
        deferred_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        defer_count=3,
        defer_reason="no account capacity",
    )
    resp = await _delete(JOB, OWNER_HEADERS)
    assert (resp.status_code, resp.json()["status"]) == (200, "cancelled")


async def test_cancel_is_idempotent(mem):
    mem.add(JOB, "pending")
    first = await _delete(JOB, OWNER_HEADERS)
    second = await _delete(JOB, OWNER_HEADERS)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"job_id": JOB, "status": "cancelled"}


async def test_running_job_is_409_already_running(mem):
    mem.add(JOB, "running")
    resp = await _delete(JOB, OWNER_HEADERS)
    assert resp.status_code == 409
    err = _err(resp)
    assert (err["code"], err["reason"], err["retryable"], err["status"]) == (
        "job_already_running",
        "job_already_running",
        False,
        "running",
    )
    assert mem.rows[JOB]["status"] == "running"


async def test_race_lost_to_the_worker_is_409_already_running(mem, monkeypatch):
    """The route read 'pending', but a worker claimed the row before the cancel
    reached the store. The answer must come from the store's atomic outcome,
    not from the status the route read a moment earlier."""
    mem.add(JOB, "pending")
    real_cancel = mem.cancel_job

    async def worker_wins_first(job_id):
        assert await mem.mark_running(job_id) is True  # the worker's claim lands
        return await real_cancel(job_id)

    monkeypatch.setattr(store_client, "cancel_job", worker_wins_first)
    resp = await _delete(JOB, OWNER_HEADERS)
    assert resp.status_code == 409
    assert _err(resp)["code"] == "job_already_running"
    assert mem.rows[JOB]["status"] == "running"


@pytest.mark.parametrize("terminal", ["done", "error"])
async def test_terminal_job_is_409_job_terminal_with_status(mem, terminal):
    mem.add(JOB, terminal)
    resp = await _delete(JOB, OWNER_HEADERS)
    assert resp.status_code == 409
    err = _err(resp)
    assert (err["code"], err["retryable"], err["status"]) == (
        "job_terminal",
        False,
        terminal,
    )
    assert mem.rows[JOB]["status"] == terminal


@pytest.mark.parametrize(
    "foreign_headers",
    [
        {"X-App-ID": "werking-energy", "X-User-ID": "user-b"},  # other user, same app
        {"X-App-ID": "werking-report", "X-User-ID": "user-a"},  # same user, other app
        {"X-App-ID": "werking-energy"},  # user missing
        {},  # no attribution at all
    ],
)
async def test_foreign_job_is_indistinguishable_from_an_unknown_one(
    mem, foreign_headers
):
    """Not yours = does not exist: same status, same body, same headers, and
    the same store work in front of it (one read, no write)."""
    other = f"job_{HOME}_{'f' * 32}"
    mem.add(JOB, "pending")
    foreign = await _delete(JOB, foreign_headers)
    calls_foreign = list(mem.calls)
    mem.calls.clear()
    # The unknown id differs only in the id; compare with the id swapped in.
    unknown = await _delete(other, foreign_headers)
    calls_unknown = list(mem.calls)

    assert foreign.status_code == unknown.status_code == 404
    a, b = _comparable(foreign), _comparable(unknown)
    assert json.loads(json.dumps(a).replace(JOB, "<id>")) == json.loads(
        json.dumps(b).replace(other, "<id>")
    )
    err = _err(foreign)
    assert (err["code"], err["reason"], err["retryable"]) == (
        "job_not_found",
        "job_not_found",
        False,
    )
    assert "status" not in err  # no state leak
    assert [c[0] for c in calls_foreign] == [c[0] for c in calls_unknown] == ["get_job"]
    assert mem.rows[JOB]["status"] == "pending"  # untouched


async def test_foreign_running_job_is_also_404_not_409(mem):
    """A 409 for someone else's job would confirm that it exists."""
    mem.add(JOB, "running")
    resp = await _delete(JOB, {"X-App-ID": "werking-energy", "X-User-ID": "user-b"})
    assert (resp.status_code, _err(resp)["code"]) == (404, "job_not_found")


async def test_owner_matching_uses_the_submit_headers_including_client_id_fallback(mem):
    """energy sends DELETE with the headers of its submit. A submit attributed
    only through X-Client-ID ("app/area/agent") must match the same headers."""
    mem.add(
        JOB,
        "pending",
        attribution={
            "app_id": "werking-energy",
            "user_id": None,
            "agent_id": "llm-client",
        },
    )
    resp = await _delete(JOB, {"X-Client-ID": "werking-energy/api/llm-client"})
    assert (resp.status_code, resp.json()["status"]) == (200, "cancelled")


async def test_agent_and_session_do_not_decide_ownership(mem):
    mem.add(JOB, "pending")
    resp = await _delete(
        JOB, {**OWNER_HEADERS, "X-Agent-ID": "other-agent", "X-Session-ID": "s-2"}
    )
    assert resp.status_code == 200


async def test_job_removed_between_read_and_cancel_is_404(mem, monkeypatch):
    mem.add(JOB, "pending")
    monkeypatch.setattr(store_client, "cancel_job", AsyncMock(return_value=None))
    resp = await _delete(JOB, OWNER_HEADERS)
    assert (resp.status_code, _err(resp)["code"]) == (404, "job_not_found")


async def test_malformed_and_misdirected_ids_keep_the_get_guard(mem):
    bad = await _delete("not-a-job", OWNER_HEADERS)
    assert bad.status_code == 400
    peer = await _delete(f"job_prod_{HEX32}", OWNER_HEADERS)
    assert peer.status_code == 421
    assert mem.calls == []  # the store is not even asked


async def test_no_extractor_fails_closed(mem, monkeypatch):
    mem.add(JOB, "pending")
    monkeypatch.setattr(routes, "_attribution_extractor", None)
    with pytest.raises(RuntimeError, match="attribution extractor"):
        await _delete(JOB, OWNER_HEADERS)
    assert mem.rows[JOB]["status"] == "pending"


async def test_get_after_cancel_reports_cancelled(mem):
    mem.add(
        JOB,
        "pending",
        deferred_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        defer_count=1,
        defer_reason="no account capacity",
    )
    await _delete(JOB, OWNER_HEADERS)
    body = (await _get(JOB, OWNER_HEADERS)).json()
    assert body["status"] == "cancelled"
    assert (
        body["result"],
        body["error"],
        body["deferred_until"],
        body["defer_reason"],
    ) == (None, None, None, None)
    # Terminal: elapsed is frozen at updated_at, not still counting.
    assert body["elapsed_seconds"] == pytest.approx(120, abs=2)


async def test_list_filter_accepts_cancelled(monkeypatch):
    from src.main import extract_attribution_context

    monkeypatch.setattr(routes, "verify_api_key", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(routes, "_attribution_extractor", extract_attribution_context)
    lister = AsyncMock(return_value=[])
    monkeypatch.setattr(store_client, "list_jobs", lister)
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as c:
        resp = await c.get(
            "/v1/jobs",
            params={"app_id": "werking-energy", "status": "cancelled"},
            headers=OWNER_HEADERS,
        )
    assert resp.status_code == 200
    assert lister.await_args.kwargs["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 3a. The runner: a cancelled job is never started, so never billed
# ---------------------------------------------------------------------------


async def test_cancelled_before_dispatch_never_runs_the_executor(mem):
    """POST persists 'pending' and spawns run_generic_job; a DELETE that lands
    before the spawned task claims the row must stop it. The executor is where
    the billed LLM call happens — not called means nothing to bill."""
    executor = AsyncMock(return_value={"ok": True})
    registry.register_executor("br10-probe", executor)
    mem.add(JOB, "pending")
    await _delete(JOB, OWNER_HEADERS)
    with patch.multiple(
        store_client,
        mark_done=AsyncMock(),
        mark_error=AsyncMock(),
        heartbeat=AsyncMock(),
    ):
        await registry.run_generic_job(JOB, "br10-probe", {}, OWNER_ATTR)
        executor.assert_not_awaited()
        store_client.mark_done.assert_not_awaited()
        store_client.mark_error.assert_not_awaited()
    assert mem.rows[JOB]["status"] == "cancelled"


async def test_claimed_job_still_runs(mem):
    executor = AsyncMock(return_value={"ok": True})
    registry.register_executor("br10-probe", executor)
    mem.add(JOB, "pending")
    with patch.multiple(
        store_client,
        mark_done=AsyncMock(),
        mark_error=AsyncMock(),
        heartbeat=AsyncMock(),
    ):
        await registry.run_generic_job(JOB, "br10-probe", {}, OWNER_ATTR)
        executor.assert_awaited_once()
        store_client.mark_done.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3b. The worker → platform-api seam
# ---------------------------------------------------------------------------


class _PResp:
    def __init__(self, status_code, json_body=None):
        self.status_code = status_code
        self.json = json_body


@pytest.mark.parametrize("status,expected", [(204, True), (409, False)])
async def test_store_client_mark_running_reads_the_claim(status, expected):
    with patch.object(
        store_client, "call_platform", AsyncMock(return_value=_PResp(status))
    ):
        assert await store_client.mark_running(JOB) is expected


async def test_store_client_mark_running_unknown_status_is_loud():
    with patch.object(
        store_client, "call_platform", AsyncMock(return_value=_PResp(500, {}))
    ):
        with pytest.raises(store_client.JobStoreUnavailable):
            await store_client.mark_running(JOB)


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"job": None}, None),
        (
            {"job": {"status": "cancelled", "changed": True}},
            {"status": "cancelled", "changed": True},
        ),
        (
            {"job": {"status": "running", "changed": False}},
            {"status": "running", "changed": False},
        ),
    ],
)
async def test_store_client_cancel_job_contract(body, expected):
    with patch.object(
        store_client, "call_platform", AsyncMock(return_value=_PResp(200, body))
    ):
        assert await store_client.cancel_job(JOB) == expected


@pytest.mark.parametrize(
    "status,body",
    [
        (404, {"detail": "Not Found"}),  # platform-api before BR10: no route
        (405, {"detail": "Method Not Allowed"}),
        (200, {"job": {"status": "cancelled"}}),  # malformed answer
    ],
)
async def test_store_client_cancel_job_never_reads_a_missing_route_as_missing_job(
    status, body
):
    with patch.object(
        store_client, "call_platform", AsyncMock(return_value=_PResp(status, body))
    ):
        with pytest.raises(store_client.JobStoreUnavailable):
            await store_client.cancel_job(JOB)


async def test_store_client_cancel_falls_back_to_the_direct_store():
    from src.platform_client import PlatformUnavailable

    with (
        patch.object(
            store_client,
            "call_platform",
            AsyncMock(side_effect=PlatformUnavailable("down")),
        ),
        patch.object(store_client, "is_db_enabled", return_value=True),
        patch.object(
            store,
            "cancel_job",
            AsyncMock(return_value={"status": "cancelled", "changed": True}),
        ) as direct,
    ):
        assert (await store_client.cancel_job(JOB))["status"] == "cancelled"
        direct.assert_awaited_once_with(JOB)


async def test_internal_routes_cancel_and_conditional_claim(monkeypatch):
    from fastapi import FastAPI

    from src import internal_routes

    app = FastAPI()
    app.include_router(internal_routes.router)
    app.dependency_overrides[internal_routes.require_service_token] = lambda: {
        "sub": "t"
    }
    monkeypatch.setattr(
        store,
        "cancel_job",
        AsyncMock(side_effect=[None, {"status": "cancelled", "changed": True}]),
    )
    monkeypatch.setattr(store, "mark_running", AsyncMock(side_effect=[True, False]))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://p") as c:
        r1 = await c.post(f"/v1/internal/jobs/{JOB}/cancel")
        r2 = await c.post(f"/v1/internal/jobs/{JOB}/cancel")
        m1 = await c.post(f"/v1/internal/jobs/{JOB}/mark-running")
        m2 = await c.post(f"/v1/internal/jobs/{JOB}/mark-running")
    assert (r1.status_code, r1.json()) == (200, {"job": None})
    assert (r2.status_code, r2.json()) == (
        200,
        {"job": {"status": "cancelled", "changed": True}},
    )
    assert (m1.status_code, m2.status_code) == (204, 409)


# ---------------------------------------------------------------------------
# 2. The SQL, against a real Postgres
# ---------------------------------------------------------------------------

_JOBS_DDL = [
    REPO / "docker" / "migrations" / "031_ai_jobs.sql",
    REPO / "docker" / "migrations" / "044_ai_jobs_dependency_deferral.sql",
    REPO / "docker" / "migrations" / "063_ai_jobs_finished_at.sql",
    REPO / "docker" / "migrations" / "064_ai_jobs_cancel_requested.sql",
]


@pytest.fixture
async def pg_store():
    if not PG_URL:
        pytest.skip(
            "BRIDGE_TEST_PG_URL fehlt — Abbruch-SQL nicht gegen echten Postgres "
            "geprueft"
        )
    import asyncpg

    schema = f"jobs_cancel_probe_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(PG_URL)
    await admin.execute(f"CREATE SCHEMA {schema}")
    pool = await asyncpg.create_pool(
        PG_URL, min_size=2, max_size=4, server_settings={"search_path": schema}
    )
    try:
        async with pool.acquire() as conn:
            for f in _JOBS_DDL:  # the real migrations, not a re-typed copy
                await conn.execute(f.read_text(encoding="utf-8"))
        with patch.object(store, "get_pool", return_value=pool):
            yield pool
    finally:
        await pool.close()
        await admin.execute(f"DROP SCHEMA {schema} CASCADE")
        await admin.close()


async def _insert(
    pool,
    job_id,
    status,
    *,
    deferred_in_s=None,
    heartbeat_age_s=0,
    created_age_s=200,
    attribution=None,
):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ai_jobs (job_id, kind, status, attribution, heartbeat_at,
                                 deferred_until, created_at)
            VALUES ($1, 'chat', $2, $3::jsonb,
                    NOW() - ($4 || ' seconds')::interval,
                    CASE WHEN $5::text IS NULL THEN NULL
                         ELSE NOW() + ($5::text || ' seconds')::interval END,
                    NOW() - ($6 || ' seconds')::interval)
            """,
            job_id,
            status,
            json.dumps(attribution or OWNER_ATTR),
            str(heartbeat_age_s),
            None if deferred_in_s is None else str(deferred_in_s),
            str(created_age_s),
        )


async def _status(pool, job_id) -> Optional[str]:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT status FROM ai_jobs WHERE job_id = $1", job_id
        )


async def test_sql_cancel_by_status(pg_store):
    cases = {
        "j_pend": ("pending", None),
        "j_def": ("pending", 300),
        "j_run": ("running", None),
        "j_done": ("done", None),
        "j_err": ("error", None),
    }
    for jid, (st, d) in cases.items():
        await _insert(pg_store, jid, st, deferred_in_s=d)
    assert await store.cancel_job("j_pend") == {
        "status": "cancelled",
        "changed": True,
        "cancel_requested": False,
    }
    assert await store.cancel_job("j_def") == {
        "status": "cancelled",
        "changed": True,
        "cancel_requested": False,
    }
    assert await store.cancel_job("j_run") == {
        "status": "running",
        "changed": False,
        "cancel_requested": True,
    }
    assert await store.cancel_job("j_done") == {
        "status": "done",
        "changed": False,
        "cancel_requested": False,
    }
    assert await store.cancel_job("j_err") == {
        "status": "error",
        "changed": False,
        "cancel_requested": False,
    }
    assert await store.cancel_job("j_none") is None
    # Idempotent: the second cancel reports the state, it does not move it.
    assert await store.cancel_job("j_pend") == {
        "status": "cancelled",
        "changed": False,
        "cancel_requested": False,
    }
    assert [await _status(pg_store, j) for j in cases] == [
        "cancelled",
        "cancelled",
        "running",
        "done",
        "error",
    ]


async def test_sql_cancelled_job_is_never_claimed(pg_store):
    """Every way a job gets started selects 'pending'/'running' only: the fresh
    claim (mark_running) and the watchdog claim — even for a row that would be
    due (deferral over) AND stale."""
    await _insert(pg_store, "j_fresh", "pending")
    await _insert(pg_store, "j_due", "pending", deferred_in_s=300, heartbeat_age_s=900)
    await store.cancel_job("j_fresh")
    await store.cancel_job("j_due")
    async with pg_store.acquire() as conn:  # the wait runs out, the heartbeat is old
        await conn.execute(
            "UPDATE ai_jobs SET deferred_until = NOW() - interval '1 second'"
        )
    assert await store.mark_running("j_fresh") is False
    assert await store.claim_stale_job(90, 3) is None
    assert await store.find_abandoned(90, 3) == []
    active = await store.find_active()
    assert (active["active"], active["waiting"]) == (0, 0)
    assert [await _status(pg_store, j) for j in ("j_fresh", "j_due")] == [
        "cancelled"
    ] * 2


async def test_sql_mark_running_claims_only_pending(pg_store):
    await _insert(pg_store, "j_p", "pending")
    await _insert(pg_store, "j_r", "running")
    assert await store.mark_running("j_p") is True
    assert await store.mark_running("j_p") is False  # second claim of the same row
    assert await store.mark_running("j_r") is False
    assert await store.mark_running("j_missing") is False


async def test_sql_race_worker_claim_commits_first(pg_store):
    """Deterministic interleaving on the row lock: a worker's claim holds the
    row (uncommitted) while the DELETE arrives. The cancel must WAIT, then see
    'running' — never cancel a job a worker already owns."""
    await _insert(pg_store, "j_race", "pending")
    async with pg_store.acquire() as worker:
        tx = worker.transaction()
        await tx.start()
        await worker.execute(
            "UPDATE ai_jobs SET status='running', attempts=attempts+1 "
            "WHERE job_id='j_race' AND status='pending'"
        )
        cancel = asyncio.create_task(store.cancel_job("j_race"))
        await asyncio.sleep(0.3)
        assert not cancel.done(), "cancel did not wait for the claim's row lock"
        await tx.commit()
        assert await asyncio.wait_for(cancel, 5) == {
            "status": "running",
            "changed": False,
            "cancel_requested": True,
        }
    assert await _status(pg_store, "j_race") == "running"


async def test_sql_race_cancel_commits_first(pg_store):
    """The other order: the cancel holds the row lock (its SELECT … FOR UPDATE
    and UPDATE, uncommitted) while the dispatching worker tries to claim. The
    claim must wait, then find no 'pending' row and refuse to start the job."""
    await _insert(pg_store, "j_race2", "pending")
    async with pg_store.acquire() as canceller:
        tx = canceller.transaction()
        await tx.start()
        await canceller.fetchval(
            "SELECT status FROM ai_jobs WHERE job_id='j_race2' FOR UPDATE"
        )
        await canceller.execute(
            "UPDATE ai_jobs SET status='cancelled' WHERE job_id='j_race2'"
        )
        claim = asyncio.create_task(store.mark_running("j_race2"))
        await asyncio.sleep(0.3)
        assert not claim.done(), "claim did not wait for the cancel's row lock"
        await tx.commit()
        assert await asyncio.wait_for(claim, 5) is False
    assert await _status(pg_store, "j_race2") == "cancelled"


async def test_sql_cancelled_is_listed_and_expires_like_any_job(pg_store):
    await _insert(pg_store, "j_list", "pending")
    await store.cancel_job("j_list")
    listed = await store.list_jobs(app_id="werking-energy", status="cancelled")
    assert [j["job_id"] for j in listed] == ["j_list"]
    assert listed[0]["status"] == "cancelled"
    assert listed[0]["elapsed_seconds"] is not None
    # BR11: retention counts from the terminal transition (finished_at, stamped
    # by cancel_job), not from created_at — submitted 200 s ago, cancelled just
    # now, TTL 100 s: still readable.
    assert (await store.get_job("j_list"))["finished_at"] is not None
    assert await store.cleanup_old(100) == 0
    async with pg_store.acquire() as conn:
        await conn.execute(
            "UPDATE ai_jobs SET finished_at = NOW() - interval '200 seconds' "
            "WHERE job_id = 'j_list'"
        )
    assert await store.cleanup_old(100) == 1
    assert await store.get_job("j_list") is None


async def test_sql_route_end_to_end(pg_store, monkeypatch):
    """The route over the real store (stage 2 of store_client, direct DB):
    owner cancels, a stranger gets the unknown-id answer, the worker then
    cannot start the cancelled job."""
    from src.main import extract_attribution_context

    monkeypatch.setenv("BRIDGE_ORIGIN_ID", HOME)
    monkeypatch.setattr(routes, "verify_api_key", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(routes, "_attribution_extractor", extract_attribution_context)
    for name in ("get_job", "cancel_job", "mark_running"):
        monkeypatch.setattr(store_client, name, getattr(store, name))
    await _insert(pg_store, JOB, "pending", deferred_in_s=600)

    stranger = await _delete(JOB, {"X-App-ID": "werking-energy", "X-User-ID": "user-b"})
    assert (stranger.status_code, _err(stranger)["code"]) == (404, "job_not_found")
    assert await _status(pg_store, JOB) == "pending"

    owner = await _delete(JOB, OWNER_HEADERS)
    assert (owner.status_code, owner.json()) == (
        200,
        {"job_id": JOB, "status": "cancelled"},
    )
    again = await _delete(JOB, OWNER_HEADERS)
    assert again.json() == {"job_id": JOB, "status": "cancelled"}

    executor = AsyncMock(return_value={"ok": True})
    registry.register_executor("br10-probe", executor)
    await registry.run_generic_job(JOB, "br10-probe", {}, OWNER_ATTR)
    executor.assert_not_awaited()
    assert (await _get(JOB, OWNER_HEADERS)).json()["status"] == "cancelled"
