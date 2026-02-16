"""
Pre-request Budget Enforcement for AI-Bridge.

Checks billing_mode and budget/token limits BEFORE processing a request.
Returns HTTP 402 Payment Required if budget is exceeded.

Budget Rules:
- billing_mode "demo": Always allowed (free, dev partners)
- billing_mode "byo_key": Always allowed (user pays directly)
- billing_mode "platform_managed": Check monthly_token_limit and budget_limit_eur
"""

import os
import time
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

# Try to import httpx for async HTTP calls
try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

from .client import TenantSettings


@dataclass
class BudgetCheckResult:
    """Result of a pre-request budget check."""
    allowed: bool
    reason: Optional[str] = None
    billing_mode: str = "platform_managed"
    # Current usage
    current_tokens: int = 0
    current_vision_calls: int = 0
    current_cost_usd: float = 0.0
    # Limits
    token_limit: Optional[int] = None
    vision_limit: Optional[int] = None
    budget_limit_eur: Optional[float] = None
    # Percentages
    token_usage_percent: float = 0.0
    budget_usage_percent: float = 0.0


class BudgetCache:
    """
    In-memory cache for monthly usage to avoid hitting Supabase on every request.

    Cache is per-tenant with configurable TTL (default 60 seconds).
    """

    def __init__(self, ttl_seconds: int = 60):
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[str, Tuple[BudgetCheckResult, float]] = {}

    def get(self, tenant_id: str) -> Optional[BudgetCheckResult]:
        """Get cached budget result if not expired."""
        entry = self._cache.get(tenant_id)
        if entry and time.time() - entry[1] < self.ttl_seconds:
            return entry[0]
        return None

    def set(self, tenant_id: str, result: BudgetCheckResult) -> None:
        """Cache budget result."""
        self._cache[tenant_id] = (result, time.time())

    def invalidate(self, tenant_id: str) -> None:
        """Invalidate cache for a tenant (e.g., after usage update)."""
        self._cache.pop(tenant_id, None)

    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()


# Global cache instance
_budget_cache = BudgetCache(ttl_seconds=60)


def get_budget_cache() -> BudgetCache:
    """Get singleton budget cache."""
    return _budget_cache


