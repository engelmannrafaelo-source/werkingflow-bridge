"""BRREF2 — Idempotency-Key on POST /v1/jobs.

The contract, agreed with the report side, point by point:

  1. Header Idempotency-Key, printable ASCII, at most 200 characters.
  2. Same key + same owner (principal/shared key + app) within retention
     (24 h past the terminal state, never while pending/running) → no new
     job: the same job_id, HTTP 200 instead of 201, whatever its status.
  3. Same key, other body (kind or payload digest) → 422
     idempotency_key_reused, no job.
  4. Concurrent POSTs with the same key → exactly one job, atomically through
     the UNIQUE index of migration 065 — not read-then-write.
  5. Every keyed answer echoes Idempotency-Key and sets
     Idempotent-Replayed: true|false.
  6. Without the header: unchanged.

Three layers:
  1. the route (in-memory store with the store's semantics, real attribution
     extractor from main.py),
  2. the SQL against a real Postgres (BRIDGE_TEST_PG_URL) — atomicity and the
     race are properties of the unique index and cannot be shown with a mock;
     the parallel double POST runs there through the route as well,
  3. the worker → platform-api seam (store_client + internal_routes).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import httpx
import pytest

from src.jobs import routes, store, store_client

REPO = Path(__file__).resolve().parents[2]
PG_URL = os.getenv("BRIDGE_TEST_PG_URL")
HOME = "dev"
APP_HEADERS = {"X-App-ID": "werking-report", "X-User-ID": "user-a"}
KEY = "report-uz:tenant-1:0123456789abcdef:g1"
BODY = {"kind": "idem-probe", "payload": {"text": "hallo", "n": 1}}


# ---------------------------------------------------------------------------
# 1. The route
# ---------------------------------------------------------------------------


class MemStore:
    """create_job / create_job_idempotent with store.py's semantics, in memory."""

    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.keys: Dict[tuple, str] = {}
        self.calls: List[str] = []

    async def create_job(self, job_id, kind, payload, attribution):
        self.calls.append("create_job")
        self.rows[job_id] = {
            "job_id": job_id,
            "kind": kind,
            "status": "pending",
            "digest": store.payload_digest(payload),
        }

    async def find_job_by_idempotency_key(self, owner, key, kind, payload):
        self.calls.append("find_job_by_idempotency_key")
        held = self.keys.get((owner, key))
        if held is None:
            return None
        row = self.rows[held]
        if row["kind"] != kind or row["digest"] != store.payload_digest(payload):
            raise store.IdempotencyKeyReused(held)
        return {k: row[k] for k in ("job_id", "kind", "status")}

    async def create_job_idempotent(
        self, job_id, kind, payload, attribution, owner, key
    ):
        self.calls.append("create_job_idempotent")
        digest = store.payload_digest(payload)
        held = self.keys.get((owner, key))
        if held is None:
            self.keys[(owner, key)] = job_id
            self.rows[job_id] = {
                "job_id": job_id,
                "kind": kind,
                "status": "pending",
                "digest": digest,
            }
            return {
                "created": True,
                "job": {"job_id": job_id, "kind": kind, "status": "pending"},
            }
        row = self.rows[held]
        if row["kind"] != kind or row["digest"] != digest:
            raise store.IdempotencyKeyReused(held)
        return {
            "created": False,
            "job": {k: row[k] for k in ("job_id", "kind", "status")},
        }


