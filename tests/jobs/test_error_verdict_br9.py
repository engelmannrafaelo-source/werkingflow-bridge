"""BR9: every error the bridge hands out carries its verdict structured and
true — retryable (+ retry_after_s when known) — decided in one place
(src/error_contract.py), never by text.

Energy RETRY reads only these fields. Before BR9:
  * job errors were {message, code}, no verdict at all (store.mark_error);
  * research-cloud failures were transient only in the TEXT (_mark_retryable);
  * the chat pin error of the base class (unknown provider, 401/403/404 of the
    home bridge, missing peer) went out as 503 and therefore retryable:true,
    so a permanent configuration error would be retried (BR8R §5c);
  * GET /v1/jobs/{id} showed a parked job only as 'pending', so a poller could
    not tell "waits on purpose" from "hangs".
"""
from __future__ import annotations

import os

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx

from src.jobs import executors, registry, store_client

HEX32 = "0123456789abcdef" * 2


def _main():
    import sys
    from unittest.mock import MagicMock

    for name in ("claude_code_sdk", "claude_code_sdk._errors",
                 "claude_code_sdk._internal", "claude_code_sdk._internal.client"):
        sys.modules.setdefault(name, MagicMock())
    from src import main

    return main


class _Seam:
    def __init__(self, defer_count: int = 0):
        self.mark_error = AsyncMock()
        self.mark_done = AsyncMock()
        self.defer_job = AsyncMock()
        self.defer_count = defer_count

    def verdict(self):
        self.mark_error.assert_awaited_once()
        kw = self.mark_error.await_args.kwargs
        return kw.get("code"), kw.get("retryable"), kw.get("retry_after_s")


async def _run(kind: str, seam: _Seam, *, answer=None, raises=None, executor=None):
    async def fake_post(client, path, body, headers):
        if raises is not None:
            raise raises
        return answer, None

    with patch.object(executors, "_post_with_capacity_redispatch", fake_post), \
         patch("src.auth.auth_manager.get_api_key", return_value="k"), \
         patch.multiple(
             store_client,
             mark_running=AsyncMock(), heartbeat=AsyncMock(),
             update_progress=AsyncMock(), mark_done=seam.mark_done,
             mark_error=seam.mark_error, defer_job=seam.defer_job,
             get_job=AsyncMock(return_value={"defer_count": seam.defer_count}),
         ):
        registry.register_executor(kind, executor or {
            "chat": executors.chat_executor,
            "research": executors.research_executor,
        }[kind])
        await registry.run_generic_job(f"job_dev_{HEX32}", kind, {"query": "q"}, None)


# --- job errors carry the verdict ---------------------------------------------

async def test_research_transient_error_reaches_the_job_structured():
    """research-cloud cap / upstream 5xx: transient in the endpoint's answer,
    so transient in the job error — as a field, not only as text."""
    seam = _Seam()
    answer = httpx.Response(200, json={
        "status": "error", "query": "q", "model": "m",
        "error": 'cap (bridge: HTTP 503, "retryable": true)',
        "retryable": True, "retry_after_s": 60,
    })
    await _run("research", seam, answer=answer)
    assert seam.verdict() == ("EXECUTOR_ERROR", True, 60)


async def test_research_final_error_is_final():
    seam = _Seam()
    answer = httpx.Response(200, json={
        "status": "error", "query": "q", "model": "m",
        "error": "research provider pin unservable", "retryable": False,
    })
    await _run("research", seam, answer=answer)
    assert seam.verdict() == ("EXECUTOR_ERROR", False, None)


async def test_chat_pin_error_of_the_base_class_is_final_in_the_job():
    """503 stays the status (existing callers read UPSTREAM_HTTP_503), the
    envelope's retryable:false becomes the job's verdict."""
    seam = _Seam()
    answer = httpx.Response(503, json={"error": {
        "message": "provider 'x' not supported", "code": "503",
        "retryable": False, "retry_after_s": None,
    }})
    await _run("chat", seam, answer=answer)
    assert seam.verdict() == ("UPSTREAM_HTTP_503", False, None)


async def test_chat_transient_503_keeps_its_wait():
    seam = _Seam()
    answer = httpx.Response(503, json={"error": {
        "message": "busy", "code": "503", "retryable": True, "retry_after_s": 10,
    }})
    await _run("chat", seam, answer=answer)
    assert seam.verdict() == ("UPSTREAM_HTTP_503", True, 10)


async def test_upstream_status_without_envelope_falls_back_to_the_status():
    seam = _Seam()
    await _run("chat", seam, answer=httpx.Response(502, text="bad gateway"))
    assert seam.verdict()[:2] == ("UPSTREAM_HTTP_502", True)
    seam = _Seam()
    await _run("chat", seam, answer=httpx.Response(400, text="nope"))
    assert seam.verdict()[:2] == ("UPSTREAM_HTTP_400", False)


