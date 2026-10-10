"""BR9f (BR9eR): a vision error that recurs identically is not retried.

Befund (BR9eR §2): since BR9e the vision stream raises what the sync branch
raises, BridgeError(classify_exception(e)). classify_exception read only the
message text, so a provider 4xx (Anthropic 401/413, Gemini 400), Gemini's
refusal after a 200 and a parse error after a 200 all fell through to
internal_error: 500, retryable. nginx retries http_500 on every worker (up to
5), PC1 bridgeFetch retries the rewritten 502 twice more — up to 15 uploads of
the same image for an answer that cannot change, and after a 200 every one is
paid.

Now the status of an HTTPException decides (4xx except 408/425/429: not
retryable, and the wire status stays 4xx — nginx reads the status, never the
body), and the typed vision errors carry their own verdict. Transport errors
and upstream 5xx stay retryable, unchanged. One rule for sync and stream:
both go through classify_exception.

The cases are driven through the real providers with a fake httpx client, so
the same file also runs on 3598224 (where the typed errors do not exist).
"""
from __future__ import annotations

import json
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

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from src import stream_start  # noqa: E402
from src import vision_provider as vp  # noqa: E402
from src.middleware.bridge_error import BridgeError, classify_exception  # noqa: E402
from src.models import BackendType  # noqa: E402
from src.providers import gemini_vision as gv  # noqa: E402
from tests.unit.test_stream_br9e import (  # noqa: E402
    _json_body,
    _run_route,
    _status,
    _text,
    _vision,
)

# What retries a status without reading the body: nginx proxy_next_upstream
# (docker/nginx.conf) and PC1 bridgeFetch DEFAULT_RETRY_ON
# (werkingflow-production packages/ai-bridge-client/src/core/fetch.ts).
NGINX_RETRIES = {500, 502, 503, 504, 429}
PC1_RETRIES = {429, 500, 502, 503, 529}

_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def _image_messages():
    return [{"role": "user", "content": [
        {"type": "text", "text": "Beschreibe den Plan."},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_PNG}"}},
    ]}]


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)  # raises like httpx on a non-JSON body
        return self._body


def _client(status_code, body):
    class _Client:
        def __init__(self, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            return _Response(status_code, body)
    return _Client


async def _anthropic_raises(monkeypatch, status_code, body) -> BaseException:
    monkeypatch.setenv("ANTHROPIC_VISION_API_KEY", "test-key")
    monkeypatch.setattr(vp.httpx, "AsyncClient", _client(status_code, body))
    with pytest.raises(Exception) as caught:
        await vp.VisionProvider().analyze(messages=_image_messages(), model="claude-sonnet-4-5")
    return caught.value


async def _gemini_raises(monkeypatch, status_code, body, *, key="test-key") -> BaseException:
    if key:
        monkeypatch.setenv("GEMINI_VISION_API_KEY", key)
    else:
        monkeypatch.delenv("GEMINI_VISION_API_KEY", raising=False)
    monkeypatch.setattr(gv.httpx, "AsyncClient", _client(status_code, body))
    with pytest.raises(Exception) as caught:
        await gv.GeminiVisionProvider().analyze(messages=_image_messages())
    return caught.value


def _anthropic_error(kind, message):
    return {"type": "error", "error": {"type": kind, "message": message}}


# (provider call → expected status, reason) — every one of them NOT retryable.
_FINAL = [
    pytest.param("anthropic", 401, _anthropic_error("authentication_error", "invalid x-api-key"),
                 424, "upstream_request_rejected", id="anthropic-401"),
    pytest.param("anthropic", 403, _anthropic_error("permission_error", "no access"),
                 424, "upstream_request_rejected", id="anthropic-403"),
    pytest.param("anthropic", 404, _anthropic_error("not_found_error", "model: nope"),
                 404, "upstream_request_rejected", id="anthropic-404"),
    pytest.param("anthropic", 413, _anthropic_error(
        "request_too_large", "Request exceeds the maximum allowed number of bytes."),
                 413, "upstream_request_rejected", id="anthropic-413"),
    pytest.param("anthropic", 400, _anthropic_error("invalid_request_error", "image exceeds 5 MB maximum"),
                 400, "upstream_invalid_request", id="anthropic-400-invalid-as-before"),
    pytest.param("anthropic", 400, _anthropic_error(
        "invalid_request_error", "Your credit balance is too low to access the Anthropic API."),
                 402, "vision_billing_exhausted", id="anthropic-400-credit-as-before"),
    pytest.param("anthropic", 200, "<html>not json</html>",
                 422, "vision_response_unreadable", id="anthropic-200-unparseable"),
    pytest.param("anthropic", 200, ["not", "an", "object"],
                 422, "vision_response_unreadable", id="anthropic-200-wrong-shape"),
    pytest.param("gemini", 400, {"error": {"code": 400, "message": "Unable to process input image.",
                                           "status": "INVALID_ARGUMENT"}},
                 400, "upstream_request_rejected", id="gemini-400"),
    pytest.param("gemini", 403, {"error": {"code": 403, "status": "PERMISSION_DENIED"}},
                 424, "upstream_request_rejected", id="gemini-403"),
    pytest.param("gemini", 200, {"promptFeedback": {"blockReason": "SAFETY"}},
                 422, "vision_response_rejected", id="gemini-200-blocked"),
    pytest.param("gemini", 200, "not json",
                 422, "vision_response_unreadable", id="gemini-200-unparseable"),
    pytest.param("gemini", 200, {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}],
                                 "usageMetadata": {"promptTokenCount": "viele"}},
                 422, "vision_response_unreadable", id="gemini-200-bad-usage"),
]


