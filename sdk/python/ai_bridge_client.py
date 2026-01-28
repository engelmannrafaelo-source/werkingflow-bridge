"""
AI-Bridge Client SDK
====================

Smart URL resolution with optional fallback from Hetzner to local.

Usage:
    from ai_bridge_sdk import get_bridge_url, create_client

    # Simple (Hetzner only, fail if unavailable)
    url = get_bridge_url()

    # With fallback to local
    url = get_bridge_url(fallback_enabled=True)

    # Create OpenAI-compatible client
    client = create_client(fallback_enabled=True)

Environment Variables:
    WRAPPER_URL: Override URL (disables fallback logic)
    AI_BRIDGE_FALLBACK: Enable fallback globally ("true" or "1")
"""

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("ai_bridge_sdk")

# Constants
HETZNER_URL = "http://49.12.72.66:8000"
LOCAL_URL = "http://localhost:8000"
HETZNER_TIMEOUT = 3.0  # seconds
LOCAL_TIMEOUT = 1.0  # seconds


class AIBridgeConnectionError(Exception):
    """Raised when no AI-Bridge instance is reachable."""

    pass


def health_check(url: str, timeout: float) -> bool:
    """
    Check if AI-Bridge is reachable at the given URL.

    Args:
        url: Base URL of the AI-Bridge instance
        timeout: Request timeout in seconds

    Returns:
        True if health check passes, False otherwise
    """
    try:
        response = httpx.get(f"{url}/health", timeout=timeout)
        return response.status_code == 200
    except (httpx.RequestError, httpx.TimeoutException):
        return False


def _is_fallback_enabled_env() -> bool:
    """Check if fallback is enabled via environment variable."""
    value = os.getenv("AI_BRIDGE_FALLBACK", "").lower()
    return value in ("true", "1", "yes")


def get_bridge_url(fallback_enabled: Optional[bool] = None) -> str:
    """
    Resolve the AI-Bridge URL with optional fallback.

    Priority:
    1. WRAPPER_URL env var (explicit override, no fallback)
    2. Hetzner (if reachable)
    3. localhost:8000 (if fallback enabled and Hetzner unavailable)
    4. Raise AIBridgeConnectionError (if nothing reachable)

    Args:
        fallback_enabled: Enable fallback to local. If None, checks
                         AI_BRIDGE_FALLBACK env var.

    Returns:
        The resolved AI-Bridge URL

    Raises:
        AIBridgeConnectionError: If no AI-Bridge instance is reachable
    """
    # Check for explicit URL override
    if override_url := os.getenv("WRAPPER_URL"):
        logger.info(f"Using override URL: {override_url}")
        return override_url

    # Determine if fallback is enabled
    if fallback_enabled is None:
        fallback_enabled = _is_fallback_enabled_env()

    # Try Hetzner first
    if health_check(HETZNER_URL, HETZNER_TIMEOUT):
        logger.info(f"Using Hetzner: {HETZNER_URL}")
        return HETZNER_URL

    logger.warning(f"Hetzner unavailable: {HETZNER_URL}")

    # Try local fallback if enabled
    if fallback_enabled:
        if health_check(LOCAL_URL, LOCAL_TIMEOUT):
            logger.warning(f"Fallback to local: {LOCAL_URL}")
            return LOCAL_URL
        logger.error(f"Local also unavailable: {LOCAL_URL}")

    # Nothing reachable - fail loud
    msg = f"AI-Bridge not reachable. Hetzner: {HETZNER_URL}, Local: {LOCAL_URL}"
    if not fallback_enabled:
        msg += " (fallback disabled, set AI_BRIDGE_FALLBACK=true to enable)"
    raise AIBridgeConnectionError(msg)


def _get_api_key(api_key: Optional[str] = None) -> str:
    """Get API key from parameter or environment variable."""
    key = api_key or os.getenv("AI_BRIDGE_API_KEY")
    if not key:
        raise ValueError(
            "[ai-bridge-sdk] AI_BRIDGE_API_KEY not set. "
            "Set it in environment or pass via api_key parameter."
        )
    return key


def create_client(
    fallback_enabled: Optional[bool] = None,
    api_key: Optional[str] = None,
):
    """
    Create an OpenAI-compatible client connected to AI-Bridge.

    Args:
        fallback_enabled: Enable fallback to local. If None, checks
                         AI_BRIDGE_FALLBACK env var.
        api_key: API key. If not provided, reads from AI_BRIDGE_API_KEY env var.

    Returns:
        OpenAI client configured for AI-Bridge

    Raises:
        AIBridgeConnectionError: If no AI-Bridge instance is reachable
        ValueError: If API key is not configured
        ImportError: If openai package is not installed
    """
    try:
        from openai import OpenAI
    except ImportError as e:
        raise ImportError(
            "openai package required. Install with: pip install openai"
        ) from e

    base_url = get_bridge_url(fallback_enabled=fallback_enabled)
    resolved_api_key = _get_api_key(api_key)

    return OpenAI(
        base_url=f"{base_url}/v1",
        api_key=resolved_api_key,
    )


# ============================================
# CACHED URL FOR SYNC ACCESS
# ============================================

_cached_url: Optional[str] = None


def initialize_bridge_url(fallback_enabled: Optional[bool] = None) -> str:
    """
    Initialize and cache the bridge URL.
    Call this once at app startup, then use get_bridge_url_sync() for sync access.

    Example:
        # In app initialization
        initialize_bridge_url(fallback_enabled=True)

        # Later, in sync code
        url = get_bridge_url_sync()
    """
    global _cached_url
    _cached_url = get_bridge_url(fallback_enabled=fallback_enabled)
    return _cached_url


def get_bridge_url_sync() -> str:
    """
    Get the cached bridge URL synchronously.
    Requires initialize_bridge_url() to be called first.

    Raises:
        RuntimeError: If not initialized
    """
    if _cached_url is None:
        raise RuntimeError(
            "[ai-bridge-sdk] Not initialized. Call initialize_bridge_url() first."
        )
    return _cached_url


def is_initialized() -> bool:
    """Check if the SDK has been initialized."""
    return _cached_url is not None


def get_bridge_url_or_default() -> str:
    """
    Get cached URL or fallback to Hetzner default.
    Use this when you can't guarantee initialization but want a sensible default.
    """
    return _cached_url if _cached_url is not None else HETZNER_URL


if __name__ == "__main__":
    # Quick test
    logging.basicConfig(level=logging.INFO)
    try:
        url = get_bridge_url(fallback_enabled=True)
        print(f"Resolved URL: {url}")
    except AIBridgeConnectionError as e:
        print(f"Error: {e}")
