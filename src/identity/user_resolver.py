"""Central resolution of an inbound billing identity → canonical Bridge user UUID.

Most apps authenticate against the Bridge's own user pool, so the `user_id`
they send IS already a Bridge UUID. Engelmann is the exception: it has its own
Supabase user pool, so it sends the user's *licensed email* as the billing
identity. The Bridge keys licenses + budgets by the Bridge UUID (resolvable from
`users.email`).

This resolver is the single place that turns either form into the canonical
Bridge UUID, so identity handling is symmetric across every billing endpoint
(the post-call deduction writer already did email→uuid inline; lease-token and
the budget gate now use this shared path instead of diverging).

Fail-fast by design: it RAISES on an unresolvable identity. Callers that cannot
proceed without a billable identity (sandbox lease) surface the error loudly;
callers that must stay resilient (the budget gate) wrap it in their own
fail-open try/except — the resolver itself never silently invents an identity.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any, Optional

from src.db.client import get_pool
from src.platform_client import PlatformUnavailable, call_platform

logger = logging.getLogger(__name__)

# ── Email-identity resolution cache (ADR-0009 Schritt 2b, C1) ───────────────
# Same shape and reasoning as src/principals.py's cache: an email→id mapping is
# configuration-like, not live money state, so a short TTL is safe and keeps a
# burst of Engelmann calls from becoming one lookup per call. Misses are cached
# too, so a flood of unknown identities cannot amplify into a lookup storm.
_EMAIL_CACHE_TTL_S = float(os.getenv("BRIDGE_EMAIL_IDENTITY_CACHE_TTL_S", "20"))
_email_cache: dict[str, tuple[float, Optional[uuid.UUID]]] = {}


def _email_cache_get(email: str) -> tuple[bool, Optional[uuid.UUID]]:
    entry = _email_cache.get(email)
    if entry is None:
        return False, None
    ts, uid = entry
    if (time.monotonic() - ts) > _EMAIL_CACHE_TTL_S:
        _email_cache.pop(email, None)
        return False, None
    return True, uid


def _email_cache_put(email: str, uid: Optional[uuid.UUID]) -> None:
    _email_cache[email] = (time.monotonic(), uid)


def invalidate_email_cache() -> None:
    """Drop all cached email→id resolutions. No production caller today; exists
    so a future user-management change can take effect without waiting out the
    TTL, mirroring principals.invalidate_cache."""
    _email_cache.clear()


class UnresolvableUserIdentity(ValueError):
    """Base: an inbound identity could not be resolved to a Bridge user UUID.

    Kept as the catch-all so existing handlers (sandbox lease, provider
    override) keep working unchanged. New code should distinguish the two
    subclasses below — they are different KINDS of failure and must be
    handled differently by anything that gates on budget:
    """


class MalformedUserIdentity(UnresolvableUserIdentity):
    """The identity is not even well-formed — neither a UUID nor an email.

    This is a CALLER BUG, not a data condition: no legitimate caller can
    produce it. A marker string like `anonymous:<grund>` lands here.

    Callers must fail CLOSED on this. Treating it like a transient/unknown
    condition is what silently opened an unmetered hole in the budget gate:
    the check funnel sent `anonymous:public-check-funnel`, the gate could not
    resolve it, logged a warning and let every call through — unbudgeted
    (verified 2026-08-03; see spec-check-eigene-lizenz-20260803.md §1.3).
    """


class UnknownUserIdentity(UnresolvableUserIdentity):
    """The identity is well-formed (an email) but no Bridge user matches it.

    This is a DATA condition, not a caller bug — the address may simply not be
    licensed yet. Whether an unlicensed-but-well-formed identity should be
    blocked is a business decision, not an architectural one, so callers keep
    their existing policy for this case.
    """


async def resolve_user_id(raw: Any) -> uuid.UUID:
    """Resolve `raw` (a Bridge UUID or a licensed email) to the Bridge user UUID.

    Raises MalformedUserIdentity if `raw` is neither a UUID nor an email,
    UnknownUserIdentity if it is an email with no matching user. Both derive
    from UnresolvableUserIdentity. Does NOT fall back to a default identity.
    """
    if raw is None:
        raise MalformedUserIdentity("missing user identity")

    s = str(raw).strip()

    # Already a Bridge UUID (every app except Engelmann) — no DB round-trip.
    try:
        return uuid.UUID(s)
    except (ValueError, AttributeError, TypeError):
        pass

    # Email identity (Engelmann) — resolve via platform-api, then direct DB.
    if "@" in s:
        uid = await _resolve_email_identity(s)
        if uid is None:
            # No PII in the message — the email is the lookup key, not for logs.
            raise UnknownUserIdentity("no Bridge user for the given email identity")
        return uid

    raise MalformedUserIdentity("identity is neither a UUID nor an email")


async def lookup_user_id_by_email(email: str) -> Optional[uuid.UUID]:
    """The single DB leaf of resolve_user_id: users.email → users.id.

    Split out so platform-api can expose exactly this one query as an internal
    endpoint (ADR-0009 Schritt 2b, C1) and the worker's own direct-DB path can
    keep calling the identical function — the query lives in ONE place, not two.

    Returns None for "no such user". That is a real answer, not an error: the
    caller (resolve_user_id) turns it into UnknownUserIdentity, and the HTTP
    endpoint turns it into a 404. Everything else about identity handling —
    UUID parsing, the malformed-vs-unknown distinction — stays in the caller,
    because none of it touches the database.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM users WHERE email = $1", email)
    if row is None:
        return None
    uid = row["id"]
    return uid if isinstance(uid, uuid.UUID) else uuid.UUID(str(uid))


async def _resolve_email_identity(email: str) -> Optional[uuid.UUID]:
    """Cache → platform-api → direct-DB fallback, in that order.

    Same three-stage shape as principals.resolve_principal_by_token (ADR-0009
    Schritt 2a): platform-api answers a cache miss; if it cannot answer at all,
    the old direct query runs IN THE SAME CALL so nothing silently degrades.
    That fallback exists as long as this worker still has BRIDGE_DB_URL — it
    goes away when a worker actually moves off production-barrier (Schritt 3),
    which is the point at which the bounded retry below becomes the only
    protection against a brief platform-api restart.

    Opts into ONE retry: this is a pure read, so replaying it is safe. See
    platform_client.call_platform for why retrying is opt-in and not a default.
    """
    hit, cached = _email_cache_get(email)
    if hit:
        return cached

    try:
        resp = await call_platform(
            "POST", "/v1/internal/users/lookup-by-email",
            json={"email": email}, retries=1,
        )
    except PlatformUnavailable as e:
        logger.error(
            "email identity lookup via platform-api failed (%s) — falling back to direct DB", e
        )
        uid = await lookup_user_id_by_email(email)
    else:
        if resp.status_code == 404:
            uid = None
        elif resp.status_code == 200 and isinstance(resp.json, dict) and resp.json.get("id"):
            uid = uuid.UUID(str(resp.json["id"]))
        else:
            # Unexpected contract (wrong status, malformed body) is treated like
            # unreachable rather than like "no such user": answering "unknown
            # identity" for what may be a platform-api bug would reject a
            # legitimate caller, and on this path that means refusing a paying
            # customer's call.
            logger.error(
                "email identity lookup via platform-api returned unexpected "
                "status=%s body=%r — falling back to direct DB",
                resp.status_code, resp.json,
            )
            uid = await lookup_user_id_by_email(email)

    _email_cache_put(email, uid)
    return uid