async def check_budget(tenant: TenantSettings) -> BudgetCheckResult:
    """
    Check if tenant can make another AI request.

    Rules:
    - billing_mode "demo": Always allowed (free, dev partners)
    - billing_mode "byo_key": Always allowed (user pays directly to Anthropic)
    - billing_mode "platform_managed": Check limits
      → monthly_token_limit vs. current month usage
      → budget_limit_eur vs. current month cost
      → Return 402 if exceeded

    Args:
        tenant: TenantSettings from validate_api_key()

    Returns:
        BudgetCheckResult with allowed=True/False and usage details
    """
    # Demo mode: always allowed (free)
    if tenant.is_demo_mode():
        return BudgetCheckResult(
            allowed=True,
            billing_mode="demo",
            reason="Demo mode - no limits"
        )

    # BYO Key mode: always allowed (user pays directly)
    if tenant.is_byo_key():
        return BudgetCheckResult(
            allowed=True,
            billing_mode="byo_key",
            reason="BYO key - user pays directly"
        )

    # Platform managed: check limits
    # First check cache
    cache = get_budget_cache()
    cached = cache.get(tenant.tenant_id)
    if cached:
        logger.debug(f"Budget cache hit for {tenant.tenant_slug}")
        return cached

    # Query current month usage from Supabase
    usage = await _get_monthly_usage(tenant.tenant_id)

    # Calculate percentages
    token_percent = 0.0
    if tenant.monthly_token_limit and tenant.monthly_token_limit > 0:
        token_percent = (usage["total_tokens"] / tenant.monthly_token_limit) * 100

    budget_percent = 0.0
    if tenant.budget_limit_eur and tenant.budget_limit_eur > 0:
        # Convert USD to EUR (rough estimate, use 0.92 rate)
        cost_eur = usage["billable_cost_usd"] * 0.92
        budget_percent = (cost_eur / tenant.budget_limit_eur) * 100

    # Build result
    result = BudgetCheckResult(
        allowed=True,
        billing_mode=tenant.billing_mode,
        current_tokens=usage["total_tokens"],
        current_vision_calls=usage["total_vision_calls"],
        current_cost_usd=usage["billable_cost_usd"],
        token_limit=tenant.monthly_token_limit,
        vision_limit=tenant.monthly_vision_limit,
        budget_limit_eur=tenant.budget_limit_eur,
        token_usage_percent=token_percent,
        budget_usage_percent=budget_percent,
    )

    # Check token limit
    if tenant.monthly_token_limit and usage["total_tokens"] >= tenant.monthly_token_limit:
        result.allowed = False
        result.reason = (
            f"Monthly token limit exceeded: {usage['total_tokens']:,} / {tenant.monthly_token_limit:,} tokens. "
            f"Please purchase more credits or upgrade your plan."
        )
        logger.warning(f"Budget exceeded for {tenant.tenant_slug}: {result.reason}")

    # Check vision limit
    elif tenant.monthly_vision_limit and usage["total_vision_calls"] >= tenant.monthly_vision_limit:
        result.allowed = False
        result.reason = (
            f"Monthly vision limit exceeded: {usage['total_vision_calls']:,} / {tenant.monthly_vision_limit:,} calls. "
            f"Please purchase more credits or upgrade your plan."
        )
        logger.warning(f"Vision limit exceeded for {tenant.tenant_slug}: {result.reason}")

    # Check budget limit (in EUR)
    elif tenant.budget_limit_eur:
        cost_eur = usage["billable_cost_usd"] * 0.92
        if cost_eur >= tenant.budget_limit_eur:
            result.allowed = False
            result.reason = (
                f"Monthly budget exceeded: EUR {cost_eur:.2f} / EUR {tenant.budget_limit_eur:.2f}. "
                f"Please increase your budget limit or contact support."
            )
            logger.warning(f"Budget exceeded for {tenant.tenant_slug}: {result.reason}")

    # Cache result (even if over limit - will be refreshed on TTL)
    cache.set(tenant.tenant_id, result)

    return result


async def _get_monthly_usage(tenant_id: str) -> Dict:
    """
    Get current month usage from Supabase via RPC.

    Returns:
        Dict with total_tokens, total_vision_calls, billable_cost_usd
    """
    supabase_url = os.getenv("WERKFLOW_SUPABASE_URL")
    supabase_key = os.getenv("WERKFLOW_SUPABASE_KEY")

    if not supabase_url or not supabase_key or not HTTPX_AVAILABLE:
        logger.warning("Supabase not configured - returning zero usage")
        return {
            "total_tokens": 0,
            "total_vision_calls": 0,
            "total_cost_usd": 0.0,
            "billable_cost_usd": 0.0,
        }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{supabase_url}/rest/v1/rpc/get_tenant_monthly_usage",
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                    "Content-Type": "application/json"
                },
                json={"p_tenant_id": tenant_id}
            )

            if response.status_code != 200:
                logger.warning(f"Failed to get monthly usage: {response.status_code}")
                return {
                    "total_tokens": 0,
                    "total_vision_calls": 0,
                    "total_cost_usd": 0.0,
                    "billable_cost_usd": 0.0,
                }

            data = response.json()

            # RPC returns array with one row
            if data and len(data) > 0:
                row = data[0]
                return {
                    "total_tokens": int(row.get("total_tokens", 0) or 0),
                    "total_vision_calls": int(row.get("total_vision_calls", 0) or 0),
                    "total_cost_usd": float(row.get("total_cost_usd", 0) or 0),
                    "billable_cost_usd": float(row.get("billable_cost_usd", 0) or 0),
                }

            return {
                "total_tokens": 0,
                "total_vision_calls": 0,
                "total_cost_usd": 0.0,
                "billable_cost_usd": 0.0,
            }

    except Exception as e:
        logger.error(f"Failed to get monthly usage: {e}")
        return {
            "total_tokens": 0,
            "total_vision_calls": 0,
            "total_cost_usd": 0.0,
            "billable_cost_usd": 0.0,
        }
