"""Provider Registry — Maps provider tiers to backend configurations.

Each tier defines a complete provider configuration:
- Backend type (anthropic, bedrock, openai_compatible)
- Model to use
- API endpoint (for OpenAI-compatible providers)
- Authentication method (API key or OAuth)
- Pricing per 1M tokens (USD)

Tiers are selected per-tenant in workspace settings (WerkING Report)
and sent via the `provider_tier` request parameter.
"""

import os
import logging
from enum import Enum
from typing import Optional
from dataclasses import dataclass

from src.models import BackendType

logger = logging.getLogger(__name__)


class AuthType(str, Enum):
    """Authentication method for provider API."""
    API_KEY = "api_key"            # Static API key from env var (IONOS, Mistral)
    OAUTH_GOOGLE = "oauth_google"  # Google OAuth with token refresh (Gemini free tier)


@dataclass(frozen=True)
class ProviderConfig:
    """Configuration for a provider tier."""
    tier_id: str
    name: str
    backend: BackendType
    model: str
    base_url: Optional[str] = None      # Only for OPENAI_COMPATIBLE
    api_key_env: Optional[str] = None   # Env var name for API key
    auth_type: AuthType = AuthType.API_KEY  # API key or OAuth
    pricing_input: float = 3.00         # USD per 1M input tokens
    pricing_output: float = 15.00       # USD per 1M output tokens
    dsgvo_compliant: bool = False       # EU data residency guaranteed
    description: str = ""


# =============================================================================
# PROVIDER REGISTRY
# =============================================================================

PROVIDERS: dict[str, ProviderConfig] = {
    # --- Default: Claude via Anthropic API ---
    "claude-premium": ProviderConfig(
        tier_id="claude-premium",
        name="Claude Premium (Anthropic)",
        backend=BackendType.ANTHROPIC,
        model="claude-sonnet-4-5-20250929",
        pricing_input=3.00,
        pricing_output=15.00,
        dsgvo_compliant=False,
        description="Schnellster und intelligentester Anbieter. Daten werden in den USA verarbeitet.",
    ),

    # --- DSGVO: Claude via AWS Bedrock EU ---
    "claude-dsgvo": ProviderConfig(
        tier_id="claude-dsgvo",
        name="Claude DSGVO (AWS Frankfurt)",
        backend=BackendType.BEDROCK,
        model="claude-sonnet-4-5-20250929",
        pricing_input=3.00,
        pricing_output=15.00,
        dsgvo_compliant=True,
        description="Claude-Qualitaet mit EU-Datenresidenz (AWS Frankfurt).",
    ),

    # =========================================================================
    # Google Gemini — CLI Subprocess (OAuth, Google Account Subscription)
    # =========================================================================

    "gemini-flash": ProviderConfig(
        tier_id="gemini-flash",
        name="Gemini Flash (CLI)",
        backend=BackendType.GEMINI_CLI,
        model="gemini-2.5-flash",
        pricing_input=0.00,
        pricing_output=0.00,
        description="Gemini 2.5 Flash via CLI subprocess. OAuth ueber Google Account.",
    ),

    # =========================================================================
    # OpenRouter — LLM Gateway (Fallback wenn Anthropic down)
    # 1 Key, 400+ Modelle, 5.5% Credit-Kauf-Fee, Provider-Preise 1:1
    # =========================================================================

    "openrouter-claude": ProviderConfig(
        tier_id="openrouter-claude",
        name="OpenRouter Claude Sonnet",
        backend=BackendType.OPENAI_COMPATIBLE,
        model="anthropic/claude-sonnet-4-5-20250929",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        pricing_input=3.00,
        pricing_output=15.00,
        dsgvo_compliant=False,
        description="Claude via OpenRouter Gateway. Automatisches Failover bei Anthropic-Ausfall.",
    ),
}

DEFAULT_TIER = "claude-premium"


def get_provider(tier_id: Optional[str]) -> ProviderConfig:
    """Get provider config by tier ID. Falls back to default."""
    if not tier_id or tier_id not in PROVIDERS:
        if tier_id:
            logger.warning(f"Unknown provider tier '{tier_id}', falling back to '{DEFAULT_TIER}'")
        return PROVIDERS[DEFAULT_TIER]
    return PROVIDERS[tier_id]


def get_provider_api_key(config: ProviderConfig) -> Optional[str]:
    """Get the API key for a provider from environment."""
    if not config.api_key_env:
        return None
    key = os.getenv(config.api_key_env)
    if not key:
        logger.error(f"API key not configured: {config.api_key_env} (provider: {config.tier_id})")
    return key


def list_available_providers() -> list[dict]:
    """List all configured providers (with API keys / OAuth credentials present)."""
    available = []
    for tier_id, config in PROVIDERS.items():
        has_key = True
        if config.auth_type == AuthType.OAUTH_GOOGLE:
            from src.providers.gemini_oauth import gemini_oauth_manager
            has_key = gemini_oauth_manager.is_configured()
        elif config.api_key_env:
            has_key = bool(os.getenv(config.api_key_env))

        entry = {
            "tier_id": tier_id,
            "name": config.name,
            "model": config.model,
            "auth_type": config.auth_type.value,
            "dsgvo_compliant": config.dsgvo_compliant,
            "pricing": {
                "input_per_1m": config.pricing_input,
                "output_per_1m": config.pricing_output,
            },
            "available": has_key,
            "description": config.description,
        }

        # Add Gemini rate limit status
        if config.auth_type == AuthType.OAUTH_GOOGLE and has_key:
            from src.providers.gemini_oauth import gemini_oauth_manager
            entry["rate_limits"] = gemini_oauth_manager.get_status()

        available.append(entry)
    return available
