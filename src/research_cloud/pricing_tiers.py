"""Customer-facing price staffle per research depth (Weg C).

Rafael-Go 2026-07-25: a fixed price per depth instead of cost-pass-through
(DESIGN.md "Kundenpreis: fixe Staffel pro Depth ... Startwerte als
Platzhalter"). The quick/standard/deep values are the ones Rafael actually
named; ``exhaustive`` is NOT part of that decision — the default below is a
documented extrapolation, not a confirmed number. All four are env-overridable
so Rafael can set final numbers without a code change.

OPEN POINT (flagged, not resolved by this build — see specs/research-cloud-
overflow/ notes): this value is currently only persisted into
provider_metadata.customer_price_eur for observability. It is NOT wired into
the actual customer/tenant deduction path — that still runs on the real
computed cost (tokens + search fees) via persist_ai_call_activity's normal
hypothetical_cost_eur, same as every other provider. Wiring a depth-based
flat charge into the budget-deduction path instead of computed cost is a
billing-model decision beyond this build's scope.
"""
from __future__ import annotations

import os
from typing import Optional

_DEFAULT_PRICES_EUR = {
    "quick": 1.0,
    "standard": 3.0,
    "deep": 8.0,
    "exhaustive": 12.0,  # extrapolated — not part of the 2026-07-25 decision, confirm with Rafael
}

_ENV_KEYS = {
    "quick": "RESEARCH_CLOUD_PRICE_QUICK_EUR",
    "standard": "RESEARCH_CLOUD_PRICE_STANDARD_EUR",
    "deep": "RESEARCH_CLOUD_PRICE_DEEP_EUR",
    "exhaustive": "RESEARCH_CLOUD_PRICE_EXHAUSTIVE_EUR",
}

_DEFAULT_DEPTH = "standard"


def customer_price_eur(depth: Optional[str]) -> float:
    depth_key = depth if depth in _DEFAULT_PRICES_EUR else _DEFAULT_DEPTH
    env_key = _ENV_KEYS[depth_key]
    try:
        return float(os.getenv(env_key, str(_DEFAULT_PRICES_EUR[depth_key])))
    except (TypeError, ValueError):
        return _DEFAULT_PRICES_EUR[depth_key]
