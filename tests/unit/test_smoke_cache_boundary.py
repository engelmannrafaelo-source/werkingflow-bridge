"""Smoke probe chat_cache_boundary (scripts/bridge_smoke.py, CACHEFIX 11.10.2026).

DBD2: every chat call WITH a cache boundary failed inside the CLI (Messages API
400) while the boundary-free chat probe stayed green, so the deploy smoke could
not see it. The probe sends a content block with cache_control and fails on a
non-200, on a boundary that did not reach the CLI, and on a 200 without real
input tokens (an API error inside the CLI carries no usage).
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock

_SMOKE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts",
    "bridge_smoke.py",
)
_spec = importlib.util.spec_from_file_location(
    "bridge_smoke_cache_boundary_under_test", _SMOKE_PATH
)
smoke = importlib.util.module_from_spec(_spec)
sys.modules["bridge_smoke_cache_boundary_under_test"] = smoke
_spec.loader.exec_module(smoke)

API_TEXT = (
    "API Error: 400 messages.0.content.6.cache_control.ttl: a ttl='1h' cache_control "
    "block must not come after a ttl='5m' cache_control block"
)


def _probe():
    (p,) = [p for p in smoke.PROBES if p.name == "chat_cache_boundary"]
    return p


def _resp(status, body, headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    r.text = str(body)
    r.headers = headers or {}
    return r


def _answer(content="OK", prompt_tokens=1200):
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 1},
    }


def _run(monkeypatch, resp):
    sent = {}

    def post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, json=json)
        return resp

    monkeypatch.setattr(smoke.requests, "post", post)
    ctx = smoke.Ctx(base_url="http://x.invalid", api_key="k")
    return _probe().fn(ctx), sent


def test_probe_is_registered_pool_gated_and_sends_a_boundary(monkeypatch):
    p = _probe()
    assert p.endpoint == "/v1/chat/completions" and p.profiles == {"hetzner", "server2"}
    assert "chat_cache_boundary" in smoke.POOL_GATED_PROBES
    _, sent = _run(monkeypatch, _resp(200, _answer(), {"X-Cache-Boundary": "applied"}))
    blocks = sent["json"]["messages"][0]["content"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1]


def test_healthy_answer_passes(monkeypatch):
    res, _ = _run(monkeypatch, _resp(200, _answer(), {"X-Cache-Boundary": "applied"}))
    assert res.ok, res.detail


def test_dbd2_build_fails_the_probe(monkeypatch):
    """276d146: 200, boundary applied, the API error as content, no usage."""
    res, _ = _run(
        monkeypatch, _resp(200, _answer(API_TEXT, 0), {"X-Cache-Boundary": "applied"})
    )
    assert not res.ok and "prompt_tokens=0" in res.detail


def test_fixed_build_error_fails_the_probe(monkeypatch):
    body = {"error": {"message": API_TEXT, "upstream_status": 400, "retryable": False}}
    res, _ = _run(monkeypatch, _resp(400, body))
    assert not res.ok and "HTTP 400" in res.detail


def test_boundary_lost_fails_the_probe(monkeypatch):
    res, _ = _run(
        monkeypatch,
        _resp(200, _answer(), {"X-Cache-Boundary": "lost:marker_removed_in_transit"}),
    )
    assert not res.ok and "X-Cache-Boundary" in res.detail
    res, _ = _run(monkeypatch, _resp(200, _answer(), {}))
    assert not res.ok
