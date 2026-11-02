"""Operator funding policy, independent of caller-supplied routing wishes.

Energy uses verified promotional credits only (Rafael, 2026-10-30).
No subscription, foreign provider or legacy key is a fallback for this app.

Staging exception (Rafael, 2026-11-01, ENTSCHEID-STAGING-WEITERENTWICKLUNG):
on the DEV bridge only, Energy may run on the existing subscription (Claude
CLI accounts, normal pool admission/limiter) when the promotional credit pool
is missing, unreachable or has no eligible budget. The pool stays first
choice; a paid API fallback stays forbidden on every bridge
(automatic_paid_fallback is always False).

The exception needs BOTH, read from the worker's own environment and never
from a request header:

* ``ENERGY_STAGING_SUBSCRIPTION_FALLBACK`` truthy (operator switch), and
* ``BRIDGE_ORIGIN_ID`` (the bridge identity of ADR-0011/0012, the value nginx
  uses as ``${BRIDGE_ID}``) in ``SUBSCRIPTION_FALLBACK_BRIDGES``.

Without either, the strict promotional_only behaviour is unchanged. A prod
bridge (``BRIDGE_ORIGIN_ID=prod``) stays strict even if the switch were set by
mistake.

On top, the REQUEST must belong to this bridge (ADR-0011 origin, set by
federation.OriginMiddleware from the LB-stamped ``X-Bridge-Origin``). The prod
upstreams carry the dev bridge as backup (docker/upstreams-prod.conf; nginx
retries 503/429 there): a prod Energy call that lands on a dev worker arrives
with origin ``prod`` and is billed on prod (ADR-0011) — it must never run on
the dev subscription, so it stays strict. nginx resets every untrusted
sender's origin to its own ``${BRIDGE_ID}`` (``$bridge_origin_out``), so a
Railway staging backend or an external caller of the dev bridge reaches the
worker as origin ``dev``. A request without any origin never passed the LB
(direct worker call) and counts as own, the same rule as
federation.is_foreign_origin().

Visible markers (kept small; a later "Finanzierungsquelle" branch may extend
them):

* ``funding_policy``: ``"promotional_only"`` | ``"subscription_staging_fallback"``
* ``funding_fallback_cause``: why the subscription served the call
  (``credit_pool_missing`` | ``credit_pool_unavailable``)
* ``automatic_paid_fallback``: always ``False``

They appear in the chat response (``x_funding``), the research response
(``funding_policy``) and in the ledger row's ``provider_metadata``.
"""
from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Any, Optional

from fastapi import HTTPException

PROMOTIONAL_ONLY = "promotional_only"
SUBSCRIPTION_STAGING_FALLBACK = "subscription_staging_fallback"

CAUSE_POOL_MISSING = "credit_pool_missing"
CAUSE_POOL_UNAVAILABLE = "credit_pool_unavailable"

FALLBACK_SWITCH_ENV = "ENERGY_STAGING_SUBSCRIPTION_FALLBACK"
#: Bridge identities (BRIDGE_ORIGIN_ID) on which the switch may take effect.
#: "dev" is the primary/staging bridge marker already used by nginx
#: (upstreams-primary.conf) and the job-id home marker; "prod" is never here.
SUBSCRIPTION_FALLBACK_BRIDGES = frozenset({"dev"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_STATE_ATTR = "funding_subscription_fallback"

# Ledger marker of the current request (and of tasks it spawns, e.g. async
# research jobs: asyncio copies the context at task creation). Read by
# persist_ai_call_activity on the live path only.
_ledger_marker: ContextVar[Optional[dict]] = ContextVar("bridge_funding_ledger_marker", default=None)


def promotional_only(request):
    return request is not None and request.headers.get("x-app-id", "").strip().lower() == "werking-energy"


def require_pool():
    from src.providers.credit_pool import enabled
    if not enabled():
        raise HTTPException(503, "promotional_only: credit coordinator missing; no paid fallback")


def staging_subscription_fallback_allowed() -> bool:
    """True only on a bridge that names itself as an allowed non-prod bridge,
    has the operator switch on, AND serves a request of its own origin (not a
    peer bridge's request relayed here, see module doc). The switch and the
    identity come from the worker environment; the origin is the LB-stamped
    ADR-0011 value, never a raw client header."""
    if (os.getenv(FALLBACK_SWITCH_ENV) or "").strip().lower() not in _TRUTHY:
        return False
    from src.federation import get_request_origin, self_origin_id
    own = self_origin_id().strip().lower()
    if own not in SUBSCRIPTION_FALLBACK_BRIDGES:
        return False
    origin = get_request_origin()
    return origin is None or origin == own


def fallback_cause(request) -> Optional[str]:
    """The recorded cause, or None. Only a real cause string counts (a test
    double's auto-attribute must never read as an active fallback)."""
    if request is None:
        return None
    cause = getattr(getattr(request, "state", None), _STATE_ATTR, None)
    return cause if cause in (CAUSE_POOL_MISSING, CAUSE_POOL_UNAVAILABLE) else None


def subscription_fallback_active(request) -> bool:
    return fallback_cause(request) is not None


def fallback_eligible(request) -> bool:
    """Energy call on a bridge that allows the staging fallback, not yet switched."""
    return (promotional_only(request) and not subscription_fallback_active(request)
            and staging_subscription_fallback_allowed())


def activate_subscription_fallback(request, cause: str) -> None:
    """Mark this request as served by the subscription (staging fallback)."""
    if not staging_subscription_fallback_allowed():
        # Defence in depth: no call site may switch without the identity check.
        raise HTTPException(503, "promotional_only: subscription fallback not allowed on this bridge; "
                                 "no paid fallback")
    setattr(request.state, _STATE_ATTR, cause)
    _ledger_marker.set({"funding_policy": SUBSCRIPTION_STAGING_FALLBACK,
                        "funding_fallback_cause": cause, "automatic_paid_fallback": False})


def current_policy(request) -> Optional[str]:
    if subscription_fallback_active(request):
        return SUBSCRIPTION_STAGING_FALLBACK
    if promotional_only(request):
        return PROMOTIONAL_ONLY
    return None


def response_marker(request) -> Optional[dict]:
    policy = current_policy(request)
    if policy is None:
        return None
    marker = {"funding_policy": policy, "automatic_paid_fallback": False}
    if policy == SUBSCRIPTION_STAGING_FALLBACK:
        marker["funding_fallback_cause"] = fallback_cause(request)
    return marker


def mark_chat_response(request, result: Any) -> Any:
    """Attach the funding marker to a chat result (dict body or Response header)."""
    marker = response_marker(request)
    if marker is None:
        return result
    if isinstance(result, dict):
        result["x_funding"] = marker
    else:
        headers = getattr(result, "headers", None)
        if headers is not None:
            try:
                headers["X-Bridge-Funding-Policy"] = marker["funding_policy"]
            except Exception:  # noqa: BLE001 — immutable header container: body stays as is
                pass
    return result


def ledger_marker() -> dict:
    """provider_metadata additions for the call being booked right now."""
    return dict(_ledger_marker.get() or {})
