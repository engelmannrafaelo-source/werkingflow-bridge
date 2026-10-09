"""Kein stilles Abschneiden (ZB3B, Rafael 2026-10-08): jeder Antwortweg meldet
den ECHTEN Abbruchgrund. Eine bei max_tokens abgeschnittene Antwort kommt beim
Aufrufer als finish_reason "length" an — nie als "stop".
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src import bedrock_service
from src.models import ChatCompletionRequest, Message
from src.providers import anthropic_direct
from src.routing.backend_router import BackendConfig


def _request(**kw):
    return ChatCompletionRequest(
        model="claude-sonnet-5", messages=[Message(role="user", content="hi")], **kw
    )


def _config():
    return BackendConfig(
        backend=None, region=None, model_id="m", bedrock_model_id=None,
        privacy_enabled=False, env_vars={}, provider_model="m",
    )


# ── anthropic_direct (H3) ───────────────────────────────────────────────────

@pytest.mark.parametrize("stop_reason,expected", [
    ("end_turn", "stop"), ("max_tokens", "length"),
])
async def test_direct_meldet_echten_abbruchgrund(monkeypatch, stop_reason, expected):
    async def fake(**kw):
        return SimpleNamespace(content="abc", model="m", usage={}, stop_reason=stop_reason)

    monkeypatch.setattr(anthropic_direct, "route_to_vision", fake)
    out = await anthropic_direct.call_anthropic_direct(_request(), _config())
    assert out["choices"][0]["finish_reason"] == expected


async def test_direct_reicht_max_tokens_durch_und_meldet_vorgabe(monkeypatch):
    seen = {}

    async def fake(**kw):
        seen.update(kw)
        return SimpleNamespace(content="x", model="m", usage={}, stop_reason="end_turn")

    monkeypatch.setattr(anthropic_direct, "route_to_vision", fake)
    out = await anthropic_direct.call_anthropic_direct(_request(max_tokens=777), _config())
    assert seen["max_tokens"] == 777
    assert out["x_bridge_max_tokens"] == 777
    assert out["x_bridge_max_tokens_defaulted"] is False

    out = await anthropic_direct.call_anthropic_direct(
        SimpleNamespace(**{**_request().__dict__, "max_tokens": None}), _config()
    )
    assert seen["max_tokens"] == anthropic_direct.DEFAULT_MAX_TOKENS
    assert out["x_bridge_max_tokens_defaulted"] is True


# ── Bedrock-Streaming (H4) ──────────────────────────────────────────────────

class _FakeBoto:
    def __init__(self, stop_reason):
        self.stop_reason = stop_reason

    def invoke_model_with_response_stream(self, **kw):
        def ev(d):
            return {"chunk": {"bytes": json.dumps(d).encode()}}
        events = [
            ev({"type": "message_start", "message": {"usage": {"input_tokens": 3}}}),
            ev({"type": "content_block_delta", "delta": {"text": "teil"}}),
        ]
        if self.stop_reason:
            events.append(ev({"type": "message_delta",
                              "delta": {"stop_reason": self.stop_reason},
                              "usage": {"output_tokens": 5}}))
        events.append(ev({"type": "message_stop"}))
        return {"body": events, "ResponseMetadata": {"RequestId": "r"}}


@pytest.mark.parametrize("stop_reason,expected", [
    ("end_turn", "stop"), ("max_tokens", "length"),
])
async def test_bedrock_stream_meldet_stop_reason(monkeypatch, stop_reason, expected):
    fake_client = SimpleNamespace(
        default_region="eu-central-1", get_client=lambda region: _FakeBoto(stop_reason)
    )
    monkeypatch.setattr(bedrock_service, "get_bedrock_client", lambda: fake_client)
    chunks = [c async for c in bedrock_service.stream_bedrock(_request(stream=True))]
    finals = [
        json.loads(c[6:])["choices"][0]["finish_reason"]
        for c in chunks if c.startswith("data: {") and '"finish_reason":' in c
        and json.loads(c[6:])["choices"][0]["finish_reason"]
    ]
    assert finals == [expected]


def test_bedrock_unbekannter_grund_wird_durchgereicht():
    assert bedrock_service._map_bedrock_stop_reason("tool_use") == "tool_calls"
    assert bedrock_service._map_bedrock_stop_reason("pause_turn") == "pause_turn"
    assert bedrock_service._map_bedrock_stop_reason("max_tokens") == "length"


# ── Vision: fehlender stop_reason (M8) ──────────────────────────────────────

def test_fehlender_stop_reason_wird_nicht_zu_end_turn():
    from src.vision_provider import _stop_reason_or_unknown, finish_reason_for

    assert _stop_reason_or_unknown(None) == "unknown"
    assert finish_reason_for(_stop_reason_or_unknown(None)) == "unknown"
    assert _stop_reason_or_unknown("max_tokens") == "max_tokens"


def test_unbekannter_finish_reason_bricht_das_antwortmodell_nicht():
    from src.models import Choice, StreamChoice

    Choice(index=0, message=Message(role="assistant", content="x"), finish_reason="refusal")
    StreamChoice(index=0, delta={}, finish_reason="model_context_window_exceeded")
