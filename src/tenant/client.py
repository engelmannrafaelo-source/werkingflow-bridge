"""
Supabase Client for Tenant Lookup

Handles:
- Tenant settings retrieval with caching
- API key validation
- Usage logging
"""

import os
import hashlib
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from functools import lru_cache
import logging

logger = logging.getLogger(__name__)

# Try to import httpx for async HTTP calls
try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    logger.warning("httpx not available - tenant lookup will be disabled")


@dataclass
class TenantSettings:
    """Tenant configuration for AI requests."""
    tenant_id: str
    tenant_slug: str
    privacy_mode: str = "full"  # none | basic | full
    allowed_models: List[str] = field(default_factory=lambda: ["claude-sonnet-4-5-20250929", "claude-haiku-4-5-20251001"])
    rate_limit_rpm: int = 60
    budget_limit_eur: float = 1000.0
    budget_alert_threshold: float = 0.8
    is_enabled: bool = True
    # Billing fields (added for usage tracking)
    billing_mode: str = "platform_managed"  # demo | byo_key | platform_managed
    monthly_token_limit: Optional[int] = None  # None = unlimited
    monthly_vision_limit: Optional[int] = None  # None = unlimited
    billing_margin: float = 1.50  # 1.50 = 50% markup
    plan: str = "free"  # free | trial | starter | pro | enterprise
    tenant_name: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TenantSettings":
        """Create TenantSettings from Supabase response."""
        return cls(
            tenant_id=data.get("tenant_id", ""),
            tenant_slug=data.get("tenant_slug", ""),
            tenant_name=data.get("tenant_name", ""),
            privacy_mode=data.get("privacy_mode", "full"),
            allowed_models=data.get("allowed_models", ["claude-sonnet-4-5-20250929"]),
            rate_limit_rpm=data.get("rate_limit_rpm", 60),
            budget_limit_eur=float(data.get("budget_limit_eur", 1000.0) or 1000.0),
            budget_alert_threshold=float(data.get("budget_alert_threshold", 0.8) or 0.8),
            is_enabled=data.get("is_enabled", True),
            billing_mode=data.get("billing_mode", "platform_managed"),
            monthly_token_limit=data.get("monthly_token_limit"),  # Can be None
            monthly_vision_limit=data.get("monthly_vision_limit"),  # Can be None
            billing_margin=float(data.get("billing_margin", 1.50) or 1.50),
            plan=data.get("plan", "free"),
        )

    def is_model_allowed(self, model: str) -> bool:
        """Check if model is in allowed list."""
        if not self.allowed_models:
            return True  # No restrictions
        return model in self.allowed_models

    def is_budget_unlimited(self) -> bool:
        """Check if tenant has unlimited token budget."""
        return self.monthly_token_limit is None

    def is_demo_mode(self) -> bool:
        """Check if tenant is in demo (free) mode."""
        return self.billing_mode == "demo"

    def is_byo_key(self) -> bool:
        """Check if tenant uses their own API key."""
        return self.billing_mode == "byo_key"


@dataclass
class CacheEntry:
    """Cache entry with TTL."""
    settings: TenantSettings
    expires_at: datetime


