"""
Impersonation — admin-only, audit-logged JWT issuance to act-as a user.

POST /v1/auth/impersonate/{user_id}   admin only — issues a short-lived JWT
                                       that carries the target user's identity
                                       PLUS an impersonatedBy claim for audit.
GET  /v1/auth/impersonate/active     diagnostic — list of currently-active
                                       impersonation sessions (rows in
                                       sessions where token has 'impersonatedBy').

Every impersonation start automatically writes an audit_log entry so admin
mis-use is traceable.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException

from src.api_auth import require_admin, AuthClaims
from src.config import config
from src.db.client import get_pool
from src.identity.jwt_utils import _ALGORITHM  # type: ignore[attr-defined]

router = APIRouter(prefix="/v1/auth/impersonate", tags=["impersonation"])

# Impersonation JWTs expire fast (1 hour) so a stolen token has limited blast.
_IMPERSONATION_TTL_HOURS = 1


def _sign_impersonation_jwt(target_user_id: str, target_email: str, target_tenant_id: str,
                             app_licenses: List[Dict[str, Any]], admin_user_id: str,
                             admin_email: str) -> tuple[str, datetime]:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=_IMPERSONATION_TTL_HOURS)
    payload = {
        "sub": target_user_id,
        "email": target_email,
        "tenantId": target_tenant_id,
        "appLicenses": app_licenses,
        "isAdmin": False,
        "iat": now,
        "exp": expires_at,
        "jti": str(uuid.uuid4()),
        # Custom claims — visible in every audit downstream
        "impersonatedBy": admin_user_id,
        "impersonatedByEmail": admin_email,
    }
    return pyjwt.encode(payload, config.jwt_secret, algorithm=_ALGORITHM), expires_at


@router.post("/{user_id}")
async def start_impersonation(
    user_id: str,
    claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    if not claims.user_id:
        raise HTTPException(status_code=403, detail="Only user-JWT admins may impersonate (service tokens cannot)")

    target_uuid = uuid.UUID(user_id)
    admin_uuid = uuid.UUID(claims.user_id)

    pool = get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT id, email, name, tenant_id FROM users WHERE id = $1",
            target_uuid,
        )
        if not user:
            raise HTTPException(status_code=404, detail=f"User {user_id} not found")
        licenses = await conn.fetch(
            "SELECT app_id, plan_id, start_date, end_date, seats FROM app_licenses WHERE user_id = $1",
            target_uuid,
        )

    app_licenses = [
        {
            "appId": r["app_id"],
            "planId": r["plan_id"],
            "startDate": r["start_date"].isoformat(),
            "endDate": r["end_date"].isoformat() if r["end_date"] else None,
            "seats": r["seats"],
        }
        for r in licenses
    ]

    token, expires_at = _sign_impersonation_jwt(
        target_user_id=str(user["id"]),
        target_email=user["email"],
        target_tenant_id=user["tenant_id"],
        app_licenses=app_licenses,
        admin_user_id=claims.user_id,
        admin_email=claims.email or "",
    )

    # Persist session + audit row in one transaction for atomic accounting.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO sessions (user_id, token, created_at, expires_at) VALUES ($1, $2, NOW(), $3)",
                target_uuid, token, expires_at,
            )
            await conn.execute(
                """
                INSERT INTO audit_log
                  (actor_user_id, actor_label, action, target_kind, target_id, metadata)
                VALUES ($1, $2, 'user.impersonation.started', 'user', $3, $4::jsonb)
                """,
                admin_uuid,
                claims.email or "admin",
                str(target_uuid),
                '{"impersonationTtlHours": ' + str(_IMPERSONATION_TTL_HOURS) + '}',
            )

    return {
        "jwt": token,
        "expiresAt": expires_at.isoformat(),
        "target": {
            "id": str(user["id"]),
            "email": user["email"],
            "name": user["name"],
            "tenantId": user["tenant_id"],
        },
        "impersonatedBy": {
            "userId": claims.user_id,
            "email": claims.email,
        },
    }


@router.get("/active")
async def list_active_impersonations(
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Inspect currently-valid impersonation sessions. We decode JWTs from
    sessions table; only rows whose JWT has an 'impersonatedBy' claim
    are reported.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT token, user_id, created_at, expires_at FROM sessions WHERE expires_at > NOW()"
        )

    active: List[Dict[str, Any]] = []
    for r in rows:
        try:
            payload = pyjwt.decode(r["token"], config.jwt_secret, algorithms=[_ALGORITHM])
        except pyjwt.InvalidTokenError:
            continue
        if "impersonatedBy" not in payload:
            continue
        active.append({
            "targetUserId": str(r["user_id"]),
            "targetEmail": payload.get("email"),
            "impersonatedBy": payload.get("impersonatedBy"),
            "impersonatedByEmail": payload.get("impersonatedByEmail"),
            "createdAt": r["created_at"].isoformat(),
            "expiresAt": r["expires_at"].isoformat(),
            "jti": payload.get("jti"),
        })
    return {"items": active, "count": len(active)}
