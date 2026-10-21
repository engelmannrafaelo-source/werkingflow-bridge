"""Vertex EU durch den ECHTEN Chat-Handler (src.main.chat_completions).

Geprueft wird, was am Ende ankommt: 503 fuer einen Pin ohne Schalter, 403 fuer
ein Client-Opt-in ohne Pin oder aus staging, und beim zugelassenen Aufruf der
Vertex-Zweig mit Buchung provider='vertex-eu' — auch im Fehlerfall (Quote 0).
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
from fastapi import HTTPException  # noqa: E402

import src.main  # noqa: E402
from src.models import ChatCompletionRequest  # noqa: E402

_PIN = {"provider": "vertex_eu"}


def _request(body, env="prod", app_id="werking-report"):
    req = MagicMock()
    req.state = MagicMock(spec=[])
    req.headers = {"X-User-ID": "user-vertex", "X-App-Env": env,
                   **({"X-App-ID": app_id} if app_id else {})}
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    req.state.cached_body_dict = body
    req.state.adaptive_est_tokens = 10
    return req


def _tracker():
    t = MagicMock()
    t.should_reject_new_request.return_value = False
    t.is_hard_limited.return_value = False
    return t


async def _run(body, *, pin=None, env="prod", call=None, persist=None):
    handler = inspect.unwrap(src.main.chat_completions)
    call = call or AsyncMock(side_effect=AssertionError("Vertex darf hier nicht erreicht werden"))
    persist = persist or AsyncMock()
    with patch.object(src.main, "verify_api_key", AsyncMock()), \
         patch.object(src.main, "enforce_pool_admission", AsyncMock()), \
         patch("src.budget.gate.enforce_budget", AsyncMock()), \
         patch("src.claude_cli.rate_limit_tracker", _tracker()), \
         patch.object(src.main, "_org_disabled_precheck", MagicMock(return_value=None)), \
         patch("src.routing.user_provider_override.get_user_provider_config",
               AsyncMock(return_value=pin)), \
         patch("src.routing.app_tier_policy.resolve_app_tier_policy", AsyncMock(return_value=None)), \
         patch.object(src.main.claude_cli, "run_completion",
                      MagicMock(side_effect=AssertionError("SDK-Weg erreicht"))), \
         patch("src.vertex_eu_service.call_vertex_eu", call), \
         patch("src.activity.ai_call_writer.persist_ai_call_activity", persist):
        return await handler(ChatCompletionRequest(**body), _request(body, env), None, None)


def _body(**kw):
    b = {"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "Hallo"}],
         "max_tokens": 50}
    b.update(kw)
    return b


@pytest.fixture
def an(monkeypatch):
    monkeypatch.setenv("VERTEX_EU_ENABLED", "1")
    monkeypatch.setenv("GCP_VERTEX_SA_JSON", json.dumps({
        "client_email": "x@y.iam.gserviceaccount.com", "private_key": "unbenutzt",
        "project_id": "p"}))
    monkeypatch.delenv("VERTEX_EU_ALLOW_NON_PROD", raising=False)


@pytest.mark.asyncio
async def test_pin_bei_schalter_aus_ist_503_ohne_rueckfall(monkeypatch):
    monkeypatch.delenv("VERTEX_EU_ENABLED", raising=False)
    with pytest.raises(HTTPException) as ei:
        await _run(_body(), pin=_PIN)
    assert ei.value.status_code == 503
    assert ei.value.detail["error"]["code"] == "user_provider_override_unavailable"
    assert "VERTEX_EU_ENABLED" in ei.value.detail["error"]["message"]


@pytest.mark.asyncio
async def test_client_opt_in_ohne_pin_ist_403(an):
    with pytest.raises(HTTPException) as ei:
        await _run(_body(backend="vertex_eu"), pin=None)
    assert ei.value.status_code == 403
    assert ei.value.detail["error"]["code"] == "vertex_eu_requires_operator_pin"


@pytest.mark.asyncio
async def test_pin_aus_staging_ohne_freigabe_ist_403(an):
    with pytest.raises(HTTPException) as ei:
        await _run(_body(), pin=_PIN, env="staging")
    assert ei.value.detail["error"]["code"] == "vertex_eu_requires_production_env"


@pytest.mark.asyncio
async def test_zugelassener_aufruf_erreicht_vertex_und_bucht(an):
    antwort = {"id": "c", "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 3, "completion_tokens": 4},
               "x_backend_info": {"backend": "vertex_eu", "provider_request_id": "msg_vrtx"}}
    call, persist = AsyncMock(return_value=antwort), AsyncMock()
    res = await _run(_body(), pin=_PIN, call=call, persist=persist)
    assert res is antwort
    call.assert_awaited_once()
    kw = persist.await_args.kwargs
    assert kw["provider"] == "vertex-eu" and kw["status"] == "success"
    assert kw["input_tokens"] == 3 and kw["output_tokens"] == 4
    assert kw["provider_meta"]["provider_request_id"] == "msg_vrtx"


@pytest.mark.asyncio
async def test_quote_null_wird_gebucht_und_laut_durchgereicht(an):
    fehler = HTTPException(status_code=429, detail={
        "message": "Vertex EU quota is 0 (eu_multi_region_online_prediction_requests_per_base_model)",
        "code": "vertex_eu_quota_zero", "retryable": False})
    persist = AsyncMock()
    with pytest.raises(HTTPException) as ei:
        await _run(_body(), pin=_PIN, call=AsyncMock(side_effect=fehler), persist=persist)
    assert ei.value.status_code == 429
    kw = persist.await_args.kwargs
    assert kw["provider"] == "vertex-eu" and kw["status"] == "error"
    assert kw["error_code"] == "vertex_eu_quota_zero"
