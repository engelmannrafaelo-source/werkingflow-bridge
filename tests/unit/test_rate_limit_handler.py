"""
Unit tests for claude_cli._handle_rate_limit_event.

Bug context (2026-05-06):
worker4 (WER) took 249 Anthropic-429 hits today. The cross-worker retry
machinery existed (main.py:_cross_worker_retry) but never fired —
@app.exception_handler(RateLimitError) only triggers if a RateLimitError
propagates out, but the rate_limit_event "hit" handler did `continue`
instead of raising. The CLI loop then waited for Anthropic's reset
(mins-hours) until Bridge timeouts.

This test pins the new behavior: hit MUST raise RateLimitError so the
exception flow lands in classify_exception → account_exhausted_error →
BridgeError-handler → _cross_worker_retry.
"""
import os
import sys
import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from src.claude_cli import (
    RateLimitError,
    RateLimitEvent,
    _handle_rate_limit_event,
    rate_limit_tracker,
)


def make_event(status=None, retry_after=None, reset_at=None,
               message="", rl_type="anthropic", resetsAt=None,
               utilization=None, surpassedThreshold=None):
    """Build a RateLimitEvent matching the SDK shape used by the parser."""
    rli = {"status": status, "rateLimitType": rl_type}
    if resetsAt is not None:
        rli["resetsAt"] = resetsAt
    if utilization is not None:
        rli["utilization"] = utilization
    if surpassedThreshold is not None:
        rli["surpassedThreshold"] = surpassedThreshold
    raw = {
        "rate_limit_info": rli,
        "retry_after": retry_after,
        "reset_at": reset_at,
        "message": message,
    }
    return RateLimitEvent(raw)


class TestHeartbeat:
    """heartbeat = SDK polling, request still in progress, no action."""

    def test_status_allowed_no_raise_no_penalty(self):
        ev = make_event(status="allowed")
        with patch.object(rate_limit_tracker, "mark_soft_penalty") as m:
            assert _handle_rate_limit_event(ev, "worker1") is None
            m.assert_not_called()

    def test_status_none_no_raise(self):
        ev = make_event(status=None)
        assert _handle_rate_limit_event(ev, "worker1") is None


class TestWarning:
    """allowed_warning = approaching limit, soft penalty only, no raise."""

    def test_allowed_warning_soft_penalty_no_raise(self):
        ev = make_event(status="allowed_warning", utilization=0.92,
                        surpassedThreshold=0.90)
        with patch.object(rate_limit_tracker, "mark_soft_penalty") as m:
            assert _handle_rate_limit_event(ev, "worker1") is None
            m.assert_called_once_with("worker1", 60)


class TestHit:
    """hit = real Anthropic 429, MUST raise so cross-worker retry fires."""

    def test_hit_with_status_and_reset_raises(self):
        ev = make_event(
            status="weekly_limit_reached",
            retry_after=300,
            reset_at=1778100000,
            rl_type="anthropic_weekly",
        )
        with patch.object(rate_limit_tracker, "mark_soft_penalty"), \
             patch("src.middleware.capacity_lock.get_capacity_lock") as gcl, \
             patch("src.middleware.rolling_metrics.get_rolling_metrics") as grm:
            gcl.return_value = MagicMock()
            grm.return_value = MagicMock()
            with pytest.raises(RateLimitError) as exc_info:
                _handle_rate_limit_event(ev, "worker4")
        err = exc_info.value
        assert "worker4" in str(err)
        assert "anthropic_weekly" in str(err)
        assert err.retry_after_seconds == 300

    def test_hit_with_status_and_retry_after_only_raises(self):
        """status set, no reset_at — retry_after is the only timing hint."""
        ev = make_event(status="rate_limit_exceeded", retry_after=120)
        with patch.object(rate_limit_tracker, "mark_soft_penalty"), \
             patch("src.middleware.capacity_lock.get_capacity_lock"), \
             patch("src.middleware.rolling_metrics.get_rolling_metrics"):
            with pytest.raises(RateLimitError):
                _handle_rate_limit_event(ev, "worker3")

    def test_hit_with_status_and_message_only_raises(self):
        """status set, no retry_after, no reset_at — message body alone."""
        ev = make_event(status="error",
                        message="rate limit exceeded for tier xyz")
        with patch.object(rate_limit_tracker, "mark_soft_penalty"), \
             patch("src.middleware.capacity_lock.get_capacity_lock"), \
             patch("src.middleware.rolling_metrics.get_rolling_metrics"):
            with pytest.raises(RateLimitError):
                _handle_rate_limit_event(ev, "worker3")

    def test_status_none_treated_as_heartbeat_even_with_retry_after(self):
        """status=None is the heartbeat signal — retry_after does not override."""
        ev = make_event(status=None, retry_after=120)
        with patch.object(rate_limit_tracker, "mark_soft_penalty") as m:
            assert _handle_rate_limit_event(ev, "worker3") is None
            m.assert_not_called()

    def test_hit_without_reset_signal_still_raises(self):
        """Even without reset_target, hit must raise so retry can fire.
        capacity_lock just gets skipped (logged as warning)."""
        ev = make_event(status="quota_exceeded", retry_after=None,
                        reset_at=None)
        with patch.object(rate_limit_tracker, "mark_soft_penalty"), \
             patch("src.middleware.capacity_lock.get_capacity_lock") as gcl, \
             patch("src.middleware.rolling_metrics.get_rolling_metrics"):
            cap = MagicMock()
            gcl.return_value = cap
            with pytest.raises(RateLimitError):
                _handle_rate_limit_event(ev, "worker2")
            cap.lock_until.assert_not_called()

    def test_hit_calls_housekeeping_BEFORE_raise(self):
        """soft_penalty + capacity_lock + record_rate_limit must run before
        the raise — they update routing state for FUTURE requests, even when
        the cross-worker retry of THIS request fails."""
        ev = make_event(
            status="weekly_limit_reached",
            retry_after=600,
            reset_at=1778100000,
        )
        with patch.object(rate_limit_tracker, "mark_soft_penalty") as ms, \
             patch("src.middleware.capacity_lock.get_capacity_lock") as gcl, \
             patch("src.middleware.rolling_metrics.get_rolling_metrics") as grm:
            cap = MagicMock()
            gcl.return_value = cap
            metrics = MagicMock()
            grm.return_value = metrics
            with pytest.raises(RateLimitError):
                _handle_rate_limit_event(ev, "worker4")
            ms.assert_called_once_with("worker4", 600)
            cap.lock_until.assert_called_once()
            metrics.record_rate_limit.assert_called_once_with("worker4")