@pytest.fixture
def mem(monkeypatch):
    from src.jobs import registry
    from src.main import extract_attribution_context

    m = MemStore()
    spawned: List[Any] = []

    def _spawn(coro):
        spawned.append(coro)
        coro.close()  # never run the job — only count dispatches

    monkeypatch.setenv("BRIDGE_ORIGIN_ID", HOME)
    monkeypatch.setattr(routes, "verify_api_key", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(routes, "_attribution_extractor", extract_attribution_context)
    monkeypatch.setattr(routes, "spawn", _spawn)
    monkeypatch.setitem(registry._EXECUTORS, "idem-probe", AsyncMock())
    monkeypatch.setattr(store_client, "create_job", m.create_job)
    monkeypatch.setattr(store_client, "create_job_idempotent", m.create_job_idempotent)
    monkeypatch.setattr(
        store_client, "find_job_by_idempotency_key", m.find_job_by_idempotency_key
    )
    m.spawned = spawned
    return m


def _app():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(routes.router)
    return app


async def _post(
    body=None, key: Optional[str] = None, headers=None, app=None
) -> httpx.Response:
    h = dict(APP_HEADERS if headers is None else headers)
    if key is not None:
        h["Idempotency-Key"] = key
    transport = httpx.ASGITransport(app=app or _app())
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as c:
        return await c.post("/v1/jobs", json=body or BODY, headers=h)


async def test_6_without_header_unchanged(mem):
    r1 = await _post()
    r2 = await _post()
    assert (r1.status_code, r2.status_code) == (200, 200)
    assert r1.json()["job_id"] != r2.json()["job_id"]
    assert r1.json()["status"] == "pending" and r1.json()["kind"] == "idem-probe"
    assert "idempotency-key" not in r1.headers
    assert "idempotent-replayed" not in r1.headers
    assert mem.calls == ["create_job", "create_job"]
    assert len(mem.spawned) == 2


async def test_1_5_new_key_creates_201_and_echoes(mem):
    r = await _post(key=KEY)
    assert r.status_code == 201
    assert r.headers["Idempotency-Key"] == KEY
    assert r.headers["Idempotent-Replayed"] == "false"
    assert r.json()["status"] == "pending"
    assert len(mem.spawned) == 1


@pytest.mark.parametrize("status", ["pending", "running", "done", "error", "cancelled"])
async def test_2_5_replay_same_job_200_whatever_status(mem, status):
    first = await _post(key=KEY)
    job_id = first.json()["job_id"]
    mem.rows[job_id]["status"] = status

    again = await _post(key=KEY)
    assert again.status_code == 200
    assert again.json() == {"job_id": job_id, "status": status, "kind": "idem-probe"}
    assert again.headers["Idempotency-Key"] == KEY
    assert again.headers["Idempotent-Replayed"] == "true"
    assert len(mem.rows) == 1
    assert len(mem.spawned) == 1  # the replay dispatched nothing


async def test_2_owner_is_key_namespace(mem):
    """Another app with the same key string is another job."""
    a = await _post(key=KEY)
    b = await _post(key=KEY, headers={"X-App-ID": "werking-energy"})
    assert (a.status_code, b.status_code) == (201, 201)
    assert a.json()["job_id"] != b.json()["job_id"]


async def test_2_owner_ignores_body_attribution(mem):
    """The body's attribution is caller-chosen per request; it must not open
    a fresh key namespace on a retry."""
    a = await _post(key=KEY)
    b = await _post(body={**BODY, "attribution": {"app_id": "something-else"}}, key=KEY)
    assert (a.status_code, b.status_code) == (201, 200)
    assert b.json()["job_id"] == a.json()["job_id"]


@pytest.mark.parametrize(
    "other",
    [
        {"kind": "idem-probe", "payload": {"text": "hallo", "n": 2}},
        {"kind": "idem-probe", "payload": {}},
    ],
)
async def test_3_other_body_422_no_job(mem, other):
    first = await _post(key=KEY)
    r = await _post(body=other, key=KEY)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == err["reason"] == "idempotency_key_reused"
    assert err["retryable"] is False
    # IDEMR: the answer names no job — neither field nor message.
    assert "job_id" not in err
    assert first.json()["job_id"] not in r.text
    assert r.headers["Idempotency-Key"] == KEY
    assert len(mem.rows) == 1 and len(mem.spawned) == 1


async def test_3_other_kind_422(mem, monkeypatch):
    from src.jobs import registry

    monkeypatch.setitem(registry._EXECUTORS, "idem-probe-2", AsyncMock())
    await _post(key=KEY)
    r = await _post(body={**BODY, "kind": "idem-probe-2"}, key=KEY)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "idempotency_key_reused"


async def test_3_same_payload_other_key_order_is_same_body(mem):
    await _post(key=KEY)
    r = await _post(
        body={"kind": "idem-probe", "payload": {"n": 1, "text": "hallo"}}, key=KEY
    )
    assert r.status_code == 200


@pytest.mark.parametrize(
    "bad",
    ["x" * 201, "schlüssel", "tab\there"],
)
async def test_1_invalid_key_400_no_job(mem, bad):
    if not bad.isascii():
        # httpx refuses non-ASCII header values itself; send raw bytes.
        r = await _post(
            key=None, headers={**APP_HEADERS, "Idempotency-Key": bad.encode("utf-8")}
        )
    else:
        r = await _post(key=bad)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "idempotency_key_invalid"
    assert mem.calls == [] and mem.spawned == []


async def test_1_empty_key_400(mem):
    r = await _post(headers={**APP_HEADERS, "Idempotency-Key": ""})
    assert r.status_code == 400
    assert mem.calls == []


async def test_1_200_chars_accepted(mem):
    r = await _post(key="k" * 200)
    assert r.status_code == 201


async def test_platform_without_route_is_503_no_job(mem, monkeypatch):
    async def _unsupported(*a, **k):
        raise store_client.IdempotencyUnsupported("platform-api predates BRREF2")

    monkeypatch.setattr(store_client, "create_job_idempotent", _unsupported)
    monkeypatch.setattr(store_client, "find_job_by_idempotency_key", _unsupported)
    r = await _post(key=KEY)
    assert r.status_code == 503
    assert mem.spawned == []


async def test_own_job_id_reported_as_replay_is_dispatched(mem, monkeypatch):
    """After a platform timeout the direct-DB fallback finds the row the lost
    call inserted — under THIS request's job_id. That is our job: dispatch."""

    async def _found_own(job_id, kind, payload, attribution, owner, key):
        return {
            "created": False,
            "job": {"job_id": job_id, "kind": kind, "status": "pending"},
        }

    monkeypatch.setattr(store_client, "create_job_idempotent", _found_own)
    r = await _post(key=KEY)
    assert r.status_code == 201
    assert r.headers["Idempotent-Replayed"] == "false"
    assert len(mem.spawned) == 1


# ---------------------------------------------------------------------------
# 2. The SQL, against a real Postgres
# ---------------------------------------------------------------------------

_JOBS_DDL = [
    REPO / "docker" / "migrations" / "031_ai_jobs.sql",
    REPO / "docker" / "migrations" / "044_ai_jobs_dependency_deferral.sql",
    REPO / "docker" / "migrations" / "063_ai_jobs_finished_at.sql",
    REPO / "docker" / "migrations" / "064_ai_jobs_cancel_requested.sql",
    REPO / "docker" / "migrations" / "065_ai_jobs_idempotency_key.sql",
]


@pytest.fixture
async def pg_store():
    if not PG_URL:
        pytest.skip(
            "BRIDGE_TEST_PG_URL fehlt — Idempotenz-SQL nicht gegen "
            "echten Postgres geprueft"
        )
    import asyncpg

    schema = f"jobs_idem_probe_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(PG_URL)
    await admin.execute(f"CREATE SCHEMA {schema}")
    pool = await asyncpg.create_pool(
        PG_URL, min_size=8, max_size=12, server_settings={"search_path": schema}
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


def _jid() -> str:
    return f"job_{HOME}_{uuid.uuid4().hex}"


async def _count(pool) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM ai_jobs")


async def test_pg_migration_is_rerunnable(pg_store):
    async with pg_store.acquire() as conn:
        await conn.execute(_JOBS_DDL[-1].read_text(encoding="utf-8"))


async def test_pg_create_replay_reuse(pg_store):
    a = await store.create_job_idempotent(_jid(), "k", {"x": 1}, None, "o", KEY)
    assert a["created"] is True
    b = await store.create_job_idempotent(_jid(), "k", {"x": 1}, None, "o", KEY)
    assert b == {"created": False, "job": a["job"]}
    with pytest.raises(store.IdempotencyKeyReused) as exc:
        await store.create_job_idempotent(_jid(), "k", {"x": 2}, None, "o", KEY)
    assert exc.value.job_id == a["job"]["job_id"]
    with pytest.raises(store.IdempotencyKeyReused):
        await store.create_job_idempotent(_jid(), "k2", {"x": 1}, None, "o", KEY)
    other_owner = await store.create_job_idempotent(
        _jid(), "k", {"x": 1}, None, "o2", KEY
    )
    assert other_owner["created"] is True
    assert await _count(pg_store) == 2


async def test_pg_replay_reports_current_status(pg_store):
    a = await store.create_job_idempotent(_jid(), "k", {}, None, "o", KEY)
    async with pg_store.acquire() as conn:
        await conn.execute("UPDATE ai_jobs SET status='done', finished_at=NOW()")
    b = await store.create_job_idempotent(_jid(), "k", {}, None, "o", KEY)
    assert b["created"] is False and b["job"]["status"] == "done"
    assert b["job"]["job_id"] == a["job"]["job_id"]


async def test_pg_unkeyed_jobs_untouched_by_index(pg_store):
    for _ in range(3):
        await store.create_job(_jid(), "k", {"x": 1}, None)
    assert await _count(pg_store) == 3


async def test_4_pg_parallel_same_key_exactly_one_job(pg_store):
    n = 16
    outs = await asyncio.gather(
        *[
            store.create_job_idempotent(_jid(), "k", {"x": 1}, None, "o", KEY)
            for _ in range(n)
        ]
    )
    created = [o for o in outs if o["created"]]
    assert len(created) == 1
    assert {o["job"]["job_id"] for o in outs} == {created[0]["job"]["job_id"]}
    assert await _count(pg_store) == 1


async def test_4_pg_parallel_double_post_through_route(pg_store, mem, monkeypatch):
    """The real route against the real store: two POSTs at once, one job."""
    monkeypatch.setattr(
        store_client, "create_job_idempotent", store.create_job_idempotent
    )
    monkeypatch.setattr(
        store_client, "find_job_by_idempotency_key", store.find_job_by_idempotency_key
    )
    app = _app()
    r1, r2 = await asyncio.gather(_post(key=KEY, app=app), _post(key=KEY, app=app))
    assert sorted((r1.status_code, r2.status_code)) == [200, 201]
    assert r1.json()["job_id"] == r2.json()["job_id"]
    assert sorted(r.headers["Idempotent-Replayed"] for r in (r1, r2)) == [
        "false",
        "true",
    ]
    assert await _count(pg_store) == 1
    assert len(mem.spawned) == 1


async def _age_terminal(pool, hours: float) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE ai_jobs SET status='done', "
            "finished_at = NOW() - ($1 || ' hours')::interval",
            str(hours),
        )


@pytest.mark.parametrize("prune", ["prune_jobs", "cleanup_old"])
async def test_2_pg_key_outlives_terminal_ttl_24h(pg_store, prune):
    ttl = 2 * 3600

    async def run():
        if prune == "prune_jobs":
            return (await store.prune_jobs(ttl, 24 * 3600))["removed"]
        return await store.cleanup_old(ttl)

    keyed = await store.create_job_idempotent(_jid(), "k", {}, None, "o", KEY)
    await store.create_job(_jid(), "k", {}, None)  # unkeyed
    await _age_terminal(pg_store, 3)
    assert await run() == 1  # only the unkeyed row is past its 2 h
    replay = await store.create_job_idempotent(_jid(), "k", {}, None, "o", KEY)
    assert replay["created"] is False
    assert replay["job"]["job_id"] == keyed["job"]["job_id"]

    await _age_terminal(pg_store, 25)
    assert await run() == 1  # 24 h past the terminal state: the key goes too
    fresh = await store.create_job_idempotent(_jid(), "k", {}, None, "o", KEY)
    assert fresh["created"] is True


async def test_2_pg_running_keyed_row_never_pruned(pg_store):
    await store.create_job_idempotent(_jid(), "k", {}, None, "o", KEY)
    async with pg_store.acquire() as conn:
        await conn.execute(
            "UPDATE ai_jobs SET status='running', "
            "created_at = NOW() - interval '30 hours'"
        )
    r = await store.prune_jobs(2 * 3600, 24 * 3600)
    assert r["removed"] == 0 and r["running_over_max_age"] == 1
    again = await store.create_job_idempotent(_jid(), "k", {}, None, "o", KEY)
    assert again["created"] is False and again["job"]["status"] == "running"


# ---------------------------------------------------------------------------
# 3. The worker → platform-api seam
# ---------------------------------------------------------------------------


class _PResp:
    def __init__(self, status_code, json_body=None):
        self.status_code = status_code
        self.json = json_body


async def test_store_client_reads_create_and_replay():
    body = {"created": False, "job": {"job_id": "j", "kind": "k", "status": "done"}}
    with patch.object(
        store_client, "call_platform", AsyncMock(return_value=_PResp(200, body))
    ):
        assert (
            await store_client.create_job_idempotent("j2", "k", {}, None, "o", KEY)
            == body
        )


async def test_store_client_reads_reuse():
    body = {"detail": {"reason": "idempotency_key_reused", "job_id": "j"}}
    with patch.object(
        store_client, "call_platform", AsyncMock(return_value=_PResp(409, body))
    ):
        with pytest.raises(store_client.IdempotencyKeyReused) as exc:
            await store_client.create_job_idempotent("j2", "k", {}, None, "o", KEY)
    assert exc.value.job_id == "j"


@pytest.mark.parametrize("status", [404, 405])
async def test_store_client_old_platform_is_unsupported_not_created(status):
    with patch.object(
        store_client,
        "call_platform",
        AsyncMock(return_value=_PResp(status, {"detail": "x"})),
    ):
        with pytest.raises(store_client.IdempotencyUnsupported):
            await store_client.create_job_idempotent("j2", "k", {}, None, "o", KEY)


@pytest.mark.parametrize(
    "status,body", [(409, {"detail": "x"}), (500, {}), (200, {"job": {}})]
)
async def test_store_client_unknown_answer_is_loud(status, body):
    with patch.object(
        store_client, "call_platform", AsyncMock(return_value=_PResp(status, body))
    ):
        with pytest.raises(store_client.JobStoreUnavailable):
            await store_client.create_job_idempotent("j2", "k", {}, None, "o", KEY)


async def test_internal_route_create_replay_reuse(monkeypatch):
    from fastapi import FastAPI

    from src import internal_routes

    app = FastAPI()
    app.include_router(internal_routes.router)
    app.dependency_overrides[internal_routes.require_service_token] = lambda: {
        "sub": "t"
    }

    outcomes = [
        {"created": True, "job": {"job_id": "j", "kind": "k", "status": "pending"}},
        store.IdempotencyKeyReused("j"),
    ]

    async def fake(*a, **k):
        o = outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o

    monkeypatch.setattr(store, "create_job_idempotent", fake)
    req = {
        "job_id": "j",
        "kind": "k",
        "payload": {},
        "attribution": None,
        "idempotency_owner": "o",
        "idempotency_key": KEY,
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://p") as c:
        r1 = await c.post("/v1/internal/jobs/create-idempotent", json=req)
        r2 = await c.post("/v1/internal/jobs/create-idempotent", json=req)
        r3 = await c.post(
            "/v1/internal/jobs/create-idempotent",
            json={**req, "idempotency_key": "x" * 201},
        )
    assert r1.status_code == 200 and r1.json()["created"] is True
    assert r2.status_code == 409
    assert r2.json()["detail"] == {"reason": "idempotency_key_reused", "job_id": "j"}
    assert r3.status_code == 422  # validation — never read as "reused"
