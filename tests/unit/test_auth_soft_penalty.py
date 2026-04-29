"""
Unit tests for TokenRotator soft-penalty behavior (single-token workers)
and TokenInvalidError for 401 auth failures.

Fixes audited 2026-04-29:
- Fix 1: rotate_token() / mark_token_failed() must NOT raise AllTokensExhausted
  when len(tokens) == 1 — instead apply soft-penalty cooldown so nginx-LB
  failovers via 503.
- Fix 2: 401 (token invalid) is unrecoverable; mark_token_invalid raises
  TokenInvalidError (CRITICAL log, worker dies until restart). This must NOT
  share cooldown bucket with 429 rate limits.
"""

import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from src.auth import (
    TokenRotator,
    AllTokensExhausted,
    TokenInvalidError,
)


def make_rotator(tokens):
    """Create a TokenRotator with a controlled set of tokens.

    Bypasses _load_tokens (which scans the filesystem) so unit tests can run
    without mounting fake secrets.
    """
    rotator = TokenRotator.__new__(TokenRotator)
    rotator.tokens = list(tokens)
    rotator.current_index = 0
    rotator.token_files = [Path(f"claude_token_{i}.txt") for i in range(len(tokens))]
    return rotator


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    """Ensure each test runs with INSTANCE_NAME=test-worker and clean state."""
    monkeypatch.setenv("INSTANCE_NAME", "test-worker")
    yield


class TestSingleTokenSoftPenalty:
    """Single-token-per-worker setup — current Hetzner deployment."""

    def test_single_token_429_does_not_raise(self):
        """rotate_token() on single-token worker must not raise AllTokensExhausted."""
        rotator = make_rotator(["sk-ant-oat01-token-A"])

        with patch("src.auth.TokenRotator._apply_soft_penalty") as mock_penalty:
            result = rotator.rotate_token()

        assert result == "sk-ant-oat01-token-A"
        mock_penalty.assert_called_once()
        # worker_id is positional, retry_after kwarg
        args, kwargs = mock_penalty.call_args
        assert args[0] == "test-worker"
        assert kwargs.get("retry_after") == 15

    def test_single_token_mark_token_failed_returns_same_token(self):
        """mark_token_failed on single-token worker rotates to itself (no-op)."""
        rotator = make_rotator(["sk-ant-oat01-token-only"])

        with patch("src.auth.TokenRotator._apply_soft_penalty"):
            result = rotator.mark_token_failed("429 rate_limit")

        assert result == "sk-ant-oat01-token-only"
        assert rotator.current_index == 0

    def test_single_token_soft_penalty_calls_rate_limit_tracker(self):
        """_apply_soft_penalty should call rate_limit_tracker.mark_soft_penalty."""
        rotator = make_rotator(["sk-ant-oat01-A"])

        mock_tracker = MagicMock()
        fake_module = MagicMock()
        fake_module.rate_limit_tracker = mock_tracker

        with patch.dict("sys.modules", {"src.claude_cli": fake_module}):
            rotator._apply_soft_penalty("worker1", retry_after=15)

        mock_tracker.mark_soft_penalty.assert_called_once_with("worker1", retry_after=15)


class TestSingleTokenInvalid:
    """401 / token revoked — unrecoverable."""

    def test_mark_token_invalid_raises(self):
        """mark_token_invalid must always raise TokenInvalidError."""
        rotator = make_rotator(["sk-ant-oat01-revoked"])

        with pytest.raises(TokenInvalidError) as exc_info:
            rotator.mark_token_invalid("401 Unauthorized", account="office")

        err = exc_info.value
        assert err.worker_id == "test-worker"
        assert err.account == "office"
        assert "INVALID" in str(err)

    def test_mark_token_invalid_logs_critical(self, caplog):
        """mark_token_invalid emits a CRITICAL log with operator instructions."""
        import logging
        caplog.set_level(logging.CRITICAL)
        rotator = make_rotator(["sk-ant-oat01-bad"])

        with pytest.raises(TokenInvalidError):
            rotator.mark_token_invalid("401 invalid token", account="office")

        text = caplog.text
        assert "INVALID" in text
        assert "test-worker" in text
        assert "Replace" in text
        assert "container restart" in text.lower() or "restart" in text.lower()

    def test_mark_token_invalid_does_not_apply_cooldown(self):
        """401 must NOT mark soft-penalty (no shared bucket with 429)."""
        rotator = make_rotator(["sk-ant-oat01-bad"])

        with patch("src.auth.TokenRotator._apply_soft_penalty") as mock_penalty:
            with pytest.raises(TokenInvalidError):
                rotator.mark_token_invalid("401 Unauthorized", account="office")

        mock_penalty.assert_not_called()


class TestMultiTokenRotation:
    """Multi-token pool — original design intention."""

    def test_multi_token_429_rotates_round_robin(self):
        """rotate_token cycles through tokens when more than one is loaded."""
        rotator = make_rotator(["token-A", "token-B", "token-C"])

        result1 = rotator.rotate_token()
        assert result1 == "token-B"
        assert rotator.current_index == 1

        result2 = rotator.rotate_token()
        assert result2 == "token-C"
        assert rotator.current_index == 2

        # Wrap-around
        result3 = rotator.rotate_token()
        assert result3 == "token-A"
        assert rotator.current_index == 0

    def test_multi_token_does_not_call_soft_penalty(self):
        """Multi-token rotation must NOT touch the rate_limit_tracker."""
        rotator = make_rotator(["token-A", "token-B"])

        with patch("src.auth.TokenRotator._apply_soft_penalty") as mock_penalty:
            rotator.rotate_token()

        mock_penalty.assert_not_called()


class TestNoTokensExhausted:
    """Zero tokens loaded — token file missing entirely."""

    def test_no_tokens_raises_exhausted(self):
        """rotate_token with empty token list raises AllTokensExhausted."""
        rotator = make_rotator([])

        with pytest.raises(AllTokensExhausted) as exc_info:
            rotator.rotate_token()

        assert "No tokens loaded" in str(exc_info.value)

    def test_mark_token_failed_no_tokens_raises_exhausted(self):
        """mark_token_failed with empty token list propagates AllTokensExhausted."""
        rotator = make_rotator([])

        with pytest.raises(AllTokensExhausted):
            rotator.mark_token_failed("any error")
