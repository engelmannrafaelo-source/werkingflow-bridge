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


# Default pricing (EUR per 1K tokens) - fallback if Supabase not configured
DEFAULT_PRICING = {
    "claude-haiku-4-20250514": {"input": 0.0008, "output": 0.004},
    "claude-sonnet-4-20250514": {"input": 0.003, "output": 0.015},
    "claude-opus-4-5-20251101": {"input": 0.015, "output": 0.075},
    "gemini-2.0-flash": {"input": 0.0001, "output": 0.0004},
}

# Default markup factor (2x = 100% margin)
DEFAULT_MARKUP = 2.0


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

    def calculate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int
    ) -> float:
        """
        Calculate request cost in EUR.

        Args:
            model: Model name
            input_tokens: Input token count
            output_tokens: Output token count

        Returns:
            Cost in EUR (including markup)
        """
        # Get pricing for model
        pricing = DEFAULT_PRICING.get(model)

        if not pricing:
            # Unknown model - use Sonnet pricing as fallback
            logger.warning(f"Unknown model pricing: {model}, using Sonnet fallback")
            pricing = DEFAULT_PRICING["claude-sonnet-4-20250514"]

        # Calculate raw cost
        input_cost = (input_tokens / 1000) * pricing["input"]
        output_cost = (output_tokens / 1000) * pricing["output"]
        raw_cost = input_cost + output_cost

        # Apply markup
        final_cost = raw_cost * self.markup_factor

        return round(final_cost, 6)

    async def track(self, record: UsageRecord) -> None:
        """
        Track usage record (fire-and-forget).

        Args:
            record: Usage record to log
        """
        if not self.tenant_client.enabled:
            logger.debug("Usage tracking disabled (Supabase not configured)")
            return

        # Calculate cost
        cost_eur = self.calculate_cost(
            record.model,
            record.input_tokens,
            record.output_tokens
        )

        # Log to Supabase (async, non-blocking)
        try:
            await self.tenant_client.log_usage(
                tenant_id=record.tenant_id,
                model=record.model,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                cost_eur=cost_eur,
                privacy_mode=record.privacy_mode,
                workflow_id=record.workflow_id,
                job_id=record.job_id,
                endpoint=record.endpoint,
                latency_ms=record.latency_ms,
                status=record.status,
                error_message=record.error_message,
                pii_detected=record.pii_detected,
                pii_entities_count=record.pii_entities_count
            )

            logger.debug(
                f"Usage tracked: {record.tenant_id} - {record.model} "
                f"({record.input_tokens}+{record.output_tokens} tokens = {cost_eur})"
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
