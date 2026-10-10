"""BR9d (BR9R2 SOLL 1 + 2): the stream before its first chunk, and one error form.

SOLL 1 — Befund (BR9R2): since BR9c the route pulls the first chunk itself
(src/stream_start.py). Before that Starlette's listen_for_disconnect cancelled
the generator the moment the caller left; now nobody listened until the first
chunk existed. An OpenAI-compatible or Bedrock call — with ``thinking`` the
whole paid thinking phase — ran on for a caller who was gone. Now the wait for
the first chunk asks the request's shared delivery probe; on disconnect the
pending call is cancelled and the generator closed.

SOLL 2 — Befund (BR9R2): Bedrock's stream errors were flat,
``{"error": "<text>", "retryable": …}``. PC1 (ai-bridge-client core/sse.ts,
bridgeStreamError) reads retryable only nested, so a Bedrock transport error
arrived there as NOT retryable. Now every stream error producer sends the one
form of stream_error_event: ``{"error": {message, type, code, retryable,
retry_after_s}}``.
"""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import ExitStack
from types import SimpleNamespace
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
from botocore.exceptions import (  # noqa: E402
    ClientError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from fastapi import HTTPException  # noqa: E402

import src.main as main  # noqa: E402
from src import bedrock_service, stream_start  # noqa: E402
from src.activity import delivery  # noqa: E402
from src.models import BackendType, ChatCompletionRequest, Message  # noqa: E402
from src.providers import openai_compatible  # noqa: E402
from src.routing.backend_router import BackendConfig  # noqa: E402

# Old code waits for the first chunk forever; red means "timed out here".
_GUARD_S = 3.0


@pytest.fixture(autouse=True)
def _fast_probe(monkeypatch):
    monkeypatch.setattr(stream_start, "_DISCONNECT_POLL_S", 0.01, raising=False)


class _Provider:
    """Stand-in for a provider call that has not produced its first token yet
    (Bedrock thinking phase, slow OpenAI-compatible upstream)."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.aborted = False
        self.release = asyncio.Event()

    async def call(self):
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.aborted = True
            raise


def _probe(gone: asyncio.Event):
    async def receive():
        await gone.wait()
        return {"type": "http.disconnect"}
    probe = delivery.DeliveryProbe(receive)
    delivery._probe.set(probe)
    return probe


# --- SOLL 1: disconnect before the first chunk ---------------------------------

async def test_caller_gone_before_first_chunk_closes_generator_and_aborts_provider():
    provider = _Provider()
    closed = []

    async def gen():
        try:
            await provider.call()
            yield "data: never\n\n"
        finally:
            closed.append(True)

    gone = asyncio.Event()
    _probe(gone)
    route = asyncio.ensure_future(stream_start.event_stream_response(gen()))
    await asyncio.wait_for(provider.entered.wait(), _GUARD_S)
    gone.set()

    resp = await asyncio.wait_for(route, _GUARD_S)

    assert provider.aborted, "provider call must be cancelled, not left running"
    assert closed == [True], "generator must be closed"
    assert resp.status_code == 499
    assert not hasattr(resp, "body_iterator"), "no stream is started for nobody"


async def test_caller_present_slow_first_chunk_streams_normally():
    """Wächter: a first chunk slower than many probe rounds is not a disconnect."""
    async def gen():
        await asyncio.sleep(0.1)  # 10 probe rounds
        yield "data: a\n\n"
        yield "data: [DONE]\n\n"

    _probe(asyncio.Event())  # never gone
    resp = await asyncio.wait_for(stream_start.event_stream_response(gen()), _GUARD_S)
    assert resp.status_code == 200
    assert [c async for c in resp.body_iterator] == ["data: a\n\n", "data: [DONE]\n\n"]


async def test_failure_before_first_chunk_still_leaves_the_route():
    """Wächter: the probe does not swallow the BR9c start errors."""
    async def gen():
        await asyncio.sleep(0.05)
        raise openai_compatible.ProviderError(429, "slow down")
        yield  # pragma: no cover

    _probe(asyncio.Event())
    with pytest.raises(Exception) as exc:
        await asyncio.wait_for(stream_start.event_stream_response(gen()), _GUARD_S)
    assert json.loads(exc.value.response.body)["error"]["retryable"] is True


async def test_route_cancelled_while_waiting_takes_the_provider_call_with_it():
    provider = _Provider()

    async def gen():
        await provider.call()
        yield "x"

    _probe(asyncio.Event())
    route = asyncio.ensure_future(stream_start.event_stream_response(gen()))
    await asyncio.wait_for(provider.entered.wait(), _GUARD_S)
    route.cancel()
    with pytest.raises(asyncio.CancelledError):
        await route
    await asyncio.sleep(0)
    assert provider.aborted


def _config() -> BackendConfig:
    return BackendConfig(
        backend=BackendType.OPENAI_COMPATIBLE, region="eu-central-1",
        model_id="claude-sonnet-4-5", bedrock_model_id=None, privacy_enabled=False,
        env_vars={}, provider_tier="test-tier", provider_base_url="http://provider.test",
        provider_api_key="k", provider_model="m",
    )


async def test_real_route_openai_compatible_caller_gone_aborts_upstream(monkeypatch):
    """The REAL /v1/chat/completions route over raw ASGI: the caller sends its
    body, the upstream is still silent, the caller hangs up. The upstream
    request must be cancelled and no response started."""
    from src.middleware.adaptive_limiter import adaptive_limit_dependency

    upstream = _Provider()

    async def handler(request):
        await upstream.call()
        return httpx.Response(200, content=b"data: [DONE]\n")

    real = httpx.AsyncClient
    monkeypatch.setattr(openai_compatible.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("CLAUDE_SKIP_AUTH", "1")

    async def _kein_limiter():
        return None

    body = json.dumps({"model": "claude-sonnet-4-5", "stream": True,
                       "messages": [{"role": "user", "content": "ping"}]}).encode()
    messages = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        if messages:
            return messages.pop(0)
        await upstream.entered.wait()
        return {"type": "http.disconnect"}

    sent = []

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

    main.app.dependency_overrides[adaptive_limit_dependency] = _kein_limiter
    try:
        with ExitStack() as stack:
            for p in (
                patch("src.main.validate_claude_code_auth", return_value=(True, {"method": "test"})),
                patch("src.main.verify_api_key", new_callable=AsyncMock),
                patch("src.main.enforce_pool_admission", new_callable=AsyncMock),
                patch("src.main._cross_worker_retry", new=AsyncMock(return_value=None)),
                patch("src.main.resolve_backend_config", return_value=_config()),
                patch("src.providers.fallback.get_fallback_tiers", side_effect=lambda t, **kw: [t]),
            ):
                stack.enter_context(p)
            await asyncio.wait_for(main.app(scope, receive, send), _GUARD_S)
    finally:
        main.app.dependency_overrides.clear()

    assert upstream.aborted, "upstream request must be cancelled when the caller is gone"
    starts = [m for m in sent if m["type"] == "http.response.start"]
    assert [m["status"] for m in starts] in ([], [499]), starts
    assert not any(b"data:" in m.get("body", b"") for m in sent)


# --- SOLL 2: one wire form for every stream error ------------------------------

def _assert_the_one_form(sse: str) -> dict:
    """The contract PC1 reads (core/sse.ts bridgeStreamError)."""
    assert sse.startswith("event: error\ndata: "), sse
    assert sse.endswith("\n\n")
    payload = json.loads(sse.split("data: ", 1)[1])
    assert set(payload) == {"error"}, payload
    err = payload["error"]
    assert isinstance(err, dict), f"flat error, PC1 reads no verdict: {payload}"
    assert isinstance(err["message"], str) and err["message"]
    assert isinstance(err["type"], str) and err["type"]
    assert isinstance(err["code"], str) and err["code"]
    assert isinstance(err["retryable"], bool)
    assert "retry_after_s" in err
    return err


def _req(model="claude-sonnet-5"):
    return ChatCompletionRequest(model=model, messages=[Message(role="user", content="hi")],
                                 stream=True)


def _bedrock_raising(monkeypatch, exc):
    def invoke(**kw):
        raise exc
    boto = SimpleNamespace(invoke_model_with_response_stream=invoke)
    monkeypatch.setattr(bedrock_service, "get_bedrock_client", lambda: SimpleNamespace(
        default_region="eu-central-1", get_client=lambda region: boto))


async def _bedrock_last(monkeypatch, exc) -> str:
    _bedrock_raising(monkeypatch, exc)
    return [c async for c in bedrock_service.stream_bedrock(_req(), usage_sink={})][-1]


@pytest.mark.parametrize("exc", [
    pytest.param(EndpointConnectionError(endpoint_url="https://bedrock"), id="endpoint"),
    pytest.param(ReadTimeoutError(endpoint_url="https://bedrock"), id="read-timeout"),
])
async def test_bedrock_transport_error_arrives_nested_and_retryable(monkeypatch, exc):
    err = _assert_the_one_form(await _bedrock_last(monkeypatch, exc))
    assert err["retryable"] is True


def _producers():
    """Every function that writes a stream error event."""
    def bedrock_client_error(mp):
        return _bedrock_last(mp, ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "m"}},
            "InvokeModelWithResponseStream"))

    def bedrock_unclassified(mp):
        return _bedrock_last(mp, KeyError("content"))

    async def bedrock_aborted(mp):
        boto = SimpleNamespace(invoke_model_with_response_stream=lambda **kw: {
            "body": [], "ResponseMetadata": {"RequestId": "r"}})
        mp.setattr(bedrock_service, "get_bedrock_client", lambda: SimpleNamespace(
            default_region="eu-central-1", get_client=lambda region: boto))
        return [c async for c in bedrock_service.stream_bedrock(_req(), usage_sink={})][-1]

    async def bedrock_no_credentials(mp):
        def none():
            raise RuntimeError("no Bedrock credentials on this worker")
        mp.setattr(bedrock_service, "get_bedrock_client", none)
        return [c async for c in bedrock_service.stream_bedrock(_req(), usage_sink={})][-1]

    async def bedrock_unknown_model(mp):
        mp.setattr(bedrock_service, "get_bedrock_client", lambda: SimpleNamespace(
            default_region="eu-central-1", get_client=lambda region: None))
        mp.setattr(bedrock_service, "resolve_model", lambda m: (None, None))
        return [c async for c in bedrock_service.stream_bedrock(_req(), usage_sink={})][-1]

    async def after_first_chunk(mp):
        return stream_start.stream_error_event(HTTPException(status_code=503, detail="later"))

    async def openai_compatible_truncated(mp):
        return openai_compatible._incomplete_stream_event("http://provider.test", "no [DONE]")

    async def _after_first_chunk(exc):
        async def gen():
            yield "data: x\n\n"
            raise exc
        resp = await stream_start.event_stream_response(gen())
        return [c async for c in resp.body_iterator][-1]

    async def vision_after_first_chunk(mp):
        # BR9e: the vision branch raises what the sync branch raises.
        from src.middleware.bridge_error import BridgeError, classify_exception
        return await _after_first_chunk(BridgeError(classify_exception(RuntimeError("vision boom"))))

    async def vision_4xx_after_first_chunk(mp):
        # BR9f: an upstream 4xx is final, same form.
        from fastapi import HTTPException as _HE

        from src.middleware.bridge_error import BridgeError, classify_exception
        return await _after_first_chunk(BridgeError(classify_exception(_HE(413, "too large"))))

    async def vision_rejected_after_200_after_first_chunk(mp):
        from src.middleware.bridge_error import (
            BridgeError,
            UpstreamResponseUnreadable,
            classify_exception,
        )
        return await _after_first_chunk(BridgeError(classify_exception(
            UpstreamResponseUnreadable("anthropic", KeyError("content")))))

    async def account_org_disabled_after_first_chunk(mp):
        from src.claude_cli import OrgSubscriptionDisabledError
        return await _after_first_chunk(OrgSubscriptionDisabledError("w", "assistant_text", 60))

    return [
        pytest.param(bedrock_client_error, id="bedrock-client-error"),
        pytest.param(bedrock_unclassified, id="bedrock-unclassified"),
        pytest.param(bedrock_aborted, id="bedrock-aborted"),
        pytest.param(bedrock_no_credentials, id="bedrock-no-credentials"),
        pytest.param(bedrock_unknown_model, id="bedrock-unknown-model"),
        pytest.param(after_first_chunk, id="stream_start-after-first-chunk"),
        pytest.param(openai_compatible_truncated, id="openai-compatible-truncated"),
        pytest.param(vision_after_first_chunk, id="vision-after-first-chunk"),
        pytest.param(account_org_disabled_after_first_chunk, id="account-org-disabled-after-first-chunk"),
        pytest.param(vision_4xx_after_first_chunk, id="vision-4xx-after-first-chunk"),
        pytest.param(vision_rejected_after_200_after_first_chunk, id="vision-unreadable-after-first-chunk"),
    ]


@pytest.mark.parametrize("producer", _producers())
async def test_every_stream_error_producer_sends_the_one_form(monkeypatch, producer):
    _assert_the_one_form(await producer(monkeypatch))
