"""The Gemini image lane must not be rejected by a worker's Claude account locks.

Defect (BR1, 09.10.2026): a call with provider_tier="gemini-vision" (and the
X-Vision-Provider: gemini header the nginx router reads) reached a dev worker
whose Claude account was Anthropic-rate-limited. The worker pre-check answered
429 worker_account_rate_limited, and nginx @bridge_full relabelled that as
"Anthropic rate limit on selected worker" — although nothing would have gone to
Anthropic. Text calls on the pool were fine at the same time; every image
wizard on dev/staging was blocked.

The bypass is keyed on the body field (the thing that actually routes the
worker to Gemini), so a header-only or plain Claude call keeps every gate.
"""
import sys
from unittest.mock import MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
    "src.identity.routes",
    "src.db.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

import inspect  # noqa: E402
import json  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

import src.main  # noqa: E402
from src.middleware.adaptive_limiter import (  # noqa: E402
    adaptive_limit_dependency,
    is_gemini_vision_request,
)
from src.models import ChatCompletionRequest  # noqa: E402

GEMINI_BODY = {
    "model": "claude-sonnet-5",
    "provider_tier": "gemini-vision",
    "messages": [{"role": "user", "content": "describe"}],
}
_LIMITER = "src.middleware.adaptive_limiter.get_adaptive_limiter"
CLAUDE_BODY = {
    "model": "claude-sonnet-5",
    "messages": [{"role": "user", "content": "describe"}],
}


def _request(body: dict, headers: dict = None):
    req = MagicMock()
    req.state = MagicMock(spec=[])  # bare namespace: getattr(...) misses are real
    req.headers = headers or {}
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    return req


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------
def test_classifier_reads_body_tier():
    req = _request(GEMINI_BODY)
    req.state.cached_body_dict = GEMINI_BODY
    assert is_gemini_vision_request(req) is True


def test_classifier_header_alone_is_not_enough():
    """Header without body tier = Claude path in the worker -> Claude gates stay."""
    req = _request(CLAUDE_BODY, {"X-Vision-Provider": "gemini"})
    req.state.cached_body_dict = CLAUDE_BODY
    assert is_gemini_vision_request(req) is False


def test_classifier_without_cached_body_is_false():
    req = _request(CLAUDE_BODY)
    assert is_gemini_vision_request(req) is False


# ---------------------------------------------------------------------------
# Adaptive pool admission
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pool_admission_skipped_for_gemini_lane():
    req = _request(GEMINI_BODY, {"X-Vision-Provider": "gemini"})
    limiter = MagicMock()
    limiter.acquire_with_wait = AsyncMock(return_value=(False, "full", {}, 0.0))
    with patch(_LIMITER, return_value=limiter):
        await adaptive_limit_dependency(req)
    limiter.acquire_with_wait.assert_not_called()
    assert isinstance(req.state.adaptive_est_tokens, int)  # still measured


@pytest.mark.asyncio
async def test_pool_admission_still_runs_for_claude_call():
    req = _request(CLAUDE_BODY)
    limiter = MagicMock()
    limiter.acquire_with_wait = AsyncMock(return_value=(True, "ok", {}, 0.0))
    with patch(_LIMITER, return_value=limiter):
        await adaptive_limit_dependency(req)
    limiter.acquire_with_wait.assert_awaited_once()


# ---------------------------------------------------------------------------
# Worker rate-limit / org-lock pre-check in /v1/chat/completions
# ---------------------------------------------------------------------------
class _PassedPrecheck(BaseException):
    """Raised at the first step AFTER the pre-check; BaseException so no
    `except Exception` in the handler can swallow it."""


async def _run_chat(body: dict, *, locked: bool):
    req = _request(body)
    req.state.cached_body_dict = body
    req.state.adaptive_est_tokens = 10
    tracker = MagicMock()
    tracker.should_reject_new_request.return_value = locked
    tracker.is_hard_limited.return_value = True
    tracker.get_retry_after.return_value = 600
    org_check = MagicMock(return_value=None)
    handler = inspect.unwrap(src.main.chat_completions)
    with patch.object(src.main, "verify_api_key", AsyncMock()), \
         patch("src.budget.gate.enforce_budget", AsyncMock()), \
         patch("src.claude_cli.rate_limit_tracker", tracker), \
         patch.object(src.main, "_org_disabled_precheck", org_check), \
         patch.object(src.main, "get_tenant_from_request", side_effect=_PassedPrecheck):
        try:
            resp = await handler(ChatCompletionRequest(**body), req, None, None)
        except _PassedPrecheck:
            return "passed", org_check
    return resp, org_check


@pytest.mark.asyncio
async def test_locked_worker_lets_gemini_lane_through():
    result, org_check = await _run_chat(GEMINI_BODY, locked=True)
    assert result == "passed"
    org_check.assert_not_called()


@pytest.mark.asyncio
async def test_locked_worker_still_rejects_claude_call():
    resp, org_check = await _run_chat(CLAUDE_BODY, locked=True)
    assert resp != "passed"
    assert resp.status_code == 429
    reason = json.loads(bytes(resp.body))["error"]["reason"]
    assert reason == "worker_account_rate_limited"
    org_check.assert_called_once()
