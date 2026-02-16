"""Backend Router - Routes requests to appropriate backend.

Supports four backend types:
- Anthropic: Claude Code SDK (default, full tool support)
- Bedrock: AWS Bedrock (DSGVO-compliant, EU data residency)
- OpenAI-Compatible: Generic httpx client (OpenRouter, etc.)
- Gemini CLI: Gemini CLI subprocess (Google OAuth, subscription models)

Provider tiers (e.g. 'claude-premium', 'openrouter-claude') override backend
selection and are resolved via the provider registry.
"""

import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass, field

from src.auth import bedrock_credential_manager
from src.models import BackendType, PrivacyMode
from src.model_registry import to_bedrock_model_id

logger = logging.getLogger(__name__)


@dataclass
class BackendConfig:
    """Configuration resolved for a backend request."""
    backend: BackendType
    region: Optional[str]
    model_id: str  # Anthropic model ID (for consistency in logs/response)
    bedrock_model_id: Optional[str]  # Bedrock model ID (only set if backend=bedrock)
    privacy_enabled: bool
    env_vars: Dict[str, str]  # Env vars to set for Claude Code SDK subprocess
    # OpenAI-compatible provider fields
    provider_tier: Optional[str] = None
    provider_base_url: Optional[str] = None
    provider_api_key: Optional[str] = None
    provider_model: Optional[str] = None


def resolve_backend_config(
    backend: BackendType,
    model: str,
    privacy: PrivacyMode = PrivacyMode.AUTO,
    bedrock_region: Optional[str] = None,
    provider_tier: Optional[str] = None,
) -> BackendConfig:
    """Resolve backend configuration for a request.

    If provider_tier is set, it overrides the backend selection via the registry.

    Args:
        backend: Target backend (anthropic, bedrock, or openai_compatible)
        model: Anthropic model ID (after fuzzy resolution from model_registry)
        privacy: Privacy mode (auto, enabled, disabled)
        bedrock_region: AWS region for Bedrock (default: eu-central-1 from credentials)
        provider_tier: Provider tier ID (overrides backend if set)

    Returns:
        BackendConfig with all settings resolved

    Raises:
        RuntimeError: If requested backend is not configured
    """
    # Provider tier overrides backend selection
    if provider_tier:
        return _resolve_provider_tier(provider_tier, model, privacy)

    env_vars = {}
    bedrock_model_id = None
    region = None

    if backend == BackendType.BEDROCK:
        # Validate Bedrock credentials
        is_valid, error_info = bedrock_credential_manager.validate()
        if not is_valid:
            raise RuntimeError(
                f"Bedrock backend requested but not configured: {', '.join(error_info['errors'])}"
            )

        # Get region (request override > default from credentials)
        region = bedrock_region or bedrock_credential_manager.default_region

        # Get env vars for Bedrock routing
        env_vars = bedrock_credential_manager.get_bedrock_env_vars(region)

        # Convert model ID to Bedrock format
        bedrock_model_id = to_bedrock_model_id(model)

        logger.info(f"🔀 Backend routing: bedrock (region={region}, model={bedrock_model_id})")
    else:
        logger.debug("🔀 Backend routing: anthropic (default SDK)")

    # Resolve privacy mode
    privacy_enabled = _resolve_privacy_mode(privacy, backend, region)

    return BackendConfig(
        backend=backend,
        region=region,
        model_id=model,
        bedrock_model_id=bedrock_model_id,
        privacy_enabled=privacy_enabled,
        env_vars=env_vars
    )


