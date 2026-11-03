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

Abo-Modus (KD 03.11.2026, Energy-Owner): the caller may ask for the
subscription EXPLICITLY, then the pool is not touched at all (no /accounts,
no reserve, no Messages call) — chat (text and images), research and the
document vision. Contract (docs/contracts/energy-abo-bildweg.md):

* header ``X-Funding-Source: subscription`` (alias ``abo``); ``pool`` or no
  header = today's behaviour; any other value = 400;
* or the worker switch ``ENERGY_STAGING_SUBSCRIPTION_ONLY`` — only for the
  Energy calls whose ``X-User-ID`` is in ``ENERGY_STAGING_SUBSCRIPTION_ONLY_USERS``
  (comma list, UUID or e-mail, case-insensitive; empty = the switch has no
  effect, fail-closed), unless the header says ``pool``. The list separates
  environments by convention: under the shared API key X-User-ID is
  caller-claimed, not proof of origin (no secure origin detection today);
* effective only where the staging fallback is allowed (switch, dev identity,
  own origin) and for werking-energy. A header asking for the subscription
  where that does not hold is refused with 409 — never a silent pool route.
* causes ``subscription_requested`` / ``subscription_mode``; markers add
  ``pool_umgangen`` and ``funding_source``. Worker check:
  ``pool_umgehung_status()``.

Images (Abo-Bildweg, KD 03.11.2026): in the fallback, images go over the CLI
subscription with only the Read tool (src/providers/subscription_vision.py,
docs/contracts/energy-abo-bildweg.md) — chat and /v1/document/convert. Every
other image route (paid vision key, Gemini, Bedrock) stays refused.
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
#: Abo-Modus: the pool is bypassed on purpose, never asked.
CAUSE_SUBSCRIPTION_REQUESTED = "subscription_requested"
CAUSE_SUBSCRIPTION_MODE = "subscription_mode"
_BYPASS_CAUSES = (CAUSE_SUBSCRIPTION_REQUESTED, CAUSE_SUBSCRIPTION_MODE)
_ALL_CAUSES = (CAUSE_POOL_MISSING, CAUSE_POOL_UNAVAILABLE, *_BYPASS_CAUSES)

FUNDING_SOURCE_HEADER = "x-funding-source"
SUBSCRIPTION_ONLY_ENV = "ENERGY_STAGING_SUBSCRIPTION_ONLY"
SUBSCRIPTION_ONLY_USERS_ENV = "ENERGY_STAGING_SUBSCRIPTION_ONLY_USERS"
FUNDING_SOURCE_SUBSCRIPTION = "subscription"
FUNDING_SOURCE_POOL = "pool"
_SUBSCRIPTION_VALUES = frozenset({"subscription", "abo"})

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
    return cause if cause in _ALL_CAUSES else None


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
                        "funding_fallback_cause": cause, "automatic_paid_fallback": False,
                        "funding_source": FUNDING_SOURCE_SUBSCRIPTION,
                        "pool_umgangen": cause in _BYPASS_CAUSES})


def requested_funding_source(request) -> Optional[str]:
    """``X-Funding-Source`` of the request: "subscription", "pool" or None.
    An unknown value is a caller error (400), not a silent default."""
    if request is None:
        return None
    raw = (request.headers.get(FUNDING_SOURCE_HEADER) or "").strip().lower()
    if not raw:
        return None
    if raw in _SUBSCRIPTION_VALUES:
        return FUNDING_SOURCE_SUBSCRIPTION
    if raw == FUNDING_SOURCE_POOL:
        return FUNDING_SOURCE_POOL
    raise HTTPException(400, {"error": "funding_source_invalid",
                              "message": f"X-Funding-Source={raw!r} unknown; allowed: subscription, abo, pool"})


