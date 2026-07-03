"""
Pricing SSoT: cache-token pricing + model-ID robustness (PRICING_VERSION v2).

Guards the two invariants the usage ledger depends on:
  1. cost_eur prices all four token classes (in / out / cache-write 1.25x /
     cache-read 0.1x) — the pre-v2 signature silently dropped cache traffic.
  2. The sandbox pricing wrapper delegates to the SSoT — its own table copy
     had drifted (opus-4-6/4-7 at the old 15/75 Opus-4 price).
"""
import pytest

from src.pricing import (
    CACHE_READ_MULT,
    CACHE_WRITE_MULT,
    cost_eur,
    usd_to_eur_rate,
)
from src.sandbox.pricing import compute_hypothetical_cost_eur


def test_cost_eur_prices_cache_tokens():
    # 1M of each class on sonnet-4-5 (in 3.00 / out 15.00 USD per 1M):
    # 3.00 + 15.00 + 1M*3.00*1.25 + 1M*3.00*0.10 = 22.05 USD
    eur = cost_eur(
        "claude-sonnet-4-5-20250929",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
    )
    expected_usd = 3.00 + 15.00 + 3.00 * CACHE_WRITE_MULT + 3.00 * CACHE_READ_MULT
    assert eur == pytest.approx(expected_usd * usd_to_eur_rate(), abs=1e-6)


def test_cost_eur_cache_defaults_keep_legacy_callers_working():
    # Positional two-token call (budget gate) must be unchanged.
    assert cost_eur("claude-sonnet-4-5", 1_000_000, 0) == pytest.approx(
        3.00 * usd_to_eur_rate(), abs=1e-6
    )


def test_opus_4_7_prices_at_5_25_not_legacy_15_75():
    eur = cost_eur("claude-opus-4-7", 1_000_000, 1_000_000)
    assert eur == pytest.approx((5.00 + 25.00) * usd_to_eur_rate(), abs=1e-6)


def test_date_suffixed_model_prices_as_base_model():
    # Snapshot IDs not in the table fall back to their base model price
    # instead of silently costing 0.
    assert cost_eur("claude-opus-4-7-20260115", 1_000_000, 0) == cost_eur(
        "claude-opus-4-7", 1_000_000, 0
    )


def test_unknown_model_costs_zero():
    assert cost_eur("some-future-model", 1_000_000, 1_000_000) == 0.0


def test_sandbox_wrapper_delegates_to_ssot():
    args = dict(
        input_tokens=123_456,
        output_tokens=7_890,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=50_000,
    )
    assert compute_hypothetical_cost_eur("claude-opus-4-7", **args) == cost_eur(
        "claude-opus-4-7", **args
    )
