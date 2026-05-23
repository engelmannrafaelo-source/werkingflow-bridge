"""
Sandbox lease DB operations (asyncpg).

All functions require a live DB connection from get_pool().
They raise HTTPException on not-found/conflict conditions and RuntimeError
on unexpected failures (fail-loud, no silent fallback).
"""
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)

_SECRETS_DIR = Path(os.getenv("BRIDGE_SECRETS_DIR", "/root/werkingflow-bridge/secrets"))


# ---------------------------------------------------------------------------
# Token file
# ---------------------------------------------------------------------------

def read_oauth_token(account_id: str) -> str:
    """
    Read the OAuth token file for an account. Fails loud if missing or empty.

    File convention: <BRIDGE_SECRETS_DIR>/claude_token_<account_id>.txt
    """
    token_path = _SECRETS_DIR / f"claude_token_{account_id}.txt"
    if not token_path.exists():
        raise RuntimeError(
            f"OAuth token file not found for account '{account_id}': {token_path}. "
            "Ensure the file exists and is readable before calling lease-token."
        )
    token = token_path.read_text().strip()
    if not token:
        raise RuntimeError(
            f"OAuth token file is empty for account '{account_id}': {token_path}"
        )
    return token


# ---------------------------------------------------------------------------
# Tenant lookup
# ---------------------------------------------------------------------------

async def get_tenant_info(
    conn: Any,
    user_id: uuid.UUID,
    app: Optional[str] = None,
    auto_provision: bool = True,
) -> dict:
    """
    Return {'tenant_id': str, 'billing_mode': str} for a user.

    When the user is missing and `app` + `auto_provision` are set, JIT-provision
    the user against the app-named tenant (created on demand with the default
    billing_mode). This unblocks first-time sandbox sessions for users that
    were created in an app's own auth system (Supabase, etc.) without a
    matching POST /v1/users call.

    Raises 404 if user not found AND auto-provisioning is disabled or no app
    was supplied.
    """
    row = await conn.fetchrow(
        """
        SELECT u.tenant_id, t.billing_mode
        FROM users u
        JOIN tenants t ON t.id = u.tenant_id
        WHERE u.id = $1
        """,
        user_id,
    )
    if row is None:
        if auto_provision and app:
            await _jit_provision_user(conn, user_id, app)
            row = await conn.fetchrow(
                """
                SELECT u.tenant_id, t.billing_mode
                FROM users u
                JOIN tenants t ON t.id = u.tenant_id
                WHERE u.id = $1
                """,
                user_id,
            )
        if row is None:
            raise HTTPException(status_code=404, detail=f"User {user_id} not found")
    return {"tenant_id": row["tenant_id"], "billing_mode": row["billing_mode"]}


async def _jit_provision_user(conn: Any, user_id: uuid.UUID, app: str) -> None:
    """
    Idempotent JIT user+tenant provisioning.

    Convention: tenant_id = app name (one tenant per single-tenant app like
    'engelmann', 'werking-report', etc.). For multi-tenant apps the caller
    should provision explicitly via POST /v1/users.

    Email is a synthetic placeholder (`jit-<user_id>@<app>.local`) so the
    UNIQUE constraint on users.email can never collide with real signups.
    Apps can PATCH the user row later with the real email once known.
    """
    tenant_id = app
    placeholder_email = f"jit-{user_id}@{app}.local"
    placeholder_name = f"JIT-provisioned ({app})"

    # Ensure tenant exists. ON CONFLICT DO NOTHING handles the race where two
    # concurrent first-lease calls for the same fresh tenant arrive together.
    await conn.execute(
        """
        INSERT INTO tenants (id, name, created_at)
        VALUES ($1, $2, NOW())
        ON CONFLICT (id) DO NOTHING
        """,
        tenant_id,
        f"JIT tenant for {app}",
    )

    # Insert user. ON CONFLICT DO NOTHING covers both id-collision (re-entry of
    # the same provisioning) and the rare email-collision via the placeholder
    # pattern. The follow-up SELECT in the caller re-reads, so either branch
    # leaves the system in a consistent state.
    await conn.execute(
        """
        INSERT INTO users (id, email, name, tenant_id, created_at, updated_at)
        VALUES ($1, $2, $3, $4, NOW(), NOW())
        ON CONFLICT (id) DO NOTHING
        """,
        user_id,
        placeholder_email,
        placeholder_name,
        tenant_id,
    )

    logger.info(
        f"JIT-provisioned user={user_id} app={app} tenant={tenant_id} "
        f"(email=placeholder, name=placeholder)"
    )


# ---------------------------------------------------------------------------
# Lease CRUD
# ---------------------------------------------------------------------------

async def create_lease(
    conn: Any,
    user_id: uuid.UUID,
    tenant_id: str,
    app: str,
    account_id: str,
    expires_at: datetime,
) -> uuid.UUID:
    """Insert a new lease row. Returns the generated lease_id."""
    lease_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO sandbox_leases
            (lease_id, user_id, tenant_id, app, account_id, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        lease_id,
        user_id,
        tenant_id,
        app,
        account_id,
        expires_at,
    )
    return lease_id


