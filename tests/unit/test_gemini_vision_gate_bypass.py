"""Gemini-Bildweg darf nicht an Claude-Konto-Sperren des Workers scheitern."""
from types import SimpleNamespace

from src.middleware.adaptive_limiter import is_gemini_vision_request


def _req(headers=None, body=None):
    return SimpleNamespace(headers=headers or {}, state=SimpleNamespace(cached_body_dict=body))


def test_header_gemini():
    assert is_gemini_vision_request(_req({"X-Vision-Provider": " Gemini "}))


def test_provider_tier_gemini_vision():
    assert is_gemini_vision_request(_req(body={"provider_tier": "gemini-vision"}))


def test_claude_and_plain_not_bypassed():
    assert not is_gemini_vision_request(_req())
    assert not is_gemini_vision_request(_req({"X-Vision-Provider": "claude"}))
    assert not is_gemini_vision_request(_req(body={"provider_tier": "claude-direct"}))
