"""Fail-open daily spend cap for the research-cloud overflow path.

Pattern mirrors src/routing/prepaid_cap.py (the prepaid-vision daily cap) —
same shape, same fail-open contract, different ledger marker and different
caller behavior on "over cap":

  - prepaid_vision_over_cap(): over_cap -> the vision call is REJECTED (429).
  - research_cloud_over_cap(): over_cap -> the caller (the /v1/research
    routing decision in src/research_cloud/routing.py, before execution
    starts) raises ResearchCloudCapExceededError instead of booking the
    cloud call. Until 2026-08-02 this fell back to the subscription-worker
    pool instead — a silent replacement of provider/cost-model/privacy
    posture. That fallback is gone: the caller now stops and defers, same as
    every other failure in the research-cloud path.

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


async def query_spent_last_24h_from_db() -> float:
    """Raw DB query — used directly by platform-api (src/internal_routes.py,
    GET /v1/internal/research-cloud/spent-24h) since platform-api holds the
    pool, and by this module's own direct-DB fallback below. Raises on infra
    error; callers decide what to do about it."""
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
    return float(val or 0.0)


async def _spent_last_24h() -> float:
    """Rolling-24h research-cloud real spend in EUR, cached ~60s. Raises on
    infra error (caller fail-opens).

    ADR-0009 Schritt 2d: resolves via platform-api first (same cache/fallback
    shape as prepaid_cap's C6), falling back to the direct DB query in the
    same call when platform-api is unreachable.
    """
    now = time.time()
    if now - _cache["at"] < _CACHE_TTL_S:
        return _cache["spent"]

    from src.platform_client import PlatformUnavailable, call_platform

    try:
        resp = await call_platform("GET", "/v1/internal/research-cloud/spent-24h")
    except PlatformUnavailable as e:
        logger.error(
            "research-cloud spend lookup via platform-api failed (%s) — "
            "falling back to direct DB", e,
        )
        spent = await query_spent_last_24h_from_db()
    else:
        if resp.status_code == 200 and isinstance(resp.json, dict) and "spent_eur" in resp.json:
            spent = float(resp.json["spent_eur"])
        else:
            logger.error(
                "research-cloud spend lookup via platform-api returned "
                "unexpected status=%s body=%r — falling back to direct DB",
                resp.status_code, resp.json,
            )
            spent = await query_spent_last_24h_from_db()

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
