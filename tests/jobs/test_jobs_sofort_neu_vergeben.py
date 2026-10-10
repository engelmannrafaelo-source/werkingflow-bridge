"""A job refused for capacity goes to a free account NOW — and a parked job
restarts as soon as its wait is over.

Measured 29.09.2026 on the dev bridge: a job whose landing worker sat in a
soft penalty (weekly util >= 75 %) got 429 on its pinned self-call, was parked
(deferred_until = now + 30 s, heartbeat_at = now) and only re-claimed after the
90 s stale window plus a 30 s watchdog tick. 28-38 % of 3-5 s jobs took
90-130 s. Status trail: running(0.3s) -> pending(1.4s) -> running(90.6s) -> done.

Three fixes, three groups of tests:
  1. executor: on a capacity 429 re-dispatch once through the local LB
     (X-Bridge-Hop: 1 = local tier only), bounded, then park as before;
  2. store/watchdog: a parked 'pending' job is claimable at deferred_until,
     not only once it is also stale — stale-running detection unchanged;
  3. the defer log line names the worker/account that refused.

The SQL is exercised against a real Postgres when BRIDGE_TEST_PG_URL is set
(same convention as tests/billing/test_topup_gutschrift_echte_db.py);
without it only the structural guards run.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest

from src.jobs import executors, registry, store, store_client
from src.jobs.executors import ExecutorHTTPError, chat_executor, proxy_executor

REPO = Path(__file__).resolve().parents[2]
PG_URL = os.getenv("BRIDGE_TEST_PG_URL")


# ---------------------------------------------------------------------------
# httpx stand-ins (same shape as tests/jobs/test_executors.py)
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status, json_data=None, text="", headers=None):
        self.status_code = status
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json


def _refusal(worker: str, retry_after: str = "45") -> _Resp:
    """The chat_completions soft-penalty envelope (main.py, 'NGINX failover')."""
    return _Resp(
        429,
        {"error": {
            "message": f"[Bridge {worker}] Worker rate-limited (soft)",
            "bridge_worker": worker,
            "reason": "worker_account_rate_limited",
            "bridge_type": "worker_unavailable",
        }},
        text="rate-limited",
        headers={"Retry-After": retry_after},
    )


class _Client:
    """Answers each post from a script; an Exception entry is raised."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": dict(headers or {})})
        nxt = self._script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _patched(client, **env):
    return [
        patch("httpx.AsyncClient", return_value=client),
        patch("src.auth.auth_manager.get_api_key", return_value="k"),
        patch.object(executors, "SELF_BASE_URL", "http://localhost:8000"),
        patch.object(executors, "JOB_REDISPATCH_BASE_URL", env.get("lb", "http://nginx:80")),
        patch.dict(os.environ, {"INSTANCE_NAME": "worker1", "WORKER_ACCOUNT": "engelmann"}),
    ]


async def _run_chat(client, **env):
    with ExitStack() as stack:  # a failing start() must not leak earlier patches
        for p in _patched(client, **env):
            stack.enter_context(p)
        return await chat_executor({"messages": [{"role": "user", "content": "x"}]},
                                   {"app_id": "werking-report", "bridge_origin": "dev"}, AsyncMock())


# ---------------------------------------------------------------------------
# 1. Re-dispatch through the local LB
# ---------------------------------------------------------------------------

async def test_capacity_429_is_redispatched_through_the_lb_and_succeeds():
    ok = _Resp(200, {"id": "chatcmpl-1", "choices": []}, headers={"X-Upstream-Server": "worker3"})
    client = _Client([_refusal("worker1"), ok])

    out = await _run_chat(client)

    assert out["id"] == "chatcmpl-1"
    assert len(client.calls) == 2
    first, second = client.calls
    assert first["url"] == "http://localhost:8000/v1/chat/completions"
    assert second["url"] == "http://nginx:80/v1/chat/completions"
    # Loop guard + dev/prod isolation: the LB serves a hopped request from the
    # LOCAL tier only (ADR-0010) — never cross-bridge.
    assert second["headers"]["X-Bridge-Hop"] == "1"
    assert second["headers"][executors.REDISPATCH_HEADER] == "worker1"
    # Same request, same attribution/budget home.
    assert second["json"] == first["json"]
    assert second["headers"]["X-App-ID"] == "werking-report"
    assert second["headers"]["X-Bridge-Origin"] == "dev"


async def test_redispatch_is_bounded_then_the_original_429_parks_the_job():
    client = _Client([_refusal("worker1", "45"), _refusal("worker4", "30")])

    with pytest.raises(ExecutorHTTPError) as ei:
        await _run_chat(client)

    assert len(client.calls) == 1 + executors.JOB_CAPACITY_REDISPATCH_ATTEMPTS
    assert ei.value.status_code == 429
    # The ORIGINAL refusal's Retry-After drives the park.
    assert ei.value.retry_after_s == 45.0
    rej = ei.value.rejection
    assert "worker=worker1" in rej and "account=engelmann" in rej
    assert "worker_account_rate_limited" in rej
    assert "LB#1" in rej and "worker=worker4" in rej


