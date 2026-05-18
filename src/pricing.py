"""
Model pricing — Single Source of Truth.

Every EUR cost in the Bridge — budget-gate pre-call estimate, post-call
budget deduction, metrics dashboard, invoices — is computed from THIS
table. Previously four divergent copies existed (metrics/_DEFAULT_PRICING,
providers/registry, tenant/usage_tracker, an inline gate estimate in
main.py); they had already started to drift. Consolidated 2026-05-18.

Prices are USD per 1M tokens (Anthropic / OpenAI list prices). EUR
conversion via USD_TO_EUR_RATE (env, default 0.92) — a fixed rate keeps
invoices predictable.
"""
from __future__ import annotations

import json
import os

# USD per 1M tokens. {model_id: {"in": input_price, "out": output_price}}
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-4-5":          {"in": 3.00,  "out": 15.00},
    "claude-sonnet-4-5-20250929": {"in": 3.00,  "out": 15.00},
    "claude-sonnet-4-6":          {"in": 3.00,  "out": 15.00},
    "claude-opus-4":              {"in": 15.00, "out": 75.00},
    "claude-opus-4-7":            {"in": 15.00, "out": 75.00},
    "claude-haiku-4-5":           {"in": 1.00,  "out": 5.00},
    "claude-haiku-4-5-20251001":  {"in": 1.00,  "out": 5.00},
    "gpt-5":                      {"in": 5.00,  "out": 15.00},
    "gpt-5-mini":                 {"in": 0.30,  "out": 1.20},
}

_DEFAULT_USD_TO_EUR = 0.92


def usd_to_eur_rate() -> float:
    """Fixed USD→EUR rate for invoice predictability. Override via env."""
    try:
        return float(os.environ.get("USD_TO_EUR_RATE", str(_DEFAULT_USD_TO_EUR)))
    except (TypeError, ValueError):
        return _DEFAULT_USD_TO_EUR


def load_pricing() -> dict[str, dict[str, float]]:
    """MODEL_PRICING merged with the optional MODEL_PRICING_JSON env override."""
    pricing = {model: dict(p) for model, p in MODEL_PRICING.items()}
    override = os.environ.get("MODEL_PRICING_JSON", "")
    if override:
        try:
            pricing.update(json.loads(override))
        except (ValueError, TypeError):
            pass
    return pricing


def cost_eur(model: str | None, input_tokens: int, output_tokens: int) -> float:
    """
    EUR cost of one LLM call. Unknown / missing model → 0.0; the caller
    decides how to surface that (metrics logs it on the aggregate level,
    the deduction treats 0.0 as "nothing to deduct").
    """
    if not model:
        return 0.0
    p = load_pricing().get(model)
    if p is None:
        return 0.0
    usd = (
        (input_tokens or 0) / 1_000_000.0 * p["in"]
        + (output_tokens or 0) / 1_000_000.0 * p["out"]
    )
    return round(usd * usd_to_eur_rate(), 6)
