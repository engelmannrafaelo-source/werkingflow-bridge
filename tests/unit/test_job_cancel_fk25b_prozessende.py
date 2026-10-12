"""FK25B / FK25B2: running job → DELETE → the CLI process and its child are
gone — and already gone when DELETE says 200.

Taken over from Codex' counterprobe (FK25BR, test_process_all.py, all three
kinds × both ways) as the acceptance of FK25B2: there 200 came while CLI and
child still lived and the ledger was empty (6/6); here 200 may only come
after both ended and the stop is booked. The stand-in CLI reports one
completed model call before it hangs: its usage must reach the ledger through
the REAL SDK parser (sdk_parser → teilverbrauch, FK25BR Befund 5).

The whole chain with real parts: the job runner (registry._run_body) runs the
chat executor, whose self-call goes over a real socket into the real app
(uvicorn); the app runs the REAL SDK transport against a stand-in "claude"
that ignores SIGTERM and has a child. DELETE /v1/jobs/{id} by the owner. The
CLI and its child must die through the bounded close (BRLZR R2).

Needs the REAL SDK, like test_cli_frist_prozessende.py — run it on its own;
in a session where another module installed the SDK stand-in it skips loudly.
"""
from __future__ import annotations

import asyncio
import os
import signal
import socket
import threading
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import claude_code_sdk
import pytest

if not isinstance(getattr(claude_code_sdk, "__file__", None), str):
    pytest.skip(
        "claude_code_sdk is a test stand-in in this session — run "
        "tests/unit/test_job_cancel_fk25b_prozessende.py on its own",
        allow_module_level=True,
    )

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import src.claude_cli as claude_cli  # noqa: E402
import src.main as main  # noqa: E402
from src.claude_cli import ClaudeCodeCLI  # noqa: E402
from src.jobs import executors, registry, routes, selbstaufruf, store_client  # noqa: E402

JOB = f"job_dev_{'0123456789abcdef' * 2}"
OWNER = {"X-App-ID": "werking-energy", "X-User-ID": "user-a"}


def _eigentuemer() -> str:
    from starlette.requests import Request

    req = Request({
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in OWNER.items()],
    })
    with patch.object(routes, "_attribution_extractor", main.extract_attribution_context):
        return routes._idempotency_owner(req)


