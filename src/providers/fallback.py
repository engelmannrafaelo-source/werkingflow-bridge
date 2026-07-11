"""Fallback Chain — Automatic provider failover on errors.

Defines fallback chains per primary provider tier:
- claude-premium   → claude-direct-notools (same model, no CLI subprocess,
  skipped when the request needs tools) → openrouter-claude (same model,
  different infra) → bridge-prod-emergency (last resort)
- claude-dsgvo     → (no fallback — DSGVO data must stay in EU)
- openrouter-claude → (no fallback — OpenRouter has its own internal failover)

Triggers: HTTP 429, 500, 502, 503, 504, connect timeout, connection refused,
CLI-subprocess session timeout (asyncio.TimeoutError — the Claude Code SDK
path re-raises this on its own MAX_TIMEOUT ceiling, see claude_cli.py; this
is the failure mode a long Extended Thinking generation hits).
Retries once per fallback provider with a short delay.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from src.auth import AllTokensExhausted
from src.providers.openai_compatible import ProviderError

logger = logging.getLogger(__name__)


# =============================================================================
# FALLBACK CHAIN CONFIGURATION
# =============================================================================

FALLBACK_CHAINS: dict[str, list[str]] = {
    # Claude Code SDK (CLI subprocess) down/hung → direct Anthropic (same
    # model, no tools — get_fallback_tiers() drops this hop for
    # tools_required=True) → OpenRouter → Production Bridge (last resort:
    # token exhaustion)
    "claude-premium": ["claude-direct-notools", "openrouter-claude", "bridge-prod-emergency"],

    # DSGVO: No fallback — data residency must stay in EU (Bedrock Frankfurt)
    # "claude-dsgvo": [],

    # OpenRouter has its own internal failover, no additional chain needed
    # "openrouter-claude": [],
}

# HTTP status codes that trigger a fallback attempt
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Status codes that mean the provider is genuinely BROKEN (≠ rate-limited). A 429
# is a capacity/throttle signal, NOT brokenness — see is_breaker_failure.
BREAKER_FAILURE_STATUS_CODES = {500, 502, 503, 504}

# Circuit-breaker: a provider is "down" at/above this many consecutive genuine
# failures; with no fresh failure for RECOVERY_SECONDS it self-heals. Without
# time-based decay a transient blip pinned a provider "down" until process
# restart (the deadlock the comments below describe).
BREAKER_DOWN_THRESHOLD = 3
BREAKER_RECOVERY_SECONDS = 120

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
        # Self-heal: once the recovery window has elapsed since the last error,
        # report recovered. A non-decaying failure count would pin a provider
        # "down" forever after a transient blip, with no success to reset it.
        if self.last_error is not None and (time.time() - self.last_error) >= BREAKER_RECOVERY_SECONDS:
            return "up"
        if self.consecutive_failures < BREAKER_DOWN_THRESHOLD:
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
    """Record a GENUINE provider failure (brokenness only).

    Callers MUST gate on is_breaker_failure — rate-limits / throttles must never
    reach here, or they pin the provider "down" (see is_breaker_failure).
    """
    health = get_provider_health(tier_id)
    now = time.time()
    # Decay a stale streak so a fresh failure after a long quiet period starts a
    # new streak instead of resuming an old "down" state.
    if health.last_error is not None and (now - health.last_error) >= BREAKER_RECOVERY_SECONDS:
        health.consecutive_failures = 0
    health.last_error = now
    health.last_error_msg = error_msg
    health.consecutive_failures += 1


def reset_provider_health(tier_id: str) -> bool:
    """Reset health state for a provider. Returns True if provider existed."""
    if tier_id in _provider_health:
        _provider_health[tier_id] = ProviderHealth()
        return True
    return False


def reset_all_provider_health() -> int:
    """Reset health state for all providers. Returns count of reset providers."""
    count = len(_provider_health)
    _provider_health.clear()
    return count


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

def get_fallback_tiers(primary_tier: str, tools_required: bool = True) -> list[str]:
    """Get the ordered list of usable fallback tier IDs for a primary provider.

    Returns the full chain: [primary, fallback1, fallback2, ...].
    Tiers that are not usable (missing config/URL) are silently filtered out.
    If no chain is configured, returns just [primary].

    ``tools_required`` (default True — the safe default) drops any tier whose
    ``ProviderConfig.supports_tools`` is False, e.g. ``claude-direct-notools``
    (no CLI subprocess = no tool support). Callers whose request already runs
    with ``enable_tools=False`` should pass ``tools_required=False`` so that
    tier stays eligible — nothing is lost by rerouting a call that was never
    going to use tools anyway.
    """
    from src.providers.registry import is_tier_usable, PROVIDERS

    chain = [primary_tier]
    chain.extend(FALLBACK_CHAINS.get(primary_tier, []))

    usable = []
    for tier_id in chain:
        if tier_id != primary_tier:
            if tools_required and not PROVIDERS.get(tier_id, PROVIDERS[primary_tier]).supports_tools:
                logger.debug(f"🔕 Fallback tier '{tier_id}' filtered (no tool support, request needs tools)")
                continue
            # Filter out tiers that are not configured (e.g. bridge-prod-emergency without URL)
            if not is_tier_usable(tier_id):
                logger.debug(f"🔕 Fallback tier '{tier_id}' filtered (not usable — config missing)")
                continue
        usable.append(tier_id)
    return usable


def is_retryable_error(error: Exception) -> bool:
    """Check if an error should trigger a fallback attempt."""
    # AllTokensExhausted: all dev-bridge OAuth tokens gone → try next fallback
    if isinstance(error, AllTokensExhausted):
        return True

    # ProviderError with retryable status code
    if isinstance(error, ProviderError):
        return error.status_code in RETRYABLE_STATUS_CODES

    # httpx connection errors (timeout, refused, DNS failure)
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
        return True

    # CLI-subprocess session-level timeout (claude_cli.py re-raises this bare
    # on its MAX_TIMEOUT ceiling — NOT a RuntimeError, so needs its own check).
    # This is the failure mode a long Extended Thinking generation hits.
    if isinstance(error, asyncio.TimeoutError):
        return True

    # Generic RuntimeError from Bedrock or other backends
    if isinstance(error, RuntimeError):
        msg = str(error).lower()
        return any(code in msg for code in ["429", "500", "502", "503", "504", "timeout"])

    return False


def is_breaker_failure(error: Exception) -> bool:
    """Whether an error means the provider is genuinely BROKEN (trips the breaker).

    DISTINCT from is_retryable_error: a 429 / throttle (or token exhaustion) is
    retryable — we still fall back — but it must NOT trip the circuit breaker.
    Recording a rate-limit as a failure was the deadlock that pinned
    claude-premium "down" forever: marked down on a transient throttle, no
    success to reset it, no time-based recovery. Only 5xx / connection / timeout
    count as brokenness.
    """
    if isinstance(error, AllTokensExhausted):
        return False  # capacity exhaustion, not brokenness
    if isinstance(error, ProviderError):
        return error.status_code in BREAKER_FAILURE_STATUS_CODES  # excludes 429
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)):
        return True
    # Session-level CLI timeout — genuine brokenness (the worker/subprocess
    # is stuck), not a transient rate-limit, so it trips the breaker.
    if isinstance(error, asyncio.TimeoutError):
        return True
    if isinstance(error, RuntimeError):
        msg = str(error).lower()
        if "429" in msg or "throttle" in msg or "rate limit" in msg or "rate-limit" in msg:
            return False
        return any(code in msg for code in ["500", "502", "503", "504", "timeout", "connection"])
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
