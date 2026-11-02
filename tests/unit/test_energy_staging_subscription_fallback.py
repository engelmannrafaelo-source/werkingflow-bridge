"""Energy staging subscription fallback (Rafael 01.11.2026) — no network.

Pool first; on the dev bridge only, a missing/unfundable pool hands Energy to
the normal subscription path. Never a paid route; prod stays strict.
"""
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

for name in ['claude_code_sdk', 'claude_code_sdk._errors', 'claude_code_sdk._internal',
             'claude_code_sdk._internal.client', 'src.identity.routes', 'src.db.client']:
    if name not in sys.modules:
        sys.modules[name] = MagicMock()

import src.main as main  # noqa: E402
from src.models import BackendType, ChatCompletionRequest, ResearchRequest, ResearchResponse  # noqa: E402
from src.providers import credit_pool, funding_policy as fp  # noqa: E402

SWITCH = fp.FALLBACK_SWITCH_ENV


def _req(headers=None, client=("10.0.0.1", 1234)):
    from starlette.requests import Request
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {"X-App-ID": "werking-energy"}).items()]
    return Request({'type': 'http', 'method': 'POST', 'path': '/v1/chat/completions',
                    'headers': raw, 'client': client})


def _body(**extra):
    return ChatCompletionRequest(**{'model': 'claude-sonnet-5-5', 'enable_tools': False,
                                    'messages': [{'role': 'user', 'content': 'test'}], **extra})


@pytest.fixture
def dev_bridge(monkeypatch):
    monkeypatch.setenv(SWITCH, "1")
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "dev")


@pytest.fixture
def prod_bridge(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "prod")


# --- Kennung -------------------------------------------------------------

@pytest.mark.parametrize("switch,origin,allowed", [
    (None, None, False), (None, "dev", False), ("1", None, False), ("1", "", False),
    ("1", "prod", False), ("true", "PROD", False), ("0", "dev", False), ("", "dev", False),
    ("1", "staging", False), ("1", "dev", True), (" TRUE ", " Dev ", True),
])
def test_allowed_needs_switch_and_dev_identity(monkeypatch, switch, origin, allowed):
    for env, val in ((SWITCH, switch), ("BRIDGE_ORIGIN_ID", origin)):
        if val is None:
            monkeypatch.delenv(env, raising=False)
        else:
            monkeypatch.setenv(env, val)
    assert fp.staging_subscription_fallback_allowed() is allowed


def test_activation_refused_without_identity(prod_bridge):
    req = _req()
    with pytest.raises(HTTPException) as e:
        fp.activate_subscription_fallback(req, fp.CAUSE_POOL_MISSING)
    assert e.value.status_code == 503 and not fp.subscription_fallback_active(req)


@pytest.mark.parametrize("headers", [
    # Railway staging backend (staging-energy): only the app id, no worker-only
    # headers, no tenant, foreign source address.
    {"X-App-ID": "werking-energy"},
    {"x-app-id": " Werking-Energy "},
    # Client-sent environment/origin markers must not switch anything.
    {"X-App-ID": "werking-energy", "X-App-Env": "production", "X-Bridge-Origin": "prod"},
])
def test_railway_staging_caller_gets_same_subscription_route(dev_bridge, headers):
    assert fp.fallback_eligible(_req(headers, client=("35.0.0.9", 443)))


def test_request_headers_cannot_enable_fallback_on_prod(prod_bridge):
    req = _req({"X-App-ID": "werking-energy", "X-App-Env": "staging", "X-Bridge-Origin": "dev",
                SWITCH: "1"})
    assert not fp.fallback_eligible(req)


def test_other_apps_never_eligible(dev_bridge):
    assert not fp.fallback_eligible(_req({"X-App-ID": "werking-report"}))
    assert not fp.fallback_eligible(None)


# --- Chat: Umschaltung ---------------------------------------------------

@pytest.mark.asyncio
async def test_chat_pool_missing_runs_subscription(dev_bridge):
    req, seen = _req(), []

    async def kern(body, request, credentials, _adaptive=None):
        seen.append(fp.fallback_cause(request))
        return {"choices": []}
    with patch.object(credit_pool, "enabled", return_value=False), patch.object(main, "_chat_completions_lauf", kern):
        result = await main._chat_completions_finanziert(_body(), req, None)
    assert seen == [fp.CAUSE_POOL_MISSING]
    assert result["x_funding"] == {"funding_policy": "subscription_staging_fallback",
                                   "automatic_paid_fallback": False,
                                   "funding_fallback_cause": "credit_pool_missing"}