async def test_dependency_patience_spent_is_a_named_final_abort():
    """UPSTREAM_HTTP_424 comes only after the runner waited ~15 min itself."""
    seam = _Seam(defer_count=registry.DEPENDENCY_PATIENCE["provider-config"][1])
    answer = httpx.Response(
        503, json={"error": {"message": "gap", "retryable": True}},
        headers={"X-Bridge-Dependency-Unavailable": "provider-config"},
    )
    await _run("chat", seam, answer=answer)
    seam.defer_job.assert_not_awaited()
    assert seam.verdict()[:2] == ("UPSTREAM_HTTP_424", False)


async def test_lost_connection_to_the_self_call_is_transient():
    seam = _Seam()
    await _run("chat", seam, raises=httpx.ConnectError("refused"))
    assert seam.verdict()[:2] == ("EXECUTOR_ERROR", True)


async def test_self_call_read_timeout_is_not_promised_as_transient():
    seam = _Seam()
    await _run("research", seam, raises=httpx.ReadTimeout("slow"))
    assert seam.verdict()[:2] == ("EXECUTOR_ERROR", False)


async def test_unclassified_crash_is_not_promised_as_transient():
    async def boom(payload, attribution, report_progress):
        raise RuntimeError("something nobody classified")

    seam = _Seam()
    await _run("br9-boom", seam, executor=boom)
    assert seam.verdict() == ("EXECUTOR_ERROR", False, None)


async def test_no_executor_and_requeue_exhausted_carry_a_verdict():
    seam = _Seam()
    with patch.object(registry, "get_executor", return_value=None), \
         patch.object(store_client, "mark_error", seam.mark_error):
        await registry._run_body("j", "nope", {}, None)
    assert seam.verdict()[:2] == ("NO_EXECUTOR", False)

    seam = _Seam()
    with patch.object(registry, "run_claim_pass", AsyncMock(return_value=0)), \
         patch.object(store_client, "find_abandoned",
                      AsyncMock(return_value=[{"job_id": "j", "attempts": 3}])), \
         patch.object(store_client, "mark_error", seam.mark_error):
        await registry.run_watchdog_pass(90, 3)
    # The bridge already retried it max_attempts times; a poison job must not be
    # resubmitted by the client (BR9b).
    assert seam.verdict()[:2] == ("REQUEUE_EXHAUSTED", False)


# --- the verdict is stored, and the platform-api accepts it ---------------------

async def test_store_client_sends_the_verdict_to_the_platform_api():
    sent = {}

    async def fake_call(method, path, json=None, timeout_s=None):
        sent.update(json)
        return type("R", (), {"status_code": 204, "json": None})()

    with patch.object(store_client, "call_platform", fake_call):
        await store_client.mark_error("j", "m", code="UPSTREAM_HTTP_503",
                                      retryable=False, retry_after_s=None)
    assert sent == {"message": "m", "code": "UPSTREAM_HTTP_503",
                    "retryable": False, "retry_after_s": None}


async def test_store_writes_the_verdict_into_the_error_row():
    from src.jobs import store

    conn = AsyncMock()
    # asyncpg's command tag; since BR11 mark_error reads the row count from it.
    conn.execute.return_value = "UPDATE 1"

    class _Pool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    return conn

                async def __aexit__(self_inner, *a):
                    return False
            return _Ctx()

    with patch.object(store, "get_pool", return_value=_Pool()):
        await store.mark_error("j", "m", code="EXECUTOR_ERROR",
                               retryable=True, retry_after_s=60)
    row = json.loads(conn.execute.await_args.args[2])
    assert row == {"message": "m", "code": "EXECUTOR_ERROR",
                   "retryable": True, "retry_after_s": 60}


def test_internal_error_body_accepts_the_verdict():
    from src.internal_routes import InternalJobError

    body = InternalJobError(message="m", code="X", retryable=True, retry_after_s=5)
    assert (body.retryable, body.retry_after_s) == (True, 5)
    old_worker = InternalJobError(message="m", code="X")
    assert old_worker.retryable is None


# --- GET /v1/jobs/{id} --------------------------------------------------------

async def _poll(job: dict, monkeypatch) -> dict:
    from starlette.requests import Request

    from src.jobs import routes

    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "dev")
    monkeypatch.setattr(routes, "verify_api_key", AsyncMock())
    monkeypatch.setattr(routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(routes.store_client, "get_job", AsyncMock(return_value=job))
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                       "query_string": b""})
    return await routes.get_job_endpoint(f"job_dev_{HEX32}", request, None)


def _job(**over) -> dict:
    now = datetime.now(timezone.utc)
    return {"kind": "chat", "status": "pending", "created_at": now - timedelta(minutes=3),
            "updated_at": now, "progress": None, "result": None, "error": None,
            "deferred_until": None, "defer_count": 0, "defer_reason": None, **over}


async def test_poll_shows_a_parked_job_as_waiting_on_purpose(monkeypatch):
    until = datetime.now(timezone.utc) + timedelta(seconds=30)
    out = await _poll(_job(deferred_until=until, defer_count=2,
                           defer_reason="dependency 'provider-config' temporarily unavailable"),
                      monkeypatch)
    assert out["status"] == "pending", "existing pollers keep working"
    assert out["deferred_until"] == until.isoformat()
    assert out["defer_count"] == 2
    assert "provider-config" in out["defer_reason"]