class SupabaseTenantClient:
    """
    Client for tenant lookup from Werkflow Supabase.

    Provides:
    - API key validation via validate_tenant_api_key() RPC
    - Tenant settings caching (5 min TTL)
    - Usage logging via ai_usage_logs table
    """

    def __init__(
        self,
        supabase_url: Optional[str] = None,
        supabase_key: Optional[str] = None,
        cache_ttl_seconds: int = 300  # 5 minutes
    ):
        """
        Initialize Supabase tenant client.

        Args:
            supabase_url: Supabase project URL (or WERKFLOW_SUPABASE_URL env)
            supabase_key: Supabase service role key (or WERKFLOW_SUPABASE_KEY env)
            cache_ttl_seconds: Cache TTL for tenant settings
        """
        self.supabase_url = supabase_url or os.getenv("WERKFLOW_SUPABASE_URL")
        self.supabase_key = supabase_key or os.getenv("WERKFLOW_SUPABASE_KEY")
        self.cache_ttl = timedelta(seconds=cache_ttl_seconds)

        # In-memory cache for tenant settings
        self._cache: Dict[str, CacheEntry] = {}
        self._cache_lock = asyncio.Lock()

        # Validate configuration
        if not self.supabase_url or not self.supabase_key:
            logger.warning(
                "Werkflow Supabase not configured - tenant routing disabled. "
                "Set WERKFLOW_SUPABASE_URL and WERKFLOW_SUPABASE_KEY to enable."
            )
            self.enabled = False
        else:
            self.enabled = HTTPX_AVAILABLE
            if self.enabled:
                logger.info(f"Werkflow Supabase tenant client enabled: {self.supabase_url}")

    def _hash_api_key(self, api_key: str) -> str:
        """Hash API key for lookup (SHA-256)."""
        return hashlib.sha256(api_key.encode()).hexdigest()

    async def validate_api_key(self, api_key: str) -> Optional[TenantSettings]:
        """
        Validate tenant API key and return settings.

        Calls Supabase RPC function: validate_tenant_api_key(key_hash)

        Args:
            api_key: Raw API key from X-Tenant-API-Key header

        Returns:
            TenantSettings if valid, None if invalid/not found
        """
        if not self.enabled:
            return None

        key_hash = self._hash_api_key(api_key)

        # Check cache first
        cached = await self._get_cached(key_hash)
        if cached:
            logger.debug(f"Tenant cache hit: {cached.tenant_slug}")
            return cached

        # Query Supabase
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{self.supabase_url}/rest/v1/rpc/validate_tenant_api_key",
                    headers={
                        "apikey": self.supabase_key,
                        "Authorization": f"Bearer {self.supabase_key}",
                        "Content-Type": "application/json"
                    },
                    json={"p_key_hash": key_hash}
                )

                if response.status_code != 200:
                    logger.warning(f"Supabase API key validation failed: {response.status_code}")
                    return None

                data = response.json()

                if not data or len(data) == 0:
                    logger.info(f"Invalid tenant API key (no match)")
                    return None

                # RPC returns array, take first row
                row = data[0] if isinstance(data, list) else data

                settings = TenantSettings.from_dict(row)

                # Cache the result
                await self._set_cached(key_hash, settings)

                logger.info(
                    f"Tenant validated: {settings.tenant_slug} (privacy={settings.privacy_mode})"
                )

                return settings

        except httpx.TimeoutException:
            logger.error("Supabase tenant lookup timeout")
            return None
        except Exception as e:
            logger.error(f"Supabase tenant lookup error: {e}", exc_info=True)
            return None

    async def update_key_last_used(self, api_key: str) -> None:
        """
        Update API key last_used_at timestamp (async, fire-and-forget).

        Args:
            api_key: Raw API key
        """
        if not self.enabled:
            return

        key_hash = self._hash_api_key(api_key)

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{self.supabase_url}/rest/v1/rpc/update_api_key_last_used",
                    headers={
                        "apikey": self.supabase_key,
                        "Authorization": f"Bearer {self.supabase_key}",
                        "Content-Type": "application/json"
                    },
                    json={"p_key_hash": key_hash}
                )
        except Exception as e:
            # Non-critical - just log
            logger.debug(f"Failed to update API key last_used: {e}")

    async def log_usage(
        self,
        tenant_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: float,
        operation: str = "prompt",  # prompt | vision | tool_use
        billing_mode: str = "platform_managed",
        image_count: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        user_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Log AI usage to Supabase ai_usage_events table for billing.

        This triggers the PostgreSQL trigger that auto-aggregates to ai_usage_monthly.

        Args:
            tenant_id: Tenant UUID
            model: Model name used (e.g., claude-sonnet-4-5-20250929)
            input_tokens: Input token count
            output_tokens: Output token count
            estimated_cost_usd: Calculated cost in USD (Anthropic pricing)
            operation: Operation type (prompt, vision, tool_use)
            billing_mode: Billing mode (demo, byo_key, platform_managed)
            image_count: Number of images processed (for vision)
            cache_read_tokens: Cached tokens read
            cache_write_tokens: Cached tokens written
            user_id: Optional user UUID
            metadata: Optional JSONB metadata (endpoint, latency, pii_info, etc.)
        """
        if not self.enabled:
            return

        try:
            # Build payload matching ai_usage_events schema
            payload = {
                "tenant_id": tenant_id,
                "model": model,
                "operation": operation,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
                "image_count": image_count,
                "estimated_cost_usd": estimated_cost_usd,
                "billing_mode": billing_mode,
                "metadata": metadata or {}
            }

            # Add user_id if provided
            if user_id:
                payload["user_id"] = user_id

            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    f"{self.supabase_url}/rest/v1/ai_usage_events",  # FIXED: was ai_usage_logs
                    headers={
                        "apikey": self.supabase_key,
                        "Authorization": f"Bearer {self.supabase_key}",
                        "Content-Type": "application/json",
                        "Prefer": "return=minimal"
                    },
                    json=payload
                )

                if response.status_code >= 400:
                    logger.warning(f"Failed to log usage: {response.status_code} - {response.text}")
                else:
                    logger.debug(f"Usage logged for tenant {tenant_id}: {model} ({input_tokens}+{output_tokens} tokens)")

        except Exception as e:
            # Non-critical - just log, don't fail the request
            logger.warning(f"Failed to log usage: {e}")

    async def _get_cached(self, key_hash: str) -> Optional[TenantSettings]:
        """Get cached tenant settings if not expired."""
        async with self._cache_lock:
            entry = self._cache.get(key_hash)
            if entry and entry.expires_at > datetime.now():
                return entry.settings
            elif entry:
                # Expired - remove
                del self._cache[key_hash]
            return None

    async def _set_cached(self, key_hash: str, settings: TenantSettings) -> None:
        """Cache tenant settings."""
        async with self._cache_lock:
            self._cache[key_hash] = CacheEntry(
                settings=settings,
                expires_at=datetime.now() + self.cache_ttl
            )

    def clear_cache(self) -> None:
        """Clear all cached tenant settings."""
        self._cache.clear()
        logger.info("Tenant cache cleared")


# Singleton instance
_tenant_client: Optional[SupabaseTenantClient] = None


def get_tenant_client() -> SupabaseTenantClient:
    """Get singleton Supabase tenant client."""
    global _tenant_client
    if _tenant_client is None:
        _tenant_client = SupabaseTenantClient()
    return _tenant_client
