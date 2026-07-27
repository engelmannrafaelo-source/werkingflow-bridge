"""Routing decision for /v1/research: subscription pool vs. research-cloud.

Entry point: resolve_research_cloud_routing(). Default OFF
(RESEARCH_CLOUD_ENABLED env flag) — existing callers/paths are unaffected
unless the flag is explicitly on. Even then, a request only reaches the cloud
path if EITHER:

  - the user has a research-scoped compliance/preference pin
    (users.provider_config.research_provider == "cloud"), OR
  - the pool looks saturated (src.research_cloud.pool_signal) AND the caller
    explicitly opted into overflow (ResearchRequest.cloud_overflow=true).

The daily spend cap (src.research_cloud.cap) is a hard override on BOTH
paths — over cap always falls back to the pool, even for an explicit pin.
This is the one legitimate silent fallback in the whole research-cloud path
(DESIGN.md: "bei Cap-Erreichen stauen Jobs geordnet im Pool ... statt Cloud
zu buchen") — everywhere else, a failure is a loud job error.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def research_cloud_enabled() -> bool:
    return os.getenv("RESEARCH_CLOUD_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


async def resolve_research_cloud_routing(
    raw_user_id: Optional[str], cloud_overflow: bool, *, implicit_pin: bool = False
) -> bool:
    """Return True iff this /v1/research call should run on the research-cloud
    path instead of the subscription worker pool.

    implicit_pin=True treats the user as cloud-pinned without reading
    provider_config.research_provider — used for globally Bedrock-pinned
    users (Rafael 2026-07-27): the Bedrock pin marks a production user, and
    research cannot run on Bedrock at all (no WebSearch there — DESIGN.md
    options matrix), so their research takes the cloud path whenever it is
    available.
    """
    if not research_cloud_enabled():
        return False

    from src.research_cloud.cap import research_cloud_over_cap
    from src.research_cloud.pool_signal import is_worker_pool_saturated
    from src.routing.research_provider_override import get_user_research_pin

    pinned = "cloud" if implicit_pin else await get_user_research_pin(raw_user_id)
    wants_overflow = bool(cloud_overflow)

    if pinned != "cloud" and not (wants_overflow and is_worker_pool_saturated()):
        return False

    over_cap, spent_eur, cap_eur = await research_cloud_over_cap()
    if over_cap:
        logger.info(
            f"research-cloud daily cap reached ({spent_eur:.2f}/{cap_eur:.2f} EUR) — "
            "falling back to the subscription pool despite pin/overflow eligibility"
        )
        return False

    return True
