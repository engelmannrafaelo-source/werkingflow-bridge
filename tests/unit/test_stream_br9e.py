"""BR9e (BR9dR): BR9d Teil B nachgebessert.

MUSS — Befund (BR9dR): BR9d pulled the first ``__anext__`` in a helper task.
A generator that enters ``asyncio.timeout`` before its first yield (the CLI
path: MAX_TIMEOUT around run_completion) binds that timeout to the task that
runs the step — the helper, already finished once the first chunk was there.
The timeout then cancelled a dead task and never fired: a CLI stream with
steady output was no longer bounded. Now ``__anext__`` stays in the route
task and a watcher beside it cancels the route when the caller is gone.

SOLL 2 — the vision stream and account_org_disabled wrote their own
``data:`` error lines (no ``event: error``), even before the first chunk.
Now both raise: before the first chunk the route answers with the HTTP
status of the non-streaming path, after it stream_start ends the stream as
the one ``event: error`` form.

SOLL 3 — a Bedrock stream cut off from outside (caller gone before or after
the first chunk) left no ledger row, although AWS bills input and the
tokens generated up to the cut. Now it is booked in ``finally``, once.

SOLL 4 — the Bedrock single service had no delivery probe; event_stream_response
counted its caller as always present.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch
from unittest.mock import MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402

import src.main as main  # noqa: E402
from src import bedrock_service, stream_start  # noqa: E402
from src.activity import delivery  # noqa: E402
from src.claude_cli import OrgSubscriptionDisabledError  # noqa: E402
from src.middleware.bridge_error import BridgeError, classify_exception  # noqa: E402
from src.models import BackendType, ChatCompletionRequest, Message  # noqa: E402
from src.routing.backend_router import BackendConfig  # noqa: E402

_GUARD_S = 3.0


@pytest.fixture(autouse=True)
def _fast_probe(monkeypatch):
    monkeypatch.setattr(stream_start, "_DISCONNECT_POLL_S", 0.01, raising=False)


class _Provider:
    """A provider call that has not produced its next token yet."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.aborted = False

    async def call(self):
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.aborted = True
            raise


# --- MUSS: a timeout inside the generator still fires after the first chunk ----

def _timed_generator(log: list, limit_s: float, run_s: float):
    async def gen():
        t0 = time.monotonic()
        try:
            async with asyncio.timeout(limit_s):  # like claude_cli run_completion
                yield "data: first\n\n"
                while time.monotonic() - t0 < run_s:
                    await asyncio.sleep(0.02)
                    yield "data: more\n\n"
        except TimeoutError:
            log.append("timeout")
            yield 'event: error\ndata: {"error": {"message": "timed out"}}\n\n'
            return
        log.append("ran to end")
    return gen()


async def test_generator_timeout_fires_after_the_first_chunk_in_the_route_task():
    log: list = []

    async def route_and_stream():
        resp = await stream_start.event_stream_response(_timed_generator(log, 0.2, 1.5))
        return [c async for c in resp.body_iterator]

    task = asyncio.ensure_future(route_and_stream())
    try:
        await asyncio.wait_for(asyncio.shield(task), _GUARD_S)
    except (asyncio.CancelledError, TimeoutError):
        pass
    task.cancel()
    assert log == ["timeout"], f"CLI-style timeout must fire after the first chunk: {log}"


async def test_generator_timeout_fires_over_the_real_asgi_stack():
    """Measured shape of BR9dR (exp_timeout.py): FastAPI + DeliveryProbeMiddleware."""
    log: list = []
    app = FastAPI()
    app.add_middleware(delivery.DeliveryProbeMiddleware)

    @app.post("/s")
    async def s():
        return await stream_start.event_stream_response(_timed_generator(log, 0.2, 1.5))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as client:
        try:
            await asyncio.wait_for(client.post("/s"), _GUARD_S)
        except (asyncio.CancelledError, TimeoutError, httpx.HTTPError):
            pass
    assert log == ["timeout"], f"timeout did not fire: {log}"


