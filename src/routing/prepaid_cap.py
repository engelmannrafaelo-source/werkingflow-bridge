"""Fail-open daily spend cap for the direct Anthropic PREPAID vision key.

The vision key (ANTHROPIC_VISION_API_KEY, ledger lane 'vision_prepaid') is billed
real prepaid money upstream but recorded real_cost_eur=0 in the ledger, so no
budget/quota ever fires on it. A runaway (a misrouted text loop) bled ~100 EUR in
3 days unnoticed (2026-07-18). This is a hard ceiling: once the rolling-24h
prepaid-vision spend reaches the cap, further vision calls are rejected (429)
until it drains — turning a 100-EUR bleed into a bounded stop.

It is a COST guard, so it FAILS OPEN: disabled by default (inert until switched
on), and any check error (DB down, etc.) ALLOWS the call (logged loud) rather
than breaking legit report/energy vision. Enable + tune via env (same pattern as
BRIDGE_APP_TIER_POLICY_ENABLED — a master flag in the prod compose):

    PREPAID_VISION_DAILY_CAP_ENABLED=true   # default false (inert)
    PREPAID_VISION_DAILY_CAP_EUR=30         # rolling-24h ceiling (EUR)

Kill switch = set ENABLED=false (no redeploy needed if read per-call, which it is).
"""

import logging
import os
import time

logger = logging.getLogger(__name__)

# Per-worker cache so the DB is queried at most ~once/minute, not per vision call.
# The cap is global (all workers read the same usage_events sum), so a per-worker
# cache still converges on the same number.
_CACHE_TTL_S = 60.0
_cache = {"at": 0.0, "spent": 0.0}


def _enabled() -> bool:
    return os.getenv("PREPAID_VISION_DAILY_CAP_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _cap_eur() -> float:
    try:
        return float(os.getenv("PREPAID_VISION_DAILY_CAP_EUR", "30"))
    except (TypeError, ValueError):
        return 30.0


async def _spent_last_24h() -> float:
    """Rolling-24h prepaid-vision spend in EUR, cached ~60s. Raises on infra error
    (caller fail-opens)."""
    now = time.time()
    if now - _cache["at"] < _CACHE_TTL_S:
        return _cache["spent"]

    from src.db.client import get_pool, is_db_enabled

    if not is_db_enabled():
        raise RuntimeError("db disabled — cannot evaluate prepaid vision cap")

    pool = get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            """
            SELECT COALESCE(SUM(hypothetical_cost_eur), 0)
            FROM usage_events
            WHERE provider_metadata->>'api_key_lane' = 'vision_prepaid'
              AND recorded_at > now() - interval '24 hours'
            """
        )
    spent = float(val or 0.0)
    _cache["at"] = now
    _cache["spent"] = spent
    return spent


async def prepaid_vision_over_cap() -> tuple[bool, float, float]:
    """Return (over_cap, spent_eur, cap_eur) for the prepaid vision key.

    Call this ONLY when the request is already known to be a vision call (so a
    non-vision request is never blocked). Fail-open: disabled or any error →
    (False, ...), i.e. the call is allowed.
    """
    cap = _cap_eur()
    if not _enabled():
        return (False, 0.0, cap)
    try:
        spent = await _spent_last_24h()
    except Exception as e:  # fail-open cost guard — never break vision on a check error
        logger.warning(f"⚠️ prepaid vision cap check failed (fail-open, allowing call): {e}")
        return (False, 0.0, cap)
    return (spent >= cap, spent, cap)
