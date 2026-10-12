"""FK25B2 — Codex' FK25BR counterprobes, taken over as the acceptance.

Source: /root/projekte/local-storage/fk25br-20261012/test_fk25br.py (Codex,
12.10.2026, against ef6f5bc). Each probe keeps its scenario; where it proved
a defect, the assertion is the corrected outcome, and the docstring names
the FK25BR finding and what the probe saw before. The process probe (all
kinds × both ways, real SDK) is tests/unit/test_job_cancel_fk25b_prozessende.py.

All credentials are synthetic; Postgres only via BRIDGE_TEST_PG_URL (a
throwaway container).
"""
import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from src.jobs import registry, routes, selbstaufruf, store, store_client
from tests.jobs import test_job_cancel_fk25b as fk25b
from tests.jobs.test_job_cancel_fk25b import (
    BODY,
    FOREIGN_HEADERS,
    JOB,
    OWNER_ATTR,
    OWNER_HEADERS,
    _bis,
    _delete,
)

mem = fk25b.mem
pg_store = fk25b.pg_store
welt = fk25b.welt

SHARED = "review-shared"


def _owner_for(headers, principal=None):
    from src.main import extract_attribution_context

    req = Request({
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    })
    if principal is not None:
        req.state.principal = principal
    routes_extractor = routes._attribution_extractor
    routes._attribution_extractor = extract_attribution_context
    try:
        return routes._idempotency_owner(req)
    finally:
        routes._attribution_extractor = routes_extractor


def _shared_key(monkeypatch):
    from src import auth

    monkeypatch.setattr(routes, "verify_api_key", auth.verify_api_key)
    monkeypatch.setattr(auth.auth_manager, "get_api_key", lambda: SHARED)


