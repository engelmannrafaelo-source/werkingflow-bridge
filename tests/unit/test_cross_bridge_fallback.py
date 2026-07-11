"""
Unit Tests for Cross-Bridge Fallback (bridge-prod-emergency)

Tests:
1. Normal flow: claude-premium works → no fallback triggered
2. Token exhaustion: AllTokensExhausted → fallback to openrouter → fallback to bridge-prod-emergency → success
3. Missing AI_BRIDGE_URL_PROD_FALLBACK: tier filtered from chain, bridge starts cleanly
"""

import os
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


# =============================================================================
# Test 1: Normal flow — no fallback triggered
# =============================================================================

class TestNormalFlow:
    """claude-premium works normally — no fallback chain activated."""

    @pytest.mark.asyncio
    async def test_success_on_primary_no_fallback(self):
        """When claude-premium succeeds, execute_with_fallback returns immediately."""
        from src.providers.fallback import execute_with_fallback

        expected_response = {"choices": [{"message": {"content": "ok"}}]}
        execute_fn = AsyncMock(return_value=expected_response)
        resolve_config_fn = MagicMock(return_value=MagicMock(spec=[]))

        result = await execute_with_fallback(
            primary_tier="claude-premium",
            execute_fn=execute_fn,
            resolve_config_fn=resolve_config_fn,
        )

        assert result == expected_response
        # Only called once — no fallback
        assert execute_fn.call_count == 1


# =============================================================================
# Test 2: Full fallback chain: AllTokensExhausted → OpenRouter → Prod Bridge
# =============================================================================

class TestFullFallbackChain:
    """All dev tokens exhausted → falls back through chain → prod bridge succeeds."""

    @pytest.mark.asyncio
    async def test_token_exhaustion_falls_back_to_prod_bridge(self):
        """
        Scenario:
        - claude-premium: raises AllTokensExhausted (all OAuth tokens 429'd)
        - openrouter-claude: raises ProviderError(429)
        - bridge-prod-emergency: returns 200 → client sees success
        """
        from src.auth import AllTokensExhausted
        from src.providers.openai_compatible import ProviderError
        from src.providers.fallback import execute_with_fallback

        prod_bridge_response = {"choices": [{"message": {"content": "prod-bridge-ok"}}]}

        call_count = {"n": 0}

        async def execute_fn(config, tier_id):
            call_count["n"] += 1
            if tier_id == "claude-premium":
                raise AllTokensExhausted("All dev tokens exhausted")
            if tier_id == "openrouter-claude":
                raise ProviderError(429, "Rate limited")
            if tier_id == "bridge-prod-emergency":
                return prod_bridge_response
            raise AssertionError(f"Unexpected tier: {tier_id}")

        # Mock prod bridge and openrouter as usable
        with patch.dict(os.environ, {
            "AI_BRIDGE_URL_PROD_FALLBACK": "http://178.104.178.79:8000",
            "AI_BRIDGE_API_KEY": "test-key",
            "OPENROUTER_API_KEY": "test-openrouter-key",
        }):
            # Reload registry so new env vars are picked up
            import importlib
            import src.providers.registry as registry_mod
            importlib.reload(registry_mod)

            resolve_config_fn = MagicMock(return_value=MagicMock())

            from src.providers.fallback import execute_with_fallback as ewf

            result = await ewf(
                primary_tier="claude-premium",
                execute_fn=execute_fn,
                resolve_config_fn=resolve_config_fn,
            )

        assert result == prod_bridge_response
        assert call_count["n"] == 3  # claude-premium + openrouter + prod-bridge


# =============================================================================
# Test 3: Missing AI_BRIDGE_URL_PROD_FALLBACK — tier filtered, chain works fine
# =============================================================================

class TestMissingProdFallbackUrl:
    """When AI_BRIDGE_URL_PROD_FALLBACK is not set, bridge-prod-emergency is silently skipped."""

    def test_tier_not_usable_without_url(self):
        """is_tier_usable returns False when AI_BRIDGE_URL_PROD_FALLBACK is not set."""
        # Ensure env var is absent
        env = {k: v for k, v in os.environ.items() if k != "AI_BRIDGE_URL_PROD_FALLBACK"}
        with patch.dict(os.environ, env, clear=True):
            import importlib
            import src.providers.registry as registry_mod
            importlib.reload(registry_mod)
            from src.providers.registry import is_tier_usable
            assert not is_tier_usable("bridge-prod-emergency")

    def test_fallback_chain_excludes_prod_tier_when_url_missing(self):
        """get_fallback_tiers filters out bridge-prod-emergency when URL not configured."""
        env = {k: v for k, v in os.environ.items() if k != "AI_BRIDGE_URL_PROD_FALLBACK"}
        with patch.dict(os.environ, env, clear=True):
            import importlib
            import src.providers.registry as registry_mod
            importlib.reload(registry_mod)
            from src.providers.fallback import get_fallback_tiers
            chain = get_fallback_tiers("claude-premium")
            assert "bridge-prod-emergency" not in chain
            assert "claude-premium" in chain

    @pytest.mark.asyncio
    async def test_existing_fallback_chain_still_works_without_prod_tier(self):
        """Without prod bridge configured, openrouter-claude fallback still works."""
        from src.providers.openai_compatible import ProviderError

        openrouter_response = {"choices": [{"message": {"content": "openrouter-ok"}}]}

        async def execute_fn(config, tier_id):
            if tier_id == "claude-premium":
                raise ProviderError(503, "Anthropic unavailable")
            if tier_id == "openrouter-claude":
                return openrouter_response
            raise AssertionError(f"bridge-prod-emergency should not be called: {tier_id}")

        # Set OPENROUTER_API_KEY but NOT AI_BRIDGE_URL_PROD_FALLBACK
        env_override = {"OPENROUTER_API_KEY": "test-openrouter-key"}
        env_without_prod = {k: v for k, v in os.environ.items() if k != "AI_BRIDGE_URL_PROD_FALLBACK"}
        with patch.dict(os.environ, {**env_without_prod, **env_override}, clear=True):
            import importlib
            import src.providers.registry as registry_mod
            importlib.reload(registry_mod)
            from src.providers.fallback import execute_with_fallback

            resolve_config_fn = MagicMock(return_value=MagicMock())
            result = await execute_with_fallback(
                primary_tier="claude-premium",
                execute_fn=execute_fn,
                resolve_config_fn=resolve_config_fn,
            )

        assert result == openrouter_response


