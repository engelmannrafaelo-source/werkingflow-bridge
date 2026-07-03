"""
Hypothetical cost calculator for sandbox usage events.

If the Daemon already computes hypothetical_cost_eur, Bridge stores it directly.
This module is only used as a fallback if the Daemon omits the field.

Thin wrapper around the pricing SSoT (src/pricing.py) — this module used to
carry its own PRICING_TABLE copy, which had already drifted (opus-4-6/4-7 at
the old 15/75 Opus-4 price, unknown models silently priced as Sonnet). The
signature stays unchanged for the daemon-fallback call-site.
"""
from src.pricing import cost_eur


def compute_hypothetical_cost_eur(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    return cost_eur(
        model,
        input_tokens,
        output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
    )
