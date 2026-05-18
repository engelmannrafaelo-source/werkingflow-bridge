"""
Developer-Tokens endpoints — programmatic API keys per user/tenant.

Tokens are generated server-side, returned ONCE on creation (plaintext),
stored as sha256 hash. Listing shows only last 4 chars + metadata.

POST   /v1/developer-tokens                 create (return secret once)
GET    /v1/developer-tokens                 list (admin sees all, user own)
DELETE /v1/developer-tokens/{id}            revoke
POST   /v1/developer-tokens/{id}/rotate     issue new secret, revoke old
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api_auth import (
    require_admin,
    require_jwt_or_service,
    AuthClaims,
)
from src.db.client import get_pool

router = APIRouter(prefix="/v1/developer-tokens", tags=["developer-tokens"])

_TOKEN_PREFIX = "wkft_"  # werkingflow token


def _generate_token() -> tuple[str, str, str]:
    """Returns (plaintext, sha256_hex, last_4)."""
    raw = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    h = hashlib.sha256(raw.encode()).hexdigest()
    return raw, h, raw[-4:]


def _row(r: Any, *, include_secret: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": str(r["id"]),
        "userId": str(r["user_id"]),
        "tenantId": r["tenant_id"],
        "name": r["name"],
        "last4": r["last_4"],
        "scopes": list(r["scopes"] or []),
        "createdAt": r["created_at"].isoformat(),
        "expiresAt": r["expires_at"].isoformat() if r["expires_at"] else None,
        "lastUsedAt": r["last_used_at"].isoformat() if r["last_used_at"] else None,
        "revokedAt": r["revoked_at"].isoformat() if r["revoked_at"] else None,
    }
    if include_secret:
        out["secret"] = include_secret  # only on create/rotate
    return out


class TokenCreate(BaseModel):
    userId: str
    tenantId: Optional[str] = None
    name: str = Field(min_length=1, max_length=255)
    scopes: List[str] = Field(default_factory=list)
    expiresAt: Optional[str] = None  # ISO datetime


@router.post("", status_code=201)
async def create_token(
    body: TokenCreate,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    # Scoped callers (user JWT, customer proxy) create only their own tokens;
    # operators may create tokens for any user.
    if not claims.is_operator and body.userId != claims.effective_user_id:
        raise HTTPException(status_code=403, detail="Can only create own tokens")

    plaintext, sha, last4 = _generate_token()

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO developer_tokens
              (user_id, tenant_id, name, token_hash, last_4, scopes, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id, user_id, tenant_id, name, last_4, scopes,
                      created_at, expires_at, last_used_at, revoked_at
            """,
            uuid.UUID(body.userId), body.tenantId, body.name, sha, last4,
            body.scopes, body.expiresAt,
        )
    return _row(row, include_secret=plaintext)


@router.get("")
async def list_tokens(
    userId: Optional[str] = Query(default=None),
    includeRevoked: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    # Scoped callers see only their own tokens; operators see all.
    if not claims.is_operator:
        if userId and userId != claims.effective_user_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        userId = claims.effective_user_id

    where: List[str] = []
    args: List[Any] = []

    def add(cond: str, val: Any) -> None:
        args.append(val)
        where.append(cond.replace("$$", f"${len(args)}"))

    if userId:
        add("user_id = $$", uuid.UUID(userId))
    if not includeRevoked:
        where.append("revoked_at IS NULL")

    sql = """
      SELECT id, user_id, tenant_id, name, last_4, scopes,
             created_at, expires_at, last_used_at, revoked_at
        FROM developer_tokens
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT $" + str(len(args) + 1)
    args.append(limit)

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return {"items": [_row(r) for r in rows], "count": len(rows)}


@router.delete("/{token_id}")
async def revoke_token(
    token_id: str,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM developer_tokens WHERE id = $1",
            uuid.UUID(token_id),
        )
        if not row:
            raise HTTPException(status_code=404, detail=f"Token {token_id} not found")
        if not claims.is_operator and str(row["user_id"]) != claims.effective_user_id:
            raise HTTPException(status_code=403, detail="Can only revoke own tokens")
        await conn.execute(
            "UPDATE developer_tokens SET revoked_at = NOW() WHERE id = $1 AND revoked_at IS NULL",
            uuid.UUID(token_id),
        )
    return {"revoked": True}


@router.post("/{token_id}/rotate")
async def rotate_token(
    token_id: str,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            old = await conn.fetchrow(
                """
                SELECT id, user_id, tenant_id, name, scopes, expires_at
                FROM developer_tokens WHERE id = $1 FOR UPDATE
                """,
                uuid.UUID(token_id),
            )
            if not old:
                raise HTTPException(status_code=404, detail=f"Token {token_id} not found")
            if not claims.is_operator and str(old["user_id"]) != claims.effective_user_id:
                raise HTTPException(status_code=403, detail="Can only rotate own tokens")

            # Revoke old
            await conn.execute(
                "UPDATE developer_tokens SET revoked_at = NOW() WHERE id = $1 AND revoked_at IS NULL",
                old["id"],
            )

            # Issue new
            plaintext, sha, last4 = _generate_token()
            new_row = await conn.fetchrow(
                """
                INSERT INTO developer_tokens
                  (user_id, tenant_id, name, token_hash, last_4, scopes, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id, user_id, tenant_id, name, last_4, scopes,
                          created_at, expires_at, last_used_at, revoked_at
                """,
                old["user_id"], old["tenant_id"], old["name"], sha, last4,
                list(old["scopes"] or []), old["expires_at"],
            )
    return _row(new_row, include_secret=plaintext)
