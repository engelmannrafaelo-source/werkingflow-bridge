"""
Auth endpoints: login / logout / session.
Mounted under /v1/auth — only active when BRIDGE_DB_URL is set.

POST /v1/auth/login      {email, password} -> {jwt, user, appLicenses[]}  PUBLIC
POST /v1/auth/logout     Bearer <jwt> -> 204                             require_jwt
GET  /v1/auth/session    Bearer <jwt> -> {user, appLicenses[]}           require_jwt
POST /v1/auth/issue      X-Bridge-Service-Token {userId} -> {jwt, expiresAt}  require_service_token
POST /v1/auth/test-token {email} -> {jwt, user, appLicenses[]}           service-token, test tenants only
"""
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Security
from pydantic import BaseModel, EmailStr

from src.api_auth import require_jwt, require_service_token, AuthClaims
from src.db.client import get_pool
from src.identity.password import verify_password
from src.identity.jwt_utils import sign_jwt, verify_jwt

router = APIRouter(prefix="/v1/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)

_SESSION_TTL_HOURS = 8

logger = logging.getLogger(__name__)


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
        "role": row["role"],
        "providerConfig": row["provider_config"],  # JSONB or None; None = inherit tenant default
        "appLicenses": licenses,
        "createdAt": row["created_at"].isoformat(),
        "updatedAt": row["updated_at"].isoformat(),
    }


async def _fetch_user_with_licenses(conn: Any, user_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    row = await conn.fetchrow(
        "SELECT id, email, name, tenant_id, role, provider_config, created_at, updated_at FROM users WHERE id = $1",
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
            "SELECT id, email, name, tenant_id, role, provider_config, password_hash, created_at, updated_at FROM users WHERE email = $1",
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
        role=row["role"],
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
# POST /v1/auth/test-token
# ---------------------------------------------------------------------------

class TestTokenRequest(BaseModel):
    email: EmailStr


async def _issue_token(conn: Any, row: Any) -> Dict[str, Any]:
    """Sign a JWT for an already-fetched user row and persist its session.

    `row` must carry: id, email, name, tenant_id, role, created_at, updated_at.
    """
    user_id = row["id"]
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
        role=row["role"],
    )

    now = datetime.now(timezone.utc)
    await conn.execute(
        "INSERT INTO sessions (user_id, token, created_at, expires_at) VALUES ($1, $2, $3, $4)",
        user_id,
        token,
        now,
        now + timedelta(hours=_SESSION_TTL_HOURS),
    )
    return {"jwt": token, "user": _user_dict(row, app_licenses), "appLicenses": app_licenses}


@router.post("/test-token")
async def test_token(
    body: TestTokenRequest,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """
    Mint a JWT for a test user without a password — for the unified tester.

    The tester needs Bearer tokens for users it did not create the password
    for, and must bypass the single-active-session rule. Two hard guards keep
    this safe in every environment (fail-fast, no silent fallback):

      1. require_service_token — only callers holding the Bridge service token.
      2. The user's tenant MUST be account_type='test'. A token can never be
         minted for a 'customer' or 'internal' account, even with a valid
         service token. This is the wall, not the service token alone.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.id, u.email, u.name, u.tenant_id, u.role,
                   u.provider_config, u.created_at, u.updated_at, t.account_type
            FROM users u
            JOIN tenants t ON t.id = u.tenant_id
            WHERE u.email = $1
            """,
            body.email,
        )

        if not row:
            raise HTTPException(status_code=404, detail="User not found")

        if row["account_type"] != "test":
            raise HTTPException(
                status_code=403,
                detail=(
                    f"test-token refused: tenant account_type is "
                    f"'{row['account_type']}', not 'test'"
                ),
            )

        return await _issue_token(conn, row)


# ---------------------------------------------------------------------------
# POST /v1/auth/logout
# ---------------------------------------------------------------------------

@router.post("/logout", status_code=204, response_class=Response)
async def logout(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    _claims: AuthClaims = Depends(require_jwt),
) -> Response:
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
    return Response(status_code=204)


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


# ---------------------------------------------------------------------------
# POST /v1/auth/issue  — service-token -> user-scoped JWT
#
# Lets a trusted internal service (the agent-sandbox daemon, authenticated by
# X-Bridge-Service-Token) mint a user-scoped JWT WITHOUT the user's password.
# This is how a sandbox session is bound to a real Bridge user: the daemon
# requests a JWT for its configured user id and carries it as the session
# identity. Never exposed to end users. Every issuance is audit-logged.
# ---------------------------------------------------------------------------

class IssueTokenRequest(BaseModel):
    userId: uuid.UUID


@router.post("/issue")
async def issue_token(
    body: IssueTokenRequest,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """
    Issue a user-scoped JWT for `userId`. Service-token auth only.

    Fails loud: raises 404 if the user does not exist — there is no anonymous
    or fallback identity. The caller must hold a real, provisioned user id.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, name, tenant_id, created_at, updated_at FROM users WHERE id = $1",
            body.userId,
        )
        if not row:
            raise HTTPException(status_code=404, detail=f"User {body.userId} not found")
        license_rows = await conn.fetch(
            "SELECT app_id, plan_id, start_date, end_date, seats FROM app_licenses WHERE user_id = $1",
            body.userId,
        )

    app_licenses = [_license_row(lr) for lr in license_rows]

    token = sign_jwt(
        user_id=str(row["id"]),
        email=row["email"],
        tenant_id=row["tenant_id"],
        app_licenses=app_licenses,
    )

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=_SESSION_TTL_HOURS)
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO sessions (user_id, token, created_at, expires_at) VALUES ($1, $2, $3, $4)",
            row["id"],
            token,
            now,
            expires_at,
        )

    logger.info(
        "identity.issue: minted service JWT user_id=%s tenant_id=%s expires_at=%s",
        row["id"],
        row["tenant_id"],
        expires_at.isoformat(),
    )
    return {"jwt": token, "expiresAt": expires_at.isoformat()}