def _resolve_provider_tier(
    tier_id: str,
    fallback_model: str,
    privacy: PrivacyMode,
) -> BackendConfig:
    """Resolve a provider tier to a full BackendConfig."""
    from src.providers.registry import get_provider, get_provider_api_key, AuthType

    config = get_provider(tier_id)

    if config.backend == BackendType.OPENAI_COMPATIBLE:
        # Resolve API key based on auth type
        if config.auth_type == AuthType.OAUTH_GOOGLE:
            from src.providers.gemini_oauth import gemini_oauth_manager

            if not gemini_oauth_manager.is_configured():
                raise RuntimeError(
                    f"Provider '{tier_id}' requires Gemini OAuth but credentials not configured. "
                    "Set GEMINI_OAUTH_CREDS_FILE or run secrets/setup-gemini-oauth.sh"
                )

            gemini_oauth_manager.check_rate_limit()
            api_key = gemini_oauth_manager.get_access_token()
            gemini_oauth_manager.track_request()
        else:
            api_key = get_provider_api_key(config)
            if not api_key:
                raise RuntimeError(
                    f"Provider '{tier_id}' not configured: {config.api_key_env} env var missing"
                )

        # DSGVO providers auto-disable privacy
        privacy_enabled = not config.dsgvo_compliant if privacy == PrivacyMode.AUTO else (privacy == PrivacyMode.ENABLED)

        logger.info(f"🔀 Provider tier: {tier_id} → {config.name} (model={config.model})")

        return BackendConfig(
            backend=BackendType.OPENAI_COMPATIBLE,
            region=None,
            model_id=config.model,
            bedrock_model_id=None,
            privacy_enabled=privacy_enabled,
            env_vars={},
            provider_tier=tier_id,
            provider_base_url=config.base_url,
            provider_api_key=api_key,
            provider_model=config.model,
        )

    elif config.backend == BackendType.GEMINI_CLI:
        # Tier maps to Gemini CLI subprocess (e.g. 'gemini-flash')
        logger.info(f"🔀 Provider tier: {tier_id} → {config.name} (model={config.model})")

        return BackendConfig(
            backend=BackendType.GEMINI_CLI,
            region=None,
            model_id=config.model,
            bedrock_model_id=None,
            privacy_enabled=False,  # Gemini CLI handles its own data
            env_vars={},
            provider_tier=tier_id,
            provider_model=config.model,
        )

    elif config.backend == BackendType.BEDROCK:
        # Tier maps to Bedrock (e.g. 'claude-dsgvo')
        return resolve_backend_config(
            backend=BackendType.BEDROCK,
            model=config.model,
            privacy=privacy,
            bedrock_region=None,
        )

    else:
        # Tier maps to Anthropic (e.g. 'claude-premium')
        return resolve_backend_config(
            backend=BackendType.ANTHROPIC,
            model=config.model,
            privacy=privacy,
        )


def _resolve_privacy_mode(
    privacy: PrivacyMode,
    backend: BackendType,
    region: Optional[str]
) -> bool:
    """Resolve whether privacy (Presidio anonymization) should be enabled.

    Auto mode logic:
    - Bedrock EU (eu-*): Disable privacy (data stays in EU, DSGVO-compliant)
    - Bedrock non-EU: Enable privacy (data may leave EU)
    - Anthropic: Use global middleware setting (PRIVACY_ENABLED env var)

    Args:
        privacy: Privacy mode from request
        backend: Target backend
        region: AWS region (only for Bedrock)

    Returns:
        True if privacy should be enabled, False otherwise
    """
    if privacy == PrivacyMode.DISABLED:
        return False
    if privacy == PrivacyMode.ENABLED:
        return True

    # AUTO mode
    if backend == BackendType.BEDROCK and region:
        # Disable privacy for EU regions (data residency guaranteed)
        is_eu_region = region.startswith("eu-")
        if is_eu_region:
            logger.info(f"🔒 Privacy auto-disabled: Bedrock EU region ({region}) guarantees data residency")
            return False
        else:
            logger.info(f"🔒 Privacy auto-enabled: Bedrock non-EU region ({region})")
            return True

    # Default: use global middleware setting
    from src.privacy import get_privacy_middleware
    middleware = get_privacy_middleware()
    return middleware.enabled


def get_backend_info_dict(config: BackendConfig) -> Dict[str, Any]:
    """Generate backend info dict for response metadata.

    Returns dict suitable for x_backend_info field in ChatCompletionResponse.
    """
    info = {
        "backend": config.backend.value,
        "region": config.region,
        "privacy_applied": config.privacy_enabled,
        "model_id_used": config.provider_model or config.bedrock_model_id or config.model_id,
    }
    if config.provider_tier:
        info["provider_tier"] = config.provider_tier
    return info
