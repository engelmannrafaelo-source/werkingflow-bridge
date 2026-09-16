"""Pin ``provider_config.provider='anthropic_direct'`` (user_provider_override).

Rafael 2026-09-16: solange das AWS-Konto fuer Bedrock gesperrt ist, laufen die
TB-Kainer-Nutzer nicht auf den internen Flatrate-Konten, sondern auf dem
aufgeladenen Anthropic-API-Key (Tier ``claude-direct-notools``). Der Pin muss
den Tier setzen — vorher fiel ein primaerer Aufruf mit diesem Tier still auf den
CLI-Pool (dev-Bridge gemessen: x_backend_info.backend='anthropic').
"""
import os
os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest
from types import SimpleNamespace

from src.models import BackendType
from src.routing.user_provider_override import (
    DIRECT_PIN_TIER,
    SUPPORTED_PROVIDERS,
    UserProviderOverrideError,
    apply_user_provider_override,
    assert_bedrock_is_pinned,
)
from src.routing.backend_router import _resolve_provider_tier  # noqa: F401  (import guard)


def _body(enable_tools=False):
    return SimpleNamespace(
        backend=None, bedrock_region=None, provider_tier="claude-premium",
        enable_tools=enable_tools,
    )


DIRECT_PIN = {"provider": "anthropic_direct"}


class TestDirectPin:
    def test_provider_is_supported(self):
        assert "anthropic_direct" in SUPPORTED_PROVIDERS

    @pytest.mark.parametrize("env", ["prod", None])
    def test_prod_and_unknown_env_set_the_direct_tier(self, env):
        """prod UND fehlender Header wenden den Pin an (wie beim Bedrock-Pin:
        ein Deployment, das X-App-Env vergisst, darf nicht still auf die
        Pool-Konten wechseln)."""
        body = _body()
        assert apply_user_provider_override(body, DIRECT_PIN, app_env=env) == "anthropic_direct"
        assert body.provider_tier == DIRECT_PIN_TIER == "claude-direct-notools"
        assert body.backend == BackendType.ANTHROPIC

    @pytest.mark.parametrize("env", ["staging", "local"])
    def test_non_prod_downgrades_to_pool(self, env):
        body = _body()
        assert apply_user_provider_override(body, DIRECT_PIN, app_env=env) == "anthropic"
        assert body.provider_tier is None
        assert body.backend == BackendType.ANTHROPIC

    def test_client_tier_is_overridden_by_the_pin(self):
        body = _body()
        body.provider_tier = "claude-dsgvo"
        apply_user_provider_override(body, DIRECT_PIN, app_env="prod")
        assert body.provider_tier == DIRECT_PIN_TIER

    def test_tools_are_refused_not_rerouted(self):
        """Kein stiller Rueckfall auf den Pool: ein Tool-Aufruf eines
        direkt-gepinnten Users wird laut abgewiesen."""
        body = _body(enable_tools=True)
        with pytest.raises(UserProviderOverrideError):
            apply_user_provider_override(body, DIRECT_PIN, app_env="prod")

    def test_direct_pin_passes_the_bedrock_gate(self):
        """Der Bedrock-Gate prueft nur den Bedrock-Backend — ein
        anthropic_direct-Pin darf ihn nicht ausloesen."""
        body = _body()
        pinned = apply_user_provider_override(body, DIRECT_PIN, app_env="prod")
        assert_bedrock_is_pinned(body.backend, pinned, app_env="prod")

    def test_tier_resolves_to_direct_backend(self, monkeypatch):
        """Der gesetzte Tier muss im Router auch beim ANTHROPIC_DIRECT-Backend
        landen — sonst waere der Pin wieder ein No-op."""
        monkeypatch.setenv("ANTHROPIC_VISION_API_KEY", "sk-ant-test")
        from src.routing.backend_router import resolve_backend_config
        from src.models import PrivacyMode
        cfg = resolve_backend_config(
            backend=BackendType.ANTHROPIC, model="sonnet",
            privacy=PrivacyMode.AUTO, bedrock_region=None,
            provider_tier=DIRECT_PIN_TIER,
        )
        assert cfg.backend == BackendType.ANTHROPIC_DIRECT
        assert cfg.provider_tier == DIRECT_PIN_TIER
