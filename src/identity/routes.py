"""
Auth endpoints: login / logout / session / register.
Mounted under /v1/auth — only active when BRIDGE_DB_URL is set.

POST /v1/auth/login      {email, password} -> {jwt, user, appLicenses[]}  PUBLIC
POST /v1/auth/register   {email, password, name, appId, checkId?} -> {jwt, user, appLicenses[]}  PUBLIC
POST /v1/auth/logout     Bearer <jwt> -> 204                             require_jwt
GET  /v1/auth/session    Bearer <jwt> -> {user, appLicenses[]}           require_jwt
POST /v1/auth/issue      X-Bridge-Service-Token {userId} -> {jwt, expiresAt}  require_service_token
POST /v1/auth/test-token {email} -> {jwt, user, appLicenses[]}           service-token, test tenants only
"""
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import asyncpg
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Security
from pydantic import BaseModel, EmailStr, Field

from src.api_auth import require_jwt, require_service_token, AuthClaims
from src.db.client import get_pool
from src.identity.password import hash_password, verify_password
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


# ---------------------------------------------------------------------------
# POST /v1/auth/register  — public self-service registration
#
# Replaces the per-app users.json (werking-report standalone-auth.createUser).
# Bridge becomes the single source of truth for identity: a register-then-login
# cycle works because both sides write to / read from the same `users` table.
#
# Approval flow: the `users` table has no `approved`/`verified` column today, so
# registration auto-approves and the response includes a JWT (auto-login). If a
# future migration adds such a column, this endpoint should switch to 202 +
# {pendingApproval: true} for non-approved users — see the architecture-decision
# block in the function body.
# ---------------------------------------------------------------------------

# Mirrors app_id ENUM from migrations/001_initial_schema.sql. If the enum grows,
# add the value here in the same change — fail-loud beats a confusing 500 from
# Postgres on an unknown enum literal.
_REGISTER_ALLOWED_APP_IDS = frozenset({
    "werking-report", "werking-energy", "werking-safety",
    "werking-noise", "engelmann",
})

# Default plan_id assigned to the initial app_license at registration. 'trial'
# is the only plan_id value in the enum that is app-agnostic and does not
# pre-commit the new user to a paid tier.
_REGISTER_DEFAULT_PLAN_ID = "trial"


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, description="Minimum 8 characters")
    name: str = Field(min_length=1, max_length=255)
    appId: str = Field(description="Initial app context — must be a known app_id")
    checkId: Optional[str] = Field(
        default=None,
        description="Opaque correlation id for app-side flows (e.g. werking-report check)",
    )