@pytest.mark.asyncio
async def test_chat_pool_exhausted_reruns_original_body_on_subscription(dev_bridge):
    req, calls = _req(), []

    async def kern(body, request, credentials, _adaptive=None):
        calls.append((body.provider_tier, fp.fallback_cause(request)))
        if len(calls) == 1:
            body.provider_tier = "claude-direct-notools"  # pool attempt mutates the body
            request.state.api_reserve_retry = True
            raise credit_pool.PoolUnavailable(429, "No eligible Anthropic API credit budget available")
        assert request.state.api_reserve_retry is False
        return {"choices": []}
    with patch.object(credit_pool, "enabled", return_value=True), patch.object(main, "_chat_completions_lauf", kern):
        result = await main._chat_completions_finanziert(_body(), req, None)
    assert calls == [(None, None), (None, fp.CAUSE_POOL_UNAVAILABLE)]
    assert result["x_funding"]["funding_policy"] == "subscription_staging_fallback"


@pytest.mark.asyncio
async def test_chat_pool_success_stays_promotional(dev_bridge):
    kern = AsyncMock(return_value={"choices": []})
    with patch.object(credit_pool, "enabled", return_value=True), patch.object(main, "_chat_completions_lauf", kern):
        result = await main._chat_completions_finanziert(_body(), _req(), None)
    kern.assert_awaited_once()
    assert result["x_funding"] == {"funding_policy": "promotional_only", "automatic_paid_fallback": False}


@pytest.mark.asyncio
async def test_chat_other_failures_are_not_rerouted(dev_bridge):
    kern = AsyncMock(side_effect=HTTPException(400, "credit balance or request problem"))
    with patch.object(credit_pool, "enabled", return_value=True), patch.object(main, "_chat_completions_lauf", kern):
        with pytest.raises(HTTPException) as e:
            await main._chat_completions_finanziert(_body(), _req(), None)
    assert e.value.status_code == 400 and kern.await_count == 1


# --- Chat: Prod/ohne Kennung bleibt strikt ------------------------------

@pytest.mark.asyncio
async def test_prod_pool_exhausted_stays_strict(prod_bridge):
    kern = AsyncMock(side_effect=credit_pool.PoolUnavailable(429, "No eligible"))
    with patch.object(credit_pool, "enabled", return_value=True), patch.object(main, "_chat_completions_lauf", kern):
        with pytest.raises(HTTPException) as e:
            await main._chat_completions_finanziert(_body(), _req(), None)
    assert e.value.status_code == 429 and kern.await_count == 1


@pytest.mark.asyncio
async def test_prod_pool_missing_still_503_before_subscription(prod_bridge):
    admission = AsyncMock(side_effect=AssertionError("subscription reached"))
    with patch.object(credit_pool, "enabled", return_value=False), patch.object(main, "enforce_pool_admission", admission):
        with pytest.raises(HTTPException) as e:
            await main._chat_completions_finanziert(_body(), _req(), None)
    assert e.value.status_code == 503 and "promotional_only" in str(e.value.detail)
    admission.assert_not_awaited()


# --- Chat: Abo-Lauf nutzt den Limiter, nie einen bezahlten Weg ----------

@pytest.mark.asyncio
async def test_fallback_goes_through_subscription_admission(dev_bridge):
    stop = HTTPException(429, "limiter says no")
    admission = AsyncMock(side_effect=stop)
    with patch.object(credit_pool, "enabled", return_value=False), patch.object(main, "enforce_pool_admission", admission):
        with pytest.raises(HTTPException) as e:
            await main._chat_completions_finanziert(_body(provider_tier="claude-direct-notools"), _req(), None)
    assert e.value is stop
    admission.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"provider_tier": "openrouter"}, {"provider_tier": "gemini-vision"},
                                   {"enable_tools": True}, {"session_id": "old"},
                                   {"messages": [{"role": "user", "content": [
                                       {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]}])
async def test_fallback_refuses_paid_or_unsupported_shapes(dev_bridge, extra):
    admission = AsyncMock(side_effect=AssertionError("must refuse before admission"))
    with patch.object(credit_pool, "enabled", return_value=False), patch.object(main, "enforce_pool_admission", admission):
        with pytest.raises(HTTPException) as e:
            await main._chat_completions_finanziert(_body(**extra), _req(), None)
    assert e.value.status_code == 409 and "no paid fallback" in str(e.value.detail)


def test_automatic_chain_has_no_paid_tier_without_pool(monkeypatch):
    from src.providers.fallback import get_fallback_tiers
    monkeypatch.setenv('ANTHROPIC_VISION_API_KEY', 'paid')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'paid')
    with patch.object(credit_pool, "enabled", return_value=False):
        assert get_fallback_tiers('claude-premium', tools_required=False) == ['claude-premium']