async def test_poll_of_an_unparked_pending_job_has_no_wait(monkeypatch):
    out = await _poll(_job(), monkeypatch)
    assert out["deferred_until"] is None and out["defer_reason"] is None


async def test_poll_error_always_carries_the_verdict(monkeypatch):
    stored = {"message": "m", "code": "EXECUTOR_ERROR", "retryable": True,
              "retry_after_s": 60}
    out = await _poll(_job(status="error", error=stored), monkeypatch)
    assert out["error"]["retryable"] is True and out["error"]["retry_after_s"] == 60

    # A row written before BR9 (or by a platform-api that drops the fields):
    # the verdict comes from the code, the same rules.
    for code, want in (("UPSTREAM_HTTP_503", True), ("UPSTREAM_HTTP_400", False),
                       ("UPSTREAM_HTTP_424", False), ("REQUEUE_EXHAUSTED", False),
                       ("EXECUTOR_ERROR", False)):
        out = await _poll(_job(status="error", error={"message": "m", "code": code}),
                          monkeypatch)
        assert out["error"]["retryable"] is want, code
        assert "retry_after_s" in out["error"]
        assert out["error"]["code"] == code


# --- direct calls: chat envelope, research, doc-agent ---------------------------

async def test_chat_pin_error_of_the_base_class_goes_out_final(monkeypatch):
    """main.chat_completions raises 503 with retryable:false for the base class;
    the envelope handler must keep that verdict instead of the 503 default."""
    from fastapi import HTTPException
    from starlette.requests import Request

    main = _main()
    request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions",
                       "headers": [], "query_string": b""})
    final = HTTPException(503, detail={"error": {
        "message": "provider 'x' not supported", "code": "user_provider_override_unavailable",
        "retryable": False}})
    body = json.loads((await main.http_exception_handler(request, final)).body)["error"]
    assert body["retryable"] is False
    assert body["code"] == "503", "the status field callers read is unchanged"

    transient = HTTPException(503, detail={"error": {"message": "gap", "retryable": True}})
    body = json.loads((await main.http_exception_handler(request, transient)).body)["error"]
    assert body["retryable"] is True

    plain = HTTPException(503, detail={"error": {"message": "no verdict"}})
    body = json.loads((await main.http_exception_handler(request, plain)).body)["error"]
    assert body["retryable"] is True, "no verdict → the status rule, as before"


async def test_chat_route_marks_the_base_class_pin_error_final(monkeypatch):
    """The raise site itself (main.py chat path) — read from the source so the
    test does not need the whole chat stack."""
    import inspect

    main = _main()
    src = inspect.getsource(main)
    block = src.split("except UserProviderOverrideError as e:\n            # Final", 1)
    assert len(block) == 2, "the chat base-class branch names its verdict"
    assert '"retryable": False' in block[1].split("raise HTTPException", 2)[1][:600]


async def test_research_transient_errors_carry_the_field_and_the_text(monkeypatch):
    from starlette.requests import Request

    from src.models import ResearchRequest
    from src.routing import user_provider_override as upo

    main = _main()
    monkeypatch.setattr(main, "verify_api_key", AsyncMock())
    monkeypatch.setattr(main, "enforce_attribution", lambda request: None)
    monkeypatch.setattr(upo, "enforce_user_provider_override",
                        AsyncMock(side_effect=upo.ProviderConfigTemporarilyUnavailable("gap")))
    request = Request({"type": "http", "method": "POST", "path": "/v1/research",
                       "headers": [], "query_string": b""})
    response = await main.research(ResearchRequest(query="q", model="sonnet"), request,
                                   None, None)
    body = json.loads(response.body)
    assert body["retryable"] is True
    assert '"retryable": true' in body["error"], "text marker kept for old classifiers"

    monkeypatch.setattr(upo, "enforce_user_provider_override",
                        AsyncMock(side_effect=upo.UserProviderOverrideError("no")))
    response = await main.research(ResearchRequest(query="q", model="sonnet"), request,
                                   None, None)
    assert response.retryable is False


def test_research_and_doc_agent_errors_never_lack_a_verdict():
    from src.models import DocAgentResponse, ResearchResponse

    assert ResearchResponse(status="error", query="q", model="m", error="x").retryable is False
    assert DocAgentResponse(status="error", question="q", model="m", error="x").retryable is False
    ok = ResearchResponse(status="success", query="q", model="m")
    assert ok.retryable is None and ok.retry_after_s is None


def test_status_rule_of_the_envelope_is_unchanged():
    from src.middleware.bridge_error import bridge_error

    for status, want in ((429, True), (500, True), (503, True), (400, False),
                         (401, False), (424, False)):
        body = json.loads(bridge_error(source="bridge_internal", error_type="internal",
                                       message="m", status_code=status).body)
        assert body["error"]["retryable"] is want, status
