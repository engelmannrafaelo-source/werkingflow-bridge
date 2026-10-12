"""IDEMR R2 — one ownership boundary for every access to a keyed job.

GET and DELETE go through routes._visible_to_caller; the list applies the
same owner in SQL (store.list_jobs viewer_owner). Unkeyed jobs keep their
rules. A platform-api that does not confirm the list filter is refused.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.jobs import store, store_client
from tests.jobs.test_job_idempotenz_brref2 import (  # noqa: F401  (fixtures)
    KEY,
    _app,
    _jid,
    _post,
    mem,
    pg_store,
)

ALICE = {"X-App-ID": "app", "X-User-ID": "alice"}
BOB = {"X-App-ID": "app", "X-User-ID": "bob"}


@pytest.fixture
def real_store(monkeypatch):
    for name in (
        "create_job",
        "create_job_idempotent",
        "find_job_by_idempotency_key",
        "get_job",
        "list_jobs",
        "cancel_job",
    ):
        monkeypatch.setattr(store_client, name, getattr(store, name))


async def _call(method, path, headers):
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as c:
        return await c.request(method, path, headers=headers)


async def test_delete_foreign_keyed_job_is_unknown(mem, pg_store, real_store):  # noqa: F811
    jid = (await _post(key=KEY, headers=ALICE)).json()["job_id"]
    foreign = await _call("DELETE", f"/v1/jobs/{jid}", BOB)
    unknown = await _call("DELETE", f"/v1/jobs/job_dev_{'0' * 32}", BOB)
    assert foreign.status_code == unknown.status_code == 404
    assert (await store.get_job(jid))["status"] == "pending"
    own = await _call("DELETE", f"/v1/jobs/{jid}", ALICE)
    assert own.status_code == 200 and own.json()["status"] == "cancelled"


async def test_list_shows_keyed_jobs_only_to_their_owner(mem, pg_store, real_store):  # noqa: F811
    keyed = (await _post(key=KEY, headers=ALICE)).json()["job_id"]
    plain = (await _post(headers=ALICE)).json()["job_id"]
    mine = (await _call("GET", "/v1/jobs?app_id=app", ALICE)).json()["jobs"]
    theirs = (await _call("GET", "/v1/jobs?app_id=app", BOB)).json()["jobs"]
    assert {j["job_id"] for j in mine} == {keyed, plain}
    assert {j["job_id"] for j in theirs} == {plain}  # unkeyed: unchanged


async def test_pg_list_without_viewer_hides_keyed_rows(pg_store):  # noqa: F811
    await store.create_job_idempotent(_jid(), "k", {}, {"app_id": "app"}, "o", KEY)
    plain = _jid()
    await store.create_job(plain, "k", {}, {"app_id": "app"})
    rows = await store.list_jobs(app_id="app")
    assert [r["job_id"] for r in rows] == [plain]
    assert len(await store.list_jobs(app_id="app", viewer_owner="o")) == 2
    assert len(await store.list_jobs(app_id="app", viewer_owner="x")) == 1


class _PResp:
    def __init__(self, status_code, json_body=None):
        self.status_code = status_code
        self.json = json_body


async def test_store_client_list_sends_owner_and_needs_confirmation():
    ok = AsyncMock(return_value=_PResp(200, {"jobs": [], "keyed_owner_filter": True}))
    with patch.object(store_client, "call_platform", ok):
        assert await store_client.list_jobs(app_id="a", viewer_owner="o") == []
    assert ok.await_args.kwargs["params"]["viewer_owner"] == "o"
    old = AsyncMock(return_value=_PResp(200, {"jobs": [{"job_id": "foreign"}]}))
    with patch.object(store_client, "call_platform", old):
        with pytest.raises(store_client.IdempotencyUnsupported):
            await store_client.list_jobs(app_id="a", viewer_owner="o")


async def test_internal_list_route_passes_owner_and_confirms(monkeypatch):
    from fastapi import FastAPI

    from src import internal_routes

    seen = {}

    async def fake(**kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(store, "list_jobs", fake)
    app = FastAPI()
    app.include_router(internal_routes.router)
    app.dependency_overrides[internal_routes.require_service_token] = lambda: {
        "sub": "t"
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://p") as c:
        r = await c.get("/v1/internal/jobs?app_id=a&viewer_owner=o")
    assert r.json() == {"jobs": [], "keyed_owner_filter": True}
    assert seen["viewer_owner"] == "o"