async def test_caller_gone_leaves_no_pending_cancel_on_the_route_task():
    """The watcher's cancel is taken back: the route task goes on with a clean
    cancel count (anything else would turn the next await into a cancel)."""
    provider = _Provider()

    async def gen():
        await provider.call()
        yield "x"

    gone = asyncio.Event()

    async def receive():
        await gone.wait()
        return {"type": "http.disconnect"}

    async def route():
        delivery._probe.set(delivery.DeliveryProbe(receive))
        resp = await stream_start.event_stream_response(gen())
        await asyncio.sleep(0.02)  # would raise if a cancel were still pending
        return resp.status_code, asyncio.current_task().cancelling()

    task = asyncio.ensure_future(route())
    await asyncio.wait_for(provider.entered.wait(), _GUARD_S)
    gone.set()
    status, cancelling = await asyncio.wait_for(task, _GUARD_S)
    assert (status, cancelling) == (499, 0)
    assert provider.aborted


# --- shared: the real /v1/chat/completions over raw ASGI -----------------------

def _config(backend: BackendType) -> BackendConfig:
    return BackendConfig(
        backend=backend, region="eu-central-1", model_id="claude-sonnet-4-5",
        bedrock_model_id="eu.anthropic.claude-sonnet-4-5", privacy_enabled=False,
        env_vars={}, provider_tier="test-tier", provider_base_url="http://provider.test",
        provider_api_key="k", provider_model="m",
    )


async def _run_route(monkeypatch, backend, *, disconnect_when=None, extra=()):
    """Drive main.app like uvicorn (spec 2.3): once the caller is gone, every
    receive() says http.disconnect. Returns the sent ASGI messages."""
    from src.middleware.adaptive_limiter import adaptive_limit_dependency

    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("CLAUDE_SKIP_AUTH", "1")

    async def _kein_limiter():
        return None

    body = json.dumps({"model": "claude-sonnet-4-5", "stream": True,
                       "messages": [{"role": "user", "content": "ping"}]}).encode()
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list = []
    first_body = asyncio.Event()

    async def receive():
        if messages:
            return messages.pop(0)
        if disconnect_when is None:
            await asyncio.Event().wait()
        await disconnect_when(first_body)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            first_body.set()

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
        "query_string": b"", "root_path": "",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
        "client": ("127.0.0.1", 1), "server": ("testserver", 80),
    }
    main.app.dependency_overrides[adaptive_limit_dependency] = _kein_limiter
    try:
        with ExitStack() as stack:
            for p in (
                patch("src.main.validate_claude_code_auth", return_value=(True, {"method": "test"})),
                patch("src.main.verify_api_key", new_callable=AsyncMock),
                patch("src.main.enforce_pool_admission", new_callable=AsyncMock),
                patch("src.main._cross_worker_retry", new=AsyncMock(return_value=None)),
                patch("src.main.resolve_backend_config", return_value=_config(backend)),
                patch("src.providers.fallback.get_fallback_tiers", side_effect=lambda t, **kw: [t]),
                *extra,
            ):
                stack.enter_context(p)
            await asyncio.wait_for(main.app(scope, receive, send), _GUARD_S)
            # Bookings shielded from the cancel may still be finishing.
            pending = getattr(main, "_BACKGROUND_BOOKINGS", ())
            if pending:
                await asyncio.wait_for(asyncio.gather(*pending), _GUARD_S)
    finally:
        main.app.dependency_overrides.clear()
    return sent


def _status(sent) -> int:
    return [m["status"] for m in sent if m["type"] == "http.response.start"][0]


def _json_body(sent) -> dict:
    return json.loads(b"".join(m.get("body", b"") for m in sent
                               if m["type"] == "http.response.body"))


def _text(sent) -> str:
    return b"".join(m.get("body", b"") for m in sent
                    if m["type"] == "http.response.body").decode()


# --- SOLL 2: vision and account_org_disabled before the first chunk -----------

def _vision(raise_exc):
    async def failing(**kw):
        raise raise_exc
    return (
        patch("src.main.has_vision_content", return_value=True),
        patch("src.main.resolve_vision_target", return_value="anthropic"),
        patch("src.main.prepaid_vision_over_cap", new=AsyncMock(return_value=(False, 0.0, 0.0))),
        patch("src.main.check_and_route_vision", new=failing),
    )


