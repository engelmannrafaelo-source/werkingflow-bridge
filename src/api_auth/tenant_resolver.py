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

Resolution path (ADR-0009 Schritt 2, worker-DB-free reads): the single DB leaf
(users.tenant_id) is asked via platform-api
(GET /v1/internal/users/{id}/tenant, see src/internal_routes.py), with the
direct query kept as an in-call fallback while this process still has
BRIDGE_DB_URL. Same three-stage shape as principals.py and
identity/user_resolver.py.

FAIL-CLOSED, and here that means something specific: an unanswerable lookup
RAISES (TenantLookupUnavailable) and is never allowed to collapse into
get_tenant_of_user's ordinary None. None means "this user has no tenant" — a
data fact the callers turn into a 400. If an outage produced the same None, a
tenant-scoped write would be refused with a message blaming the user's data,
and any caller that treats None as "not tenant-scoped" would write across the
isolation boundary this module exists to hold. Fail-open has no defensible
reading for tenant scoping: there is no safe tenant to guess.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from fastapi import HTTPException

from src.api_auth.deps import AuthClaims
from src.platform_client import PlatformUnavailable, call_platform

logger = logging.getLogger(__name__)


class TenantLookupUnavailable(RuntimeError):
    """Neither platform-api nor a direct connection could answer who this
    user's tenant is. Distinct from "the user has no tenant" on purpose — see
    the module docstring. Surfaces as a 500 to the HTTP caller, which is
    correct: it is our outage, not their bad request."""


# ── Tenant lookup cache (ADR-0009 Schritt 2) ────────────────────────────────
# resolve_tenant_id sits in front of every tenant-scoped INSERT, so a cache
# miss per write would turn one round trip into two. A user's tenant is
# membership, not live state: it is set at creation and changed only by an
# admin PATCH (which invalidates this cache in-process, src/db/admin_routes.py).
#
# POSITIVE ENTRIES ONLY — deliberately unlike principals.py, which caches
# misses to stop a token-scanning flood from amplifying into DB load. There is
# no such flood here (the id comes from an authenticated claim), and a cached
# miss would be actively harmful: a user created and given a tenant seconds
# apart would keep failing with "has no tenant_id" for the rest of the TTL,
# turning a momentary provisioning race into a hard, repeating 400.
_TENANT_CACHE_TTL_S = float(os.getenv("BRIDGE_TENANT_CACHE_TTL_S", "20"))
_tenant_cache: dict[str, tuple[float, str]] = {}


def invalidate_tenant_cache(user_id: Optional[str] = None) -> None:
    """Drop cached tenant resolutions (all, or one user) — called after an
    admin PATCH that moves a user between tenants. Only clears THIS process's
    cache; other processes converge via the TTL, same as the provider-pin
    cache."""
    if user_id is None:
        _tenant_cache.clear()
    else:
        _tenant_cache.pop(str(user_id), None)


async def fetch_user_tenant_row(user_id: Any) -> Optional[dict]:
    """The one DB leaf: users.tenant_id for a user id.

    Split out so platform-api can expose exactly this query as an internal
    endpoint while this module's own direct-DB fallback calls the identical
    function — the query lives in ONE place, not two.

    Returns None when no such user exists, else {"tenantId": str|None}. The
    two are kept apart because resolve_tenant_for_user reports them as
    different errors. Raises on a DB error.
    """
    from src.db.client import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT tenant_id FROM users WHERE id = $1", user_id
        )
    if row is None:
        return None
    return {"tenantId": row["tenant_id"] or None}


async def _fetch_tenant_via_direct_db(user_id: Any) -> Optional[dict]:
    """Direct-DB fallback, used only when platform-api could not answer.

    Raises rather than returning a None that would read as "no tenant": at this
    point both channels have failed and the caller must hear an outage, not a
    verdict about the user.
    """
    from src.db.client import is_db_enabled

    if not is_db_enabled():
        raise TenantLookupUnavailable(
            "tenant lookup: platform-api could not answer and this process has "
            "no direct-DB fallback (BRIDGE_DB_URL unset). Refusing to report "
            "'no tenant' for what is an outage — a tenant-scoped write must "
            "not proceed on a guessed or NULL tenant (ADR 0007, migration 007)."
        )
    return await fetch_user_tenant_row(user_id)