async def _provider_error(monkeypatch, provider, status_code, body):
    if provider == "anthropic":
        return await _anthropic_raises(monkeypatch, status_code, body)
    return await _gemini_raises(monkeypatch, status_code, body)


@pytest.mark.parametrize("provider,status_code,body,want_status,want_reason", _FINAL)
async def test_a_recurring_vision_error_is_final_with_a_4xx(
        monkeypatch, provider, status_code, body, want_status, want_reason):
    exc = await _provider_error(monkeypatch, provider, status_code, body)
    resp = classify_exception(exc)
    err = json.loads(resp.body)["error"]

    assert (resp.status_code, err["reason"], err["retryable"]) == (want_status, want_reason, False), err
    assert resp.status_code not in NGINX_RETRIES and resp.status_code not in PC1_RETRIES
    assert err["message"], err


async def test_upstream_status_is_kept_in_the_envelope(monkeypatch):
    exc = await _anthropic_raises(monkeypatch, 401, _anthropic_error("authentication_error", "bad key"))
    err = json.loads(classify_exception(exc).body)["error"]
    # 424, not 401: it is the bridge's key the provider refused, not the caller's.
    assert err["upstream_status"] == 401
    assert err["type"] != "authentication_error"


async def test_gemini_config_and_request_errors_say_not_retryable(monkeypatch):
    """Config: config_error as for the Anthropic key (500, retryable false —
    see BR9f-bereit: nginx still fails over on the 500). Request: 400."""
    missing = await _gemini_raises(monkeypatch, 200, {}, key=None)
    err = json.loads(classify_exception(missing).body)["error"]
    assert (err["reason"], err["retryable"]) == ("worker_misconfigured", False), err

    url_image = [{"role": "user", "content": [
        {"type": "text", "text": "x"},
        {"type": "image", "source": {"type": "url", "url": "http://x"}}]}]
    monkeypatch.setenv("GEMINI_VISION_API_KEY", "k")
    with pytest.raises(gv.GeminiVisionError) as caught:
        gv._to_gemini_contents(url_image)
    resp = classify_exception(caught.value)
    assert (resp.status_code, json.loads(resp.body)["error"]["retryable"]) == (400, False)


# --- what stays retryable, exactly as before ------------------------------------

