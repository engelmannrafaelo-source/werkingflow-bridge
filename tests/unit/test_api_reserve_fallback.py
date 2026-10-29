"""Promotional reserve never opens an automatic paid fallback chain."""
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
for name in ['claude_code_sdk', 'claude_code_sdk._errors', 'claude_code_sdk._internal',
             'claude_code_sdk._internal.client', 'src.identity.routes', 'src.db.client']:
    if name not in sys.modules: sys.modules[name] = MagicMock()
import src.main as main
from src.models import ChatCompletionRequest
from fastapi import HTTPException
from src.providers.fallback import get_fallback_tiers

@pytest.mark.parametrize('tools', [True, False])
def test_configured_paid_keys_never_enter_automatic_chain(monkeypatch, tools):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'paid')
    monkeypatch.setenv('ANTHROPIC_VISION_API_KEY', 'paid')
    monkeypatch.setenv('AI_BRIDGE_URL_PROD_FALLBACK', 'https://paid.invalid')
    with patch('src.providers.credit_pool.enabled', return_value=False):
        assert get_fallback_tiers('claude-premium', tools_required=tools) == ['claude-premium']
    with patch('src.providers.credit_pool.enabled', return_value=True), patch('src.providers.registry.is_tier_usable', return_value=True):
        assert get_fallback_tiers('claude-premium', tools_required=tools) == (['claude-premium'] if tools else ['claude-premium', 'claude-direct-notools'])

@pytest.mark.asyncio
@pytest.mark.parametrize('extra', [{'enable_tools':True}, {'session_id':'continued'}, {}])
async def test_incompatible_or_unconfigured_request_never_calls_provider(extra):
    body = ChatCompletionRequest(model="claude-sonnet-5", messages=[{'role':'user','content':'test'}], **extra)
    req = SimpleNamespace(state=SimpleNamespace())
    with patch('src.providers.credit_pool.enabled', return_value=bool(extra)), patch.object(main, '_chat_completions_lauf', AsyncMock()) as run:
        with pytest.raises(HTTPException): await main._run_api_reserve_fallback(body, req, cause='account_exhausted')
        run.assert_not_called()

@pytest.mark.asyncio
async def test_reserve_reenters_normal_security_pipeline_and_restores_state():
    body = ChatCompletionRequest(model="claude-sonnet-5", messages=[{'role':'user','content':'test'}], enable_tools=False)
    req = SimpleNamespace(state=SimpleNamespace())
    async def authenticated_pipeline(retry, request, credentials):
        assert request.state.api_reserve_retry is True
        assert retry.provider_tier == 'claude-direct-notools'
        assert credentials is None  # normal verifier must read the original request
        raise HTTPException(401, 'Invalid authentication')
    with patch('src.providers.credit_pool.enabled', return_value=True), patch.object(main, '_chat_completions_lauf', authenticated_pipeline):
        with pytest.raises(HTTPException) as exc: await main._run_api_reserve_fallback(body, req, cause='account_exhausted')
    assert exc.value.status_code == 401
    assert req.state.api_reserve_retry is False
    assert body.provider_tier != 'claude-direct-notools'
