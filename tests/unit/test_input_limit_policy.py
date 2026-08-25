"""input_limit_policy — single, ENV-configurable Bridge-wide input-size gate.

Stage 1 of the two-stage rollout (Rafael-Entscheid 2026-08-25, siehe devops-
Memory project_zentrales_input_token_limit_20260825): observe-only by default
(log loudly, never reject). The reject path exists and is tested here, but
stays dormant until BRIDGE_INPUT_LIMIT_ENFORCE=true.
"""
import sys
from unittest.mock import MagicMock as _MagicMock

for _mod_name in ["claude_code_sdk", "src.db.client"]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

from unittest.mock import MagicMock  # noqa: E402

import pytest  # noqa: E402

from src.middleware.bridge_error import BridgeError  # noqa: E402
from src.middleware.input_limit_policy import (  # noqa: E402
    observe_input_limit,
    _limit_tokens,
    _LIMIT_ENV,
    _ENFORCE_ENV,
    _DEFAULT_LIMIT_TOKENS,
)


def _request(est_tokens, headers=None):
    req = MagicMock()
    req.state = MagicMock(spec=["adaptive_est_tokens"] if est_tokens is not None else [])
    if est_tokens is not None:
        req.state.adaptive_est_tokens = est_tokens
    req.headers = headers or {}
    return req


def test_no_estimate_on_state_is_a_noop():
    req = _request(est_tokens=None)
    observe_input_limit(req)  # must not raise, must not touch anything


def test_within_limit_is_a_noop():
    req = _request(est_tokens=10)
    observe_input_limit(req)  # 10 <= default limit


def test_over_limit_observe_only_logs_but_does_not_raise(monkeypatch, caplog):
    monkeypatch.delenv(_ENFORCE_ENV, raising=False)
    req = _request(est_tokens=_DEFAULT_LIMIT_TOKENS + 1, headers={"X-App-ID": "werking-report"})
    with caplog.at_level("WARNING"):
        observe_input_limit(req)  # must not raise — enforce defaults off
    assert any("input_limit_policy" in r.message for r in caplog.records)
    assert any("werking-report" in r.message for r in caplog.records)


def test_over_limit_raises_bridge_error_when_enforce_enabled(monkeypatch):
    monkeypatch.setenv(_ENFORCE_ENV, "true")
    req = _request(est_tokens=_DEFAULT_LIMIT_TOKENS + 1)
    with pytest.raises(BridgeError) as exc_info:
        observe_input_limit(req)
    body = exc_info.value.response.body
    assert b"input_too_large" in body


def test_limit_is_env_configurable(monkeypatch):
    monkeypatch.setenv(_LIMIT_ENV, "50")
    assert _limit_tokens() == 50


def test_limit_falls_back_to_default_on_garbage_env(monkeypatch, caplog):
    monkeypatch.setenv(_LIMIT_ENV, "not-a-number")
    with caplog.at_level("WARNING"):
        assert _limit_tokens() == _DEFAULT_LIMIT_TOKENS


def test_limit_falls_back_to_default_when_unset(monkeypatch):
    monkeypatch.delenv(_LIMIT_ENV, raising=False)
    assert _limit_tokens() == _DEFAULT_LIMIT_TOKENS


def test_limit_falls_back_to_default_on_non_positive_env(monkeypatch):
    monkeypatch.setenv(_LIMIT_ENV, "0")
    assert _limit_tokens() == _DEFAULT_LIMIT_TOKENS