async def test_bounded_attempts_holds_for_a_larger_budget():
    """The loop really is bounded by the constant, not by the script length."""
    client = _Client([_refusal("worker1")] + [_refusal("worker2")] * 5)
    with patch.object(executors, "JOB_CAPACITY_REDISPATCH_ATTEMPTS", 3):
        with pytest.raises(ExecutorHTTPError):
            await _run_chat(client)
    assert len(client.calls) == 4


async def test_lb_unreachable_keeps_the_429_so_the_job_is_parked():
    client = _Client([_refusal("worker1"), ConnectionError("nginx: Name or service not known")])
    with pytest.raises(ExecutorHTTPError) as ei:
        await _run_chat(client)
    assert ei.value.status_code == 429
    assert "unreachable" in ei.value.rejection


async def test_lb_pool_full_503_keeps_the_429_instead_of_a_terminal_error():
    """@bridge_full rewrites 'every worker refused' into 503 — that is still
    'no capacity', not a verdict on the job; it must park, not die."""
    client = _Client([_refusal("worker1"), _Resp(503, {"error": {"reason": "capacity_busy"}})])
    with pytest.raises(ExecutorHTTPError) as ei:
        await _run_chat(client)
    assert ei.value.status_code == 429


async def test_a_real_verdict_from_the_sibling_passes_through():
    client = _Client([_refusal("worker1"), _Resp(400, text="invalid request")])
    with pytest.raises(ExecutorHTTPError) as ei:
        await _run_chat(client)
    assert ei.value.status_code == 400


async def test_no_redispatch_when_disabled():
    client = _Client([_refusal("worker1")])
    with pytest.raises(ExecutorHTTPError) as ei:
        await _run_chat(client, lb="")
    assert len(client.calls) == 1
    assert "BRIDGE_JOB_REDISPATCH_URL empty" in ei.value.rejection


async def test_non_llm_paths_are_not_redispatched():
    """Only the account-consuming chat/research paths go through the LB."""
    client = _Client([_refusal("worker1")])
    with ExitStack() as stack:
        for p in _patched(client):
            stack.enter_context(p)
        with pytest.raises(ExecutorHTTPError):
            await proxy_executor({"path": "/v1/privacy/smart-anonymize", "body": {}}, None, AsyncMock())
    assert len(client.calls) == 1


async def test_success_on_first_try_never_touches_the_lb():
    client = _Client([_Resp(200, {"id": "c"})])
    await _run_chat(client)
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# 3. The defer line names who refused
# ---------------------------------------------------------------------------

async def test_defer_log_names_worker_account_and_reason(caplog):
    exc = ExecutorHTTPError(
        429, "chat self-call failed HTTP 429", retry_after_s=40,
        rejection="self-call refused (worker=worker1 account=engelmann; worker=worker1 "
                  "reason=worker_account_rate_limited status=429 msg='x'); LB#1 worker=worker4",
    )

    async def _raise(payload, attribution, report_progress):
        raise exc

    registry.register_executor("redispatch-log-test", _raise)
    with patch.multiple(
        store_client,
        mark_running=AsyncMock(), heartbeat=AsyncMock(), update_progress=AsyncMock(),
        mark_done=AsyncMock(), mark_error=AsyncMock(), defer_job=AsyncMock(),
        get_job=AsyncMock(return_value={"defer_count": 0}),
    ), caplog.at_level(logging.WARNING, logger="src.jobs.registry"):
        await registry.run_generic_job("job_dev_log", "redispatch-log-test", {}, None)

    lines = [r.getMessage() for r in caplog.records if "deferred" in r.getMessage()]
    assert len(lines) == 1
    assert "account=engelmann" in lines[0]
    assert "worker_account_rate_limited" in lines[0]
    assert "LB#1 worker=worker4" in lines[0]


# ---------------------------------------------------------------------------
# 2. Claimable at deferred_until — structure + real SQL
# ---------------------------------------------------------------------------

def test_claim_query_includes_the_due_deferred_branch():
    import inspect
    src = inspect.getsource(store.claim_stale_job)
    assert "_CLAIMABLE" in src
    assert store._DEFER_DUE in store._CLAIMABLE
    # Only a PARKED job may skip the stale window — a 'running' one must still
    # prove it is dead by a frozen heartbeat.
    assert "status = 'pending'" in store._DEFER_DUE
    assert "deferred_until <= NOW()" in store._DEFER_DUE