# ---------------------------------------------------------------------------
# FK25BR Befund 1 — owner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("local", [True, False])
@pytest.mark.parametrize("keyed", [True, False])
async def test_foreign_and_forged_headers(mem, monkeypatch, local, keyed):
    """Shared key, principals off. Codex: foreign → 404, owner headers → 202.
    Unchanged by design and now NAMED as the boundary (routes._caller_owns):
    under the shared key there is no authenticated user; every holder is a
    trusted backend stating the user, exactly as IDEMR treats a key's owner
    and the LB-stamped Hop/Origin headers. Authenticated binding is the
    principal path, next test."""
    _shared_key(monkeypatch)
    monkeypatch.setenv("BRIDGE_PRINCIPALS_ENABLED", "false")
    monkeypatch.setattr(routes, "JOB_CANCEL_WAIT_S", 0)
    monkeypatch.setattr(routes, "abbruch_melden", lambda j: local)
    mem.add(JOB, "running")
    if keyed:
        mem.rows[JOB]["idempotency_owner"] = _owner_for(OWNER_HEADERS)
    foreign = await _delete(JOB, {**FOREIGN_HEADERS, "Authorization": f"Bearer {SHARED}"})
    assert foreign.status_code == 404
    assert not mem.ops("cancel_job")
    stated = await _delete(JOB, {**OWNER_HEADERS, "Authorization": f"Bearer {SHARED}"})
    assert stated.status_code == 202
    assert mem.rows[JOB]["cancel_requested"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("keyed", [True, False])
async def test_principal_job_nur_vom_eigenen_principal(mem, monkeypatch, keyed):
    """FK25BR Befund 1, the authenticated case: a job a principal submitted
    is bound to that principal (stamped at submit). The shared (legacy) key
    and another principal are refused with 404 whatever headers they send —
    before FK25B2 both could stop it with the owner's headers."""
    from src import auth, principals

    _shared_key(monkeypatch)
    monkeypatch.setenv("BRIDGE_PRINCIPALS_ENABLED", "true")
    monkeypatch.setattr(routes, "JOB_CANCEL_WAIT_S", 0)
    energy = principals.Principal(id="p1", name="energy-railway", allowed_apps=["werking-energy"])
    report = principals.Principal(id="p2", name="report-vercel", allowed_apps=["*"])
    tokens = {"tok-energy": energy, "tok-report": report}
    monkeypatch.setattr(
        principals, "resolve_principal_by_token", AsyncMock(side_effect=tokens.get)
    )
    assert auth._principals_enabled()
    owner = _owner_for(OWNER_HEADERS, principal=energy)
    mem.add(JOB, "running", attribution={**OWNER_ATTR, registry.EIGENTUEMER_KEY: owner})
    if keyed:
        mem.rows[JOB]["idempotency_owner"] = owner
    for token in (SHARED, "tok-report"):
        r = await _delete(JOB, {**OWNER_HEADERS, "Authorization": f"Bearer {token}"})
        assert r.status_code == 404, (token, r.text)
        assert r.json()["error"]["code"] == "job_not_found"
    assert not mem.ops("cancel_job")
    assert mem.rows[JOB]["cancel_requested"] is False
    r = await _delete(JOB, {**OWNER_HEADERS, "Authorization": "Bearer tok-energy"})
    assert r.status_code == 202, r.text
    assert mem.rows[JOB]["cancel_requested"] is True


@pytest.mark.asyncio
async def test_submit_bindet_den_eigentuemer_serverseitig(mem, monkeypatch):
    """The stamp comes from the authenticated request, never from the body:
    a body naming another owner is overwritten."""
    created = {}

    async def create_job(job_id, kind, payload, attribution):
        created["attribution"] = attribution

    monkeypatch.setattr(store_client, "create_job", create_job)
    monkeypatch.setattr(routes, "spawn", lambda coro: coro.close())
    monkeypatch.setattr(routes, "get_executor", lambda kind: object())
    transport = httpx.ASGITransport(app=_router_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as c:
        r = await c.post(
            "/v1/jobs",
            json={"kind": "chat", "payload": BODY, "attribution": {
                "app_id": "werking-energy", "user_id": "user-a",
                registry.EIGENTUEMER_KEY: "v1:forged",
            }},
            headers=OWNER_HEADERS,
        )
    assert r.status_code in (200, 201), r.text
    assert created["attribution"][registry.EIGENTUEMER_KEY] == _owner_for(OWNER_HEADERS)


def _router_app():
    app = FastAPI()
    app.include_router(routes.router)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "suffix,method",
    [("cancel", "post"), ("finish-cancelled", "post"), ("cancel-state", "get")],
)
async def test_internal_rejects_external_credentials(monkeypatch, suffix, method):
    """Unchanged: the internal routes take the service token only."""
    from src import internal_routes

    app = FastAPI()
    app.include_router(internal_routes.router)
    changed = AsyncMock()
    monkeypatch.setattr(store, "finish_cancelled", changed)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        for headers in [{}, {"Authorization": f"Bearer {SHARED}"},
                        {"X-Bridge-Service-Token": "wrong"}]:
            r = await c.request(method, f"/v1/internal/jobs/{JOB}/{suffix}", headers=headers)
            assert r.status_code in (401, 403), r.text
    changed.assert_not_awaited()


# ---------------------------------------------------------------------------
# FK25BR Befund 4 — old platform-api
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_platform_route_is_loud(mem, monkeypatch):
    """Codex saw 409 job_already_running that named nothing. Now: 502
    job_store_incompatible naming the missing route."""
    mem.add(JOB, "running")
    monkeypatch.setattr(routes, "abbruch_melden", lambda j: False)
    monkeypatch.setattr(
        store_client, "cancel_state",
        AsyncMock(side_effect=store_client.CancelRouteMissing("no route")),
    )
    r = await _delete(JOB, OWNER_HEADERS)
    assert r.status_code == 502
    err = r.json()["error"]
    assert err["code"] == "job_store_incompatible"
    assert "no route" in err["message"] and "predates FK25B" in err["message"]
    assert "job_already_running" not in r.text


@pytest.mark.asyncio
async def test_missing_finish_route_no_false_stopping(mem, monkeypatch):
    """Codex' scenario: finish-cancelled missing, a run that never ends by
    itself. Codex saw 202 while the runner had already ended and the row read
    'running' for good. Now the runner never ends a run on its own; 202 is
    true — the run IS still going (only its endpoint can stop it and confirm;
    the confirmed path with the missing route ends 502/'error', see
    test_job_cancel_fk25b.test_finish_route_fehlt_endet_laut)."""
    mem.add(JOB, "running")
    monkeypatch.setattr(routes, "JOB_CANCEL_WAIT_S", 0.05)
    monkeypatch.setattr(
        store_client, "finish_cancelled",
        AsyncMock(side_effect=store_client.CancelRouteMissing("no finish route")),
    )
    started = asyncio.Event()

    async def work(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setitem(registry._EXECUTORS, "chat", work)
    task = asyncio.create_task(registry._run_body(JOB, "chat", {}, OWNER_ATTR))
    try:
        await started.wait()
        r = await _delete(JOB, OWNER_HEADERS)
        await asyncio.sleep(0.3)
        assert r.status_code == 202 and mem.rows[JOB]["status"] == "running"
        assert not task.done(), "202 must mean: still running"
    finally:
        task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["erkunder", "ping", "proxy", "convert-html-to-pdf"])
async def test_other_running_kinds_unchanged(mem, monkeypatch, kind):
    mem.add(JOB, "running", kind=kind)
    wake = AsyncMock()
    monkeypatch.setattr(routes, "abbruch_melden", wake)
    r = await _delete(JOB, OWNER_HEADERS)
    assert r.status_code == 409 and "job_already_running" in r.text
    wake.assert_not_called()


# ---------------------------------------------------------------------------
# FK25BR Befund 3 — result race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sql_concurrent_done_cancel(pg_store):
    from tests.jobs.test_job_cancel_br10 import _insert

    for n in range(30):
        job = f"race{n}"
        await _insert(pg_store, job, "running")
        await store.cancel_job(job)
        await asyncio.gather(
            store.mark_done(job, {"paid": "result"}), store.finish_cancelled(job)
        )
        row = await store.get_job(job)
        assert row["status"] == "done" and row["result"] == {"paid": "result"}


@pytest.mark.asyncio
async def test_http_result_completed_during_cancel_is_kept(welt, monkeypatch):
    """Codex' socket probe: the endpoint holds a finished result and returns
    it when the stop reaches it. Codex saw DELETE 200, 'cancelled',
    result=None (HTTP_RACE_RESULT_LOST). Now the endpoint answers that
    result, the job ends 'done' with it, DELETE says 409 job_terminal."""
    import threading

    from src import main

    ready = threading.Event()
    returned = threading.Event()
    result = {"choices": [{"message": {"content": "already completed paid result"}}]}

    async def finished_work(*args):
        ready.set()
        try:
            await asyncio.sleep(30)  # pending async persistence/response preparation
        except asyncio.CancelledError:
            returned.set()
            return result

    monkeypatch.setattr(main, "_chat_completions_lauf", finished_work)
    welt.mem.add(JOB, "running")
    task = asyncio.create_task(registry._run_body(JOB, "chat", BODY, OWNER_ATTR))
    assert await _bis(ready.is_set)
    response = await _delete(JOB, OWNER_HEADERS)
    await asyncio.wait_for(task, 5)
    assert await _bis(returned.is_set)
    err = response.json()["error"]
    assert (response.status_code, err["code"], err["status"]) == (409, "job_terminal", "done")
    assert welt.mem.rows[JOB]["status"] == "done"
    assert welt.mem.rows[JOB]["result"]["choices"] == result["choices"]
    assert not welt.mem.ops("finish_cancelled")
    assert not welt.ledger, "a kept result is booked by its run, not as a cancel"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["research", "doc-agent"])
