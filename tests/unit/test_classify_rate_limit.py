"""
Unit tests for classify_exception(RateLimitError) → account_exhausted.

Bug context: classify_exception is called from main.py:2862 when chat_completions'
outer except catches an exception. RateLimitError used to fall through to
the generic markers and end up classified as `internal_error` (bridge_internal/
internal). The BridgeError-handler at main.py:4789 only triggers cross-worker
retry for `bridge_account/account_exhausted` or `bridge_internal/throttle|
queue_timeout` — so internal-classified RateLimitErrors skipped retry entirely.

Fix: classify_exception detects RateLimitError explicitly and returns
account_exhausted_error so the BridgeError-handler runs _cross_worker_retry.
"""
import os
import sys
import json
import pytest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from src.claude_cli import RateLimitError
from src.middleware.bridge_error import classify_exception


def _envelope(response):
    """Decode a JSONResponse body to inspect the error envelope."""
    return json.loads(response.body)["error"]


class TestClassifyRateLimitError:
    def test_rle_with_retry_after_uses_that_value(self):
        err = RateLimitError("[worker4] Anthropic limit hit",
                             retry_after_seconds=600)
        resp = classify_exception(err)
        env = _envelope(resp)
        assert env["source"] == "bridge_account"
        assert env["bridge_type"] == "account_exhausted"
        assert env["retry_after_s"] == 600

    def test_rle_with_reset_time_calculates_retry(self):
        future = datetime.now() + timedelta(seconds=900)
        err = RateLimitError("[worker3] limit", reset_time=future)
        resp = classify_exception(err)
        env = _envelope(resp)
        assert env["source"] == "bridge_account"
        assert env["bridge_type"] == "account_exhausted"
        assert env["retry_after_s"] >= 60  # _calculate_retry_after floors at 60

    def test_rle_no_retry_info_uses_default(self):
        err = RateLimitError("[worker2] limit")
        resp = classify_exception(err)
        env = _envelope(resp)
        assert env["source"] == "bridge_account"
        assert env["bridge_type"] == "account_exhausted"
        # Default 3600s when neither retry_after nor reset_time provided
        assert env["retry_after_s"] == 3600


class TestNonRateLimitErrorsUnchanged:
    """Make sure the new RateLimitError special-case doesn't disturb existing
    classifications — regression guard."""

    def test_timeout_still_upstream_timeout(self):
        err = TimeoutError("read timeout after 30s")
        resp = classify_exception(err)
        env = _envelope(resp)
        assert env["source"] == "upstream_network"

    def test_generic_exception_still_internal(self):
        err = ValueError("something internal broke")
        resp = classify_exception(err)
        env = _envelope(resp)
        assert env["source"] == "bridge_internal"
        assert env["bridge_type"] == "internal"