@pytest.mark.parametrize("exc", [
    pytest.param(httpx.ConnectTimeout("vision upstream"), id="transport"),
    pytest.param(RuntimeError("vision boom"), id="unclassified"),
])
async def test_vision_failure_before_first_chunk_is_the_sync_http_error(monkeypatch, exc):
    sent = await _run_route(monkeypatch, BackendType.ANTHROPIC, extra=_vision(exc))
    want = classify_exception(exc)
    assert _status(sent) == want.status_code, _text(sent)[:300]
    err = _json_body(sent)["error"]
    assert err == json.loads(want.body)["error"]
    assert isinstance(err["retryable"], bool)


async def test_org_disabled_before_first_chunk_is_its_503(monkeypatch):
    def blocked(*a, **kw):
        raise OrgSubscriptionDisabledError("worker-x", "assistant_text", 3600)
    monkeypatch.setattr(main.session_manager, "process_messages", blocked)
    sent = await _run_route(monkeypatch, BackendType.ANTHROPIC)

    assert _status(sent) == 503, _text(sent)[:300]
    err = _json_body(sent)["error"]
    assert err["code"] == "account_org_disabled"
    assert err["retryable"] is True and err["retry_after_s"] == 3600


async def test_org_disabled_after_first_chunk_keeps_its_code_and_lock():
    async def gen():
        yield "data: {\"choices\": []}\n\n"
        raise OrgSubscriptionDisabledError("worker-x", "assistant_text", 1800)

    resp = await stream_start.event_stream_response(gen())
    chunks = [c async for c in resp.body_iterator]
    assert chunks[-1].startswith("event: error\n")
    err = json.loads(chunks[-1].split("data: ", 1)[1])["error"]
    assert err["code"] == "account_org_disabled"
    assert err["reason"] == "account_org_disabled"
    assert err["retryable"] is True and err["retry_after_s"] == 1800
    assert err["bridge_worker"] == "worker-x"
    assert "[DONE]" not in "".join(chunks)


async def test_vision_failure_after_first_chunk_keeps_its_envelope():
    """The vision branch raises BridgeError(classify_exception(e)); after the
    first chunk its message/code/reason reach the event, not "BridgeError"."""
    want = json.loads(classify_exception(RuntimeError("vision boom")).body)["error"]

    async def gen():
        yield "data: {\"choices\": []}\n\n"
        raise BridgeError(classify_exception(RuntimeError("vision boom")))

    resp = await stream_start.event_stream_response(gen())
    chunks = [c async for c in resp.body_iterator]
    assert chunks[-1].startswith("event: error\n")
    err = json.loads(chunks[-1].split("data: ", 1)[1])["error"]
    for key in ("message", "type", "code", "reason", "retryable", "retry_after_s"):
        assert err[key] == want[key], key


# --- SOLL 3: a cut-off Bedrock stream is booked, once --------------------------

_BEDROCK_GATES = (
    patch("src.routing.user_provider_override.assert_bedrock_is_pinned"),
    patch("src.routing.user_provider_override.assert_bedrock_attribution_complete"),
)


def _bedrock(stream):
    persist = AsyncMock()
    return persist, (
        *_BEDROCK_GATES,
        patch("src.bedrock_service.stream_bedrock", new=stream),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", new=persist),
    )


def _sink_start(usage_sink):
    usage_sink.setdefault("status", "error")
    usage_sink.update(bedrock_model_id="eu.anthropic.claude-sonnet-4-5",
                      region="eu-central-1", aws_request_id="req-1", input_tokens=1200)


async def test_bedrock_cut_off_in_the_thinking_phase_is_booked(monkeypatch):
    provider = _Provider()

    async def thinking(request, region, usage_sink):
        _sink_start(usage_sink)
        await provider.call()
        yield "data: never\n\n"

    async def when(first_body):
        await provider.entered.wait()

    persist, extra = _bedrock(thinking)
    sent = await _run_route(monkeypatch, BackendType.BEDROCK, disconnect_when=when, extra=extra)

    assert provider.aborted
    assert not any(b"data:" in m.get("body", b"") for m in sent)
    assert persist.await_count == 1, persist.await_args_list
    kw = persist.await_args.kwargs
    assert kw["provider"] == "bedrock"
    assert kw["input_tokens"] == 1200
    assert (kw["status"], kw["error_code"]) == (
        delivery.STATUS_UNDELIVERED, delivery.ERROR_CODE_CALLER_GONE), kw
    assert kw["provider_meta"]["aws_request_id"] == "req-1"


