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
# The safety net the exemption relies on (BR1 review M2, BR1R2 M3): on a LOCKED
# worker a released gemini-vision call either really goes to Gemini or ends in
# a loud 4xx — it never reaches a Claude path. Every way the resolved backend
# can end up non-Gemini (operator pin anthropic / anthropic_direct / bedrock,
# the app-tier rule) runs through the REAL routing code; only the outside world
# is stubbed. The Claude callables are patched at their MODULE SOURCE: the
# handler imports call_anthropic_direct / call_bedrock locally, so a patch on
# src.main would never be hit (BR1R2 M4).
# ---------------------------------------------------------------------------
_PIN_CONFIG = "src.routing.user_provider_override.get_user_provider_config"
_TIER_POLICY = "src.routing.app_tier_policy.resolve_app_tier_policy"
_BEDROCK_CREDS = "src.routing.backend_router.bedrock_credential_manager"
_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2O"
    "UAAAAABJRU5ErkJggg=="
)
GEMINI_IMAGE_BODY = {
    "model": "claude-sonnet-5",
    "provider_tier": "gemini-vision",
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": _PNG}},
    ]}],
}


class _ClaudeReached(BaseException):
    """Any Claude/Anthropic/Bedrock execution path was entered."""


class _GeminiReached(BaseException):
    """The Gemini vision call was entered (the one legitimate destination)."""


def _bedrock_creds():
    creds = MagicMock()
    creds.validate.return_value = (True, {"errors": []})
    creds.default_region = "eu-central-1"
    creds.get_bedrock_env_vars.return_value = {}
    return creds


async def _run_chat_to_end(body: dict, *, pin_config=None, tier_policy=None,
                           app_env="production"):
    """Run the real handler past the pre-check on a locked worker. Returns
    (response, claude_mock, vision_mock); Claude reached = AssertionError."""
    req = _request(body, {
        "X-User-ID": "user-br1",
        "X-App-ID": "br1-gate-test",
        "X-App-Env": app_env,
    })
    req.state.cached_body_dict = body
    req.state.adaptive_est_tokens = 10

    claude = MagicMock(side_effect=_ClaudeReached)
    claude_async = AsyncMock(side_effect=_ClaudeReached)
    vision = AsyncMock(side_effect=_GeminiReached)
    locked = _locked_tracker(True)
    no_org_block = MagicMock(return_value=None)
    admission = AsyncMock()
    handler = inspect.unwrap(src.main.chat_completions)
    with patch.object(src.main, "verify_api_key", AsyncMock()), \
         patch.object(src.main, "enforce_pool_admission", admission), \
         patch("src.budget.gate.enforce_budget", AsyncMock()), \
         patch("src.claude_cli.rate_limit_tracker", locked), \
         patch.object(src.main, "_org_disabled_precheck", no_org_block), \
         patch(_PIN_CONFIG, AsyncMock(return_value=pin_config)), \
         patch(_TIER_POLICY, AsyncMock(return_value=tier_policy)), \
         patch(_BEDROCK_CREDS, _bedrock_creds()), \
         patch.object(src.main.claude_cli, "run_completion", claude), \
         patch("src.providers.anthropic_direct.call_anthropic_direct", claude_async), \
         patch("src.bedrock_service.call_bedrock", claude_async), \
         patch("src.bedrock_service.stream_bedrock", claude), \
         patch.object(src.main, "check_and_route_vision", vision):
        try:
            resp = await handler(ChatCompletionRequest(**body), req, None, None)
        except _ClaudeReached:
            raise AssertionError(
                "gemini-vision body reached a Claude path with the worker locks "
                "skipped"
            ) from None
        except _GeminiReached:
            resp = "gemini"
    # The lane really was released — otherwise these tests prove nothing.
    admission.assert_not_called()
    no_org_block.assert_not_called()
    locked.should_reject_new_request.assert_not_called()
    claude.assert_not_called()
    claude_async.assert_not_called()
    return resp, vision


@pytest.fixture
def gemini_armed(monkeypatch):
    monkeypatch.setenv("BRIDGE_GEMINI_VISION_ENABLED", "true")
    monkeypatch.setenv("GEMINI_VISION_API_KEY", "test-key")
    # claude-direct-notools is servable here, so the pin / app-tier rule
    # really resolve to ANTHROPIC_DIRECT instead of failing on a missing key.
    monkeypatch.setenv("ANTHROPIC_VISION_API_KEY", "test-key")


def _assert_overridden_409(resp):
    assert resp != "gemini"
    assert resp.status_code == 409
    code = json.loads(bytes(resp.body))["error"]["code"]
    assert code == "gemini_vision_tier_overridden"


@pytest.mark.asyncio
async def test_locked_worker_gemini_without_image_is_400(gemini_armed):
    resp, vision = await _run_chat_to_end(GEMINI_BODY, app_env="preview")
    assert resp.status_code == 400
    code = json.loads(bytes(resp.body))["error"]["code"]
    assert code == "gemini_vision_requires_image"
    vision.assert_not_called()


@pytest.mark.asyncio
async def test_locked_worker_gemini_with_image_goes_to_gemini(gemini_armed):
    """The legitimate case: the released call ends at the Gemini vision call."""
    resp, vision = await _run_chat_to_end(GEMINI_IMAGE_BODY, app_env="preview")
    assert resp == "gemini"
    vision.assert_awaited_once()
    assert vision.await_args.kwargs["target"] == src.main.VISION_TARGET_GEMINI


@pytest.mark.asyncio
async def test_locked_worker_gemini_tier_removed_by_pin_is_409(gemini_armed):
    resp, _ = await _run_chat_to_end(
        GEMINI_IMAGE_BODY, pin_config={"provider": "anthropic"}
    )
    _assert_overridden_409(resp)


@pytest.mark.asyncio
async def test_locked_worker_gemini_tier_pin_anthropic_direct_is_409(gemini_armed):
    """Pin anthropic_direct (prod) rewrites the tier to claude-direct-notools.
    Red on 3ee2e95: call_anthropic_direct was reached, the 409 came too late."""
    resp, _ = await _run_chat_to_end(
        GEMINI_IMAGE_BODY, pin_config={"provider": "anthropic_direct"}
    )
    _assert_overridden_409(resp)


@pytest.mark.asyncio
async def test_locked_worker_gemini_tier_pin_bedrock_is_409(gemini_armed):
    """Pin bedrock (prod) sets backend=BEDROCK. Red on 3ee2e95: call_bedrock
    was reached, the 409 came too late."""
    resp, _ = await _run_chat_to_end(
        GEMINI_IMAGE_BODY, pin_config={"provider": "bedrock", "region": "eu-central-1"}
    )
    _assert_overridden_409(resp)


@pytest.mark.asyncio
async def test_locked_worker_gemini_tier_app_tier_rule_is_409(gemini_armed):
    """App-tier rule (no pin) forces claude-direct-notools. Red on 3ee2e95:
    call_anthropic_direct was reached."""
    from src.routing.app_tier_policy import AppTierPolicy

    policy = AppTierPolicy(target_tier="claude-direct-notools", billing_account=None)
    resp, _ = await _run_chat_to_end(GEMINI_IMAGE_BODY, tier_policy=policy)
    _assert_overridden_409(resp)