async def heartbeat_lease(conn: Any, lease_id: uuid.UUID, extend_minutes: int = 10) -> datetime:
    """
    Refresh last_heartbeat_at and extend expires_at by extend_minutes.

    Returns the new expires_at.
    Raises 404 if lease is missing or already released.

    Uses UPDATE with a WHERE predicate on released_at IS NULL to prevent
    refreshing a stale/released lease without a separate SELECT (single round-trip).
    """
    row = await conn.fetchrow(
        """
        UPDATE sandbox_leases
        SET
            last_heartbeat_at = NOW(),
            expires_at = GREATEST(expires_at, NOW() + ($2 || ' minutes')::INTERVAL)
        WHERE lease_id = $1
          AND released_at IS NULL
          AND expires_at > NOW()
        RETURNING expires_at
        """,
        lease_id,
        str(extend_minutes),
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Lease {lease_id} not found, already released, or expired",
        )
    return row["expires_at"]


async def release_lease(conn: Any, lease_id: uuid.UUID) -> bool:
    """
    Set released_at = NOW() if not already released (idempotent).

    Returns True if the lease was just released, False if it was already released.
    Raises 404 if the lease_id does not exist at all.
    """
    exists = await conn.fetchval(
        "SELECT 1 FROM sandbox_leases WHERE lease_id = $1",
        lease_id,
    )
    if exists is None:
        raise HTTPException(status_code=404, detail=f"Lease {lease_id} not found")

    result = await conn.fetchval(
        """
        UPDATE sandbox_leases
        SET released_at = NOW()
        WHERE lease_id = $1 AND released_at IS NULL
        RETURNING lease_id
        """,
        lease_id,
    )
    return result is not None  # True = just released; False = was already released


async def attach_session(conn: Any, lease_id: uuid.UUID, session_id: str) -> None:
    """
    Write session_id into an active lease. Raises 404 if lease missing or released.
    """
    result = await conn.fetchval(
        """
        UPDATE sandbox_leases
        SET session_id = $2
        WHERE lease_id = $1 AND released_at IS NULL
        RETURNING lease_id
        """,
        lease_id,
        session_id,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Lease {lease_id} not found or already released",
        )


# ---------------------------------------------------------------------------
# Usage record
# ---------------------------------------------------------------------------

async def record_usage(
    conn: Any,
    litellm_call_id: str,
    user_id: uuid.UUID,
    tenant_id: str,
    session_id: str,
    lease_id: Optional[uuid.UUID],
    account_id: str,
    app: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    hypothetical_cost_eur: float,
    billing_mode: str,
    app_env: Optional[str] = None,
) -> bool:
    """
    INSERT sandbox usage event into usage_events.

    ON CONFLICT (idempotency_key) DO NOTHING for idempotency.
    Returns True if a row was actually inserted, False if it was a duplicate —
    the caller uses this to avoid double-writing the mirrored activities row.

    billing_mode: legacy TEXT from tenants ('subscription'|'pay_per_token').
    Mapped to billing_mode_enum:
      subscription  → flat_rate_estimated (real_cost = 0, hypothetical = what it costs)
      pay_per_token → pay_per_token       (real_cost = hypothetical)

    app_env: already-normalised environment bucket (prod|staging|local), or
        None when the caller (sandbox daemon) sent no X-App-Env header. Most
        sandbox calls run outside any prod/staging/local app context — None
        is semantically correct; `source='sandbox'` carries the dimensional
        distinction. The column exists so a sandbox attached to a specific
        app variant CAN tag itself.
    """
    import json as _json
    from src.pricing import PRICING_VERSION

    if billing_mode == "pay_per_token":
        bm_enum = "pay_per_token"
        real_cost = hypothetical_cost_eur
    else:
        bm_enum = "flat_rate_estimated"
        real_cost = 0.0

    row = await conn.fetchrow(
        """
        INSERT INTO usage_events (
            source,
            user_id, tenant_id,
            app, app_env, model, provider, region,
            input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
            billing_mode, real_cost_eur, hypothetical_cost_eur, pricing_version,
            session_id, idempotency_key, provider_metadata
        ) VALUES (
            'sandbox',
            $1, $2,
            $3, $4::app_env, $5, 'anthropic', NULL,
            $6, $7, $8, $9,
            $10::billing_mode_enum, $11, $12, $13,
            $14, $15, $16::jsonb
        )
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        user_id,
        tenant_id,
        app,
        app_env,
        model,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
        bm_enum,
        real_cost,
        hypothetical_cost_eur,
        PRICING_VERSION,
        session_id,
        litellm_call_id,
        _json.dumps({
            "litellm_call_id": litellm_call_id,
            "lease_id": str(lease_id) if lease_id else None,
            "account_id": account_id,
        }),
    )
    return row is not None


async def get_session_aggregate(conn: Any, session_id: str) -> dict:
    """Aggregate token/cost totals for one sandbox session from usage_events."""
    row = await conn.fetchrow(
        """
        SELECT
            COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens), 0)
                AS total_tokens,
            COALESCE(SUM(hypothetical_cost_eur), 0.0) AS total_hypothetical_eur
        FROM usage_events
        WHERE source = 'sandbox' AND session_id = $1
        """,
        session_id,
    )
    return {
        "total_session_tokens": int(row["total_tokens"]),
        "total_session_hypothetical_eur": float(row["total_hypothetical_eur"]),
    }