async def test_real_endpoint_supervision_for_other_kinds(welt, monkeypatch, kind):
    """Unchanged scenario; now with 200 the run has ended and is booked."""
    import threading

    from src import main
    from src.jobs import executors

    started = threading.Event()
    stopped = threading.Event()
    monkeypatch.setattr(
        "src.middleware.adaptive_limiter.enforce_pool_admission", AsyncMock()
    )

    async def fake_work(*args, **kwargs):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()

    monkeypatch.setattr(main, "_execute_research_impl", fake_work)
    monkeypatch.setattr(main, "_execute_research_cloud_with_pool_fallback", fake_work)
    monkeypatch.setattr(main, "_execute_doc_agent_impl", fake_work)
    monkeypatch.setitem(
        registry._EXECUTORS, kind,
        executors.research_executor if kind == "research" else executors.doc_agent_executor,
    )
    welt.mem.add(JOB, "running", kind=kind)
    payload = (
        {"query": "review stub only", "model": "claude-sonnet-5-5"} if kind == "research"
        else {"question": "review stub only", "model": "claude-sonnet-5-5",
              "files": [{"name": "a.txt", "content": "stub"}]}
    )
    task = asyncio.create_task(registry._run_body(JOB, kind, payload, OWNER_ATTR))
    try:
        assert await _bis(started.is_set, 3), welt.mem.rows[JOB]
        response = await _delete(JOB, OWNER_HEADERS)
        assert response.status_code == 200
        assert stopped.is_set()
        assert [r["error_code"] for r in welt.ledger] == ["job_cancelled"]
        await asyncio.wait_for(task, 5)
    finally:
        if not task.done():
            task.cancel()


