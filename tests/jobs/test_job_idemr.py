"""IDEMR — Nachbesserung der Idempotenz (Codex IDEMR zu a427879).

* the key is looked up BEFORE the capacity veto: an existing job is
  replayed (and a reused key refused) even when the worker is locked;
* the owner is a digest over length-prefixed identity parts incl. the user;
* 422 names no job;
* GET of a keyed job answers only its owner — anyone else the 404 of an
  unknown id; unkeyed jobs keep GET-by-id as before.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.jobs import routes, store, store_client
from tests.jobs.test_job_idempotenz_brref2 import (  # noqa: F401  (fixtures)
    APP_HEADERS,
    BODY,
    KEY,
    _app,
    _jid,
    _post,
    mem,
    pg_store,
)


def _lock(monkeypatch):
    from src.middleware import capacity_lock

    lock = SimpleNamespace(
        is_locked=lambda _: True,
        remaining_s=lambda _: 60,
        get_lock_info=lambda _: {"reason": "session"},
    )
    monkeypatch.setattr(capacity_lock, "get_capacity_lock", lambda: lock)
    monkeypatch.setattr(routes, "_job_runs_off_pool", AsyncMock(return_value=False))


async def test_capacity_lock_without_key_still_429(mem, monkeypatch):  # noqa: F811 (pytest fixtures)
    _lock(monkeypatch)
    r = await _post(key=KEY)
    assert r.status_code == 429  # no job yet: the veto applies as before
    assert mem.rows == {}


async def test_capacity_lock_reused_key_is_422_not_429(mem, monkeypatch):  # noqa: F811 (pytest fixtures)
    await _post(key=KEY)
    _lock(monkeypatch)
    r = await _post(body={**BODY, "payload": {"x": 2}}, key=KEY)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "idempotency_key_reused"


async def test_replay_under_lock_dispatches_nothing(mem, monkeypatch):  # noqa: F811 (pytest fixtures)
    await _post(key=KEY)
    _lock(monkeypatch)
    r = await _post(key=KEY)
    assert r.status_code == 200 and r.headers["Idempotent-Replayed"] == "true"
    assert len(mem.spawned) == 1


@pytest.fixture
def extractor(monkeypatch):
    from src.main import extract_attribution_context

    monkeypatch.setattr(routes, "_attribution_extractor", extract_attribution_context)


def _owner(**headers):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "state": {},
    }
    req = Request(scope)
    return routes._idempotency_owner(req)


def test_owner_is_opaque_and_user_scoped(extractor):
    a = _owner(**{"X-App-ID": "app", "X-User-ID": "alice"})
    b = _owner(**{"X-App-ID": "app", "X-User-ID": "bob"})
    assert a != b
    assert a.startswith("v1:") and "app" not in a and "alice" not in a
    assert a == _owner(**{"X-App-ID": "app", "X-User-ID": "alice"})


def test_owner_parts_cannot_be_shifted(extractor):
    """'a|b'+'c' and 'a'+'b|c' — and any split of the same characters."""
    x = _owner(**{"X-App-ID": "a|b", "X-User-ID": "c"})
    y = _owner(**{"X-App-ID": "a", "X-User-ID": "b|c"})
    z = _owner(**{"X-App-ID": "a|b|c"})
    assert len({x, y, z}) == 3


async def _get(job_id, headers):
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as c:
        return await c.get(f"/v1/jobs/{job_id}", headers=headers)


async def test_get_keyed_job_only_for_its_owner(mem, pg_store, monkeypatch):  # noqa: F811 (pytest fixtures)
    monkeypatch.setattr(
        store_client, "create_job_idempotent", store.create_job_idempotent
    )
    monkeypatch.setattr(
        store_client, "find_job_by_idempotency_key", store.find_job_by_idempotency_key
    )
    monkeypatch.setattr(store_client, "get_job", store.get_job)
    alice = {"X-App-ID": "app", "X-User-ID": "alice"}
    bob = {"X-App-ID": "app", "X-User-ID": "bob"}
    jid = (await _post(key=KEY, headers=alice)).json()["job_id"]
    own = await _get(jid, alice)
    foreign = await _get(jid, bob)
    other_app = await _get(jid, {"X-App-ID": "app2", "X-User-ID": "alice"})
    missing = await _get(f"job_dev_{'0' * 32}", bob)
    assert own.status_code == 200 and own.json()["job_id"] == jid
    assert foreign.status_code == other_app.status_code == missing.status_code == 404
    assert foreign.json()["detail"].replace(jid, "ID") == missing.json()[
        "detail"
    ].replace(f"job_dev_{'0' * 32}", "ID")


async def test_get_unkeyed_job_unchanged(mem, pg_store, monkeypatch):  # noqa: F811 (pytest fixtures)
    monkeypatch.setattr(store_client, "create_job", store.create_job)
    monkeypatch.setattr(store_client, "get_job", store.get_job)
    jid = (await _post(headers={"X-App-ID": "app", "X-User-ID": "alice"})).json()[
        "job_id"
    ]
    r = await _get(jid, {"X-App-ID": "other"})
    assert r.status_code == 200  # GET-by-id stays a capability without a key


async def test_pg_find_by_key(pg_store):  # noqa: F811 (pytest fixtures)
    assert await store.find_job_by_idempotency_key("o", KEY, "k", {}) is None
    a = await store.create_job_idempotent(_jid(), "k", {"x": 1}, None, "o", KEY)
    found = await store.find_job_by_idempotency_key("o", KEY, "k", {"x": 1})
    assert found == a["job"]
    with pytest.raises(store.IdempotencyKeyReused):
        await store.find_job_by_idempotency_key("o", KEY, "k", {"x": 2})
    assert await store.find_job_by_idempotency_key("o2", KEY, "k", {"x": 1}) is None
    row = await store.get_job(a["job"]["job_id"])
    assert row["idempotency_owner"] == "o"


class _PResp:
    def __init__(self, status_code, json_body=None):
        self.status_code = status_code
        self.json = json_body


@pytest.mark.parametrize(
    "status,body,expect",
    [
        (200, {"job": None}, None),
        (
            200,
            {"job": {"job_id": "j", "kind": "k", "status": "done"}},
            {"job_id": "j", "kind": "k", "status": "done"},
        ),
    ],
)
async def test_store_client_lookup_reads(status, body, expect):
    with patch.object(
        store_client, "call_platform", AsyncMock(return_value=_PResp(status, body))
    ):
        assert (
            await store_client.find_job_by_idempotency_key("o", KEY, "k", {}) == expect
        )


@pytest.mark.parametrize(
    "status,body,exc",
    [
        (
            409,
            {"detail": {"reason": "idempotency_key_reused", "job_id": "j"}},
            store_client.IdempotencyKeyReused,
        ),
        (404, {"detail": "x"}, store_client.IdempotencyUnsupported),
        (405, {"detail": "x"}, store_client.IdempotencyUnsupported),
        (500, {}, store_client.JobStoreUnavailable),
        (200, {"x": 1}, store_client.JobStoreUnavailable),
    ],
)
async def test_store_client_lookup_errors(status, body, exc):
    with patch.object(
        store_client, "call_platform", AsyncMock(return_value=_PResp(status, body))
    ):
        with pytest.raises(exc):
            await store_client.find_job_by_idempotency_key("o", KEY, "k", {})


async def test_internal_lookup_route(monkeypatch):
    from fastapi import FastAPI

    from src import internal_routes

    app = FastAPI()
    app.include_router(internal_routes.router)
    app.dependency_overrides[internal_routes.require_service_token] = lambda: {
        "sub": "t"
    }
    outcomes = [
        None,
        {"job_id": "j", "kind": "k", "status": "done"},
        store.IdempotencyKeyReused("j"),
    ]

    async def fake(*a, **k):
        o = outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o

    monkeypatch.setattr(store, "find_job_by_idempotency_key", fake)
    req = {"kind": "k", "payload": {}, "idempotency_owner": "o", "idempotency_key": KEY}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://p") as c:
        rs = [
            await c.post("/v1/internal/jobs/idempotency-lookup", json=req)
            for _ in range(3)
        ]
    assert [r.status_code for r in rs] == [200, 200, 409]
    assert rs[0].json() == {"job": None}
    assert rs[1].json()["job"]["status"] == "done"
