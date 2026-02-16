"""
Usage Tracker for AI Bridge

Tracks AI API usage per tenant for:
- Billing aggregation
- Budget monitoring
- Rate limiting

Pricing is configured in Supabase ai_model_pricing table.
"""

import os
import asyncio
import logging
from typing import Optional, Dict
from dataclasses import dataclass
from datetime import datetime

from .client import get_tenant_client, TenantSettings

logger = logging.getLogger(__name__)


# Default pricing (USD per 1M tokens) - Anthropic pricing as of Jan 2026
# Source: https://www.anthropic.com/pricing
DEFAULT_PRICING_USD = {
    # Sonnet family
    "claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-3-7-sonnet-20250219": {"input": 3.00, "output": 15.00},
    "claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
    # Haiku family
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.00},
    # Opus family
    "claude-opus-4-20250514": {"input": 15.00, "output": 75.00},
    "claude-opus-4-1-20250805": {"input": 15.00, "output": 75.00},
    # Bedrock model IDs (same pricing)
    "eu.anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 3.00, "output": 15.00},
    "eu.anthropic.claude-haiku-4-5-20251001-v1:0": {"input": 0.80, "output": 4.00},
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 3.00, "output": 15.00},
}

# Default markup factor (1.0 = no markup, 1.5 = 50% margin)
# This is applied on top of the billing_margin from TenantSettings
DEFAULT_MARKUP = 1.0


@dataclass
class UsageRecord:
    """Usage record for a single API request."""
    tenant_id: str
    model: str
    input_tokens: int
    output_tokens: int
    privacy_mode: str
    endpoint: str
    latency_ms: int
    status: str
    pii_detected: bool = False
    pii_entities_count: int = 0
    workflow_id: Optional[str] = None
    job_id: Optional[str] = None
    error_message: Optional[str] = None


class UsageTracker:
    """
    Tracks and logs AI usage for billing.

    Usage:
        tracker = UsageTracker()
        await tracker.track(UsageRecord(...))
    """

    def __init__(self, markup_factor: float = DEFAULT_MARKUP):
        """
        Initialize usage tracker.

        Args:
            markup_factor: Price markup (2.0 = double the cost)
        """
        self.markup_factor = float(os.getenv("AI_PRICE_MARKUP", str(markup_factor)))
        self.tenant_client = get_tenant_client()

        logger.info(f"Usage tracker initialized (markup={self.markup_factor}x)")

    def calculate_cost_usd(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int
    ) -> float:
        """
        Calculate request cost in USD.

        Args:
            model: Model name (or Bedrock model ID)
            input_tokens: Input token count
            output_tokens: Output token count

        Returns:
            Cost in USD (raw, without markup - markup applied in Supabase trigger)
        """
        # Get pricing for model
        pricing = DEFAULT_PRICING_USD.get(model)

        if not pricing:
            # Try to find partial match (e.g., "sonnet" in model name)
            model_lower = model.lower()
            if "sonnet" in model_lower:
                pricing = DEFAULT_PRICING_USD["claude-sonnet-4-5-20250929"]
            elif "haiku" in model_lower:
                pricing = DEFAULT_PRICING_USD["claude-haiku-4-5-20251001"]
            elif "opus" in model_lower:
                pricing = DEFAULT_PRICING_USD["claude-opus-4-20250514"]
            else:
                # Unknown model - use Sonnet pricing as fallback
                logger.warning(f"Unknown model pricing: {model}, using Sonnet fallback")
                pricing = DEFAULT_PRICING_USD["claude-sonnet-4-5-20250929"]

        # Calculate raw cost (pricing is per 1M tokens)
        input_cost = (input_tokens / 1_000_000) * pricing["input"]
        output_cost = (output_tokens / 1_000_000) * pricing["output"]
        raw_cost = input_cost + output_cost

        return round(raw_cost, 6)

    async def track(self, record: UsageRecord) -> None:
        """
        Track usage record (fire-and-forget).

        Args:
            record: Usage record to log
        """
        if not self.tenant_client.enabled:
            logger.debug("Usage tracking disabled (Supabase not configured)")
            return

        # Calculate cost in USD
        cost_usd = self.calculate_cost_usd(
            record.model,
            record.input_tokens,
            record.output_tokens
        )

        # Determine operation type from endpoint
        operation = "prompt"
        if "vision" in record.endpoint.lower():
            operation = "vision"
        elif "tool" in record.endpoint.lower():
            operation = "tool_use"

        # Build metadata for additional fields
        metadata = {
            "endpoint": record.endpoint,
            "latency_ms": record.latency_ms,
            "status": record.status,
            "privacy_mode": record.privacy_mode,
        }
        if record.pii_detected:
            metadata["pii_detected"] = record.pii_detected
            metadata["pii_entities_count"] = record.pii_entities_count
        if record.workflow_id:
            metadata["workflow_id"] = record.workflow_id
        if record.job_id:
            metadata["job_id"] = record.job_id
        if record.error_message:
            metadata["error_message"] = record.error_message

        # Log to Supabase (async, non-blocking)
        try:
            await self.tenant_client.log_usage(
                tenant_id=record.tenant_id,
                model=record.model,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                estimated_cost_usd=cost_usd,
                operation=operation,
                billing_mode="platform_managed",  # Will be set correctly via tenant settings
                image_count=1 if operation == "vision" else 0,
                metadata=metadata
            )

            logger.debug(
                f"Usage tracked: {record.tenant_id} - {record.model} "
                f"({record.input_tokens}+{record.output_tokens} tokens = ${cost_usd:.6f})"
            )

        except Exception as e:
            # Non-critical - just log warning
            logger.warning(f"Failed to track usage: {e}")

    def track_async(self, record: UsageRecord) -> None:
        """
        Fire-and-forget usage tracking.

        Creates background task, doesn't block request.
        """
        asyncio.create_task(self.track(record))


# Singleton instance
_usage_tracker: Optional[UsageTracker] = None


def get_usage_tracker() -> UsageTracker:
    """Get singleton usage tracker."""
    global _usage_tracker
    if _usage_tracker is None:
        _usage_tracker = UsageTracker()
    return _usage_tracker


async def track_request_usage(
    tenant: Optional[TenantSettings],
    model: str,
    input_tokens: int,
    output_tokens: int,
    endpoint: str,
    latency_ms: int,
    status: str = "success",
    error_message: Optional[str] = None,
    pii_detected: bool = False,
    pii_entities_count: int = 0,
    workflow_id: Optional[str] = None,
    job_id: Optional[str] = None
) -> None:
    """
    Convenience function to track request usage.

    Args:
        tenant: Tenant settings (or None for anonymous requests)
        model: Model name
        input_tokens: Input token count
        output_tokens: Output token count
        endpoint: API endpoint
        latency_ms: Request latency
        status: Request status
        error_message: Error message if failed
        pii_detected: Whether PII was detected
        pii_entities_count: Number of PII entities
        workflow_id: Optional workflow ID
        job_id: Optional job ID
    """
    if not tenant:
        # No tenant - skip tracking (anonymous request)
        return

    tracker = get_usage_tracker()

    record = UsageRecord(
        tenant_id=tenant.tenant_id,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        privacy_mode=tenant.privacy_mode,
        endpoint=endpoint,
        latency_ms=latency_ms,
        status=status,
        pii_detected=pii_detected,
        pii_entities_count=pii_entities_count,
        workflow_id=workflow_id,
        job_id=job_id,
        error_message=error_message
    )

    # Fire-and-forget
    tracker.track_async(record)