def test_claim_tick_is_short():
    main_src = (REPO / "src/main.py").read_text(encoding="utf-8")
    m = re.search(r"^GENERIC_JOB_CLAIM_INTERVAL_S\s*=\s*(\d+)", main_src, re.M)
    assert m and int(m.group(1)) <= 5
    assert "asyncio.create_task(_generic_jobs_claim_loop())" in main_src


async def test_run_claim_pass_starts_every_claimed_job():
    due = {"job_id": "job_dev_a", "kind": "k", "payload": {}, "attribution": None,
           "attempts": 2, "defer_count": 1, "defer_reason": "429"}
    with patch.object(store_client, "claim_stale_job", AsyncMock(side_effect=[due, None])), \
         patch.object(registry, "spawn") as spawn, \
         patch.object(registry, "_run_body", new=lambda *a: None):
        n = await registry.run_claim_pass(90, 3)
    assert n == 1
    spawn.assert_called_once()


_JOBS_DDL = [
    REPO / "docker/migrations/031_ai_jobs.sql",
    REPO / "docker/migrations/044_ai_jobs_dependency_deferral.sql",
    REPO / "docker/migrations/063_ai_jobs_finished_at.sql",
    REPO / "docker/migrations/064_ai_jobs_cancel_requested.sql",
]


@pytest.fixture
async def pg_store():
    if not PG_URL:
        pytest.skip("BRIDGE_TEST_PG_URL fehlt — Claim-SQL nicht gegen echten Postgres geprueft")
    import asyncpg

    schema = f"jobs_claim_probe_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(PG_URL)
    await admin.execute(f"CREATE SCHEMA {schema}")
    pool = await asyncpg.create_pool(PG_URL, min_size=1, max_size=2,
                                     server_settings={"search_path": schema})
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


async def _insert(pool, job_id, status, *, heartbeat_age_s, deferred_in_s=None,
                  attempts=1, defer_count=0):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ai_jobs (job_id, kind, status, attempts, defer_count,
                                 heartbeat_at, deferred_until, created_at)
            VALUES ($1, 'chat', $2, $3, $4,
                    NOW() - ($5 || ' seconds')::interval,
                    CASE WHEN $6::text IS NULL THEN NULL
                         ELSE NOW() + ($6::text || ' seconds')::interval END,
                    NOW() - interval '200 seconds')
            """,
            job_id, status, attempts, defer_count, str(heartbeat_age_s),
            None if deferred_in_s is None else str(deferred_in_s),
        )


async def _claim_all(stale_s=90, max_attempts=3):
    got = []
    while (job := await store.claim_stale_job(stale_s, max_attempts)) is not None:
        got.append(job["job_id"])
    return got


async def test_parked_job_is_claimable_at_deferred_until_not_after_stale_window(pg_store):
    # Parked 2 s ago with a 30 s wait: fresh heartbeat (defer_job stamps it).
    await _insert(pg_store, "job_dev_wait", "pending", heartbeat_age_s=2,
                  deferred_in_s=28, attempts=1, defer_count=1)
    assert await _claim_all() == []  # still inside its wait

    async with pg_store.acquire() as conn:  # the wait ends; heartbeat still fresh
        await conn.execute(
            "UPDATE ai_jobs SET deferred_until = NOW() - interval '1 second' WHERE job_id = 'job_dev_wait'"
        )
    assert await _claim_all() == ["job_dev_wait"]
    job = await store.get_job("job_dev_wait")
    assert job["status"] == "running" and job["attempts"] == 2


async def test_stale_running_job_is_still_reclaimed(pg_store):
    await _insert(pg_store, "job_dev_dead", "running", heartbeat_age_s=120)
    await _insert(pg_store, "job_dev_alive", "running", heartbeat_age_s=5)
    assert await _claim_all() == ["job_dev_dead"]


async def test_running_job_with_old_deferred_until_is_not_double_claimed(pg_store):
    """After a due park is claimed the row is 'running' with deferred_until in
    the past. It must NOT be claimed again while its heartbeat is fresh —
    that would pay for the same call twice."""
    await _insert(pg_store, "job_dev_rerun", "running", heartbeat_age_s=3,
                  deferred_in_s=-10, attempts=2, defer_count=1)
    assert await _claim_all() == []


async def test_fresh_pending_job_is_left_to_its_dispatcher(pg_store):
    await _insert(pg_store, "job_dev_new", "pending", heartbeat_age_s=1)
    assert await _claim_all() == []


async def test_crash_budget_still_applies_to_due_parked_jobs(pg_store):
    await _insert(pg_store, "job_dev_spent", "pending", heartbeat_age_s=2,
                  deferred_in_s=-1, attempts=4, defer_count=1)  # 3 crash starts
    assert await _claim_all() == []