_TRANSIENT = [
    pytest.param("anthropic", 529, '{"type":"error","error":{"type":"overloaded_error"}}', id="anthropic-529"),
    pytest.param("anthropic", 500, "internal", id="anthropic-500"),
    pytest.param("anthropic", 429, '{"type":"error","error":{"type":"rate_limit_error"}}', id="anthropic-429"),
    pytest.param("gemini", 503, '{"error":{"status":"UNAVAILABLE"}}', id="gemini-503"),
    pytest.param("gemini", 429, '{"error":{"status":"RESOURCE_EXHAUSTED"}}', id="gemini-429"),
]


@pytest.mark.parametrize("provider,status_code,body", _TRANSIENT)
async def test_upstream_5xx_and_429_stay_retryable(monkeypatch, provider, status_code, body):
    exc = await _provider_error(monkeypatch, provider, status_code, body)
    err = json.loads(classify_exception(exc).body)["error"]
    assert err["retryable"] is True, err


@pytest.mark.parametrize("exc", [
    pytest.param(httpx.ReadTimeout(""), id="read-timeout"),
    pytest.param(httpx.ConnectError("connection refused"), id="connect-error"),
    pytest.param(RuntimeError("vision boom"), id="unclassified-as-before"),
])
def test_transport_and_unclassified_stay_as_before(exc):
    resp = classify_exception(exc)
    assert json.loads(resp.body)["error"]["retryable"] is True
    assert resp.status_code in (429, 500)


@pytest.mark.parametrize("status_code", [408, 425, 429])
def test_a_transient_4xx_http_exception_stays_retryable(status_code):
    """429 (and 408/425) keep the marker path: the status nginx retries."""
    resp = classify_exception(HTTPException(status_code=status_code, detail="slow down"))
    err = json.loads(resp.body)["error"]
    assert err["retryable"] is True
    assert resp.status_code in NGINX_RETRIES


# --- one rule for sync and stream, over the real route --------------------------

def _vision_status_error(status_code, message):
    return HTTPException(status_code=status_code, detail=json.dumps(
        _anthropic_error("authentication_error" if status_code == 401 else "request_too_large", message)))


@pytest.mark.parametrize("stream", [pytest.param(False, id="sync"), pytest.param(True, id="stream")])
@pytest.mark.parametrize("status_code,want", [(401, 424), (413, 413)])
async def test_vision_4xx_leaves_the_route_final_on_sync_and_stream(monkeypatch, stream, status_code, want):
    exc = _vision_status_error(status_code, "rejected")
    sent = await _run_route(monkeypatch, BackendType.ANTHROPIC, extra=_vision(exc), stream=stream)

    assert _status(sent) == want, _text(sent)[:300]
    err = _json_body(sent)["error"]
    assert (err["retryable"], err["reason"]) == (False, "upstream_request_rejected"), err


async def test_vision_4xx_after_the_first_chunk_is_a_final_error_event():
    async def gen():
        yield 'data: {"choices": []}\n\n'
        raise BridgeError(classify_exception(_vision_status_error(413, "too large")))

    resp = await stream_start.event_stream_response(gen())
    chunks = [c async for c in resp.body_iterator]
    assert chunks[-1].startswith("event: error\n")
    err = json.loads(chunks[-1].split("data: ", 1)[1])["error"]
    assert (err["retryable"], err["reason"], err["upstream_status"]) == (False, "upstream_request_rejected", 413)


# --- point 2: the chat path keeps its 429 ---------------------------------------

async def test_chat_http_exception_429_is_untouched(monkeypatch):
    """The sync chat route catches HTTPException before its classify_exception
    call (main.py `except HTTPException`), so BR9f cannot reach it. Wächter.
    (The CLI stream sends it through its own generic "Streaming error" branch,
    not through classify_exception either — unchanged, see BR9f-bereit.)"""
    from src import main

    def limited(*a, **kw):
        raise HTTPException(status_code=429, detail="rate limited upstream")
    monkeypatch.setattr(main.session_manager, "process_messages", limited)
    sent = await _run_route(monkeypatch, BackendType.ANTHROPIC, stream=False)
    assert _status(sent) == 429, _text(sent)[:300]
    assert _json_body(sent)["error"]["retryable"] is True
