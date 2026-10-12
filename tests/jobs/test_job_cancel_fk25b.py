"""FK25B — DELETE /v1/jobs/{id} stops a RUNNING chat/research/doc-agent job.

Befund 12.10.2026 (Energy cancel, deploy window, orphans): a DELETE on a
running job only recorded the wish (BR10b, 409). The worker ran the job to
the end — up to 40 min, paid for, result discarded, capacity held.

The contract now, for chat/research/doc-agent (FK25B2: decided and
confirmed by the endpoint that runs the work, src/jobs/selbstaufruf.py):

  own + running → the wish is stored; the self-call's endpoint sees it
                  (directly when the DELETE reaches its worker, else within
                  its poll), stops the CLI run, waits for it to end, books
                  the stop and confirms; only then the runner closes the row
                  'cancelled' → 200 cancelled (202 while not yet confirmed;
                  409 job_terminal when it finished first — its result kept)
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
from src.jobs import executors, registry, routes, selbstaufruf, store, store_client  # noqa: E402
from tests import sdk_strom as sdk  # noqa: E402

HOME = "dev"
JOB = f"job_{HOME}_{'0123456789abcdef' * 2}"
OWNER_HEADERS = {"X-App-ID": "werking-energy", "X-User-ID": "user-a"}


def _eigentuemer(headers: Dict[str, str]) -> str:
    """The owner POST /v1/jobs binds for a caller with these headers
    (shared key, no principal) — routes._idempotency_owner (FK25B2)."""
    from starlette.requests import Request

    from src.main import extract_attribution_context

    req = Request({
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    })
    with patch.object(routes, "_attribution_extractor", extract_attribution_context):
        return routes._idempotency_owner(req)


OWNER_ATTR = {
    "app_id": "werking-energy", "user_id": "user-a", "bridge_origin": "dev",
    registry.EIGENTUEMER_KEY: _eigentuemer(OWNER_HEADERS),
}
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
        self.calls.append(("update_progress", job_id))
        if job_id in self.rows:
            self.rows[job_id]["progress"] = progress

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
    monkeypatch.setattr(selbstaufruf, "JOB_CANCEL_POLL_S", 0.2)
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
    assert resp.json() == {"job_id": JOB, "status": "cancelled", "ledger": "gebucht"}
    assert dauer < 3.0, dauer
    # FK25B2: 200 means the run HAS ended and the stop IS booked — not later.
    assert "abgebrochen" in welt.lauf.ereignisse, welt.lauf.ereignisse
    assert [r["error_code"] for r in welt.ledger] == ["job_cancelled"]
    assert "ende" not in welt.lauf.ereignisse
    await asyncio.wait_for(task, 5)
    assert welt.mem.rows[JOB]["status"] == "cancelled"
    assert not welt.mem.ops("mark_done") and not welt.mem.ops("mark_error")
    assert JOB not in selbstaufruf._LAUFENDE


async def test_delete_auf_anderem_worker_wirkt_ueber_den_store(welt, monkeypatch):
    """The DELETE reaches another worker of the bridge: no local wake-up, the
    endpoint running the work sees the stored wish within its poll."""
    monkeypatch.setattr(routes, "abbruch_melden", lambda job_id: False)
    task = await _laufenden_job_starten(welt)
    resp = await _delete(JOB, OWNER_HEADERS)
    assert (resp.status_code, resp.json()["status"]) == (200, "cancelled"), resp.text
    assert await _bis(lambda: "abgebrochen" in welt.lauf.ereignisse, 5)
    await asyncio.wait_for(task, 5)
    assert welt.mem.ops("cancel_state"), "the endpoint never asked the store"


async def test_ledger_vermerkt_den_abbruch(welt):
    task = await _laufenden_job_starten(welt)
    await _delete(JOB, OWNER_HEADERS)
    await asyncio.wait_for(task, 5)
    assert await _bis(lambda: welt.ledger, 5), "no ledger row for the stopped run"
    (row,) = welt.ledger
    # Nothing measured (the stand-in reports no model call before the stop):
    # 'error', 0 tokens — and the row says the in-flight call is unknown.
    assert row["status"] == "error"
    assert row["error_code"] == "job_cancelled"
    assert row["provider"] == "anthropic"  # routed to the pool before the stop
    assert (row["input_tokens"], row["output_tokens"]) == (0, 0)
    assert row["provider_meta"] == {
        "job_id": JOB, "endpunkt": "/v1/chat/completions", "verbrauch": "unbekannt",
        "gemessene_zuege": 0, "offener_zug_unbekannt": True,
    }
    assert (row["app_id"], row["user_id"]) == ("werking-energy", "user-a")
    assert "nicht messbar" in row["error_message"]


async def test_ledger_bucht_gemessene_modellaufrufe(welt, monkeypatch):
    """FK25BR Befund 5: model calls completed before the stop are booked at
    their usage as 'undelivered' (cost-bearing), per model; the call in
    flight is named unknown. The stand-in hands its messages to the same
    collector hook the SDK parser calls (sdk_parser → teilverbrauch); that
    the real parser does is tests/unit/test_job_cancel_fk25b_prozessende.py."""
    from src.jobs import teilverbrauch

    async def query(prompt, options):
        welt.lauf.ereignisse.append("start")
        for mid, model, usage in (
            ("msg_1", "claude-sonnet-5-5", {"input_tokens": 1200, "output_tokens": 300,
                                            "cache_read_input_tokens": 5000}),
            ("msg_1", "claude-sonnet-5-5", {"input_tokens": 1200, "output_tokens": 340,
                                            "cache_read_input_tokens": 5000}),
            ("msg_2", "claude-haiku-4-5-20251001", {"input_tokens": 80, "output_tokens": 9}),
        ):
            teilverbrauch.aus_cli_nachricht(
                {"type": "assistant", "message": {"id": mid, "model": model, "usage": usage}}
            )
        try:
            yield sdk.init()
            await asyncio.sleep(LAUF_S)
        except (asyncio.CancelledError, GeneratorExit):
            welt.lauf.ereignisse.append("abgebrochen")
            raise

    monkeypatch.setattr(claude_cli, "query", query)
    task = await _laufenden_job_starten(welt)
    resp = await _delete(JOB, OWNER_HEADERS)
    assert resp.status_code == 200, resp.text
    await asyncio.wait_for(task, 5)
    rows = {r["model"]: r for r in welt.ledger}
    assert set(rows) == {"claude-sonnet-5-5", "claude-haiku-4-5-20251001"}
    sonnet = rows["claude-sonnet-5-5"]
    assert sonnet["status"] == "undelivered"
    assert (sonnet["input_tokens"], sonnet["output_tokens"], sonnet["cache_read_tokens"]) == (
        1200, 340, 5000,  # one model call, the last report wins
    )
    assert sonnet["error_code"] == "job_cancelled"
    assert sonnet["provider_meta"]["verbrauch"] == "teilweise_gemessen"
    assert sonnet["provider_meta"]["gemessene_zuege"] == 2
    assert sonnet["provider_meta"]["offener_zug_unbekannt"] is True
    assert rows["claude-haiku-4-5-20251001"]["input_tokens"] == 80


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


async def test_platform_api_ohne_route_ist_laut(mem, monkeypatch):
    """FK25BR Befund 4: never BR10b's 409 in disguise — 502 that names it."""
    async def fehlt(job_id):
        raise store_client.CancelRouteMissing("no cancel-state route")

    monkeypatch.setattr(store_client, "cancel_state", fehlt)
    mem.add(JOB, "running")
    resp = await _delete(JOB, OWNER_HEADERS)
    err = resp.json()["error"]
    assert (resp.status_code, err["code"], err["cancel_requested"], err["status"]) == (
        502, "job_store_incompatible", True, "running",
    )
    assert "no cancel-state route" in err["message"]
    assert err["retryable"] is False