ATTR = {
    "app_id": "werking-energy", "user_id": "user-a", "bridge_origin": "dev",
    registry.EIGENTUEMER_KEY: _eigentuemer(),
}
BODY = {"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "x"}]}


def _lebt(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            zustand = f.read().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return zustand not in ("Z", "X")


class Store:
    def __init__(self):
        self.row = {
            "job_id": JOB, "kind": "chat", "status": "running", "attribution": ATTR,
            "created_at": datetime.now(timezone.utc), "cancel_requested": False,
        }

    async def get_job(self, job_id):
        return dict(self.row)

    async def cancel_job(self, job_id):
        self.row["cancel_requested"] = True
        return {"status": self.row["status"], "changed": False, "cancel_requested": True}

    async def cancel_state(self, job_id):
        return {"status": self.row["status"], "cancel_requested": self.row["cancel_requested"]}

    async def finish_cancelled(self, job_id):
        if self.row["status"] == "running" and self.row["cancel_requested"]:
            self.row["status"] = "cancelled"
            return True
        return False

    async def mark_done(self, job_id, result):
        self.row["status"] = "done"

    async def mark_error(self, job_id, message, **kw):
        self.row["status"] = "error"

    async def heartbeat(self, job_id):
        pass

    async def update_progress(self, job_id, progress):
        pass


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _cli(tmp_path) -> ClaudeCodeCLI:
    cli = object.__new__(ClaudeCodeCLI)
    cli.timeout = 60
    cli.cwd = tmp_path
    cli.claude_env_vars = {}
    cli.cache_dir = tmp_path
    cli.max_cache_size_mb = 10
    cli.file_discovery = MagicMock()
    return cli


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["chat", "research", "doc-agent"])
@pytest.mark.parametrize("weg", ["dieser_worker", "anderer_worker"])
async def test_delete_beendet_cli_samt_kind(tmp_path, monkeypatch, weg, kind):
    from claude_code_sdk import ClaudeCodeOptions
    from claude_code_sdk import query as sdk_query
    from claude_code_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    pids = tmp_path / "pids"
    cli = tmp_path / "claude"  # the name routes it through the spawn patch
    cli.write_text(
        "#!/bin/sh\n"
        "trap '' TERM\n"
        "echo '{\"type\":\"assistant\",\"message\":{\"id\":\"msg_t1\","
        "\"model\":\"claude-sonnet-5-5\",\"role\":\"assistant\",\"content\":"
        "[{\"type\":\"text\",\"text\":\"Teil\"}],\"usage\":{\"input_tokens\":111,"
        "\"output_tokens\":22}}}'\n"
        "sleep 60 &\n"
        f"echo $$ $! > {pids}\n"
        "while :; do sleep 1; done\n"
    )
    cli.chmod(0o755)

    async def echte_query(prompt, options):
        tr = SubprocessCLITransport(prompt="p", options=ClaudeCodeOptions(), cli_path=str(cli))
        async for m in sdk_query(prompt="p", transport=tr):
            yield m

    import src.chat_weiterleitung as cw

    monkeypatch.setenv("INSTANCE_NAME", "worker1")
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "dev")
    monkeypatch.setattr(claude_cli, "CLI_TERM_GRACE_S", 2.0, raising=False)
    monkeypatch.setattr(claude_cli, "query", echte_query)
    monkeypatch.setattr(main.claude_cli, "run_completion", _cli(tmp_path).run_completion)
    monkeypatch.setattr(cw, "AUFSICHT_TAKT_S", 0.05)
    monkeypatch.setattr(selbstaufruf, "JOB_CANCEL_POLL_S", 0.5)
    monkeypatch.setattr(routes, "JOB_CANCEL_WAIT_S", 8.0)
    monkeypatch.setattr(routes, "verify_api_key", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(routes, "_attribution_extractor", main.extract_attribution_context)
    if weg == "anderer_worker":
        monkeypatch.setattr(routes, "abbruch_melden", lambda job_id: False)
    st = Store()
    st.row["kind"] = kind
    monkeypatch.setattr("src.middleware.adaptive_limiter.enforce_pool_admission", AsyncMock())
    for name in ("get_job", "cancel_job", "cancel_state", "finish_cancelled",
                 "mark_done", "mark_error", "heartbeat", "update_progress"):
        monkeypatch.setattr(store_client, name, getattr(st, name))
    ledger = []

    async def persist(**kw):
        ledger.append(kw)
        return "written"

    monkeypatch.setattr("src.activity.ai_call_writer.persist_ai_call_activity", persist)
    registry.register_executor(kind, {
        "chat": executors.chat_executor,
        "research": executors.research_executor,
        "doc-agent": executors.doc_agent_executor,
    }[kind])
    body = {
        "chat": BODY,
        "research": {"query": "stub review", "model": BODY["model"]},
        "doc-agent": {"question": "stub review", "model": BODY["model"],
                      "files": [{"name": "a.txt", "content": "stub"}]},
    }[kind]
    port = _port()
    monkeypatch.setattr(executors, "SELF_BASE_URL", f"http://127.0.0.1:{port}")

    async def admission(request):
        request.state.adaptive_est_tokens = 100

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
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    lauf = None
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        assert server.started
        lauf = asyncio.ensure_future(registry._run_body(JOB, kind, body, ATTR))
        for _ in range(200):
            if pids.exists() and pids.read_text().strip():
                break
            await asyncio.sleep(0.05)
        cli_pid, kind_pid = map(int, pids.read_text().split())
        assert _lebt(cli_pid) and _lebt(kind_pid)

        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(routes.router)
        t0 = time.monotonic()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://bridge"
        ) as c:
            resp = await c.delete(f"/v1/jobs/{JOB}", headers=OWNER)
        weg_nach = time.monotonic() - t0
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "cancelled"
        cli_lebt, kind_lebt = _lebt(cli_pid), _lebt(kind_pid)
        gebucht = [(r["status"], r["input_tokens"]) for r in ledger]
        print(f"\nAT_200 {kind} {weg}: {weg_nach:.2f}s cli_alive={cli_lebt} "
              f"child_alive={kind_lebt} ledger={gebucht}")
        # FK25BR Befund 2, inverted: at 200 the work IS over and booked.
        assert not cli_lebt, "200 while the CLI still lives"
        assert not kind_lebt, "200 while the CLI's child still lives"
        assert [r["error_code"] for r in ledger] == ["job_cancelled"], ledger
        (row,) = ledger
        assert (row["status"], row["model"], row["input_tokens"], row["output_tokens"]) == (
            "undelivered", "claude-sonnet-5-5", 111, 22,
        ), row
        assert row["provider_meta"]["offener_zug_unbekannt"] is True
        assert weg_nach < 8.0, weg_nach
        await asyncio.wait_for(lauf, 5)
        assert st.row["status"] == "cancelled"
    finally:
        if lauf is not None and not lauf.done():
            lauf.cancel()
        if pids.exists():
            for pid in map(int, pids.read_text().split()):
                if _lebt(pid):
                    os.kill(pid, signal.SIGKILL)
        server.should_exit = True
        thread.join(timeout=10)
        for p in reversed(patches):
            p.stop()
