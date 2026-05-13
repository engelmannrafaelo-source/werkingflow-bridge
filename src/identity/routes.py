"""
Auth endpoints: login / logout / session.
Mounted under /v1/auth — only active when BRIDGE_DB_URL is set.

POST /v1/auth/login   {email, password} -> {jwt, user, appLicenses[]}   PUBLIC
POST /v1/auth/logout  Bearer <jwt> -> 204                              require_jwt
GET  /v1/auth/session Bearer <jwt> -> {user, appLicenses[]}            require_jwt
"""
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Security
from pydantic import BaseModel, EmailStr

from src.api_auth import require_jwt, AuthClaims
from src.db.client import get_pool
from src.identity.password import verify_password
from src.identity.jwt_utils import sign_jwt, verify_jwt

router = APIRouter(prefix="/v1/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)

_SESSION_TTL_HOURS = 8


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _license_row(r: Any) -> Dict[str, Any]:
    return {
        "appId": r["app_id"],
        "planId": r["plan_id"],
        "startDate": r["start_date"].isoformat(),
        "endDate": r["end_date"].isoformat() if r["end_date"] else None,
        "seats": r["seats"],
    }


def _user_dict(row: Any, licenses: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "name": row["name"],
        "tenantId": row["tenant_id"],
        "appLicenses": licenses,
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
    }


async def _fetch_user_with_licenses(conn: Any, user_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    row = await conn.fetchrow(
        "SELECT id, email, name, tenant_id, created_at, updated_at FROM users WHERE id = $1",
        user_id,
    )
    if not row:
        return None
    license_rows = await conn.fetch(
        "SELECT app_id, plan_id, start_date, end_date, seats FROM app_licenses WHERE user_id = $1",
        user_id,
    )
    return _user_dict(row, [_license_row(lr) for lr in license_rows])


def _extract_token(credentials: Optional[HTTPAuthorizationCredentials]) -> str:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    return credentials.credentials


# ---------------------------------------------------------------------------
# POST /v1/auth/login
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


@router.post("/login")
async def login(body: LoginRequest) -> Dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, name, tenant_id, password_hash, created_at, updated_at FROM users WHERE email = $1",
            body.email,
        )

    if not row or not row["password_hash"]:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user_id = row["id"]

    async with pool.acquire() as conn:
        license_rows = await conn.fetch(
            "SELECT app_id, plan_id, start_date, end_date, seats FROM app_licenses WHERE user_id = $1",
            user_id,
        )

    app_licenses = [_license_row(lr) for lr in license_rows]

    token = sign_jwt(
        user_id=str(user_id),
        email=row["email"],
        tenant_id=row["tenant_id"],
        app_licenses=app_licenses,
    )

    # Store session
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=_SESSION_TTL_HOURS)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO sessions (user_id, token, created_at, expires_at) VALUES ($1, $2, $3, $4)",
            user_id,
            token,
            now,
            expires_at,
        )

    user = _user_dict(row, app_licenses)
    return {"jwt": token, "user": user, "appLicenses": app_licenses}


# ---------------------------------------------------------------------------
# POST /v1/auth/logout
# ---------------------------------------------------------------------------

@router.post("/logout", status_code=204)
async def logout(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    _claims: AuthClaims = Depends(require_jwt),
) -> None:
    """
    Revoke the current JWT's session row. require_jwt has already validated
    the signature + expiry; here we just mark sessions.expires_at = NOW().
    We do NOT 404 if the session row is missing — JWT validity is what
    counts for logout intent.
    """
    token = _extract_token(credentials)
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE sessions SET expires_at = NOW() WHERE token = $1",
            token,
        )


# ---------------------------------------------------------------------------
# GET /v1/auth/session
# ---------------------------------------------------------------------------

@router.get("/session")
async def get_session(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    claims: AuthClaims = Depends(require_jwt),
) -> Dict[str, Any]:
    """
    Resolve the current session from the JWT.

    require_jwt has already validated signature + expiry. We additionally
    check the sessions table because logout sets expires_at = NOW() —
    that's how we revoke a JWT before its natural expiry.
    """
    token = _extract_token(credentials)
    pool = get_pool()
    async with pool.acquire() as conn:
        session_row = await conn.fetchrow(
            "SELECT expires_at FROM sessions WHERE token = $1",
            token,
        )

    if not session_row:
        raise HTTPException(status_code=401, detail="Session not found")

    now = datetime.now(timezone.utc)
    if session_row["expires_at"].replace(tzinfo=timezone.utc) <= now:
        raise HTTPException(status_code=401, detail="Session expired or revoked")

    user_id = uuid.UUID(claims.user_id)
    async with pool.acquire() as conn:
        user = await _fetch_user_with_licenses(conn, user_id)

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {"user": user, "appLicenses": user["appLicenses"]}
