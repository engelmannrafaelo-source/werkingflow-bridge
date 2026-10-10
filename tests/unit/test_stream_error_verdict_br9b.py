"""BR9b (BR8R2 MUSS 2 / SOLL): every stream error carries `retryable`.

BR9 put the verdict into the sync envelope, research, doc-agent and jobs, and
into three CLI stream errors. Still without it: the generic stream exception,
the vision stream error, the truncated stream of the OpenAI-compatible
provider and both Bedrock stream errors (only {"error": str(e)}). A caller
reading `retryable` saw nothing there and had to guess from text.

Verdicts (src/error_contract.py):
- truncated stream (connection dropped before the end signal) → true;
- Bedrock ClientError → the status the sync path sends for it (throttle 429 →
  true, validation 400 / config 424 → false);
- Bedrock error event/chunk: overload, throttling, model timeout → true,
  validation and anything else → false;
- unclassified exception → false (rule 3).
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock as _MagicMock

import httpx
import pytest
from botocore.exceptions import ClientError

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

from src import bedrock_service  # noqa: E402
from src.models import ChatCompletionRequest, Message  # noqa: E402
from src.providers import openai_compatible  # noqa: E402


def _request(model="claude-sonnet-5"):
    return ChatCompletionRequest(
        model=model, messages=[Message(role="user", content="hi")], stream=True
    )


def _last_error(chunks):
    errs = [c for c in chunks if c.startswith("event: error\n") or '"error"' in c]
    assert errs, chunks
    return json.loads(errs[-1].split("data: ", 1)[1])


# --- Bedrock -------------------------------------------------------------------

def _ev(d):
    return {"chunk": {"bytes": json.dumps(d).encode()}}


_START = _ev({"type": "message_start", "message": {"usage": {"input_tokens": 3}}})
_TEXT = _ev({"type": "content_block_delta", "delta": {"text": "teil"}})


def _client(events=None, raises=None):
    def invoke(**kw):
        if raises is not None:
            raise raises
        return {"body": events, "ResponseMetadata": {"RequestId": "r"}}
    boto = SimpleNamespace(invoke_model_with_response_stream=invoke)
    return SimpleNamespace(default_region="eu-central-1", get_client=lambda region: boto)


async def _bedrock(monkeypatch, **kw):
    monkeypatch.setattr(bedrock_service, "get_bedrock_client", lambda: _client(**kw))
    return [c async for c in bedrock_service.stream_bedrock(_request(), usage_sink={})]


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "InvokeModelWithResponseStream")


@pytest.mark.parametrize("events,want", [
    pytest.param([_START, _TEXT], True, id="abgerissen"),
    pytest.param([_START, {"throttlingException": {"message": "slow down"}}], True,
                 id="event-throttling"),
    pytest.param([_START, {"modelStreamErrorException": {"message": "x"}}], True,
                 id="event-model-stream-error"),
    pytest.param([_START, {"validationException": {"message": "bad"}}], False,
                 id="event-validation"),
    pytest.param([_START, _ev({"type": "error", "error": {"type": "overloaded_error"}})], True,
                 id="chunk-overloaded"),
    pytest.param([_START, _ev({"type": "error", "error": {"type": "invalid_request_error"}})],
                 False, id="chunk-invalid-request"),
])
async def test_bedrock_stream_abort_carries_its_verdict(monkeypatch, events, want):
    err = _last_error(await _bedrock(monkeypatch, events=events))["error"]
    assert err["retryable"] is want
    assert "retry_after_s" in err


@pytest.mark.parametrize("code,want", [
    ("ThrottlingException", True),   # sync: 429
    ("InternalServerException", True),  # sync: 500
    ("ValidationException", False),  # sync: 400
    ("AccessDeniedException", False),  # sync: 424
])
async def test_bedrock_client_error_follows_the_sync_status(monkeypatch, code, want):
    err = _last_error(await _bedrock(monkeypatch, raises=_client_error(code)))["error"]
    assert err["retryable"] is want


async def test_bedrock_unclassified_and_config_errors_are_final(monkeypatch):
    err = _last_error(await _bedrock(monkeypatch, raises=KeyError("boom")))["error"]
    assert err["retryable"] is False

    def no_creds():
        raise RuntimeError("no Bedrock credentials on this worker")
    monkeypatch.setattr(bedrock_service, "get_bedrock_client", no_creds)
    out = [c async for c in bedrock_service.stream_bedrock(_request(), usage_sink={})]
    assert _last_error(out)["error"]["retryable"] is False


async def test_bedrock_unknown_model_is_final(monkeypatch):
    monkeypatch.setattr(bedrock_service, "get_bedrock_client", lambda: _client(events=[]))
    monkeypatch.setattr(bedrock_service, "resolve_model", lambda m: (None, None))
    out = [c async for c in bedrock_service.stream_bedrock(_request(), usage_sink={})]
    err = _last_error(out)["error"]
    assert err["retryable"] is False and "Unknown model" in err["message"]


# --- OpenAI-compatible ---------------------------------------------------------

async def test_openai_compatible_truncated_stream_is_retryable(monkeypatch):
    body = "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "ab"},
                                                "finish_reason": None}]}) + "\n"
    real = httpx.AsyncClient
    monkeypatch.setattr(
        openai_compatible.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=body.encode())), **kw),
    )
    out = [c async for c in openai_compatible.stream_openai_compatible(
        _request(model="m"), "http://x", "k")]
    err = _last_error(out)["error"]
    assert err["code"] == "stream_incomplete"
    assert err["retryable"] is True


# --- CLI stream: the generic exception -----------------------------------------

async def test_generic_stream_exception_is_not_promised_retryable(monkeypatch):
    import src.main as main

    def boom(*a, **kw):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(main.session_manager, "process_messages", boom)
    out = [c async for c in main.generate_streaming_response(_request(), "req-1")]
    err = _last_error(out)["error"]
    assert err["type"] == "streaming_error"
    assert err["retryable"] is False
    assert "retry_after_s" in err