# ---------------------------------------------------------------------------
# FK25BR Befund 5 — cost of a stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_ledger_books_measured_usage(monkeypatch, ledger_seam):
    """Codex' probe through the real writer: before, a stop booked 0 € and
    deducted nothing (CANCEL_LEDGER error 0 0). Now completed model calls are
    booked at their price ('undelivered') and deducted; with nothing
    measured the row stays 0 and says the consumption is unknown."""
    from src.activity import ai_call_writer as writer

    deduction = AsyncMock(return_value=True)
    monkeypatch.setattr(writer, "_deduct_call_cost", deduction)
    ledger_seam.context = {"tenantId": str(uuid.uuid4()), "billingMode": "pay_per_token"}
    req = SimpleNamespace(state=SimpleNamespace(ledger_provider="bedrock"))
    attr = {"app_id": "werking-energy", "user_id": str(uuid.uuid4())}
    zuege = [{"model": "claude-haiku-4-5-20251001", "input_tokens": 40_000,
              "output_tokens": 2_000, "cache_read_tokens": 0, "cache_creation_tokens": 0}]
    bericht = await selbstaufruf._im_ledger_vermerken(
        req, JOB, attr, "claude-haiku-4-5-20251001", "/v1/chat/completions", 123,
        "wunsch", zuege,
    )
    row = ledger_seam.row
    assert row["provider_metadata"]["status"] == "undelivered"
    assert row["provider_metadata"]["verbrauch"] == "teilweise_gemessen"
    assert row["real_cost_eur"] > 0 and row["hypothetical_cost_eur"] > 0
    deduction.assert_awaited_once()
    assert bericht["verbrauch"] == "teilweise_gemessen"

    deduction.reset_mock()
    bericht = await selbstaufruf._im_ledger_vermerken(
        req, JOB, attr, "claude-haiku-4-5-20251001", "/v1/chat/completions", 123,
        "wunsch", [],
    )
    row = ledger_seam.ledger_calls[-1]
    assert row["provider_metadata"]["status"] == "error" and row["real_cost_eur"] == 0
    assert row["provider_metadata"]["verbrauch"] == "unbekannt"
    deduction.assert_not_awaited()
    assert bericht["verbrauch"] == "unbekannt" and bericht["offener_zug_unbekannt"] is True