async def test_unbundener_altjob_bleibt_bei_br10b(mem):
    """A row from before FK25B2 has no bound owner: its run is not stopped,
    DELETE records the wish as BR10b did."""
    attr = {k: v for k, v in OWNER_ATTR.items() if k != registry.EIGENTUEMER_KEY}
    mem.add(JOB, "running", attribution=attr)
    resp = await _delete(JOB, OWNER_HEADERS)
    err = resp.json()["error"]
    assert (resp.status_code, err["code"], err["cancel_requested"]) == (
        409, "job_already_running", True,
    )
    assert not mem.ops("cancel_state")


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


async def test_selbstaufruf_traegt_die_jobmarke_nur_im_joblauf(mem, monkeypatch):
    """Marked: the self-call of a stoppable kind with a bound owner. Not: any
    other kind (a proxy job calling a chat path stays BR10b), an unbound
    (pre-FK25B2) job, a call outside a job."""
    seen = {}

    async def executor(payload, attribution, report_progress):
        seen.setdefault("headers", []).append(executors._build_headers(attribution))
        return {"ok": True}

    unbound = {k: v for k, v in OWNER_ATTR.items() if k != registry.EIGENTUEMER_KEY}
    monkeypatch.setitem(registry._EXECUTORS, "chat", executor)
    monkeypatch.setitem(registry._EXECUTORS, "proxy", executor)
    for kind, attr in (("chat", OWNER_ATTR), ("proxy", OWNER_ATTR), ("chat", unbound)):
        mem.add(JOB, "running", kind=kind)
        await registry._run_body(JOB, kind, {}, attr)
    marks = [h.get(selbstaufruf.JOB_LAUF_HEADER) for h in seen["headers"]]
    assert marks == [JOB, None, None]
    assert selbstaufruf.JOB_LAUF_HEADER not in executors._build_headers(OWNER_ATTR)