async def test_bedrock_cut_off_after_the_first_chunk_is_booked(monkeypatch):
    provider = _Provider()

    async def half(request, region, usage_sink):
        _sink_start(usage_sink)
        usage_sink["output_tokens"] = 40
        yield "data: {\"choices\": [{\"delta\": {\"content\": \"Hal\"}}]}\n\n"
        await provider.call()
        yield "data: never\n\n"

    async def when(first_body):
        await first_body.wait()
        await provider.entered.wait()

    persist, extra = _bedrock(half)
    await _run_route(monkeypatch, BackendType.BEDROCK, disconnect_when=when, extra=extra)

    assert provider.aborted
    assert persist.await_count == 1, persist.await_args_list
    kw = persist.await_args.kwargs
    assert (kw["input_tokens"], kw["output_tokens"]) == (1200, 40)
    assert kw["status"] == delivery.STATUS_UNDELIVERED
    assert kw["error_code"] == delivery.ERROR_CODE_CALLER_GONE


async def test_bedrock_drained_stream_is_booked_once_as_before(monkeypatch):
    """Wächter: the normal end books exactly one row, as before BR9e."""
    async def good(request, region, usage_sink):
        _sink_start(usage_sink)
        usage_sink["output_tokens"] = 9
        yield "data: {\"choices\": [{\"delta\": {\"content\": \"Hallo\"}}]}\n\n"
        usage_sink["status"] = "success"
        yield "data: [DONE]\n\n"

    persist, extra = _bedrock(good)
    sent = await _run_route(monkeypatch, BackendType.BEDROCK, extra=extra)

    assert _status(sent) == 200
    assert persist.await_count == 1
    kw = persist.await_args.kwargs
    assert (kw["status"], kw["error_code"], kw["output_tokens"]) == ("success", None, 9)


async def test_bedrock_stream_ended_by_bedrock_is_booked_as_error_as_before(monkeypatch):
    """Wächter: Bedrock's own abort keeps status=error / stream_aborted."""
    async def aborted(request, region, usage_sink):
        _sink_start(usage_sink)
        yield "data: {\"choices\": [{\"delta\": {\"content\": \"Ha\"}}]}\n\n"
        usage_sink["error_message"] = "cut"
        yield bedrock_service._stream_error_event(
            bedrock_service.BedrockStreamAborted("cut", retryable=True))

    persist, extra = _bedrock(aborted)
    await _run_route(monkeypatch, BackendType.BEDROCK, extra=extra)

    assert persist.await_count == 1
    kw = persist.await_args.kwargs
    assert (kw["status"], kw["error_code"], kw["error_message"]) == ("error", "stream_aborted", "cut")


# --- SOLL 4: the Bedrock single service has the probe --------------------------

async def test_bedrock_single_service_caller_gone_aborts_the_call(monkeypatch):
    provider = _Provider()

    async def thinking(request, usage_sink=None):
        await provider.call()
        yield "data: never\n\n"

    monkeypatch.setattr(bedrock_service, "stream_bedrock", thinking)
    body = json.dumps({"model": "claude-sonnet-4-5", "stream": True,
                       "messages": [{"role": "user", "content": "ping"}]}).encode()
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        if messages:
            return messages.pop(0)
        await provider.entered.wait()
        return {"type": "http.disconnect"}

    sent: list = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions",
        "query_string": b"", "root_path": "",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
        "client": ("127.0.0.1", 1), "server": ("testserver", 80),
    }
    await asyncio.wait_for(bedrock_service.app(scope, receive, send), _GUARD_S)

    assert provider.aborted, "without the probe the Bedrock call runs on for nobody"
    assert not any(b"data:" in m.get("body", b"") for m in sent)


# --- the real generator no longer writes its own error line --------------------

def _request():
    return ChatCompletionRequest(model="claude-sonnet-4-5",
                                 messages=[Message(role="user", content="hi")], stream=True)


async def test_vision_generator_raises_instead_of_a_data_line(monkeypatch):
    for p in _vision(RuntimeError("vision boom")):
        p.start()
    try:
        with pytest.raises(BridgeError):
            await main.generate_streaming_response(_request(), "req-1").__anext__()
    finally:
        patch.stopall()
