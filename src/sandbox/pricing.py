"""
Hypothetical cost calculator for sandbox usage events.

If the Daemon already computes hypothetical_cost_eur, Bridge stores it directly.
This module is only used as a fallback if the Daemon omits the field (future-proofing).

Prices: USD per 1M tokens, then converted to EUR at EUR_PER_USD.
Update PRICING_TABLE when Anthropic changes rates.
"""
from typing import Optional

EUR_PER_USD = 0.92  # rough fixed rate; replace with live FX if required

PRICING_TABLE: dict[str, dict[str, float]] = {
    # model_prefix → {input, output, cache_read, cache_write} per 1M tokens in USD
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5":  {"input": 1.00, "output":  5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-opus-4-7":   {"input": 15.0, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
    "claude-opus-4-6":   {"input": 15.0, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
}

_DEFAULT_PRICING = {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75}


def _get_pricing(model: str) -> dict[str, float]:
    for prefix, rates in PRICING_TABLE.items():
        if model.startswith(prefix):
            return rates
    return _DEFAULT_PRICING


def compute_hypothetical_cost_eur(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    rates = _get_pricing(model)
    cost_usd = (
        input_tokens          * rates["input"]       / 1_000_000
        + output_tokens       * rates["output"]      / 1_000_000
        + cache_read_tokens   * rates["cache_read"]  / 1_000_000
        + cache_creation_tokens * rates["cache_write"] / 1_000_000
    )
    return round(cost_usd * EUR_PER_USD, 6)
