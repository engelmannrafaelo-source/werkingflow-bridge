"""Per-(app, agent) tier routing policy — the Bridge owns *which* backend a
route runs on, not the app.

Motivation
----------
Some app call-sites must run on a specific tier for operational reasons the app
should not have to know about. The concrete case: werking-energy's large-input
LLM calls (claims/schema/sensor-position generation) overflow the Claude-Code
*worker* context (SDK sub-process compaction truncates the input), so they have
to be served by the *direct* Anthropic Messages API (tier
``claude-direct-notools``) instead of the worker pool.

Until now that decision leaked into the app as a hard-coded ``provider_tier`` on
the outbound request. That is a quick-fix: a Bridge-internal routing/billing
decision baked into a customer app. This module moves it where it belongs — the
app sends only attribution (X-App-ID, agent id, X-User-ID) and the Bridge maps
``(app, agent, env) → {target_tier, billing_account}``.

Two orthogonal effects a policy row can have:

- ``target_tier`` — overrides the request's tier (e.g. force
  ``claude-direct-notools``). Applied by setting ``request_body.provider_tier``,
  which the existing backend router already honours.
- ``billing_account`` — the call's *cost* is booked to this internal account
  instead of the customer's budget (the customer's project budget is NOT
  charged). Attribution (user/app/agent) is preserved on the usage row; only
  the deduction target changes. See ``ai_call_writer.persist_ai_call_activity``.

Fail-OPEN — the deliberate opposite of ``user_provider_override``
----------------------------------------------------------------
``user_provider_override`` fails *closed* (raises → 503) because a Bedrock/DSGVO
pin is a *compliance* guarantee that must never silently degrade. This policy is
a *cost/operational* optimisation: if the lookup fails (DB error, table absent,
flag off) the correct behaviour is to fall through to normal routing — the call
still succeeds, it just may hit the worker path it would otherwise avoid. Losing
the optimisation is always better than 503-ing a live report. Every error path
here returns ``None`` (no policy) and never raises.

Precedence: a compliance pin wins. Callers apply this policy only when the user
is NOT provider-pinned (see main.py). The DB lookup is TTL-cached per
(app, agent, env) key so steady-state per-request cost is a dict lookup.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60.0
# (app_id, agent_id, app_env) → (expires_at_monotonic, AppTierPolicy-or-None)
_cache: dict[tuple, tuple[float, Optional["AppTierPolicy"]]] = {}

# Master switch. Absent/false → the policy layer is inert (fail-open to today's
# behaviour) without needing the table dropped. Lets prod run the new code with
# the feature dark until it is explicitly turned on.
_FLAG_ENV = "BRIDGE_APP_TIER_POLICY_ENABLED"


def is_enabled() -> bool:
    return os.getenv(_FLAG_ENV, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class AppTierPolicy:
    """Resolved routing/billing policy for one (app, agent, env)."""
    target_tier: Optional[str]
    billing_account: Optional[str]


def invalidate_cache() -> None:
    """Drop all cached policies — e.g. after editing app_tier_policies."""
    _cache.clear()


async def lookup_policy_from_db(
    app_id: str, agent_id: Optional[str], app_env: Optional[str]
) -> Optional[AppTierPolicy]:
    """Most-specific enabled row for (app, agent, env), or None. The DB leaf.

    A row's ``agent_id``/``app_env`` may be NULL = "applies to every agent /
    every env of this app". More specific rows win (non-NULL before NULL).

    Runs on platform-api (ADR-0009 Schritt 2c), which holds the pool — exposed
    as GET /v1/internal/app-tier-policy. Split out of the caller so the query
    lives in ONE place: the worker reaches it over HTTP, nothing duplicates it.

    Raises on a DB failure. The fail-open policy belongs to the CALLER, not to
    this leaf — an endpoint that answered "no policy" for a broken query would
    make an outage indistinguishable from a deliberate absence.
    """
    from src.db.client import is_db_enabled, get_pool

    if not is_db_enabled():
        return None

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT target_tier, billing_account
            FROM app_tier_policies
            WHERE enabled = TRUE
              AND app_id = $1
              AND (agent_id = $2 OR agent_id IS NULL)
              AND (app_env  = $3 OR app_env  IS NULL)
            ORDER BY (agent_id IS NOT NULL) DESC,
                     (app_env  IS NOT NULL) DESC
            LIMIT 1
            """,
            app_id, agent_id, app_env,
        )

    if row is None or not row["target_tier"]:
        return None
    return AppTierPolicy(
        target_tier=row["target_tier"],
        billing_account=row["billing_account"],
    )