@router.post("/register")
async def register(body: RegisterRequest) -> Dict[str, Any]:
    """
    Public self-service registration.

    Creates: tenants row (personal tenant, owner_user_id=new user),
             users row (password_hash via bcrypt),
             app_licenses row (initial trial license for `appId`),
             sessions row (auto-login JWT).

    Fails loud:
      - 409 Conflict          → email already taken (no enumeration masking on
                                Bridge — UI may translate as needed)
      - 422 Unprocessable     → Pydantic schema violation (invalid email,
                                missing field, password < 8 chars)
      - 400 Bad Request       → appId not in the app_id enum
      - 5xx                   → DB / programmer error (re-raised, never silently
                                returns a half-built user)

    Architecture decision (Approval-Flow):
      The current schema does NOT have a users.approved / users.verified column.
      Per the migration plan we auto-approve and return a JWT immediately so
      werking-report can replace its local users.json without losing the
      register-then-immediately-use-the-app UX. When approval is later added,
      branch here on the column and return 202 with {pendingApproval: true}
      instead of minting a JWT.
    """
    if body.appId not in _REGISTER_ALLOWED_APP_IDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown appId '{body.appId}'. Must be one of: "
                f"{sorted(_REGISTER_ALLOWED_APP_IDS)}"
            ),
        )

    pool = get_pool()
    now = datetime.now(timezone.utc)
    today = now.date()
    tenant_id = str(uuid.uuid4())
    password_hash = hash_password(body.password)

    async with pool.acquire() as conn:
        # All identity rows must succeed together or none — a half-built user
        # (tenant without user, user without license) is worse than fail-loud.
        async with conn.transaction():
            # Email uniqueness is enforced by the UNIQUE constraint on
            # users.email; pre-checking would race. Catch the violation and
            # translate to 409 — same pattern as create_user in admin_routes.
            try:
                await conn.execute(
                    """
                    INSERT INTO tenants (id, name, account_type, created_at)
                    VALUES ($1, $2, 'customer'::account_type, $3)
                    """,
                    tenant_id,
                    f"Personal tenant for {body.email}",
                    now,
                )

                user_row = await conn.fetchrow(
                    """
                    INSERT INTO users (email, name, tenant_id, role, password_hash, created_at, updated_at)
                    VALUES ($1, $2, $3, 'user', $4, $5, $5)
                    RETURNING id, email, name, tenant_id, role, provider_config, created_at, updated_at
                    """,
                    body.email,
                    body.name,
                    tenant_id,
                    password_hash,
                    now,
                )

                user_id = user_row["id"]

                # Close the loop: the personal tenant is owned by the user who
                # just created it. Done after the user insert because
                # tenants.owner_user_id FK references users(id).
                await conn.execute(
                    "UPDATE tenants SET owner_user_id = $1 WHERE id = $2",
                    user_id,
                    tenant_id,
                )

                # Initial app_license — trial. start_date=today, end_date=NULL
                # (open-ended trial; billing flow may convert it to a paid plan).
                await conn.execute(
                    """
                    INSERT INTO app_licenses (user_id, app_id, plan_id, start_date, end_date, seats)
                    VALUES ($1, $2::app_id, $3::plan_id, $4, NULL, 1)
                    """,
                    user_id,
                    body.appId,
                    _REGISTER_DEFAULT_PLAN_ID,
                    today,
                )
            except asyncpg.UniqueViolationError as exc:
                # Most likely cause: users.email collision. We surface a clear
                # 409 — per the task brief, anti-enumeration is a UI concern,
                # not a Bridge concern.
                msg = str(exc).lower()
                if "email" in msg or "users_email" in msg:
                    raise HTTPException(
                        status_code=409,
                        detail=f"User with email '{body.email}' already exists",
                    )
                # Some other unique constraint — surface it explicitly rather
                # than swallowing with a generic 500.
                raise HTTPException(
                    status_code=409,
                    detail=f"Registration failed: {exc}",
                )
            except asyncpg.PostgresError as exc:
                # Fail-loud on any DB error. Logged with full detail server-side,
                # surfaced as 500 with the PG message so it never silently passes.
                logger.exception("register failed: %s", exc)
                raise HTTPException(
                    status_code=500,
                    detail=f"Database error during registration: {exc}",
                )

        # Build the JWT outside the transaction — once persisted, the user
        # exists; auto-login is a separate concern.
        license_rows = await conn.fetch(
            "SELECT app_id, plan_id, start_date, end_date, seats FROM app_licenses WHERE user_id = $1",
            user_id,
        )
        app_licenses = [_license_row(lr) for lr in license_rows]

        token = sign_jwt(
            user_id=str(user_id),
            email=user_row["email"],
            tenant_id=user_row["tenant_id"],
            app_licenses=app_licenses,
            role=user_row["role"],
        )

        expires_at = now + timedelta(hours=_SESSION_TTL_HOURS)
        await conn.execute(
            "INSERT INTO sessions (user_id, token, created_at, expires_at) VALUES ($1, $2, $3, $4)",
            user_id,
            token,
            now,
            expires_at,
        )

    logger.info(
        "identity.register: created user_id=%s tenant_id=%s appId=%s checkId=%s",
        user_id,
        tenant_id,
        body.appId,
        body.checkId,
    )

    user = _user_dict(user_row, app_licenses)
    return {"jwt": token, "user": user, "appLicenses": app_licenses}
