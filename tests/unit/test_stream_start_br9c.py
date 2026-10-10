"""BR9c (BR9R MUSS 1 + SOLL): a stream that fails before its first chunk.

Befund (BR9R): a StreamingResponse sends 200 and its headers before the
generator runs. A failure up to the first chunk — OpenAI-compatible upstream
!= 200 or network gone after retries, Bedrock model/region mismatch (an
HTTPException raised inside the generator), CLI WorkerUnavailableError —
therefore reached the client as 200 with neither `event: error` nor [DONE]:
no verdict, and PC1 had to guess "dropped connection, retry".

Now (src/stream_start.py, one place for every streamed route) the first chunk
is pulled inside the route: a failure before it is a real HTTP error with the
error_contract fields; a failure after it ends the stream as `event: error`
with retryable / retry_after_s.

SOLL: Bedrock has one rule for sync and stream — botocore transport errors are
retryable, any other unclassified exception is not.

These tests drive the REAL /v1/chat/completions route; only the backend
resolution, the provider transport, the Bedrock client and the CLI run are
stand-ins.
"""
from __future__ import annotations

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
    ConnectTimeoutError,
    EndpointConnectionError,
    ParamValidationError,
    ReadTimeoutError,
)
from fastapi import HTTPException  # noqa: E402

import src.main as main  # noqa: E402
from src import bedrock_service  # noqa: E402
from src.claude_cli import WorkerUnavailableError  # noqa: E402
from src.models import BackendType, ChatCompletionRequest, Message  # noqa: E402
from src.providers import openai_compatible  # noqa: E402
from src.routing.backend_router import BackendConfig  # noqa: E402


def _config(backend: BackendType) -> BackendConfig:
    return BackendConfig(
        backend=backend, region="eu-central-1", model_id="claude-sonnet-4-5",
        bedrock_model_id=None, privacy_enabled=False, env_vars={},
        provider_tier="test-tier", provider_base_url="http://provider.test",
        provider_api_key="k", provider_model="m",
    )


def _post_stream(monkeypatch, backend: BackendType, *extra):
    from starlette.testclient import TestClient

    from src.middleware.adaptive_limiter import adaptive_limit_dependency

    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("CLAUDE_SKIP_AUTH", "1")

    async def _kein_limiter():
        return None

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
            return TestClient(main.app, raise_server_exceptions=False).post(
                "/v1/chat/completions",
                json={"model": "claude-sonnet-4-5", "stream": True,
                      "messages": [{"role": "user", "content": "ping"}]},
            )
    finally:
        main.app.dependency_overrides.clear()


def _error(resp) -> dict:
    return resp.json()["error"]


def _stream_error(resp) -> dict:
    events = [e for e in resp.text.split("\n\n") if e.startswith("event: error\n")]
    assert len(events) == 1, resp.text[:500]
    return json.loads(events[0].split("data: ", 1)[1])["error"]


# --- OpenAI-compatible: upstream != 200 / network before the first chunk ------

def _provider(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        openai_compatible.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr(openai_compatible, "_backoff_delay", lambda attempt: 0)


@pytest.mark.parametrize("upstream,status,retryable", [
    pytest.param(500, 429, True, id="5xx-nach-wiederholungen"),
    pytest.param(429, 429, True, id="429"),
    pytest.param(401, 424, False, id="401-schluessel-der-bridge"),
    pytest.param(400, 400, False, id="400"),
    pytest.param(404, 404, False, id="404"),
])
def test_openai_compatible_upstream_error_is_an_http_error(monkeypatch, upstream, status, retryable):
    _provider(monkeypatch, lambda r: httpx.Response(upstream, text="nein"))
    resp = _post_stream(monkeypatch, BackendType.OPENAI_COMPATIBLE)

    assert resp.status_code == status, resp.text[:300]
    err = _error(resp)
    assert err["retryable"] is retryable
    assert err["source"] == "upstream_provider"
    assert err["upstream_status"] == upstream
    assert ("Retry-After" in resp.headers) is retryable


def test_openai_compatible_network_error_after_retries_is_retryable(monkeypatch):
    def down(request):
        raise httpx.ConnectError("connection refused", request=request)
    _provider(monkeypatch, down)
    resp = _post_stream(monkeypatch, BackendType.OPENAI_COMPATIBLE)

    assert resp.status_code == 429, resp.text[:300]
    err = _error(resp)
    assert err["retryable"] is True
    assert err["retry_after_s"] == 15
    assert err["source"] == "upstream_network"


def test_openai_compatible_good_stream_is_unchanged(monkeypatch):
    body = (
        "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "ab"},
                                            "finish_reason": "stop"}]}) + "\n"
        "data: [DONE]\n"
    )
    _provider(monkeypatch, lambda r: httpx.Response(200, content=body.encode()))
    resp = _post_stream(monkeypatch, BackendType.OPENAI_COMPATIBLE)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers["X-Backend"] == "openai_compatible"
    assert '"ab"' in resp.text and resp.text.endswith("data: [DONE]\n\n")


