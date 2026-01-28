"""Backend Router - Routes requests to appropriate backend (Anthropic SDK or AWS Bedrock).

This module provides per-request backend selection without creating separate providers.
The Claude Code SDK natively supports Bedrock when env vars are set.

Architecture:
    Claude Code SDK supports multiple backends via environment variables:
    - Default: OAuth/CLI authentication (Anthropic API)
    - Bedrock: CLAUDE_CODE_USE_BEDROCK=1 + AWS credentials

    We set these env vars dynamically per-request, so users can switch backends
    by adding `backend: "bedrock"` to their request.

DSGVO Compliance:
    - Bedrock EU (eu-central-1, eu-west-1, etc.) keeps data in EU
    - When using Bedrock EU, Presidio anonymization is auto-disabled (data residency guaranteed)
    - Non-EU Bedrock regions still enable Presidio by default
"""

import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass

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


def resolve_backend_config(
    backend: BackendType,
    model: str,
    privacy: PrivacyMode = PrivacyMode.AUTO,
    bedrock_region: Optional[str] = None
) -> BackendConfig:
    """Resolve backend configuration for a request.

    Args:
        backend: Target backend (anthropic or bedrock)
        model: Anthropic model ID (after fuzzy resolution from model_registry)
        privacy: Privacy mode (auto, enabled, disabled)
        bedrock_region: AWS region for Bedrock (default: eu-central-1 from credentials)

    Returns:
        BackendConfig with all settings resolved

    Raises:
        RuntimeError: If Bedrock requested but not configured (defensive programming)
    """
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
    return {
        "backend": config.backend.value,
        "region": config.region,
        "privacy_applied": config.privacy_enabled,
        "model_id_used": config.bedrock_model_id or config.model_id
    }
