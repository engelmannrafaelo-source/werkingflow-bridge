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
from src.model_registry import get_default_model

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
    # NB: no per-tier price here — list-price for display comes from pricing.py
    # (single source of truth) via _tier_pricing(); billing uses pricing.py too.
    dsgvo_compliant: bool = False       # EU data residency guaranteed
    supports_tools: bool = True         # False = fallback chains skip this tier
                                        # for any request with enable_tools=true
                                        # (get_fallback_tiers(tools_required=...))
    description: str = ""


# =============================================================================
# PROVIDER REGISTRY
# =============================================================================

# Central default model — SINGLE SOURCE OF TRUTH is model_registry.is_default.
# Claude tiers derive from it so the served model is backend-agnostic: Anthropic,
# Bedrock (claude-dsgvo) and OpenRouter all follow the ONE registry default.
# Change the default in model_registry.py, everything here follows on rebuild.
_DEFAULT_SONNET = get_default_model("sonnet").id


def _gemini_vision_model_for_display() -> str:
    """Modellname fuer den Registry-Eintrag des Gemini-Bildwegs.

    Nur Anzeige/Preis-Metadaten: der tatsaechlich gesendete Name wird bei JEDEM
    Aufruf frisch aus der Env gelesen (gemini_vision.resolve_gemini_vision_model),
    damit eine Modellumstellung ohne Rebuild wirkt. Hier faellt ein ungueltiger
    Env-Wert bewusst NICHT hart aus — der Registry-Import darf keinen Worker am
    Booten hindern; der Aufruf selbst weist ihn dann laut ab.
    """
    from src.providers.gemini_vision import (
        DEFAULT_GEMINI_VISION_MODEL,
        GeminiVisionError,
        resolve_gemini_vision_model,
    )
    try:
        return resolve_gemini_vision_model()
    except GeminiVisionError:
        return DEFAULT_GEMINI_VISION_MODEL


def _tier_pricing(model: str) -> dict:
    """Tier list-price for display, derived from the pricing SSoT (pricing.py) —
    no price numbers duplicated here. Strips OpenRouter's 'anthropic/' routing
    prefix; unpriced tiers (Gemini free tier) show 0.0. Billing always uses
    pricing.py keyed by the actually-served model; this is display metadata."""
    from src.pricing import price_entry
    lookup = model.split("/", 1)[-1] if model.startswith("anthropic/") else model
    p = price_entry(lookup)
    return {"input_per_1m": p["in"] if p else 0.0, "output_per_1m": p["out"] if p else 0.0}