async def _lookup_user_tenant(user_id: Any) -> tuple[bool, Optional[str]]:
    """Resolve (user_exists, tenant_id). Cache → platform-api → direct DB.

    Opts into ONE retry: a pure read, so a replay cannot double-write (see
    platform_client.call_platform for why retrying is opt-in).
    """
    from src.federation import cache_scope, is_foreign_origin

    # ADR-0011: user→tenant is a fact of the request's HOME bridge; the cache
    # key carries the origin scope so the same UUID cannot cross domains.
    uid_key = str(user_id)
    key = f"{cache_scope()}:{uid_key}" if cache_scope() else uid_key
    entry = _tenant_cache.get(key)
    if entry is not None:
        ts, tenant_id = entry
        if (time.monotonic() - ts) <= _TENANT_CACHE_TTL_S:
            return True, tenant_id
        _tenant_cache.pop(key, None)

    try:
        resp = await call_platform(
            "GET", f"/v1/internal/users/{uid_key}/tenant", retries=1, domain="user"
        )
    except PlatformUnavailable as e:
        if is_foreign_origin():
            # ADR-0011: never answer a foreign identity from the LOCAL users
            # table — the same UUID means a different person (or nobody) here.
            raise
        logger.error(
            "tenant lookup via platform-api failed (%s) — falling back to direct DB", e
        )
        row = await _fetch_tenant_via_direct_db(user_id)
    else:
        if resp.status_code == 200 and isinstance(resp.json, dict) and "found" in resp.json:
            row = {"tenantId": resp.json.get("tenantId")} if resp.json["found"] else None
        else:
            # Unexpected contract (wrong status, malformed body) is treated like
            # unreachable rather than like "unknown user": an undeployed
            # platform-api answers 404 on this route, and reading that as "no
            # such user" would reject legitimate tenant-scoped writes.
            if is_foreign_origin():
                raise PlatformUnavailable(
                    f"home-bridge tenant lookup returned unexpected "
                    f"status={resp.status_code} — refusing the local-DB fallback "
                    f"for a foreign-origin request (ADR-0011)"
                )
            logger.error(
                "tenant lookup via platform-api returned unexpected status=%s "
                "body=%r — falling back to direct DB",
                resp.status_code, resp.json,
            )
            row = await _fetch_tenant_via_direct_db(user_id)

    if row is None:
        return False, None
    tenant_id = row["tenantId"]
    if tenant_id:
        _tenant_cache[key] = (time.monotonic(), tenant_id)
    return True, tenant_id


async def get_tenant_of_user(user_id: str) -> Optional[str]:
    """Look up users.tenant_id. Returns None if user unknown or has no tenant.

    Raises TenantLookupUnavailable if the lookup could not be performed at all
    — that is NOT None (see module docstring).
    """
    _found, tenant_id = await _lookup_user_tenant(user_id)
    return tenant_id or None


# Module-private alias kept for callers below.
_tenant_of_user = get_tenant_of_user


async def resolve_tenant_id(
    claims: AuthClaims,
    body_tenant_id: Optional[str] = None,
    body_actor_user_id: Optional[str] = None,
) -> str:
    """
    Return the tenant_id this request writes against. Raises 400 if it can't
    be determined — never returns None.

    Resolution order:
      1. User-JWT with tenant_id in JWT → use it (fast path, no DB hit)
      2. User-JWT without tenant_id → DB-lookup users.tenant_id (defensive
         fallback for malformed JWTs / legacy tokens)
      3. Service-token with body_tenant_id → use it
      4. Service-token with body_actor_user_id → derive from that user's
         tenant (apps that log on behalf of a signed-in user but authenticate
         to the Bridge with a service token — e.g. werking-report's
         logAiActivity)
      5. Anything else → 400

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
        tenant = await _tenant_of_user(claims.user_id)
        if not tenant:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot resolve tenant: user {claims.user_id} has no tenant_id",
            )
        return tenant

    if claims.is_service:
        if body_tenant_id:
            return body_tenant_id
        if body_actor_user_id:
            tenant = await _tenant_of_user(body_actor_user_id)
            if tenant:
                return tenant
            raise HTTPException(
                status_code=400,
                detail=f"Service-token request: actor user {body_actor_user_id} "
                       f"has no tenant_id and no tenantId given in body",
            )
        raise HTTPException(
            status_code=400,
            detail="Service-token requests must pass tenantId or actorUserId in the body",
        )

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
    found, tenant_id = await _lookup_user_tenant(user_id)
    if not found:
        raise HTTPException(status_code=400, detail=f"Unknown user: {user_id}")
    if not tenant_id:
        raise HTTPException(
            status_code=400,
            detail=f"User {user_id} has no tenant_id",
        )
    return tenant_id
