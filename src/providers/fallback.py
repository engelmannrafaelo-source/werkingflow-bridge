"""Fallback Chain — Automatic provider failover on errors.

Defines fallback chains per primary provider tier:
- claude-premium   → openrouter-claude (same model, different infra)
- claude-dsgvo     → (no fallback — DSGVO data must stay in EU)
- openrouter-claude → (no fallback — OpenRouter has its own internal failover)

Triggers: HTTP 429, 500, 502, 503, 504, connect timeout, connection refused.
Retries once per fallback provider with a short delay.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from src.providers.openai_compatible import ProviderError

logger = logging.getLogger(__name__)


# =============================================================================
# FALLBACK CHAIN CONFIGURATION
# =============================================================================

FALLBACK_CHAINS: dict[str, list[str]] = {
    # Anthropic Direct down → OpenRouter routes Claude via different infra
    "claude-premium": ["openrouter-claude"],

    # DSGVO: No fallback — data residency must stay in EU (Bedrock Frankfurt)
    # "claude-dsgvo": [],

    # OpenRouter has its own internal failover, no additional chain needed
    # "openrouter-claude": [],
}

# HTTP status codes that trigger a fallback attempt
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Delay between fallback attempts (seconds)
FALLBACK_DELAY_SECONDS = 1.5


# =============================================================================
# FALLBACK STATE TRACKING
# =============================================================================

@dataclass
class ProviderHealth:
    """Tracks recent health of a provider for the /health endpoint."""
    last_success: Optional[float] = None
    last_error: Optional[float] = None
    last_error_msg: Optional[str] = None
    consecutive_failures: int = 0

    @property
    def status(self) -> str:
        if self.consecutive_failures == 0:
            return "up"
        if self.consecutive_failures < 3:
            return "degraded"
        return "down"


# Global health tracker (reset on restart, not persistent)
_provider_health: dict[str, ProviderHealth] = {}


def get_provider_health(tier_id: str) -> ProviderHealth:
    """Get or create health state for a provider."""
    if tier_id not in _provider_health:
        _provider_health[tier_id] = ProviderHealth()
    return _provider_health[tier_id]


def record_success(tier_id: str) -> None:
    """Record a successful call to a provider."""
    health = get_provider_health(tier_id)
    health.last_success = time.time()
    health.consecutive_failures = 0


def record_failure(tier_id: str, error_msg: str) -> None:
    """Record a failed call to a provider."""
    health = get_provider_health(tier_id)
    health.last_error = time.time()
    health.last_error_msg = error_msg
    health.consecutive_failures += 1


def get_all_provider_health() -> dict[str, dict]:
    """Get health status for all tracked providers (for /health endpoint)."""
    result = {}
    for tier_id, health in _provider_health.items():
        result[tier_id] = {
            "status": health.status,
            "consecutive_failures": health.consecutive_failures,
            "last_error": health.last_error_msg,
        }
    return result


# =============================================================================
# FALLBACK LOGIC
# =============================================================================

def get_fallback_tiers(primary_tier: str) -> list[str]:
    """Get the ordered list of fallback tier IDs for a primary provider.

    Returns the full chain: [primary, fallback1, fallback2, ...].
    If no chain is configured, returns just [primary].
    """
    chain = [primary_tier]
    chain.extend(FALLBACK_CHAINS.get(primary_tier, []))
    return chain


def is_retryable_error(error: Exception) -> bool:
    """Check if an error should trigger a fallback attempt."""
    # ProviderError with retryable status code
    if isinstance(error, ProviderError):
        return error.status_code in RETRYABLE_STATUS_CODES

    # httpx connection errors (timeout, refused, DNS failure)
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
        return True

    # Generic RuntimeError from Bedrock or other backends
    if isinstance(error, RuntimeError):
        msg = str(error).lower()
        return any(code in msg for code in ["429", "500", "502", "503", "504", "timeout"])

    return False


async def execute_with_fallback(
    primary_tier: str,
    execute_fn,
    resolve_config_fn,
):
    """Execute a backend call with automatic fallback on retryable errors.

    Args:
        primary_tier: The primary provider tier ID (e.g. 'claude-premium')
        execute_fn: async callable(backend_config) → response
        resolve_config_fn: callable(tier_id) → BackendConfig

    Returns:
        The response from the first successful provider.

    Raises:
        The last error if all providers in the chain fail.
    """
    tiers = get_fallback_tiers(primary_tier)
    last_error = None

    for i, tier_id in enumerate(tiers):
        try:
            config = resolve_config_fn(tier_id)
            response = await execute_fn(config, tier_id)

            record_success(tier_id)

            if i > 0:
                logger.warning(
                    f"🔄 Fallback successful: {tier_id} "
                    f"(after {i} failed provider(s): {tiers[:i]})"
                )

            return response

        except Exception as e:
            last_error = e
            error_msg = str(e)[:200]
            record_failure(tier_id, error_msg)

            if not is_retryable_error(e):
                logger.error(f"❌ {tier_id}: Non-retryable error, not attempting fallback: {error_msg}")
                raise

            remaining = tiers[i + 1:]
            if remaining:
                logger.warning(
                    f"⚠️ {tier_id} failed ({error_msg}). "
                    f"Falling back to: {remaining[0]} (delay: {FALLBACK_DELAY_SECONDS}s)"
                )
                await asyncio.sleep(FALLBACK_DELAY_SECONDS)
            else:
                logger.error(f"❌ All providers exhausted. Last: {tier_id}, error: {error_msg}")

    # All providers failed
    raise last_error
