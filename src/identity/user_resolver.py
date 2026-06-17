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
    """Raised when an inbound identity is neither a UUID nor a known email."""


async def resolve_user_id(raw: Any) -> uuid.UUID:
    """Resolve `raw` (a Bridge UUID or a licensed email) to the Bridge user UUID.

    Raises UnresolvableUserIdentity if `raw` is an email with no matching user,
    or is neither a UUID nor an email. Does NOT fall back to a default identity.
    """
    if raw is None:
        raise UnresolvableUserIdentity("missing user identity")

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
            raise UnresolvableUserIdentity("no Bridge user for the given email identity")
        uid = row["id"]
        return uid if isinstance(uid, uuid.UUID) else uuid.UUID(str(uid))

    raise UnresolvableUserIdentity("identity is neither a UUID nor an email")
