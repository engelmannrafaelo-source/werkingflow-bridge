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
import re

# Bump when MODEL_PRICING changes so usage_events rows stay attributable to a
# specific price table snapshot. Stored in usage_events.pricing_version.
# v2 (2026-07-03): opus-4-7 corrected 15/75 -> 5/25 (15/75 is the Opus 4/4.1
#                  price; Opus 4.5+ lists at 5/25), opus-4-6/4-8 added,
#                  cache pricing introduced (write 1.25x in, read 0.1x in).
# v3 (2026-07-05): opus-4-5 + opus-4-1 ergaenzt (4-5 fehlte trotz
#                  Registry-Eintrag -> unknown-model warning; 4-1 = 15/75-Tier).
PRICING_VERSION = "v4"

# USD per 1M tokens. {model_id: {"in": input_price, "out": output_price}}
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-sonnet-5":            {"in": 3.00,  "out": 15.00},  # Liste; Intro $2/$10 bis 2026-08-31 ggf. via MODEL_PRICING_JSON
    "claude-sonnet-4-5":          {"in": 3.00,  "out": 15.00},
    "claude-sonnet-4-5-20250929": {"in": 3.00,  "out": 15.00},
    "claude-sonnet-4-6":          {"in": 3.00,  "out": 15.00},
    "claude-opus-4":              {"in": 15.00, "out": 75.00},
    "claude-opus-4-1":            {"in": 15.00, "out": 75.00},
    "claude-opus-4-5":            {"in": 5.00,  "out": 25.00},
    "claude-opus-4-5-20251101":   {"in": 5.00,  "out": 25.00},
    "claude-opus-4-6":            {"in": 5.00,  "out": 25.00},
    "claude-opus-4-7":            {"in": 5.00,  "out": 25.00},
    "claude-opus-4-8":            {"in": 5.00,  "out": 25.00},
    "claude-haiku-4-5":           {"in": 1.00,  "out": 5.00},
    "claude-haiku-4-5-20251001":  {"in": 1.00,  "out": 5.00},
    "gpt-5":                      {"in": 5.00,  "out": 15.00},
    "gpt-5-mini":                 {"in": 0.30,  "out": 1.20},
}

# Prompt-cache token pricing, as a multiple of the model's input price.
# Anthropic and Bedrock use the same ratios: 5-min cache write 1.25x,
# cache read 0.1x.
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10

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


def normalize_model_id(model: str) -> str:
    """Map provider-specific model IDs onto the Anthropic IDs this table uses.

    Bedrock IDs ('eu.anthropic.claude-sonnet-4-5-20250929-v1:0') carry the
    same list price as their Anthropic counterpart — normalising here keeps
    Bedrock out of the price table and pricing in one ID space.
    """
    if ".anthropic." in model or model.startswith("anthropic."):
        from src.model_registry import from_bedrock_model_id
        try:
            return from_bedrock_model_id(model)
        except ValueError:
            return model
    return model


def cost_eur(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """
    EUR cost of one LLM call. Unknown / missing model → 0.0; the caller
    decides how to surface that (metrics logs it on the aggregate level,
    the deduction treats 0.0 as "nothing to deduct").

    input_tokens is the UNCACHED input (Anthropic/Bedrock usage semantics);
    cache writes and reads are billed at their own rates on top.
    """
    if not model:
        return 0.0
    pricing = load_pricing()
    model_id = normalize_model_id(model)
    p = pricing.get(model_id)
    if p is None:
        # Date-suffixed IDs (claude-opus-4-7-20260115) price as their base
        # model — keeps the table free of every snapshot variant.
        p = pricing.get(re.sub(r"-\d{8}$", "", model_id))
    if p is None:
        return 0.0
    usd = (
        (input_tokens or 0) / 1_000_000.0 * p["in"]
        + (cache_creation_tokens or 0) / 1_000_000.0 * p["in"] * CACHE_WRITE_MULT
        + (cache_read_tokens or 0) / 1_000_000.0 * p["in"] * CACHE_READ_MULT
        + (output_tokens or 0) / 1_000_000.0 * p["out"]
    )
    return round(usd * usd_to_eur_rate(), 6)
