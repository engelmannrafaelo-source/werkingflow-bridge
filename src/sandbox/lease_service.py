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

async def get_tenant_info(conn: Any, user_id: uuid.UUID) -> dict:
    """
    Return {'tenant_id': str, 'billing_mode': str} for a user.
    Raises 404 if user not found, 500 if tenant row missing.
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
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")
    return {"tenant_id": row["tenant_id"], "billing_mode": row["billing_mode"]}


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
) -> None:
    """
    INSERT usage event. ON CONFLICT (litellm_call_id) DO NOTHING for idempotency.
    """
    await conn.execute(
        """
        INSERT INTO sandbox_usage_events (
            litellm_call_id, user_id, tenant_id, session_id, lease_id,
            account_id, app, model,
            input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
            hypothetical_cost_eur, real_cost_eur, billing_mode
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
        ON CONFLICT (litellm_call_id) DO NOTHING
        """,
        litellm_call_id,
        user_id,
        tenant_id,
        session_id,
        lease_id,
        account_id,
        app,
        model,
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
        hypothetical_cost_eur,
        0.0,  # real_cost_eur = 0 for subscription
        billing_mode,
    )


async def get_session_aggregate(conn: Any, session_id: str) -> dict:
    row = await conn.fetchrow(
        """
        SELECT
            COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_creation_tokens), 0)
                AS total_tokens,
            COALESCE(SUM(hypothetical_cost_eur), 0.0) AS total_hypothetical_eur
        FROM sandbox_usage_events
        WHERE session_id = $1
        """,
        session_id,
    )
    return {
        "total_session_tokens": int(row["total_tokens"]),
        "total_session_hypothetical_eur": float(row["total_hypothetical_eur"]),
    }
