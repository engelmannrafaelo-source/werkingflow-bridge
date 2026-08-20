"""
Unit tests for the per-email failed-login throttle (login_throttle.py).

Guards security-audit-live-findings-20260818.md L10c/B.4: POST /v1/auth/login
had no brute-force protection at all. See login_throttle.py's docstring for
why this is keyed on the target email rather than caller IP (every app calls
the Bridge server-side from a shared Vercel egress IP — IP-based limiting
would risk cross-tenant lockout, not protect against brute force).
"""
from __future__ import annotations

import os

import pytest

from src.identity import login_throttle


@pytest.fixture(autouse=True)
def _clean_state():
    """Isolate every test — the module holds process-global state."""
    login_throttle._reset_for_tests()
    yield
    login_throttle._reset_for_tests()


@pytest.fixture
def tight_limits(monkeypatch):
    """3 attempts / 60s window — easy to drive through in a test."""
    monkeypatch.setenv("BRIDGE_LOGIN_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("BRIDGE_LOGIN_LOCKOUT_WINDOW_S", "60")


class TestNotLockedByDefault:
    def test_unknown_email_not_locked(self):
        assert login_throttle.is_locked_out("nobody@example.com") is False

    def test_none_email_not_locked(self):
        assert login_throttle.is_locked_out(None) is False

    def test_empty_email_not_locked(self):
        assert login_throttle.is_locked_out("") is False


class TestLockoutAfterMaxAttempts:
    def test_locks_out_after_max_failures(self, tight_limits):
        email = "victim@example.com"
        for _ in range(3):
            assert login_throttle.is_locked_out(email) is False
            login_throttle.record_failure(email)
        assert login_throttle.is_locked_out(email) is True

    def test_stays_unlocked_below_max_failures(self, tight_limits):
        email = "victim@example.com"
        login_throttle.record_failure(email)
        login_throttle.record_failure(email)
        assert login_throttle.is_locked_out(email) is False

    def test_email_is_normalized_case_and_whitespace(self, tight_limits):
        for _ in range(3):
            login_throttle.record_failure("  Victim@Example.com  ")
        assert login_throttle.is_locked_out("victim@example.com") is True

    def test_lockout_is_scoped_to_the_target_email(self, tight_limits):
        """A brute-force sweep across many emails at attacker-chosen IPs must
        not lock out an unrelated account."""
        for _ in range(3):
            login_throttle.record_failure("attacked@example.com")
        assert login_throttle.is_locked_out("attacked@example.com") is True
        assert login_throttle.is_locked_out("innocent-bystander@example.com") is False


class TestSuccessClearsLockout:
    def test_success_resets_the_counter(self, tight_limits):
        email = "user@example.com"
        login_throttle.record_failure(email)
        login_throttle.record_failure(email)
        login_throttle.record_success(email)
        # Two more failures after a success must NOT trip a 3-attempt lockout.
        login_throttle.record_failure(email)
        login_throttle.record_failure(email)
        assert login_throttle.is_locked_out(email) is False

    def test_success_on_never_failed_email_is_a_noop(self):
        login_throttle.record_success("fresh@example.com")
        assert login_throttle.is_locked_out("fresh@example.com") is False


class TestWindowExpiry:
    def test_lockout_clears_after_window_expires(self, monkeypatch, tight_limits):
        email = "victim@example.com"
        t = [1000.0]
        monkeypatch.setattr(login_throttle.time, "monotonic", lambda: t[0])

        for _ in range(3):
            login_throttle.record_failure(email)
        assert login_throttle.is_locked_out(email) is True

        t[0] += 61.0  # past the 60s window
        assert login_throttle.is_locked_out(email) is False

    def test_failure_after_window_expiry_starts_a_fresh_window(self, monkeypatch, tight_limits):
        email = "victim@example.com"
        t = [1000.0]
        monkeypatch.setattr(login_throttle.time, "monotonic", lambda: t[0])

        login_throttle.record_failure(email)
        login_throttle.record_failure(email)
        t[0] += 61.0
        login_throttle.record_failure(email)  # window reset — this is failure #1 again
        assert login_throttle.is_locked_out(email) is False


class TestFailOpen:
    def test_is_locked_out_never_raises(self, monkeypatch):
        monkeypatch.setattr(
            login_throttle, "_normalize", lambda e: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        assert login_throttle.is_locked_out("x@example.com") is False

    def test_record_failure_never_raises(self, monkeypatch):
        monkeypatch.setattr(
            login_throttle, "_normalize", lambda e: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        login_throttle.record_failure("x@example.com")  # must not raise

    def test_record_success_never_raises(self, monkeypatch):
        monkeypatch.setattr(
            login_throttle, "_normalize", lambda e: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        login_throttle.record_success("x@example.com")  # must not raise


class TestMemoryBound:
    def test_tracked_dict_is_pruned_when_over_capacity(self, monkeypatch):
        monkeypatch.setenv("BRIDGE_LOGIN_MAX_ATTEMPTS", "5")
        monkeypatch.setenv("BRIDGE_LOGIN_LOCKOUT_WINDOW_S", "0.01")
        monkeypatch.setenv("BRIDGE_LOGIN_THROTTLE_MAX_TRACKED", "2")

        t = [1000.0]
        monkeypatch.setattr(login_throttle.time, "monotonic", lambda: t[0])

        login_throttle.record_failure("a@example.com")
        t[0] += 1.0  # a@example.com's window has now expired
        login_throttle.record_failure("b@example.com")
        # At capacity (2 tracked) — the next call must prune the expired
        # entry instead of growing unbounded.
        login_throttle.record_failure("c@example.com")

        assert "a@example.com" not in login_throttle._attempts
