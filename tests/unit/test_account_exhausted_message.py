"""The 429 must name the limit it actually hit.

On 2026-09-03 every prod worker answered "All worker accounts have reached
their weekly Anthropic limit. Retry in ~22 minutes." while the accounts stood
at ~30 % of the WEEK and 100 % of the 5-hour session window, nine minutes from
reset. The sentence was wrong about the cause and therefore wrong about the
remedy: two sessions went looking for an architecture defect instead of
waiting nine minutes. A limit name is an instruction about what to do next.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")

import pytest

from src.middleware.bridge_error import account_exhausted_error


def _err(response) -> dict:
    return json.loads(bytes(response.body).decode())["error"]


def test_session_window_is_named_as_such():
    err = _err(account_exhausted_error(retry_after_s=540, limit_window="session_window"))
    assert "5-hour" in err["message"]
    assert "weekly" not in err["message"].lower()
    assert err["limit_window"] == "session_window"
    assert "~9 minutes" in err["message"]


def test_weekly_window_is_named_as_such():
    err = _err(account_exhausted_error(retry_after_s=3600, limit_window="weekly_window"))
    assert "weekly" in err["message"].lower()
    assert err["limit_window"] == "weekly_window"


def test_unknown_window_says_unknown_instead_of_guessing_weekly():
    """The old default. It must never come back: claiming 'weekly' when nobody
    measured which window ran out is exactly the failure above."""
    err = _err(account_exhausted_error(retry_after_s=600))
    assert "weekly" not in err["message"].lower()
    assert "unknown" in err["message"].lower()
    assert err["limit_window"] == "unknown"


def test_status_and_retry_contract_unchanged():
    resp = account_exhausted_error(retry_after_s=300, limit_window="session_window")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "300"
    err = _err(resp)
    assert err["retryable"] is True
    # Stable panel identifier — renaming it would break aggregation on a
    # dashboard for a wording fix. The honest window lives in limit_window.
    assert err["reason"] == "worker_account_weekly_exhausted"


@pytest.mark.parametrize("retry_after", [0, 30, 59])
def test_sub_minute_waits_do_not_read_as_zero_minutes(retry_after):
    """Integer minutes floor to 0 below a minute; "retry in ~0 minutes" reads
    as "never" or as a bug. Round up to the smallest honest instruction."""
    err = _err(account_exhausted_error(retry_after_s=retry_after))
    assert "~1 minutes" in err["message"]