# --- Pool: PoolUnavailable nur fuer "Pool kann nicht zahlen" -------------

@pytest.mark.asyncio
async def test_pool_unavailable_classes(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_CREDIT_POOL_URL", raising=False)
    with pytest.raises(credit_pool.PoolUnavailable) as e:
        await credit_pool.coordinator("GET", "/accounts")
    assert e.value.status_code == 503 and isinstance(e.value, HTTPException)

    async def no_accounts(method, path, data=None):
        return {"accounts": []}
    monkeypatch.setattr(credit_pool, "coordinator", no_accounts)
    with pytest.raises(credit_pool.PoolUnavailable) as e:
        await credit_pool.post_messages({"model": "m", "max_tokens": 1, "messages": []}, 5)
    assert e.value.status_code == 429


@pytest.mark.asyncio
async def test_pinned_account_loss_is_not_pool_unavailable(monkeypatch):
    async def coord(method, path, data=None):
        return {"accounts": [{"id": "b", "org_id": "o", "grant_id": "g", "api_key": "k"}]}
    monkeypatch.setattr(credit_pool, "coordinator", coord)
    with pytest.raises(credit_pool.PinnedAccountLost) as e:
        await credit_pool.post_messages({"model": "m", "max_tokens": 1, "messages": []}, 5, pin={"account_id": "a"})
    assert not isinstance(e.value, credit_pool.PoolUnavailable)


# --- Ledger --------------------------------------------------------------

def test_subscription_rows_book_zero_real_cost():
    from src.activity.ai_call_writer import resolve_ledger_cost
    from src.activity.providers import PROVIDER_ANTHROPIC
    assert resolve_ledger_cost("subscription", PROVIDER_ANTHROPIC, 1.23) == ("flat_rate_estimated", 0.0)
    # Documented limit: pay_per_token tenants book real == hypothetical.
    assert resolve_ledger_cost("pay_per_token", PROVIDER_ANTHROPIC, 1.23) == ("pay_per_token", 1.23)


class _Stop(BaseException):
    pass


async def _persist_once(spooled):
    from src.activity import ai_call_writer, ledger_spool
    from src.activity.providers import PROVIDER_ANTHROPIC

    def append(uid, record):
        spooled.update(record)
        raise _Stop()
    with patch.object(ledger_spool, "spool_enabled", return_value=True), \
         patch.object(ledger_spool, "append_call", append):
        with pytest.raises(_Stop):
            await ai_call_writer.persist_ai_call_activity(
                app_id="werking-energy", user_id="u", agent_id=None, workflow_id=None, model="m",
                input_tokens=1, output_tokens=1, status="success", duration_ms=1,
                provider=PROVIDER_ANTHROPIC, provider_meta={"usage_source": "cli"})


@pytest.mark.asyncio
async def test_ledger_row_carries_funding_marker(dev_bridge):
    import asyncio
    spooled = {}

    async def run():  # own task = own context, nothing leaks into other tests
        fp.activate_subscription_fallback(_req(), fp.CAUSE_POOL_UNAVAILABLE)
        await _persist_once(spooled)
    await asyncio.create_task(run())
    assert spooled["provider_meta"] == {"funding_policy": "subscription_staging_fallback",
                                        "funding_fallback_cause": "credit_pool_unavailable",
                                        "automatic_paid_fallback": False, "usage_source": "cli"}


@pytest.mark.asyncio
async def test_ledger_row_unchanged_without_activation():
    import asyncio
    spooled = {}
    await asyncio.create_task(_persist_once(spooled))
    assert spooled["provider_meta"] == {"usage_source": "cli"}


# --- Recherche ----------------------------------------------------------

@pytest.mark.asyncio
async def test_research_executor_flags_pool_unavailable(monkeypatch):
    from src.research_cloud.executor import ResearchCloudExecutorError, run_research_cloud
    from src.research_cloud.library import LibraryConfig
    from src.research_cloud.models import ResearchCloudConfig
    from src.research_cloud.perplexity import PerplexityConfig
    monkeypatch.setenv('ANTHROPIC_CREDIT_POOL_URL', 'http://pool')
    for exc, flag in ((credit_pool.PoolUnavailable(429, "No eligible"), True), (HTTPException(400, "bad"), False)):
        monkeypatch.setattr(credit_pool, 'post_messages', AsyncMock(side_effect=exc))
        with pytest.raises(ResearchCloudExecutorError) as e:
            await run_research_cloud('q', 's', config=ResearchCloudConfig(promotional_only=True),
                                     library_config=LibraryConfig(enabled=False),
                                     perplexity_config=PerplexityConfig(enabled=False))
        assert e.value.pool_unavailable is flag


def _rbody():
    return ResearchRequest(query="q", model="claude-sonnet-5-5")


@pytest.mark.asyncio
async def test_research_cloud_pool_unavailable_hands_over_to_subscription(dev_bridge):
    fallback = AsyncMock(return_value=ResearchResponse(status="success", query="q", model="m"))
    with patch.object(main, "_execute_research_cloud_impl", AsyncMock(side_effect=main._ResearchPoolUnavailable("x"))), \
         patch.object(main, "_research_subscription_fallback", fallback):
        await main._execute_research_cloud_with_pool_fallback(_req(), _rbody())
    fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_research_subscription_fallback_uses_admission_and_cli(dev_bridge):
    seen = {}

    async def impl(body, backend_config, attribution_ctx=None, request=None):
        seen["backend"] = backend_config.backend
        return ResearchResponse(status="success", query=body.query, model=body.model)
    with patch.object(main, "_admit_research_to_pool", AsyncMock(return_value=None)) as admit, \
         patch.object(main, "_execute_research_impl", impl):
        result = await main._research_subscription_fallback(_req(), _rbody(), grund="test")
    admit.assert_awaited_once()
    assert seen["backend"] == BackendType.ANTHROPIC
    assert result.funding_policy == "subscription_staging_fallback"


@pytest.mark.asyncio
async def test_research_subscription_fallback_respects_limiter(dev_bridge):
    from fastapi.responses import JSONResponse
    impl = AsyncMock(side_effect=AssertionError("must not run without capacity"))
    with patch.object(main, "_admit_research_to_pool", AsyncMock(return_value=JSONResponse(status_code=429, content={}))), \
         patch.object(main, "_execute_research_impl", impl):
        result = await main._research_subscription_fallback(_req(), _rbody(), grund="test")
    assert result.status == "error" and result.retryable is True
    impl.assert_not_awaited()


def test_research_gate_refuses_non_cli_backend(dev_bridge):
    req = _req()
    fp.activate_subscription_fallback(req, fp.CAUSE_POOL_MISSING)
    for backend, refused in ((BackendType.BEDROCK, True), (BackendType.ANTHROPIC_DIRECT, True),
                             (BackendType.ANTHROPIC, False)):
        out = main._research_subscription_gate(req, SimpleNamespace(backend=backend), _rbody())
        assert (out is not None) is refused
    assert main._research_subscription_gate(_req(), SimpleNamespace(backend=BackendType.BEDROCK), _rbody()) is None


@pytest.mark.asyncio
async def test_research_cloud_prod_pool_unavailable_stays_error(prod_bridge, monkeypatch):
    """Without the identity the executor error is a normal error response."""
    from src.research_cloud.executor import ResearchCloudExecutorError
    err = ResearchCloudExecutorError("promotional_only stopped: No eligible", status_code=429)
    err.pool_unavailable = True
    monkeypatch.setenv('ANTHROPIC_CREDIT_POOL_URL', 'http://pool')
    with patch("src.research_cloud.executor.run_research_cloud", AsyncMock(side_effect=err)), \
         patch("src.research_cloud.anonymize_gate.anonymize_query_for_cloud", AsyncMock(return_value="q")), \
         patch("src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock()), \
         patch.object(main, "_research_subscription_fallback", AsyncMock(side_effect=AssertionError("prod switched"))):
        result = await main._execute_research_cloud_with_pool_fallback(_req(), _rbody())
    assert result.status == "error" and result.funding_policy == "promotional_only"
    assert "No eligible" in result.error


# --- Herkunft der Anfrage (ADR-0011, Zweitpruefung A1) ------------------

@pytest.fixture
def origin(monkeypatch):
    """The request origin as OriginMiddleware would have set it (patched on
    the reader: async tests run in their own context)."""
    from src import federation

    def setze(value):
        monkeypatch.setattr(federation, "get_request_origin", lambda: value)
    return setze


def test_foreign_origin_on_dev_worker_gets_no_fallback(dev_bridge, origin):
    # Prod pool empty → nginx retries on the dev backup: origin stays "prod".
    origin("prod")
    req = _req()
    assert not fp.staging_subscription_fallback_allowed()
    assert not fp.fallback_eligible(req)
    with pytest.raises(HTTPException) as e:
        fp.activate_subscription_fallback(req, fp.CAUSE_POOL_UNAVAILABLE)
    assert e.value.status_code == 503 and not fp.subscription_fallback_active(req)


def test_own_origin_gets_fallback(dev_bridge, origin):
    # Every untrusted sender (Railway staging, external) is stamped with the
    # LB's own ${BRIDGE_ID} by nginx ($bridge_origin_out default).
    origin("dev")
    assert fp.fallback_eligible(_req())


def test_request_without_origin_counts_as_own(dev_bridge, origin):
    # Direct worker call that never passed the LB (Hetzner worker, tests).
    origin(None)
    assert fp.fallback_eligible(_req())


@pytest.mark.asyncio
async def test_prod_origin_pool_exhausted_on_dev_worker_stays_strict(dev_bridge, origin):
    origin("prod")
    kern = AsyncMock(side_effect=credit_pool.PoolUnavailable(429, "No eligible"))
    with patch.object(credit_pool, "enabled", return_value=True), patch.object(main, "_chat_completions_lauf", kern):
        with pytest.raises(credit_pool.PoolUnavailable):
            await main._chat_completions_finanziert(_body(), _req(), None)
    assert kern.await_count == 1


@pytest.mark.asyncio
async def test_prod_origin_research_pool_unavailable_not_rerouted(dev_bridge, origin):
    from src.research_cloud.executor import ResearchCloudExecutorError
    origin("prod")
    err = ResearchCloudExecutorError("promotional_only stopped: No eligible", status_code=429)
    err.pool_unavailable = True
    with patch("src.research_cloud.executor.run_research_cloud", AsyncMock(side_effect=err)), \
         patch("src.research_cloud.anonymize_gate.anonymize_query_for_cloud", AsyncMock(return_value="q")), \
         patch("src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock()), \
         patch.dict("os.environ", {"ANTHROPIC_CREDIT_POOL_URL": "http://pool"}), \
         patch.object(main, "_research_subscription_fallback", AsyncMock(side_effect=AssertionError("switched"))):
        result = await main._execute_research_cloud_with_pool_fallback(_req(), _rbody())
    assert result.status == "error" and result.funding_policy == "promotional_only"


# --- Recherche: Koordinator faellt nach dem Anheften aus (A2) -----------

def _cloud_ok(text="Bericht " * 200):
    return httpx.Response(200, json={
        "id": "m", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1}})


@pytest.mark.asyncio
async def test_coordinator_loss_after_pin_is_pinned_loss_not_pool_unavailable(monkeypatch):
    from src.research_cloud.executor import ResearchCloudExecutorError, run_research_cloud
    from src.research_cloud.library import LibraryConfig
    from src.research_cloud.models import ResearchCloudConfig
    from src.research_cloud.perplexity import PerplexityConfig
    calls = []

    async def post(body, timeout, pin=None):
        calls.append(dict(pin))
        if len(calls) == 1:
            pin.setdefault("account_id", "konto-a")
            return httpx.Response(200, json={
                "id": "m", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
                "content": [{"type": "text", "text": "teil"}], "stop_reason": "pause_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1}}), {"credit_pool_account": "konto-a"}
        raise credit_pool.PoolUnavailable(503, "Anthropic credit coordinator unavailable")
    monkeypatch.setenv('ANTHROPIC_CREDIT_POOL_URL', 'http://pool')
    monkeypatch.setattr(credit_pool, 'post_messages', post)
    with pytest.raises(ResearchCloudExecutorError) as e:
        await run_research_cloud('q', 's', config=ResearchCloudConfig(promotional_only=True),
                                 library_config=LibraryConfig(enabled=False),
                                 perplexity_config=PerplexityConfig(enabled=False))
    assert calls == [{}, {"account_id": "konto-a"}]
    assert e.value.pool_unavailable is False and e.value.konto_angeheftet_verloren is True


@pytest.mark.asyncio
async def test_coordinator_loss_after_pin_never_restarts_on_subscription(dev_bridge, monkeypatch):
    """The executor error after the pin reaches the research handler: loud
    error, no subscription rerun (no second full research)."""
    from src.research_cloud.executor import ResearchCloudExecutorError
    err = ResearchCloudExecutorError("promotional_only stopped: coordinator unavailable", status_code=503)
    err.konto_angeheftet_verloren = True  # what the executor sets after the pin
    monkeypatch.setenv('ANTHROPIC_CREDIT_POOL_URL', 'http://pool')
    with patch("src.research_cloud.executor.run_research_cloud", AsyncMock(side_effect=err)), \
         patch("src.research_cloud.anonymize_gate.anonymize_query_for_cloud", AsyncMock(return_value="q")), \
         patch("src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock()), \
         patch.object(main, "_research_subscription_fallback", AsyncMock(side_effect=AssertionError("rerun"))):
        result = await main._execute_research_cloud_with_pool_fallback(_req(), _rbody())
    assert result.status == "error" and "coordinator unavailable" in result.error


# --- Kein zweiter Pool-Versuch im Abo-Rueckfall (Punkt 4) ---------------

@pytest.mark.asyncio
async def test_api_reserve_not_retried_under_subscription_fallback(dev_bridge):
    req = _req()
    fp.activate_subscription_fallback(req, fp.CAUSE_POOL_UNAVAILABLE)
    run = AsyncMock(side_effect=AssertionError("second pool attempt"))
    with patch.object(credit_pool, "enabled", return_value=True), patch.object(main, "_chat_completions_lauf", run):
        with pytest.raises(HTTPException) as e:
            await main._run_api_reserve_fallback(_body(), req, cause="provider_failure")
    assert e.value.status_code == 429 and "subscription_staging_fallback" in str(e.value.detail)
    run.assert_not_awaited()


# --- Echter Chat-Handler (Punkt 8) --------------------------------------

def _real_handler_patches(stack, *, pin=None, budget=None, admission=None):
    stack.enter_context(patch.object(main, "verify_api_key", AsyncMock(return_value=None)))
    stack.enter_context(patch.object(main, "validate_claude_code_auth", return_value=(True, {})))
    stack.enter_context(patch("src.budget.gate.enforce_budget", budget or AsyncMock(return_value=None)))
    stack.enter_context(patch("src.routing.user_provider_override.enforce_user_provider_override",
                              AsyncMock(return_value=pin)))
    stack.enter_context(patch.object(main, "enforce_pool_admission",
                                     admission or AsyncMock(side_effect=HTTPException(429, "abo-limiter stop"))))


@pytest.mark.asyncio
async def test_pool_unavailable_through_real_chat_handler_reaches_subscription(dev_bridge, monkeypatch):
    """PoolUnavailable raised deep in the direct path (vision_provider →
    credit_pool.post_messages) survives main's `except HTTPException` re-raise
    as its own class and hands the call to the subscription admission."""
    from contextlib import ExitStack
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_URL", "http://pool")
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_TOKEN", "t")
    pool = AsyncMock(side_effect=credit_pool.PoolUnavailable(429, "No eligible Anthropic API credit budget"))
    admission = AsyncMock(side_effect=HTTPException(429, "abo-limiter stop"))
    req = _req()
    with ExitStack() as stack:
        _real_handler_patches(stack, admission=admission)
        stack.enter_context(patch.object(credit_pool, "post_messages", pool))
        stack.enter_context(patch("src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock()))
        with pytest.raises(HTTPException) as e:
            await main._chat_completions_finanziert(_body(), req, None)
    pool.assert_awaited_once()
    admission.assert_awaited_once()  # the rerun reached the subscription limiter
    assert e.value.detail == "abo-limiter stop"
    assert fp.fallback_cause(req) == fp.CAUSE_POOL_UNAVAILABLE


@pytest.mark.asyncio
async def test_subscription_run_refuses_anthropic_direct_pin_409(dev_bridge):
    """The 409 riegel: an operator pin to anthropic_direct (paid API key) is
    refused in the subscription run, never served."""
    from contextlib import ExitStack
    req = _req()
    fp.activate_subscription_fallback(req, fp.CAUSE_POOL_MISSING)
    direct = AsyncMock(side_effect=AssertionError("paid direct call"))
    with ExitStack() as stack:
        _real_handler_patches(stack, pin="anthropic_direct", admission=AsyncMock(return_value=None))
        stack.enter_context(patch("src.providers.anthropic_direct.call_anthropic_direct", direct))
        stack.enter_context(patch.object(credit_pool, "post_messages", direct))
        stack.enter_context(patch("src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock()))
        with pytest.raises(HTTPException) as e:
            await main._chat_completions_lauf(_body(), req, None)
    assert e.value.status_code == 409, e.value.detail
    assert "subscription_staging_fallback conflicts with request routing" in str(e.value.detail)
    direct.assert_not_awaited()


# --- Mandant ohne tenants-Eintrag (phasentest) --------------------------

def test_fallback_does_not_depend_on_tenant(dev_bridge):
    for headers in ({"X-App-ID": "werking-energy", "X-Tenant-ID": "phasentest"},
                    {"X-App-ID": "werking-energy", "X-Tenant-ID": "steiner-staging"},
                    {"X-App-ID": "werking-energy"}):
        assert fp.fallback_eligible(_req(headers))


@pytest.mark.asyncio
async def test_tenantless_caller_same_gates_in_subscription_run(dev_bridge, monkeypatch):
    """phasentest: the subscription rerun passes the SAME budget gate with the
    same attribution as the pool run; a gate refusal (409
    billing_plan_unresolved) is not rerouted and not reclassified."""
    from contextlib import ExitStack
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_URL", "http://pool")
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_TOKEN", "t")
    headers = {"X-App-ID": "werking-energy", "X-Tenant-ID": "phasentest",
               "X-User-ID": "00000000-0000-4000-8000-000000000001", "X-Workflow-ID": "projekt-1"}
    budget = AsyncMock(return_value=None)
    pool = AsyncMock(side_effect=credit_pool.PoolUnavailable(503, "coordinator unavailable"))
    with ExitStack() as stack:
        _real_handler_patches(stack, budget=budget)
        stack.enter_context(patch.object(credit_pool, "post_messages", pool))
        stack.enter_context(patch("src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock()))
        with pytest.raises(HTTPException) as e:
            await main._chat_completions_finanziert(_body(), _req(headers), None)
    assert e.value.detail == "abo-limiter stop"
    assert budget.await_count == 1 or budget.await_count == 2
    seen = [(c.kwargs["app_id"], c.kwargs["user_id"]) for c in budget.await_args_list]
    assert set(seen) == {("werking-energy", headers["X-User-ID"])}

    refusal = HTTPException(409, {"error": "billing_plan_unresolved"})
    kern = AsyncMock(side_effect=refusal)
    with patch.object(credit_pool, "enabled", return_value=True), patch.object(main, "_chat_completions_lauf", kern):
        with pytest.raises(HTTPException) as e:
            await main._chat_completions_finanziert(_body(), _req(headers), None)
    assert e.value is refusal and kern.await_count == 1


@pytest.mark.asyncio
async def test_ledger_skips_tenantless_row_without_breaking_the_call(dev_bridge, caplog):
    """Unchanged behaviour (fix belongs to the Finanzierungsquelle branch):
    no tenants row → load_billing_context None → skip with a warning, no raise."""
    import asyncio
    from src.activity import ai_call_writer, ledger_spool
    from src.activity.providers import PROVIDER_ANTHROPIC
    uid = "00000000-0000-4000-8000-000000000002"

    async def run():
        fp.activate_subscription_fallback(_req(), fp.CAUSE_POOL_UNAVAILABLE)
        with patch.object(ledger_spool, "spool_enabled", return_value=False), \
             patch.object(ai_call_writer, "ledger_client", create=True) as lc:
            lc.load_billing_context = AsyncMock(return_value=None)
            return await ai_call_writer.persist_ai_call_activity(
                app_id="werking-energy", user_id=uid, agent_id=None, workflow_id=None, model="m",
                input_tokens=1, output_tokens=1, status="success", duration_ms=1,
                provider=PROVIDER_ANTHROPIC, provider_meta={"usage_source": "cli"}), lc
    result, lc = await asyncio.create_task(run())
    lc.load_billing_context.assert_awaited_once_with(uid)
    assert "no_tenant" in str(result) or ai_call_writer._skip_counts[uid] >= 1