# --- Bedrock: model/region before the first chunk ------------------------------

def _bedrock_client(monkeypatch):
    boto = SimpleNamespace(invoke_model_with_response_stream=lambda **kw: {"body": []})
    monkeypatch.setattr(bedrock_service, "get_bedrock_client", lambda: SimpleNamespace(
        default_region="eu-central-1", get_client=lambda region: boto))


def test_bedrock_region_mismatch_is_an_http_400(monkeypatch):
    _bedrock_client(monkeypatch)

    def no_model(model, region):
        raise ValueError(f"{model} is not offered in {region}")
    monkeypatch.setattr(bedrock_service, "to_bedrock_model_id", no_model)
    # The route's pin and attribution gates are not under test here.
    resp = _post_stream(
        monkeypatch, BackendType.BEDROCK,
        patch("src.routing.user_provider_override.assert_bedrock_is_pinned"),
        patch("src.routing.user_provider_override.assert_bedrock_attribution_complete"),
    )

    assert resp.status_code == 400, resp.text[:300]
    err = _error(resp)
    assert err["retryable"] is False
    assert "not offered" in err["message"]


# --- CLI: WorkerUnavailableError ------------------------------------------------

def test_cli_worker_unavailable_before_first_chunk_is_the_failover_429(monkeypatch):
    """generate_streaming_response re-raises WorkerUnavailableError from
    before the CLI run (its `except WorkerUnavailableError: raise`), meant as
    nginx's failover — which a started 200 could never be."""
    def busy(*a, **kw):
        raise WorkerUnavailableError("busy")
    monkeypatch.setattr(main.session_manager, "process_messages", busy)
    resp = _post_stream(monkeypatch, BackendType.ANTHROPIC)

    assert resp.status_code == 429, resp.text[:300]
    assert resp.headers.get("X-Worker-Failover") == "true"
    err = _error(resp)
    assert err["retryable"] is True
    assert err["reason"] == "worker_unavailable_for_failover"


async def test_worker_unavailable_after_first_chunk_ends_as_event_error():
    from src.stream_start import event_stream_response

    async def gen():
        yield "data: {\"choices\": []}\n\n"
        raise WorkerUnavailableError("worker went away")

    resp = await event_stream_response(gen())
    chunks = [c async for c in resp.body_iterator]
    assert resp.status_code == 200
    assert chunks[0] == "data: {\"choices\": []}\n\n"
    err = json.loads(chunks[-1].split("data: ", 1)[1])["error"]
    assert chunks[-1].startswith("event: error\n")
    assert err["retryable"] is True and "retry_after_s" in err
    assert "[DONE]" not in "".join(chunks)


