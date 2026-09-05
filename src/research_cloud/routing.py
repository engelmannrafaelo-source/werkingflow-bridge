"""Routing decision for /v1/research: subscription pool vs. research-cloud.

Entry point: resolve_research_cloud_routing(). Default OFF
(RESEARCH_CLOUD_ENABLED env flag) — existing callers/paths are unaffected
unless the flag is explicitly on, with ONE deliberate exception: a user
explicitly pinned to the cloud lane is refused (ResearchCloudDisabledError)
rather than served from the pool while the lane is off. Even with the flag on,
a request only reaches the cloud path if EITHER:

  - the user has a research-scoped compliance/preference pin
    (users.provider_config.research_provider == "cloud"), OR
  - the pool looks saturated (src.research_cloud.pool_signal) AND the caller
    explicitly opted into overflow (ResearchRequest.cloud_overflow=true).

The daily spend cap (src.research_cloud.cap) used to be a hard override on
BOTH paths — over cap silently fell back to the pool, even for an explicit
pin. That was a silent replacement of the assurance behind the pin/overflow
eligibility (a different provider, cost model and privacy posture) and is
gone (Rafael 2026-08-02: "kein stiller Rückfall auf eine andere Quelle" —
production über Bedrock, Recherche direkt über Anthropic). Over cap now
raises ResearchCloudCapExceededError: the caller stops and defers instead of
switching source. The app-side wait strategy
(werking-report transient-infra-error.ts, up to 20h) already exists for
exactly this — the Bridge only has to make the failure recognizable as
retryable, never replace it with output from elsewhere.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class ResearchCloudCapExceededError(Exception):
    """The daily research-cloud spend cap is reached for a caller who has no
    legitimate pool alternative (explicit/implicit cloud pin, or the pool was
    already established as saturated before the overflow was even
    considered). The caller MUST surface this as a retryable error, never
    reroute to the subscription pool — that pool is a different provider,
    cost model and privacy posture (Rafael 2026-08-02)."""

    def __init__(self, spent_eur: float, cap_eur: float):
        self.spent_eur = spent_eur
        self.cap_eur = cap_eur
        super().__init__(
            f"research-cloud daily cap reached ({spent_eur:.2f}/{cap_eur:.2f} EUR)"
        )


class ResearchCloudDisabledError(Exception):
    """The research-cloud lane is switched off (RESEARCH_CLOUD_ENABLED) while
    the caller is explicitly pinned to it.

    Decided 05.09.2026: "Recherche deaktiviert" means a hard failure with a
    clear message, never a quiet switch to the subscription worker pool.

    Same reasoning as ResearchCloudCapExceededError, one step earlier: a user
    carrying ``provider_config.research_provider = "cloud"`` was deliberately
    put on a different provider, cost model and privacy posture. Serving that
    user from the pool because an operator flipped a flag would honour the
    flag and break the pin — silently, and precisely for the callers whose
    routing someone cared enough about to write down.

    Not raised for overflow-eligible callers (``cloud_overflow=true`` on a
    saturated pool): the pool IS their home lane. Overflow asks "use the cloud
    if it is there"; a full pool answering slowly or with a 429 is an honest
    answer, not a substituted assurance.
    """

    def __init__(self) -> None:
        super().__init__(
            "research-cloud is disabled (RESEARCH_CLOUD_ENABLED is off) but "
            "this user is pinned to it (provider_config.research_provider="
            "'cloud') — refusing to run the research on the subscription "
            "worker pool instead. Either re-enable the research-cloud lane or "
            "remove the pin; there is no silent substitute."
        )


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

    Raises ResearchCloudCapExceededError if the daily cap is reached while
    pinned/overflow-eligible — the pool is not a legitimate substitute for
    either reason a caller ends up here (a compliance/preference pin, or the
    pool already being saturated), so this must stop and defer, not reroute.
    """
    from src.research_cloud.cap import research_cloud_over_cap
    from src.research_cloud.pool_signal import is_worker_pool_saturated
    from src.routing.research_provider_override import get_user_research_pin

    if not research_cloud_enabled():
        # The pin is read even with the lane off — precisely BECAUSE it is off.
        # Returning False here without looking would route an explicitly
        # cloud-pinned user onto the pool without a word, which is the silent
        # substitution ruled out for the cap path (2026-08-02) and for the
        # switch-off itself (05.09.2026). The lookup is the same cached
        # provider_config read the enabled path does, so this costs nothing new.
        #
        # implicit_pin (globally Bedrock-pinned user, research exception) is
        # deliberately NOT treated as a pin here: that flag says "Bedrock
        # cannot serve research at all, take the cloud if it exists" — the pool
        # is its documented answer when the cloud does not, and always was.
        if not implicit_pin and await get_user_research_pin(raw_user_id) == "cloud":
            logger.error(
                "research-cloud is disabled while user=%s is pinned to it — "
                "refusing to substitute the worker pool",
                raw_user_id,
            )
            raise ResearchCloudDisabledError()
        return False

    pinned = "cloud" if implicit_pin else await get_user_research_pin(raw_user_id)
    wants_overflow = bool(cloud_overflow)

    if pinned != "cloud" and not (wants_overflow and is_worker_pool_saturated()):
        return False

    over_cap, spent_eur, cap_eur = await research_cloud_over_cap()
    if over_cap:
        logger.info(
            f"research-cloud daily cap reached ({spent_eur:.2f}/{cap_eur:.2f} EUR) — "
            "deferring (pin/overflow eligibility means the pool is not a "
            "legitimate substitute)"
        )
        raise ResearchCloudCapExceededError(spent_eur, cap_eur)

    return True
