"""Tests for the worker-pool saturation signal (src/research_cloud/pool_signal.py).

Combines RateLimitTracker.should_reject_new_request (per-account cooldown)
and AdaptiveLoadLimiter.snapshot() (in-flight token budget) — documented
choice, see pool_signal.py docstring for what was NOT built (cluster-wide
aggregate).
"""
import sys
from unittest.mock import MagicMock as _MagicMock

# src.claude_cli needs claude_code_sdk, unavailable/unneeded in this unit test
# (same stubbing convention as tests/test_research_tracking.py).
for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

from unittest.mock import MagicMock, patch  # noqa: E402

from src.research_cloud.pool_signal import is_worker_pool_saturated  # noqa: E402


def _snapshot(effective_cap_tokens, inflight_tokens):
    return {"effective_cap_tokens": effective_cap_tokens, "inflight_tokens": inflight_tokens}


def test_rate_limited_worker_is_saturated():
    tracker = MagicMock()
    tracker.should_reject_new_request.return_value = True
    limiter = MagicMock()
    limiter.snapshot.return_value = _snapshot(250_000, 0)
    with patch("src.claude_cli.rate_limit_tracker", tracker), patch(
        "src.middleware.adaptive_limiter.get_adaptive_limiter", return_value=limiter
    ):
        assert is_worker_pool_saturated() is True


def test_inflight_at_effective_cap_is_saturated():
    tracker = MagicMock()
    tracker.should_reject_new_request.return_value = False
    limiter = MagicMock()
    limiter.snapshot.return_value = _snapshot(250_000, 250_000)
    with patch("src.claude_cli.rate_limit_tracker", tracker), patch(
        "src.middleware.adaptive_limiter.get_adaptive_limiter", return_value=limiter
    ):
        assert is_worker_pool_saturated() is True


def test_effective_cap_collapsed_to_zero_is_saturated():
    tracker = MagicMock()
    tracker.should_reject_new_request.return_value = False
    limiter = MagicMock()
    limiter.snapshot.return_value = _snapshot(0, 0)
    with patch("src.claude_cli.rate_limit_tracker", tracker), patch(
        "src.middleware.adaptive_limiter.get_adaptive_limiter", return_value=limiter
    ):
        assert is_worker_pool_saturated() is True


def test_healthy_pool_is_not_saturated():
    tracker = MagicMock()
    tracker.should_reject_new_request.return_value = False
    limiter = MagicMock()
    limiter.snapshot.return_value = _snapshot(250_000, 10_000)
    with patch("src.claude_cli.rate_limit_tracker", tracker), patch(
        "src.middleware.adaptive_limiter.get_adaptive_limiter", return_value=limiter
    ):
        assert is_worker_pool_saturated() is False