# =============================================================================
# Test 4: claude-direct-notools tier (Rafael, 2026-07-11 — Heimbau incident:
# CLI-subprocess session timeout on long Extended Thinking generations)
# =============================================================================

class TestClaudeDirectNotoolsTier:
    """New fallback tier: direct Anthropic API, no CLI subprocess, no tools.
    Only eligible for requests that already run with enable_tools=false."""

    def test_excluded_by_default_when_tools_required(self):
        """Default (tools_required=True, matches every pre-existing call site
        that doesn't pass the param) must behave exactly as before — this
        tier never silently appears for a caller that didn't opt in."""
        with patch.dict(os.environ, {"ANTHROPIC_VISION_API_KEY": "test-vision-key"}):
            import importlib
            import src.providers.registry as registry_mod
            importlib.reload(registry_mod)
            from src.providers.fallback import get_fallback_tiers
            chain = get_fallback_tiers("claude-premium")
            assert "claude-direct-notools" not in chain

    def test_included_when_tools_not_required(self):
        """enable_tools=false callers pass tools_required=False — the tier
        becomes eligible, and sits before openrouter-claude (same model,
        closest fallback) in the chain."""
        with patch.dict(os.environ, {
            "ANTHROPIC_VISION_API_KEY": "test-vision-key",
            "OPENROUTER_API_KEY": "test-openrouter-key",
        }):
            import importlib
            import src.providers.registry as registry_mod
            importlib.reload(registry_mod)
            from src.providers.fallback import get_fallback_tiers
            chain = get_fallback_tiers("claude-premium", tools_required=False)
            assert "claude-direct-notools" in chain
            assert chain.index("claude-direct-notools") < chain.index("openrouter-claude")

    def test_tier_not_usable_without_vision_key(self):
        """is_tier_usable returns False when ANTHROPIC_VISION_API_KEY is not set —
        filtered out even with tools_required=False, never a hard crash."""
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_VISION_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            import importlib
            import src.providers.registry as registry_mod
            importlib.reload(registry_mod)
            from src.providers.registry import is_tier_usable
            from src.providers.fallback import get_fallback_tiers
            assert not is_tier_usable("claude-direct-notools")
            chain = get_fallback_tiers("claude-premium", tools_required=False)
            assert "claude-direct-notools" not in chain

    def test_resolve_provider_tier_raises_loud_without_key(self):
        """_resolve_provider_tier must fail loud (RuntimeError), never silently
        return a config that would crash deeper inside VisionProvider."""
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_VISION_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            import importlib
            import src.providers.registry as registry_mod
            importlib.reload(registry_mod)
            from src.routing.backend_router import resolve_backend_config
            with pytest.raises(RuntimeError, match="ANTHROPIC_VISION_API_KEY"):
                resolve_backend_config(
                    backend=None,
                    model="claude-sonnet-4-5-20250929",
                    provider_tier="claude-direct-notools",
                )

    def test_resolve_provider_tier_succeeds_with_key(self):
        """With the key present, resolves to a usable BackendConfig carrying
        the model and API key for call_anthropic_direct()."""
        with patch.dict(os.environ, {"ANTHROPIC_VISION_API_KEY": "test-vision-key"}):
            import importlib
            import src.providers.registry as registry_mod
            importlib.reload(registry_mod)
            from src.routing.backend_router import resolve_backend_config
            from src.models import BackendType

            config = resolve_backend_config(
                backend=None,
                model="claude-sonnet-4-5-20250929",
                provider_tier="claude-direct-notools",
            )
            assert config.backend == BackendType.ANTHROPIC_DIRECT
            assert config.provider_api_key == "test-vision-key"
            assert config.provider_model == "claude-sonnet-4-5-20250929"


# =============================================================================
# Test 5: asyncio.TimeoutError recognition (the CLI-subprocess session-level
# timeout claude_cli.py re-raises bare on its MAX_TIMEOUT ceiling)
# =============================================================================

class TestSessionTimeoutRecognition:
    """Before this fix, a bare asyncio.TimeoutError (not a RuntimeError, not
    an httpx exception) fell through every isinstance check in
    is_retryable_error/is_breaker_failure — a hung/timed-out CLI session
    never triggered the fallback chain at all."""

    def test_is_retryable_error_recognizes_timeout(self):
        from src.providers.fallback import is_retryable_error
        assert is_retryable_error(asyncio.TimeoutError()) is True

    def test_is_breaker_failure_recognizes_timeout(self):
        """A session timeout is genuine brokenness (worker stuck), not a
        transient rate-limit — it must trip the breaker."""
        from src.providers.fallback import is_breaker_failure
        assert is_breaker_failure(asyncio.TimeoutError()) is True