def _switch_on(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in _TRUTHY


def _subscription_only_users() -> frozenset:
    raw = os.getenv(SUBSCRIPTION_ONLY_USERS_ENV) or ""
    return frozenset(v.strip().lower() for v in raw.split(",") if v.strip())


def _user_allowed(request) -> bool:
    users = _subscription_only_users()
    user = (request.headers.get("x-user-id") or "").strip().lower() if request is not None else ""
    return bool(users) and bool(user) and user in users


def subscription_mode_cause(request) -> Optional[str]:
    """The Abo-Modus cause of this request, or None (today's behaviour).

    Raises 409 when the header asks for the subscription where it is not
    allowed (other app, prod bridge, foreign origin, switch off): an explicit
    funding wish that cannot be met is refused, never served from the pool."""
    source = requested_funding_source(request)
    if source == FUNDING_SOURCE_POOL:
        return None
    if source == FUNDING_SOURCE_SUBSCRIPTION:
        if not (promotional_only(request) and staging_subscription_fallback_allowed()):
            raise HTTPException(409, {
                "error": "subscription_funding_not_available",
                "message": ("X-Funding-Source: subscription only for werking-energy on the dev bridge "
                            "with the staging switch and own origin; no pool, no paid fallback"),
                "automatic_paid_fallback": False})
        return CAUSE_SUBSCRIPTION_REQUESTED
    if (_switch_on(SUBSCRIPTION_ONLY_ENV) and promotional_only(request) and _user_allowed(request)
            and staging_subscription_fallback_allowed()):
        return CAUSE_SUBSCRIPTION_MODE
    return None


def enter_subscription_mode(request) -> bool:
    """Switch this request to the subscription BEFORE any pool step when the
    Abo-Modus applies. True = the request runs on the subscription."""
    if subscription_fallback_active(request):
        return True
    cause = subscription_mode_cause(request)
    if cause is None:
        return False
    activate_subscription_fallback(request, cause)
    return True


def pool_umgangen(request) -> bool:
    """True when this request bypasses the pool on purpose (Abo-Modus)."""
    return fallback_cause(request) in _BYPASS_CAUSES


def pool_umgehung_status(user_id: Optional[str] = None) -> dict:
    """Worker check without a request (origin = own bridge): would an Energy
    call bypass the pool with / without the header? ``user_id`` = the
    X-User-ID to check against the allowlist (None: any listed user). The
    decision is one and the same for every path, ``wege`` names them."""
    gruende = []
    if not _switch_on(FALLBACK_SWITCH_ENV):
        gruende.append(f"{FALLBACK_SWITCH_ENV} nicht gesetzt")
    from src.federation import self_origin_id
    own = self_origin_id().strip().lower()
    if own not in SUBSCRIPTION_FALLBACK_BRIDGES:
        gruende.append(f"BRIDGE_ORIGIN_ID={own!r} nicht in {sorted(SUBSCRIPTION_FALLBACK_BRIDGES)}")
    erlaubt = not gruende
    users = _subscription_only_users()
    user_ok = bool(users) and (user_id is None or user_id.strip().lower() in users)
    ohne = erlaubt and _switch_on(SUBSCRIPTION_ONLY_ENV) and user_ok
    if erlaubt and not _switch_on(SUBSCRIPTION_ONLY_ENV):
        gruende.append(f"{SUBSCRIPTION_ONLY_ENV} nicht gesetzt: ohne Header gilt Pool zuerst")
    elif erlaubt and not users:
        gruende.append(f"{SUBSCRIPTION_ONLY_USERS_ENV} leer: Schalter wirkt nicht (fail-closed)")
    elif erlaubt and not user_ok:
        gruende.append(f"X-User-ID {user_id!r} nicht in {SUBSCRIPTION_ONLY_USERS_ENV}")
    wege = ("chat", "bild", "document_convert", "research")
    return {"pool_umgangen_mit_header": erlaubt, "pool_umgangen_ohne_header": ohne,
            "wege_mit_header": {w: erlaubt for w in wege},
            "wege_ohne_header": {w: ohne for w in wege},
            "allowlist_eintraege": len(users),
            "bridge_origin_id": own, "gruende": gruende}


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
        marker["funding_source"] = FUNDING_SOURCE_SUBSCRIPTION
        marker["pool_umgangen"] = pool_umgangen(request)
    return marker


def mark_chat_response(request, result: Any) -> Any:
    """Attach the funding marker to a chat result (dict body or Response header)."""
    marker = response_marker(request)
    if marker is None:
        return result
    if hasattr(result, "model_dump") and not hasattr(result, "headers"):
        # Pydantic response (vision branch: ChatCompletionResponse) — as the
        # dict FastAPI would serialise anyway, so the marker is not lost.
        result = result.model_dump(mode="json")
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


def subscription_fallback_in_context() -> bool:
    """For code without the request (VisionProvider in the document vision):
    the current request was switched to the staging subscription fallback
    (activate_subscription_fallback set the marker in this context) AND the
    bridge/origin rule holds right now. Never true from a header."""
    marker = _ledger_marker.get()
    return (isinstance(marker, dict) and marker.get("funding_policy") == SUBSCRIPTION_STAGING_FALLBACK
            and staging_subscription_fallback_allowed())


def ledger_marker() -> dict:
    """provider_metadata additions for the call being booked right now."""
    return dict(_ledger_marker.get() or {})


def pool_reservations(meta: Optional[dict]) -> list:
    """Credit-pool reservation ids a ledger row's provider_metadata names.

    Two shapes exist: ``credit_pool_reservation`` (one call: chat direct,
    vision) and ``credit_pool_reservations`` (one row for many calls:
    research-cloud run, document vision). Only what the coordinator handed
    back for this call counts; nothing is derived from app name or headers."""
    if not isinstance(meta, dict):
        return []
    out: list = []
    single = meta.get("credit_pool_reservation")
    if isinstance(single, str) and single:
        out.append(single)
    many = meta.get("credit_pool_reservations")
    if isinstance(many, list):
        out.extend(r for r in many if isinstance(r, str) and r)
    return out


def pool_funded(meta: Optional[dict]) -> bool:
    """True when the call's own metadata proves promotional pool funding:
    ``funding_policy == promotional_only`` AND at least one reservation id.
    The policy marker alone is not enough (it can be stamped from the
    request), the reservation is the coordinator's evidence (KD 03.11.2026)."""
    return (isinstance(meta, dict) and meta.get("funding_policy") == PROMOTIONAL_ONLY
            and bool(pool_reservations(meta)))
