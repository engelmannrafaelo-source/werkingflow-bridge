"""Fail-open daily spend cap for the research-cloud overflow path.

Pattern mirrors src/routing/prepaid_cap.py (the prepaid-vision daily cap) —
same shape, same fail-open contract, different ledger marker and different
caller behavior on "over cap":

  - prepaid_vision_over_cap(): over_cap -> the vision call is REJECTED (429).
  - research_cloud_over_cap(): over_cap -> the JOB IS NOT rejected. The
    caller (the /v1/research routing decision, before execution starts)
    falls back to the subscription-worker pool instead of booking the cloud
    call. This is the one place a silent fallback is legitimate per
    DESIGN.md — everywhere else in the research-cloud path a failure must be
    a loud job error, never a silent reroute mid-run.

Env (Rafael-Go 2026-07-25, DESIGN.md "Entscheidungen" #2 — startwert 50 EUR/Tag):

    RESEARCH_CLOUD_DAILY_CAP_ENABLED=true   # default false (inert)
    RESEARCH_CLOUD_DAILY_CAP_EUR=50         # rolling-24h ceiling (EUR)
"""

import logging
import os
import time

logger = logging.getLogger(__name__)

# Per-worker cache so the DB is queried at most ~once/minute, not per job.
# The cap is global (all workers read the same usage_events sum), so a
# per-worker cache still converges on the same number.
_CACHE_TTL_S = 60.0
_cache = {"at": 0.0, "spent": 0.0}


def _enabled() -> bool:
    return os.getenv("RESEARCH_CLOUD_DAILY_CAP_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _cap_eur() -> float:
    try:
        return float(os.getenv("RESEARCH_CLOUD_DAILY_CAP_EUR", "50"))
    except (TypeError, ValueError):
        return 50.0


async def _spent_last_24h() -> float:
    """Rolling-24h research-cloud real spend in EUR, cached ~60s. Raises on
    infra error (caller fail-opens)."""
    now = time.time()
    if now - _cache["at"] < _CACHE_TTL_S:
        return _cache["spent"]

    from src.db.client import get_pool, is_db_enabled

    if not is_db_enabled():
        raise RuntimeError("db disabled — cannot evaluate research-cloud daily cap")

    pool = get_pool()
    async with pool.acquire() as conn:
        val = await conn.fetchval(
            """
            SELECT COALESCE(SUM(real_cost_eur), 0)
            FROM usage_events
            WHERE provider = 'research-cloud'
              AND recorded_at > now() - interval '24 hours'
            """
        )
    spent = float(val or 0.0)
    _cache["at"] = now
    _cache["spent"] = spent
    return spent


async def research_cloud_over_cap() -> tuple[bool, float, float]:
    """Return (over_cap, spent_eur, cap_eur) for the research-cloud daily spend.

    Call this BEFORE routing a job to the cloud path (never mid-run). Fail-open:
    disabled, or any check error (DB down, etc.), returns (False, ...) — a cap
    check hiccup must never itself block research; it just means the routing
    decision falls through to whatever the non-cap signals already decided.
    """
    cap = _cap_eur()
    if not _enabled():
        return (False, 0.0, cap)
    try:
        spent = await _spent_last_24h()
    except Exception as e:  # fail-open cost guard — never break research on a check error
        logger.warning(f"⚠️ research-cloud daily cap check failed (fail-open, allowing cloud routing): {e}")
        return (False, 0.0, cap)
    return (spent >= cap, spent, cap)
