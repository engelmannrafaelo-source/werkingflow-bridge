"""
Tenant resolver — single source of truth for "which tenant does this request belong to".

Used by every endpoint that INSERTs tenant-scoped data (activity-log, feedback,
invoices, etc.). The rule:

  • User-JWT auth: tenant_id is derived from the JWT (which carries it) or
    looked up from the users table as a defensive fallback. The user CANNOT
    override it via the request body — that would be a tenant-spoofing vector.

  • Service-token auth: there is no user context, so the caller MUST pass
    `tenantId` explicitly in the body. Missing → 400. Service tokens are
    cross-tenant on purpose (used by internal jobs, app backends).

  • Either way: returns a non-null tenant_id, or raises 400. Silent NULL
    inserts are no longer possible — combined with the NOT NULL DB constraint
    in migration 007, the contract is enforced top-to-bottom.

ADR 0007 — see docs/adr/0007-tenant-from-auth-context.md
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from src.api_auth.deps import AuthClaims
from src.db.client import get_pool


async def resolve_tenant_id(
    claims: AuthClaims,
    body_tenant_id: Optional[str] = None,
) -> str:
    """
    Return the tenant_id this request writes against. Raises 400 if it can't
    be determined — never returns None.

    Resolution order:
      1. User-JWT with tenant_id in JWT → use it (fast path, no DB hit)
      2. User-JWT without tenant_id → DB-lookup users.tenant_id (defensive
         fallback for malformed JWTs / legacy tokens)
      3. Service-token → require body_tenant_id, else 400
      4. Anything else → 400

    Users that pass tenantId in the body when they have a JWT: ignored.
    The JWT is authoritative — body overrides would be a tenant-spoofing
    vector.
    """
    if claims.is_user:
        if claims.tenant_id:
            return claims.tenant_id

        # Defensive fallback: JWT doesn't carry tenant_id (legacy or
        # malformed). Look it up from users table.
        if not claims.user_id:
            raise HTTPException(
                status_code=400,
                detail="Cannot resolve tenant: JWT has neither tenant_id nor user_id",
            )
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT tenant_id FROM users WHERE id = $1", claims.user_id
            )
        if not row or not row["tenant_id"]:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot resolve tenant: user {claims.user_id} has no tenant_id",
            )
        return row["tenant_id"]

    if claims.is_service:
        if not body_tenant_id:
            raise HTTPException(
                status_code=400,
                detail="Service-token requests must pass tenantId in the body",
            )
        return body_tenant_id

    raise HTTPException(
        status_code=400,
        detail=f"Unknown auth kind: {claims.kind}",
    )


async def resolve_tenant_for_user(user_id: str) -> str:
    """
    Resolve the tenant_id that a given user belongs to.

    Used by endpoints where the row's owner is NOT the caller — e.g. an admin
    creating an invoice *for* a customer. The tenant is a property of the
    invoice's user, not of the admin's auth context.

    Raises 400 if the user is unknown or has no tenant_id. Never returns None.
    """
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required to resolve tenant")
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tenant_id FROM users WHERE id = $1", user_id
        )
    if not row:
        raise HTTPException(status_code=400, detail=f"Unknown user: {user_id}")
    if not row["tenant_id"]:
        raise HTTPException(
            status_code=400,
            detail=f"User {user_id} has no tenant_id",
        )
    return row["tenant_id"]
