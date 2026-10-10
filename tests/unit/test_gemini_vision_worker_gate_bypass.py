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
    cache_request_body_dependency,
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
# Adaptive pool admission — exempt ONLY in /v1/chat/completions
# ---------------------------------------------------------------------------
def _limiter(admit: bool):
    limiter = MagicMock()
    limiter.acquire_with_wait = AsyncMock(
        return_value=(admit, "ok" if admit else "full", {}, 0.0)
    )
    return limiter


def test_chat_handler_caches_body_without_admitting():
    """The chat handler admits itself (so it can exempt the Gemini lane);
    swapping back to adaptive_limit_dependency would admit Gemini again."""
    from fastapi.routing import APIRoute

    route = next(
        r for r in src.main.app.routes
        if isinstance(r, APIRoute) and r.path == "/v1/chat/completions"
    )
    deps = [d.call for d in route.dependant.dependencies]
    assert cache_request_body_dependency in deps
    assert adaptive_limit_dependency not in deps


def test_doc_agent_keeps_pool_admission_dependency():
    from fastapi.routing import APIRoute

    route = next(
        r for r in src.main.app.routes
        if isinstance(r, APIRoute) and r.path == "/v1/doc-agent"
    )
    assert adaptive_limit_dependency in [d.call for d in route.dependant.dependencies]


@pytest.mark.asyncio
async def test_doc_agent_with_gemini_tier_still_goes_through_admission():
    """Probe from the BR1 review: DocAgentRequest drops unknown fields, so a
    provider_tier in its body changes nothing about the Claude agent run —
    and must change nothing about its pool admission either. Red on eb1c5a3
    (acquire_with_wait was never called)."""
    body = {
        "question": "Was steht drin?",
        "files": [{"name": "a.txt", "content": "x"}],
        "provider_tier": "gemini-vision",
    }
    req = _request(body)
    limiter = _limiter(True)
    with patch(_LIMITER, return_value=limiter):
        await adaptive_limit_dependency(req)
    limiter.acquire_with_wait.assert_awaited_once()


# ---------------------------------------------------------------------------
# Worker rate-limit / org-lock pre-check in /v1/chat/completions
# ---------------------------------------------------------------------------
class _PassedPrecheck(BaseException):
    """Raised at the first step AFTER the pre-check; BaseException so no
    `except Exception` in the handler can swallow it."""


def _locked_tracker(locked: bool):
    tracker = MagicMock()
    tracker.should_reject_new_request.return_value = locked
    tracker.is_hard_limited.return_value = True
    tracker.get_retry_after.return_value = 600
    return tracker


async def _run_chat(body: dict, *, locked: bool):
    req = _request(body)
    req.state.cached_body_dict = body
    req.state.adaptive_est_tokens = 10
    org_check = MagicMock(return_value=None)
    admission = AsyncMock()
    handler = inspect.unwrap(src.main.chat_completions)
    with patch.object(src.main, "verify_api_key", AsyncMock()), \
         patch.object(src.main, "enforce_pool_admission", admission), \
         patch("src.budget.gate.enforce_budget", AsyncMock()), \
         patch("src.claude_cli.rate_limit_tracker", _locked_tracker(locked)), \
         patch.object(src.main, "_org_disabled_precheck", org_check), \
         patch.object(src.main, "get_tenant_from_request", side_effect=_PassedPrecheck):
        try:
            resp = await handler(ChatCompletionRequest(**body), req, None, None)
        except _PassedPrecheck:
            return "passed", org_check, admission
    return resp, org_check, admission


@pytest.mark.asyncio
async def test_locked_worker_lets_gemini_lane_through():
    result, org_check, admission = await _run_chat(GEMINI_BODY, locked=True)
    assert result == "passed"
    org_check.assert_not_called()
    admission.assert_not_called()


@pytest.mark.asyncio
async def test_locked_worker_still_rejects_claude_call():
    resp, org_check, admission = await _run_chat(CLAUDE_BODY, locked=True)
    assert resp != "passed"
    assert resp.status_code == 429
    reason = json.loads(bytes(resp.body))["error"]["reason"]
    assert reason == "worker_account_rate_limited"
    org_check.assert_called_once()
    admission.assert_awaited_once()


# ---------------------------------------------------------------------------
# The safety net the exemption relies on (BR1 review M2): on a LOCKED worker a
# released gemini-vision call ends in a loud 4xx and never reaches Claude.
# ---------------------------------------------------------------------------
_PIN = "src.routing.user_provider_override.enforce_user_provider_override"


class _ClaudeReached(BaseException):
    """Any Claude/Anthropic execution path was entered."""


async def _run_chat_to_end(body: dict, *, pin=None):
    """Run the real handler past the pre-check on a locked worker. Only the
    outside world is stubbed; every Claude execution path raises."""
    req = _request(body)
    req.state.cached_body_dict = body
    req.state.adaptive_est_tokens = 10

    async def _pin(_request, request_body):
        if pin is None:
            return None
        # What a real operator pin does to the tier (user_provider_override).
        request_body.provider_tier = None
        return pin

    claude = MagicMock(side_effect=_ClaudeReached)
    locked = _locked_tracker(True)
    no_org_block = MagicMock(return_value=None)
    handler = inspect.unwrap(src.main.chat_completions)
    with patch.object(src.main, "verify_api_key", AsyncMock()), \
         patch.object(src.main, "enforce_pool_admission", AsyncMock()), \
         patch("src.budget.gate.enforce_budget", AsyncMock()), \
         patch("src.claude_cli.rate_limit_tracker", locked), \
         patch.object(src.main, "rate_limit_tracker", locked, create=True), \
         patch.object(src.main, "_org_disabled_precheck", no_org_block), \
         patch(_PIN, _pin), \
         patch.object(src.main.claude_cli, "run_completion", claude), \
         patch.object(src.main, "call_anthropic_direct", claude, create=True), \
         patch.object(src.main, "call_bedrock", claude, create=True):
        resp = await handler(ChatCompletionRequest(**body), req, None, None)
    claude.assert_not_called()
    return resp


@pytest.fixture
def gemini_armed(monkeypatch):
    monkeypatch.setenv("BRIDGE_GEMINI_VISION_ENABLED", "true")
    monkeypatch.setenv("GEMINI_VISION_API_KEY", "test-key")


@pytest.mark.asyncio
async def test_locked_worker_gemini_without_image_is_400(gemini_armed):
    resp = await _run_chat_to_end(GEMINI_BODY)
    assert resp.status_code == 400
    code = json.loads(bytes(resp.body))["error"]["code"]
    assert code == "gemini_vision_requires_image"


@pytest.mark.asyncio
async def test_locked_worker_gemini_tier_removed_by_pin_is_409(gemini_armed):
    resp = await _run_chat_to_end(GEMINI_BODY, pin="anthropic")
    assert resp.status_code == 409
    code = json.loads(bytes(resp.body))["error"]["code"]
    assert code == "gemini_vision_tier_overridden"
