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

import uuid
from typing import Any

from src.db.client import get_pool


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

    # Email identity (Engelmann) — resolve via users.email.
    if "@" in s:
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id FROM users WHERE email = $1", s)
        if row is None:
            # No PII in the message — the email is the lookup key, not for logs.
            raise UnknownUserIdentity("no Bridge user for the given email identity")
        uid = row["id"]
        return uid if isinstance(uid, uuid.UUID) else uuid.UUID(str(uid))

    raise MalformedUserIdentity("identity is neither a UUID nor an email")
