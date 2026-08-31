"""Leere Vision-Antwort = FAIL-LOUD, nie stilles 200-leer (Befund 2026-08-31).

Pinnt die drei Vertragsstuecke des Fixes:
  * finish_reason spiegelt den echten Anthropic stop_reason (Trunkierung wird
    als "length" sichtbar statt als "stop" beschoenigt),
  * 0 Text-Chars + stop_reason=max_tokens → 422, non-retryable (ein
    unveraenderter Retry kann nie gelingen — er verbrennt nur Prepaid-Geld),
  * 0 Text-Chars sonst → 502, retryable.
Der Fehler ist ein VORKLASSIFIZIERTER BridgeError: eine nackte HTTPException
wuerde von classify_exception zu internal_error/500 zerdrueckt und von nginx
als "at capacity" verkleidet ueber alle Worker retried.
"""
from __future__ import annotations

import json

import pytest

from src.middleware.bridge_error import BridgeError, REASON_VISION_EMPTY_RESPONSE
from src.vision_provider import ensure_usable_vision_content, finish_reason_for


def _body(exc: BridgeError) -> dict:
    return json.loads(exc.response.body)


# ── finish_reason_for ───────────────────────────────────────────────────────

@pytest.mark.parametrize("stop_reason,expected", [
    ("end_turn", "stop"),
    ("stop_sequence", "stop"),
    ("max_tokens", "length"),
    ("tool_use", "tool_calls"),
    (None, "stop"),
    ("", "stop"),
    ("some_future_reason", "some_future_reason"),  # durchreichen, nicht beschoenigen
])
def test_finish_reason_mapping(stop_reason, expected):
    assert finish_reason_for(stop_reason) == expected


# ── ensure_usable_vision_content ────────────────────────────────────────────

def test_non_empty_content_passes():
    assert ensure_usable_vision_content("Rot", "end_turn") is None
    assert ensure_usable_vision_content("x", "max_tokens") is None  # truncated but usable


def test_empty_with_max_tokens_is_422_non_retryable():
    with pytest.raises(BridgeError) as exc:
        ensure_usable_vision_content("", "max_tokens")
    assert exc.value.response.status_code == 422
    body = _body(exc.value)["error"]
    assert body["reason"] == REASON_VISION_EMPTY_RESPONSE
    assert body["retryable"] is False
    assert body["stop_reason"] == "max_tokens"


def test_empty_with_other_stop_reason_is_502_retryable():
    with pytest.raises(BridgeError) as exc:
        ensure_usable_vision_content("", "end_turn")
    assert exc.value.response.status_code == 502
    body = _body(exc.value)["error"]
    assert body["reason"] == REASON_VISION_EMPTY_RESPONSE
    assert body["retryable"] is True


# ── VisionResult-Wrapper (Regressionspin fuer den 500 vom 31.08.) ───────────
# Der erste Deploy des Fixes scheiterte NICHT im Provider, sondern an der Naht:
# vision_router.VisionResult verpackte die Provider-Antwort um und liess
# stop_reason fallen -> AttributeError in main.py -> 500 -> nginx "at capacity".

def test_visionresult_requires_stop_reason():
    from src.routing.vision_router import VisionResult
    with pytest.raises(TypeError):  # Pflichtfeld: Vergessen scheitert beim Bauen
        VisionResult(content="x", model="m", usage={})


@pytest.mark.asyncio
async def test_route_to_vision_carries_stop_reason(monkeypatch):
    import src.routing.vision_router as vr
    from src.vision_provider import VisionResponse

    class FakeProvider:
        async def analyze(self, **kwargs):
            return VisionResponse(content="Rot", model="m",
                                  usage={"input_tokens": 1, "output_tokens": 1},
                                  stop_reason="max_tokens")

    monkeypatch.setattr(vr, "get_vision_provider", lambda: FakeProvider())
    result = await vr.route_to_vision(messages=[], model="m")
    assert result.stop_reason == "max_tokens"