async def test_ohne_bestaetigung_bleibt_der_job_im_lauf(mem, monkeypatch):
    """The runner never closes the row on its own: without the endpoint's
    confirmation the row stays running (DELETE says 202) until the run ends."""
    monkeypatch.setattr(routes, "JOB_CANCEL_WAIT_S", 0.3)
    halt = asyncio.Event()

    async def executor(payload, attribution, report_progress):
        await halt.wait()
        return {"ok": True}

    monkeypatch.setitem(registry._EXECUTORS, "chat", executor)
    mem.add(JOB, "running")
    task = asyncio.ensure_future(registry._run_body(JOB, "chat", {}, OWNER_ATTR))
    resp = await _delete(JOB, OWNER_HEADERS)
    assert resp.status_code == 202 and mem.rows[JOB]["status"] == "running"
    halt.set()
    await asyncio.wait_for(task, 5)
    assert mem.rows[JOB]["status"] == "done"
    assert not mem.ops("finish_cancelled")


async def test_finish_route_fehlt_endet_laut(mem, monkeypatch):
    """FK25BR Befund 4 (finish-cancelled missing): the run was stopped; the
    row ends 'error' JOB_STORE_INCOMPATIBLE, DELETE answers 502 — never a
    'running' row nobody runs, never 202."""
    monkeypatch.setattr(
        store_client, "finish_cancelled",
        AsyncMock(side_effect=store_client.CancelRouteMissing("no finish-cancelled route")),
    )

    async def executor(payload, attribution, report_progress):
        while not mem.rows[JOB]["cancel_requested"]:
            await asyncio.sleep(0.02)
        raise selbstaufruf.JobLaufAbgebrochen({"grund": "job_cancelled", "ledger": "gebucht"})

    monkeypatch.setitem(registry._EXECUTORS, "chat", executor)
    mem.add(JOB, "running")
    task = asyncio.ensure_future(registry._run_body(JOB, "chat", {}, OWNER_ATTR))
    resp = await _delete(JOB, OWNER_HEADERS)
    await asyncio.wait_for(task, 5)
    err = resp.json()["error"]
    assert (resp.status_code, err["code"], err["status"]) == (
        502, "job_store_incompatible", "error",
    )
    assert mem.rows[JOB]["status"] == "error"
    assert mem.rows[JOB]["error"]["code"] == "JOB_STORE_INCOMPATIBLE"


async def test_aufsicht_endet_nicht_den_lauf_wenn_der_store_hakt(welt, monkeypatch):
    """A failing poll at the endpoint is logged, never read as a cancel."""
    async def kaputt(job_id):
        raise RuntimeError("platform-api hiccup")

    monkeypatch.setattr(store_client, "cancel_state", kaputt)
    monkeypatch.setattr(sys.modules[__name__], "LAUF_S", 1.0)
    task = await _laufenden_job_starten(welt)
    await asyncio.wait_for(task, 10)
    assert welt.lauf.ereignisse == ["start", "ende"]
    assert welt.mem.rows[JOB]["status"] == "done"


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
    for name in (
        "get_job", "cancel_job", "cancel_state", "finish_cancelled", "mark_running",
        "defer_job", "heartbeat", "update_progress", "mark_done", "mark_error",
        "claim_stale_job", "find_abandoned",
    ):
        monkeypatch.setattr(store_client, name, getattr(store, name))
    gestartet = asyncio.Event()
    ende = []

    async def executor(payload, attribution, report_progress):
        # Stands for the self-call: its endpoint sees the stored wish, stops
        # the run and confirms (selbstaufruf; over a socket in section 1).
        gestartet.set()
        while not (await store.cancel_state(JOB))["cancel_requested"]:
            await asyncio.sleep(0.05)
        ende.append("abgebrochen")
        raise selbstaufruf.JobLaufAbgebrochen({"grund": "job_cancelled", "ledger": "gebucht"})

    calls = AsyncMock(side_effect=executor)
    monkeypatch.setitem(registry._EXECUTORS, "chat", calls)
    await br10._insert(pg_store, JOB, "pending", attribution=OWNER_ATTR)
    lauf = asyncio.ensure_future(registry.run_generic_job(JOB, "chat", {}, OWNER_ATTR))
    await asyncio.wait_for(gestartet.wait(), 5)
    resp = await _delete(JOB, OWNER_HEADERS)
    assert (resp.status_code, resp.json()) == (
        200, {"job_id": JOB, "status": "cancelled", "ledger": "gebucht"},
    )
    await asyncio.wait_for(lauf, 5)
    assert ende == ["abgebrochen"]
    assert await br10._status(pg_store, JOB) == "cancelled"
    # The stop's account survives finish_cancelled on the real row.
    assert (await store.get_job(JOB))["progress"]["abbruch"]["ledger"] == "gebucht"
    async with pg_store.acquire() as conn:
        await conn.execute(
            "UPDATE ai_jobs SET deferred_until = NOW() - interval '1 second', "
            "heartbeat_at = NOW() - interval '900 seconds'"
        )
    assert await registry.run_watchdog_pass(90, 3) == {"requeued": 0, "failed": 0}
    assert calls.await_count == 1