@pytest.mark.parametrize("exc,want", [
    pytest.param(HTTPException(status_code=400, detail="bad"), False, id="http-400"),
    pytest.param(HTTPException(status_code=503, detail="later"), True, id="http-503"),
    pytest.param(KeyError("x"), False, id="unklassiert"),
    pytest.param(openai_compatible.ProviderError(599, "net"), True, id="provider-netz"),
])
async def test_failure_after_first_chunk_carries_its_verdict(exc, want):
    from src.stream_start import event_stream_response

    async def gen():
        yield "data: x\n\n"
        raise exc

    resp = await event_stream_response(gen())
    chunks = [c async for c in resp.body_iterator]
    err = json.loads(chunks[-1].split("data: ", 1)[1])["error"]
    assert err["retryable"] is want


async def test_stream_is_closed_when_the_caller_stops_reading():
    from src.stream_start import event_stream_response

    closed = []

    async def gen():
        try:
            yield "a"
            yield "b"
        finally:
            closed.append(True)

    resp = await event_stream_response(gen())
    body = resp.body_iterator
    assert await body.__anext__() == "a"
    await body.aclose()
    assert closed == [True]


# --- SOLL: Bedrock, one rule for sync and stream -------------------------------

def _request():
    return ChatCompletionRequest(model="claude-sonnet-4-5",
                                 messages=[Message(role="user", content="hi")], stream=True)


def _raising_client(monkeypatch, exc):
    def invoke(**kw):
        raise exc
    boto = SimpleNamespace(invoke_model_with_response_stream=invoke, invoke_model=invoke)
    monkeypatch.setattr(bedrock_service, "get_bedrock_client", lambda: SimpleNamespace(
        default_region="eu-central-1", get_client=lambda region: boto))


_TRANSPORT = [
    pytest.param(EndpointConnectionError(endpoint_url="https://bedrock"), id="endpoint"),
    pytest.param(ConnectTimeoutError(endpoint_url="https://bedrock"), id="connect-timeout"),
    pytest.param(ReadTimeoutError(endpoint_url="https://bedrock"), id="read-timeout"),
]
_FINAL = [
    pytest.param(KeyError("content"), id="keyerror"),
    pytest.param(ParamValidationError(report="bad"), id="param-validation"),
]


@pytest.mark.parametrize("exc", _TRANSPORT)
async def test_bedrock_transport_error_is_retryable_in_the_stream(monkeypatch, exc):
    _raising_client(monkeypatch, exc)
    out = [c async for c in bedrock_service.stream_bedrock(_request(), usage_sink={})]
    err = json.loads(out[-1].split("data: ", 1)[1])
    assert out[-1].startswith("event: error\n")
    assert err["retryable"] is True


@pytest.mark.parametrize("exc", _FINAL)
async def test_bedrock_unclassified_is_final_in_the_stream(monkeypatch, exc):
    _raising_client(monkeypatch, exc)
    out = [c async for c in bedrock_service.stream_bedrock(_request(), usage_sink={})]
    assert json.loads(out[-1].split("data: ", 1)[1])["retryable"] is False


async def _sync_detail(monkeypatch, exc) -> dict:
    _raising_client(monkeypatch, exc)
    req = _request()
    req.stream = False
    with pytest.raises(HTTPException) as caught:
        await bedrock_service.call_bedrock(req)
    assert caught.value.status_code == 500
    return caught.value.detail


@pytest.mark.parametrize("exc", _TRANSPORT)
async def test_bedrock_transport_error_is_retryable_in_sync(monkeypatch, exc):
    detail = await _sync_detail(monkeypatch, exc)
    assert detail["retryable"] is True


@pytest.mark.parametrize("exc", _FINAL)
async def test_bedrock_unclassified_is_final_in_sync(monkeypatch, exc):
    detail = await _sync_detail(monkeypatch, exc)
    assert detail["retryable"] is False


async def test_bedrock_sync_verdict_reaches_the_wire():
    """The handler takes the raiser's verdict over the 500 (rule 1)."""
    from starlette.requests import Request

    exc = HTTPException(status_code=500, detail={"message": "Bedrock API error: x",
                                                 "retryable": False, "retry_after_s": None})
    resp = await main.http_exception_handler(Request({"type": "http", "headers": []}), exc)
    body = json.loads(resp.body)["error"]
    assert resp.status_code == 500 and body["retryable"] is False
