"""FK25B — DELETE /v1/jobs/{id} stops a RUNNING chat/research/doc-agent job.

Befund 12.10.2026 (Energy cancel, deploy window, orphans): a DELETE on a
running job only recorded the wish (BR10b, 409). The worker ran the job to
the end — up to 40 min, paid for, result discarded, capacity held.

The contract now, for chat/research/doc-agent:

  own + running → the wish is stored, the running worker stops the run
                  (directly when the DELETE reaches it, else within its
                  poll), its self-call's endpoint cancels the CLI run, the
                  row ends 'cancelled', the ledger notes job_cancelled
                  → 200 cancelled (202 while still being stopped; 409
                  job_terminal when it finished first)
  foreign       → 404, nothing stored, nothing stopped
  replay        → the same key after the cancel answers the cancelled job
  other kinds   → BR10b unchanged (erkunder: its run is not in this worker)

The self-call runs over a REAL socket into the real app (uvicorn), like
tests/unit/test_chat_weiterleitung_chatfwd.py: in-process the endpoint would
be cancelled together with its client, which hides exactly the defect. The
CLI is a stand-in that records how it ended; that the real CLI process and
its children die is tests/unit/test_job_cancel_fk25b_prozessende.py.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

import asyncio  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from typing import Any, Dict, List  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402

import src.claude_cli as claude_cli  # noqa: E402
import src.main as main  # noqa: E402
from src.claude_cli import ClaudeCodeCLI  # noqa: E402
from src.jobs import executors, registry, routes, store, store_client  # noqa: E402
from tests import sdk_strom as sdk  # noqa: E402

HOME = "dev"
JOB = f"job_{HOME}_{'0123456789abcdef' * 2}"
OWNER_HEADERS = {"X-App-ID": "werking-energy", "X-User-ID": "user-a"}
OWNER_ATTR = {"app_id": "werking-energy", "user_id": "user-a", "bridge_origin": "dev"}
FOREIGN_HEADERS = {"X-App-ID": "werking-energy", "X-User-ID": "user-b"}
BODY = {
    "model": "claude-sonnet-5-5",
    "messages": [{"role": "user", "content": "Funktionstest ohne Kundendaten."}],
}
LAUF_S = 30.0  # the run would go on this long — far beyond every wait here


class MemStore:
    """The store calls this feature makes, with store.py's semantics."""

    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.calls: List[tuple] = []

    def add(self, job_id, status, kind="chat", attribution=None, **over):
        now = datetime.now(timezone.utc)
        self.rows[job_id] = {
            "job_id": job_id, "kind": kind, "status": status,
            "attribution": OWNER_ATTR if attribution is None else attribution,
            "created_at": now - timedelta(minutes=2), "updated_at": now,
            "progress": None, "result": None, "error": None,
            "deferred_until": None, "defer_count": 0, "defer_reason": None,
            "cancel_requested": False, **over,
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
        if row["status"] == "running":
            row["cancel_requested"] = True
            return {"status": "running", "changed": False, "cancel_requested": True}
        if row["status"] != "pending":
            return {"status": row["status"], "changed": False, "cancel_requested": False}
        row["status"] = "cancelled"
        return {"status": "cancelled", "changed": True, "cancel_requested": False}

    async def cancel_state(self, job_id):
        self.calls.append(("cancel_state", job_id))
        row = self.rows.get(job_id)
        if row is None:
            return None
        return {"status": row["status"], "cancel_requested": row["cancel_requested"]}

    async def finish_cancelled(self, job_id):
        self.calls.append(("finish_cancelled", job_id))
        row = self.rows.get(job_id)
        if row and row["status"] == "running" and row["cancel_requested"]:
            row["status"] = "cancelled"
            return True
        return False

    async def mark_done(self, job_id, result):
        self.calls.append(("mark_done", job_id))
        self.rows[job_id].update(status="done", result=result)

    async def mark_error(self, job_id, message, **kw):
        self.calls.append(("mark_error", job_id, message))
        self.rows[job_id].update(status="error", error={"message": message, **kw})

    async def heartbeat(self, job_id):
        pass

    async def update_progress(self, job_id, progress):
        pass

    def ops(self, name):
        return [c for c in self.calls if c[0] == name]


@pytest.fixture
def mem(monkeypatch):
    from src.main import extract_attribution_context

    m = MemStore()
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", HOME)
    monkeypatch.setattr(routes, "verify_api_key", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(routes, "_attribution_extractor", extract_attribution_context)
    monkeypatch.setattr(routes, "JOB_CANCEL_WAIT_S", 5.0)
    monkeypatch.setattr(routes, "JOB_CANCEL_WAIT_TICK_S", 0.05)
    monkeypatch.setattr(registry, "JOB_CANCEL_POLL_S", 0.2)
    for name in (
        "get_job", "cancel_job", "cancel_state", "finish_cancelled",
        "mark_done", "mark_error", "heartbeat", "update_progress",
    ):
        monkeypatch.setattr(store_client, name, getattr(m, name))
    return m


def _router_app():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(routes.router)
    return app


async def _delete(job_id, headers):
    transport = httpx.ASGITransport(app=_router_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as c:
        return await c.delete(f"/v1/jobs/{job_id}", headers=headers)


# ---------------------------------------------------------------------------
# The world: the real app on a real socket, the CLI a stand-in
# ---------------------------------------------------------------------------


class Lauf:
    def __init__(self):
        self.ereignisse: List[str] = []

    def query(self):
        lauf = self

        async def query(prompt, options):
            lauf.ereignisse.append("start")
            try:
                yield sdk.init()
                await asyncio.sleep(LAUF_S)
                yield sdk.AssistantMessage(content=[sdk.TextBlock(text="OK")], model="m")
                yield sdk.result(usage={"input_tokens": 70, "output_tokens": 1})
                lauf.ereignisse.append("ende")
            except (asyncio.CancelledError, GeneratorExit):
                lauf.ereignisse.append("abgebrochen")
                raise

        return query


def _cli(tmp_path) -> ClaudeCodeCLI:
    cli = object.__new__(ClaudeCodeCLI)
    cli.timeout = 60
    cli.cwd = tmp_path
    cli.claude_env_vars = {}
    cli.cache_dir = tmp_path
    cli.max_cache_size_mb = 10
    cli.file_discovery = MagicMock()
    return cli


def _freier_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def welt(monkeypatch, tmp_path, mem):
    monkeypatch.setenv("INSTANCE_NAME", "worker1")
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("CLAUDE_SKIP_AUTH", "1")
    monkeypatch.setattr(
        claude_cli, "MessageParseError", type("MessageParseError", (Exception,), {})
    )
    lauf = Lauf()
    monkeypatch.setattr(claude_cli, "query", lauf.query())
    monkeypatch.setattr(main.claude_cli, "run_completion", _cli(tmp_path).run_completion)
    import src.chat_weiterleitung as cw

    monkeypatch.setattr(cw, "AUFSICHT_TAKT_S", 0.05)
    ledger: List[dict] = []

    async def persist(**kw):
        ledger.append(kw)
        return "written"

    monkeypatch.setattr("src.activity.ai_call_writer.persist_ai_call_activity", persist)

    async def admission(request):
        request.state.adaptive_est_tokens = 100

    registry.register_executor("chat", executors.chat_executor)  # main's startup, lifespan off
    port = _freier_port()
    monkeypatch.setattr(executors, "SELF_BASE_URL", f"http://127.0.0.1:{port}")
    patches = [
        patch("src.main.validate_claude_code_auth", return_value=(True, {"method": "test"})),
        patch("src.main.verify_api_key", new_callable=AsyncMock),
        patch("src.main.enforce_pool_admission", side_effect=admission),
        patch("src.providers.fallback.get_fallback_tiers", side_effect=lambda t, **kw: [t]),
    ]
    for p in patches:
        p.start()
    server = uvicorn.Server(
        uvicorn.Config(main.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started

    class W:
        pass

    w = W()
    w.lauf, w.ledger, w.mem = lauf, ledger, mem
    try:
        yield w
    finally:
        server.should_exit = True
        t.join(timeout=10)
        for p in reversed(patches):
            p.stop()
        if main.limiter is not None:
            main.limiter.reset()


async def _bis(bedingung, s=10.0):
    ende = time.monotonic() + s
    while time.monotonic() < ende:
        if bedingung():
            return True
        await asyncio.sleep(0.05)
    return bedingung()


async def _laufenden_job_starten(w, kind="chat"):
    w.mem.add(JOB, "running", kind=kind)
    task = asyncio.ensure_future(registry._run_body(JOB, kind, BODY, OWNER_ATTR))
    assert await _bis(lambda: "start" in w.lauf.ereignisse), "the run never started"
    return task


# ---------------------------------------------------------------------------
# 1. The scenario: running job → DELETE → the run really ends
# ---------------------------------------------------------------------------


async def test_delete_stoppt_den_laufenden_job_auf_diesem_worker(welt):
    task = await _laufenden_job_starten(welt)
    t0 = time.monotonic()
    resp = await _delete(JOB, OWNER_HEADERS)
    dauer = time.monotonic() - t0
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"job_id": JOB, "status": "cancelled"}
    assert dauer < 3.0, dauer
    assert await _bis(lambda: "abgebrochen" in welt.lauf.ereignisse, 5), welt.lauf.ereignisse
    assert "ende" not in welt.lauf.ereignisse
    await asyncio.wait_for(task, 5)
    assert welt.mem.rows[JOB]["status"] == "cancelled"
    assert not welt.mem.ops("mark_done") and not welt.mem.ops("mark_error")
    assert JOB not in registry._LAUFENDE


async def test_delete_auf_anderem_worker_wirkt_ueber_den_store(welt, monkeypatch):
    """The DELETE reaches another worker of the bridge: no local wake-up, the
    running worker sees the stored wish within its poll."""
    monkeypatch.setattr(routes, "abbruch_melden", lambda job_id: False)
    task = await _laufenden_job_starten(welt)
    resp = await _delete(JOB, OWNER_HEADERS)
    assert (resp.status_code, resp.json()["status"]) == (200, "cancelled"), resp.text
    assert await _bis(lambda: "abgebrochen" in welt.lauf.ereignisse, 5)
    await asyncio.wait_for(task, 5)
    assert welt.mem.ops("cancel_state"), "the runner never asked the store"


async def test_ledger_vermerkt_den_abbruch(welt):
    task = await _laufenden_job_starten(welt)
    await _delete(JOB, OWNER_HEADERS)
    await asyncio.wait_for(task, 5)
    assert await _bis(lambda: welt.ledger, 5), "no ledger row for the stopped run"
    (row,) = welt.ledger
    assert row["status"] == "error"
    assert row["error_code"] == "job_cancelled"
    assert row["provider"] == "anthropic"  # routed to the pool before the stop
    assert (row["input_tokens"], row["output_tokens"]) == (0, 0)
    assert row["provider_meta"] == {
        "job_id": JOB, "endpunkt": "/v1/chat/completions", "usage_bekannt": False,
    }
    assert (row["app_id"], row["user_id"]) == ("werking-energy", "user-a")
    assert "nicht gemessen" in row["error_message"]


async def test_fremder_eigentuemer_404_und_der_lauf_geht_weiter(welt):
    task = await _laufenden_job_starten(welt)
    try:
        resp = await _delete(JOB, FOREIGN_HEADERS)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "job_not_found"
        assert welt.mem.rows[JOB]["cancel_requested"] is False
        assert not welt.mem.ops("cancel_job")
        await asyncio.sleep(1.0)  # five poll rounds
        assert welt.lauf.ereignisse == ["start"]
        assert welt.mem.rows[JOB]["status"] == "running"
    finally:
        await _delete(JOB, OWNER_HEADERS)
        await asyncio.wait_for(task, 5)


async def test_selbstaufruf_ohne_jobmarke_bleibt_wie_er_war(welt, monkeypatch):
    """Only a marked job self-call is stopped by a closed connection — a
    plain chat call keeps today's behaviour (its client leaving does not end
    it; that is CHATFWD's matter for relays, not this change's)."""
    monkeypatch.setattr(sys.modules[__name__], "LAUF_S", 2.0)
    async with httpx.AsyncClient(timeout=0.5) as c:
        with pytest.raises(httpx.ReadTimeout):
            await c.post(f"{executors.SELF_BASE_URL}/v1/chat/completions", json=BODY)
    await asyncio.sleep(1.0)
    assert welt.lauf.ereignisse == ["start"]
    assert await _bis(lambda: "ende" in welt.lauf.ereignisse, 5), welt.lauf.ereignisse
    assert not [r for r in welt.ledger if r.get("error_code", "").startswith("job_")]


# ---------------------------------------------------------------------------
# 2. The route's other answers
# ---------------------------------------------------------------------------


async def test_202_solange_der_lauf_noch_gestoppt_wird(mem, monkeypatch):
    """Nobody stops the run within the wait: honest 202, the wish is stored."""
    monkeypatch.setattr(routes, "JOB_CANCEL_WAIT_S", 0.3)
    mem.add(JOB, "running")
    resp = await _delete(JOB, OWNER_HEADERS)
    assert resp.status_code == 202
    assert resp.json()["status"] == "running"
    assert resp.json()["cancel_requested"] is True
    assert mem.rows[JOB]["cancel_requested"] is True
    # A second DELETE is harmless and gives the same answer.
    assert (await _delete(JOB, OWNER_HEADERS)).status_code == 202


async def test_409_wenn_der_lauf_zuerst_fertig_wurde(mem):
    mem.add(JOB, "running")

    async def fertig():
        await asyncio.sleep(0.2)
        mem.rows[JOB]["status"] = "done"

    asyncio.ensure_future(fertig())
    resp = await _delete(JOB, OWNER_HEADERS)
    err = resp.json()["error"]
    assert (resp.status_code, err["code"], err["status"]) == (409, "job_terminal", "done")


async def test_erkunder_bleibt_bei_br10b(mem):
    mem.add(JOB, "running", kind="erkunder")
    resp = await _delete(JOB, OWNER_HEADERS)
    err = resp.json()["error"]
    assert (resp.status_code, err["code"], err["cancel_requested"]) == (
        409, "job_already_running", True,
    )
    assert not mem.ops("cancel_state")


async def test_platform_api_ohne_route_antwortet_wie_vorher(mem, monkeypatch):
    async def fehlt(job_id):
        raise store_client.CancelRouteMissing("no route")

    monkeypatch.setattr(store_client, "cancel_state", fehlt)
    mem.add(JOB, "running")
    resp = await _delete(JOB, OWNER_HEADERS)
    err = resp.json()["error"]
    assert (resp.status_code, err["code"], err["cancel_requested"]) == (
        409, "job_already_running", True,
    )


async def test_replay_nach_dem_abbruch_nennt_den_abgebrochenen_job(mem, monkeypatch):
    """Idempotency-Key: the retry after the cancel gets the same, cancelled
    job — no new job, no new run."""
    mem.add(JOB, "running")
    monkeypatch.setattr(routes, "JOB_CANCEL_WAIT_S", 0.0)
    await _delete(JOB, OWNER_HEADERS)
    await mem.finish_cancelled(JOB)  # the runner, see section 1
    owner_of = {}

    async def find(owner, key, kind, payload):
        owner_of["owner"] = owner
        return dict(mem.rows[JOB])

    spawned = MagicMock()
    monkeypatch.setattr(store_client, "find_job_by_idempotency_key", find)
    monkeypatch.setattr(routes, "spawn", spawned)
    monkeypatch.setattr(routes, "get_executor", lambda kind: object())
    transport = httpx.ASGITransport(app=_router_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as c:
        resp = await c.post(
            "/v1/jobs",
            json={"kind": "chat", "payload": BODY},
            headers={**OWNER_HEADERS, "Idempotency-Key": "fk25b-1"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"job_id": JOB, "status": "cancelled", "kind": "chat"}
    assert resp.headers["Idempotent-Replayed"] == "true"
    spawned.assert_not_called()


# ---------------------------------------------------------------------------
# 3. The runner
# ---------------------------------------------------------------------------


async def test_selbstaufruf_traegt_die_jobmarke_nur_im_joblauf(mem):
    seen = {}

    async def executor(payload, attribution, report_progress):
        seen["headers"] = executors._build_headers(attribution)
        return {"ok": True}

    registry.register_executor("fk25b-probe", executor)
    mem.add(JOB, "running", kind="fk25b-probe")
    await registry._run_body(JOB, "fk25b-probe", {}, OWNER_ATTR)
    assert seen["headers"][registry.JOB_LAUF_HEADER] == JOB
    assert registry.JOB_LAUF_HEADER not in executors._build_headers(OWNER_ATTR)


async def test_ergebnis_im_selben_moment_wird_behalten(mem):
    """The run completes in the instant it is stopped: its paid result is kept."""
    gestartet = asyncio.Event()

    async def executor(payload, attribution, report_progress):
        gestartet.set()
        try:
            await asyncio.sleep(LAUF_S)
        except asyncio.CancelledError:
            return {"schon": "fertig"}
        return {}

    registry.register_executor("chat", executor)
    try:
        mem.add(JOB, "running")
        task = asyncio.ensure_future(registry._run_body(JOB, "chat", {}, OWNER_ATTR))
        await gestartet.wait()
        mem.rows[JOB]["cancel_requested"] = True
        registry.abbruch_melden(JOB)
        await asyncio.wait_for(task, 5)
    finally:
        registry.register_executor("chat", executors.chat_executor)
    assert mem.rows[JOB]["status"] == "done"
    assert mem.rows[JOB]["result"] == {"schon": "fertig"}
    assert not mem.ops("finish_cancelled")


async def test_wache_endet_nicht_den_lauf_wenn_der_store_hakt(mem, monkeypatch):
    """A failing poll is logged, never read as a cancel."""
    async def kaputt(job_id):
        raise RuntimeError("platform-api hiccup")

    monkeypatch.setattr(store_client, "cancel_state", kaputt)

    async def executor(payload, attribution, report_progress):
        await asyncio.sleep(0.8)
        return {"ok": True}

    registry.register_executor("fk25b-probe", executor)
    mem.add(JOB, "running", kind="chat")
    with patch.object(registry, "get_executor", lambda kind: executor):
        await registry._run_body(JOB, "chat", {}, OWNER_ATTR)
    assert mem.rows[JOB]["status"] == "done"


# ---------------------------------------------------------------------------
# 4. The worker → platform-api seam
# ---------------------------------------------------------------------------


class _PResp:
    def __init__(self, status_code, json_body=None):
        self.status_code = status_code
        self.json = json_body


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"job": None}, None),
        ({"job": {"status": "running", "cancel_requested": True}},
         {"status": "running", "cancel_requested": True}),
    ],
)
async def test_store_client_cancel_state(body, expected):
    with patch.object(store_client, "call_platform", AsyncMock(return_value=_PResp(200, body))):
        assert await store_client.cancel_state(JOB) == expected


@pytest.mark.parametrize("op", ["cancel_state", "finish_cancelled"])
@pytest.mark.parametrize("status", [404, 405])
async def test_store_client_alte_platform_api_ist_laut(op, status):
    with patch.object(
        store_client, "call_platform", AsyncMock(return_value=_PResp(status, {"detail": "x"}))
    ):
        with pytest.raises(store_client.CancelRouteMissing):
            await getattr(store_client, op)(JOB)


@pytest.mark.parametrize("op", ["cancel_state", "finish_cancelled"])
async def test_store_client_unbekannte_antwort_ist_laut(op):
    with patch.object(
        store_client, "call_platform", AsyncMock(return_value=_PResp(200, {"x": 1}))
    ):
        with pytest.raises(store_client.JobStoreUnavailable):
            await getattr(store_client, op)(JOB)


@pytest.mark.parametrize("changed", [True, False])
async def test_store_client_finish_cancelled(changed):
    with patch.object(
        store_client, "call_platform",
        AsyncMock(return_value=_PResp(200, {"changed": changed})),
    ):
        assert await store_client.finish_cancelled(JOB) is changed


async def test_internal_routes_reichen_den_store_durch(monkeypatch):
    from src import internal_routes

    monkeypatch.setattr(store, "cancel_state", AsyncMock(return_value=None))
    monkeypatch.setattr(store, "finish_cancelled", AsyncMock(return_value=True))
    assert await internal_routes.internal_cancel_state(JOB, _claims=None) == {"job": None}
    assert await internal_routes.internal_finish_cancelled(JOB, _claims=None) == {
        "changed": True
    }


# ---------------------------------------------------------------------------
# 5. The SQL (real Postgres, BRIDGE_TEST_PG_URL, the real migrations)
# ---------------------------------------------------------------------------

from tests.jobs import test_job_cancel_br10 as br10  # noqa: E402

pg_store = br10.pg_store


async def test_sql_cancel_state_ist_schmal_und_ehrlich(pg_store):
    await br10._insert(pg_store, "j_run", "running")
    await br10._insert(pg_store, "j_pend", "pending")
    assert await store.cancel_state("j_run") == {"status": "running", "cancel_requested": False}
    await store.cancel_job("j_run")
    assert await store.cancel_state("j_run") == {"status": "running", "cancel_requested": True}
    await store.cancel_job("j_pend")
    assert await store.cancel_state("j_pend") == {
        "status": "cancelled", "cancel_requested": False,
    }
    assert await store.cancel_state("j_none") is None


async def test_sql_finish_cancelled_nur_mit_wunsch_und_nur_im_lauf(pg_store):
    for job_id, status in (("j_wish", "running"), ("j_nowish", "running"), ("j_done", "done")):
        await br10._insert(pg_store, job_id, status)
    await store.cancel_job("j_wish")
    assert await store.finish_cancelled("j_wish") is True
    async with pg_store.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, finished_at FROM ai_jobs WHERE job_id = 'j_wish'"
        )
    assert row["status"] == "cancelled" and row["finished_at"] is not None
    assert await store.finish_cancelled("j_wish") is False  # once
    assert await store.finish_cancelled("j_nowish") is False  # nobody asked
    assert await br10._status(pg_store, "j_nowish") == "running"
    await store.cancel_job("j_done")
    assert await store.finish_cancelled("j_done") is False  # finished first
    assert await br10._status(pg_store, "j_done") == "done"


async def test_sql_abgebrochener_job_wird_nie_wieder_geholt(pg_store):
    """A row closed by finish_cancelled is terminal: no claim path takes it."""
    await br10._insert(pg_store, "j_x", "running", heartbeat_age_s=3600)
    await store.cancel_job("j_x")
    await store.finish_cancelled("j_x")
    assert await store.claim_stale_job(60, 5) is None
    assert await store.mark_running("j_x") is False
    assert await br10._status(pg_store, "j_x") == "cancelled"


async def test_pg_laufender_chatjob_endet_cancelled_und_kommt_nie_wieder(pg_store, monkeypatch):
    """The scenario on the real store: a chat job runs, its owner's DELETE
    stops it, the row is 'cancelled', and no watchdog pass starts it again."""
    from src.main import extract_attribution_context

    monkeypatch.setenv("BRIDGE_ORIGIN_ID", HOME)
    monkeypatch.setattr(routes, "verify_api_key", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(routes, "_attribution_extractor", extract_attribution_context)
    monkeypatch.setattr(registry, "JOB_CANCEL_POLL_S", 0.2)
    for name in (
        "get_job", "cancel_job", "cancel_state", "finish_cancelled", "mark_running",
        "defer_job", "heartbeat", "update_progress", "mark_done", "mark_error",
        "claim_stale_job", "find_abandoned",
    ):
        monkeypatch.setattr(store_client, name, getattr(store, name))
    gestartet = asyncio.Event()
    ende = []

    async def executor(payload, attribution, report_progress):
        gestartet.set()
        try:
            await asyncio.sleep(LAUF_S)
        except asyncio.CancelledError:
            ende.append("abgebrochen")
            raise
        return {}

    calls = AsyncMock(side_effect=executor)
    monkeypatch.setitem(registry._EXECUTORS, "chat", calls)
    await br10._insert(pg_store, JOB, "pending")
    lauf = asyncio.ensure_future(registry.run_generic_job(JOB, "chat", {}, OWNER_ATTR))
    await asyncio.wait_for(gestartet.wait(), 5)
    resp = await _delete(JOB, OWNER_HEADERS)
    assert (resp.status_code, resp.json()) == (200, {"job_id": JOB, "status": "cancelled"})
    await asyncio.wait_for(lauf, 5)
    assert ende == ["abgebrochen"]
    assert await br10._status(pg_store, JOB) == "cancelled"
    async with pg_store.acquire() as conn:
        await conn.execute(
            "UPDATE ai_jobs SET deferred_until = NOW() - interval '1 second', "
            "heartbeat_at = NOW() - interval '900 seconds'"
        )
    assert await registry.run_watchdog_pass(90, 3) == {"requeued": 0, "failed": 0}
    assert calls.await_count == 1