async def _lookup_policy(
    app_id: str, agent_id: Optional[str], app_env: Optional[str]
) -> Optional[AppTierPolicy]:
    """Ask platform-api for the policy. Never raises — fail-open (see module
    docstring): any failure means "no policy", i.e. normal routing.

    Opts into one retry: a pure read, safe to replay.

    No direct-DB fallback, and this module is the reason the worker's money path
    no longer needs one at all — it was the last reader of a table the writer
    itself never touched (ADR-0009 Schritt 2c). Before the seam existed, a
    worker without BRIDGE_DB_URL returned None here *silently*, which on this
    particular lookup does not mean "no policy": it means the call's cost is
    booked to the CUSTOMER instead of the internal ``billing_account`` the
    policy names. Fail-open is still the right trade for a cost/operational
    optimisation — but it has to be findable, hence WARNING rather than the
    previous DEBUG when the question could not be asked. A genuine "no row"
    stays quiet.
    """
    from src.platform_client import PlatformUnavailable, call_platform

    try:
        resp = await call_platform(
            "GET", "/v1/internal/app-tier-policy",
            params={
                "app_id": app_id,
                **({"agent_id": agent_id} if agent_id else {}),
                **({"app_env": app_env} if app_env else {}),
            },
            retries=1,
        )
    except PlatformUnavailable as e:
        logger.warning(
            "app_tier_policy: lookup via platform-api failed (%s) — fail-open, no "
            "policy for app=%s agent=%s env=%s. If a policy exists for this "
            "call-site, its cost is being booked to the customer, not to its "
            "billing_account.",
            e, app_id, agent_id, app_env,
        )
        return None

    if resp.status_code != 200 or not isinstance(resp.json, dict) or "policy" not in resp.json:
        logger.warning(
            "app_tier_policy: lookup via platform-api answered status=%s body=%r "
            "— fail-open, no policy for app=%s agent=%s env=%s",
            resp.status_code, resp.json, app_id, agent_id, app_env,
        )
        return None

    raw = resp.json["policy"]
    if not raw or not raw.get("target_tier"):
        return None
    return AppTierPolicy(
        target_tier=raw.get("target_tier"),
        billing_account=raw.get("billing_account"),
    )


async def resolve_app_tier_policy(
    app_id: Optional[str],
    agent_id: Optional[str],
    app_env: Optional[str],
) -> Optional[AppTierPolicy]:
    """TTL-cached ``(app, agent, env) → AppTierPolicy`` (or None). Fail-open.

    Returns None when the feature flag is off, no app_id is given, or no row
    matches — in every such case the caller leaves routing untouched.
    """
    if not is_enabled():
        return None
    if not app_id:
        return None

    key = (app_id, agent_id, app_env)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]

    policy = await _lookup_policy(app_id, agent_id, app_env)
    _cache[key] = (now + _CACHE_TTL_SECONDS, policy)
    return policy


def apply_app_tier_policy(request_body, policy: AppTierPolicy) -> Optional[str]:
    """Apply the policy's tier override to the request; return the tier applied.

    Guard: a no-tools tier (e.g. ``claude-direct-notools``) can only serve
    requests with ``enable_tools=false``. If the request needs tools we do NOT
    force the tier (that would break the call) — we log loudly and leave routing
    as-is. The billing_account still applies independently (handled by caller),
    because the return value only reports the *tier* decision.

    Returns the tier id that was set, or None when no tier override was applied.
    """
    target = policy.target_tier
    if not target:
        return None

    if _tier_needs_no_tools(target) and _request_wants_tools(request_body):
        logger.warning(
            "app_tier_policy: policy targets no-tools tier %r but request has "
            "enable_tools=true — NOT forcing the tier (would break tool use). "
            "Routing left unchanged.",
            target,
        )
        return None

    # Fail-open if the target tier is not configured on THIS deployment (e.g. a
    # dev/local bridge without the direct-notools API key). Forcing an unusable
    # tier would 400 the call (provider_not_configured); a cost-routing policy
    # must never do that — fall through to normal routing instead. Lets the
    # master flag default on everywhere: a deployment that can't serve the tier
    # simply ignores the policy.
    if not _tier_is_usable(target):
        logger.warning(
            "app_tier_policy: target tier %r is not usable on this deployment "
            "(API key missing) — leaving routing unchanged (fail-open).",
            target,
        )
        return None

    request_body.provider_tier = target
    return target


def _request_wants_tools(request_body) -> bool:
    # Default matches the request model default; only an explicit True counts as
    # "needs tools". None/absent → the no-tools direct path is safe.
    return getattr(request_body, "enable_tools", None) is True


def _tier_needs_no_tools(tier_id: str) -> bool:
    """True if the tier cannot serve tool calls (fallback chains skip it)."""
    try:
        from src.providers.registry import PROVIDERS
        cfg = PROVIDERS.get(tier_id)
        return cfg is not None and cfg.supports_tools is False
    except Exception:  # noqa: BLE001 — unknown tier → assume tool-capable (no guard)
        return False


def _tier_is_usable(tier_id: str) -> bool:
    """True if the tier is configured (required API key present) on this host."""
    try:
        from src.providers.registry import is_tier_usable
        return is_tier_usable(tier_id)
    except Exception:  # noqa: BLE001 — can't verify → don't force the tier (fail-open)
        return False