PROVIDERS: dict[str, ProviderConfig] = {
    # --- Default: Claude via Anthropic API ---
    "claude-premium": ProviderConfig(
        tier_id="claude-premium",
        name="Claude Premium (Anthropic)",
        backend=BackendType.ANTHROPIC,
        model=_DEFAULT_SONNET,
        dsgvo_compliant=False,
        description="Schnellster und intelligentester Anbieter. Daten werden in den USA verarbeitet.",
    ),

    # =========================================================================
    # Direct Anthropic Messages API — no CLI subprocess, no tool support.
    # Fallback tier for claude-premium requests that already run with
    # enable_tools=false (get_fallback_tiers() excludes this tier whenever
    # tools_required=True). Same model, ~4K fewer scaffolding tokens/call and
    # no Claude-Code-SDK subprocess overhead — measured 2026-07-10: a 32K-output
    # call took 290s here vs. timing out against the CLI path's 2400s ceiling.
    # Rafael 2026-07-11: root cause of the "Heimbau" pipeline hang was
    # Extended-Thinking output (40-77K tokens/call), not input size — this
    # tier is the fix for calls that are safe to reroute (no tools needed).
    # =========================================================================
    "claude-direct-notools": ProviderConfig(
        tier_id="claude-direct-notools",
        name="Claude Direct (Anthropic Messages API, no tools)",
        backend=BackendType.ANTHROPIC_DIRECT,
        model=_DEFAULT_SONNET,
        api_key_env="ANTHROPIC_VISION_API_KEY",
        dsgvo_compliant=False,
        supports_tools=False,
        description=(
            "Direkter Anthropic-Call ohne Claude-Code-SDK-Subprozess. Kein Tool-"
            "Support — nur fuer Requests mit enable_tools=false. Fallback wenn "
            "der CLI-Pfad haengt/timeout (z.B. lang laufendes Extended Thinking)."
        ),
    ),

    # --- DSGVO: Claude via AWS Bedrock EU ---
    "claude-dsgvo": ProviderConfig(
        tier_id="claude-dsgvo",
        name="Claude DSGVO (AWS Frankfurt)",
        backend=BackendType.BEDROCK,
        model=_DEFAULT_SONNET,
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
        description="Gemini 2.5 Flash via CLI subprocess. OAuth ueber Google Account.",
    ),

    # =========================================================================
    # Google Gemini — API-Key MIT Bildeingabe
    #
    # NICHT verwechseln mit 'gemini-flash' darueber: das ist der CLI-Subprozess
    # per OAuth und kann KEINE Bilder.
    #
    # Auf der DEV-Bridge ist dieser Weg seit 2026-09-03 der STANDARD fuer die
    # Bildanalyse (Rafael) — dafuer braucht es diesen Tier gar nicht, das
    # erledigt BRIDGE_VISION_DEFAULT_PROVIDER. Der Tier bleibt der Weg, ihn
    # AUSDRUECKLICH pro Aufruf zu waehlen (Vergleichsmessungen, gezielte
    # Modellwahl) — normale Provider-Mechanik, kein Sonderpfad.
    #
    # Auf PRODUKTION ist er unerreichbar: src/routing/gemini_vision_gate.py
    # verlangt Master-Flag + Key + positiv erkanntes Nicht-Prod, und der Key
    # liegt dort bewusst nicht. Ohne das wird der Aufruf laut abgewiesen — nie
    # still auf Anthropic umgeleitet. Grund: Google ist kein gelisteter
    # Unterauftragsverarbeiter (avv.md §5.4).
    #
    # Das konkrete Modell kommt aus GEMINI_VISION_MODEL (Default
    # gemini-2.5-flash) — ein Modellwechsel ist Konfiguration.
    # =========================================================================
    "gemini-vision": ProviderConfig(
        tier_id="gemini-vision",
        name="Gemini Vision (Bildanalyse, nur Nicht-Produktion)",
        backend=BackendType.GEMINI_API,
        model=_gemini_vision_model_for_display(),
        api_key_env="GEMINI_VISION_API_KEY",
        dsgvo_compliant=False,
        supports_tools=False,
        description=(
            "Guenstige Bildanalyse ueber Google Gemini. Auf der dev-Bridge der "
            "Standard, auf Produktion gesperrt — Google ist kein gelisteter "
            "Unterauftragsverarbeiter (avv.md §5.4)."
        ),
    ),

    # =========================================================================
    # OpenRouter — LLM Gateway (Fallback wenn Anthropic down)
    # 1 Key, 400+ Modelle, 5.5% Credit-Kauf-Fee, Provider-Preise 1:1
    # =========================================================================

    "openrouter-claude": ProviderConfig(
        tier_id="openrouter-claude",
        name="OpenRouter Claude Sonnet",
        backend=BackendType.OPENAI_COMPATIBLE,
        model=f"anthropic/{_DEFAULT_SONNET}",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        dsgvo_compliant=False,
        description="Claude via OpenRouter Gateway. Automatisches Failover bei Anthropic-Ausfall.",
    ),

    # =========================================================================
    # Production Bridge Emergency Fallback (Sahori Account)
    # Last resort: only when ALL dev-bridge OAuth tokens are exhausted.
    # Requires AI_BRIDGE_URL_PROD_FALLBACK env var. Silently skipped if absent.
    # =========================================================================
    "bridge-prod-emergency": ProviderConfig(
        tier_id="bridge-prod-emergency",
        name="Production Bridge Emergency Fallback (Sahori)",
        backend=BackendType.OPENAI_COMPATIBLE,
        model="claude-sonnet-4-5-20250929",
        base_url=(os.getenv("AI_BRIDGE_URL_PROD_FALLBACK", "") + "/v1") if os.getenv("AI_BRIDGE_URL_PROD_FALLBACK") else None,
        api_key_env="AI_BRIDGE_API_KEY",
        dsgvo_compliant=False,
        description="Notfall-Fallback auf Production Bridge mit Sahori-Account. Nur wenn alle Dev-Tokens erschoepft.",
    ),
}

DEFAULT_TIER = "claude-premium"


def is_tier_usable(tier_id: str) -> bool:
    """Check if a provider tier is usable (all required config present).

    For OPENAI_COMPATIBLE tiers, requires both base_url and api_key to be set.
    For ANTHROPIC_DIRECT and GEMINI_API, requires the api_key to be set (no
    base_url — fixed provider endpoint, see src/providers/anthropic_direct.py
    bzw. src/providers/gemini_vision.py). Fuer GEMINI_API ist "kein Key" auf
    Produktions-Workern der ERWUENSCHTE Zustand, nicht ein Konfigurationsfehler.
    Returns False (and logs a warning) if the tier is not usable — no crash.
    """
    config = PROVIDERS.get(tier_id)
    if not config:
        return False
    if config.backend == BackendType.OPENAI_COMPATIBLE:
        if not config.base_url:
            logger.warning(f"⚠️ Provider tier '{tier_id}' skipped: base_url not configured (env var missing)")
            return False
        if config.api_key_env and not os.getenv(config.api_key_env):
            logger.warning(f"⚠️ Provider tier '{tier_id}' skipped: {config.api_key_env} not set")
            return False
    if config.backend in (BackendType.ANTHROPIC_DIRECT, BackendType.GEMINI_API):
        if config.api_key_env and not os.getenv(config.api_key_env):
            logger.warning(f"⚠️ Provider tier '{tier_id}' skipped: {config.api_key_env} not set")
            return False
    return True


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
            "pricing": _tier_pricing(config.model),
            "available": has_key,
            "description": config.description,
        }

        # Add Gemini rate limit status
        if config.auth_type == AuthType.OAUTH_GOOGLE and has_key:
            from src.providers.gemini_oauth import gemini_oauth_manager
            entry["rate_limits"] = gemini_oauth_manager.get_status()

        available.append(entry)
    return available
