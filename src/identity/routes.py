"""
Auth endpoints: login / logout / session / register + password-reset / email-verification.
Mounted under /v1/auth — only active when BRIDGE_DB_URL is set.

POST /v1/auth/login                       {email, password} -> {jwt, user, appLicenses[]}                    PUBLIC
POST /v1/auth/register                    {email, password, name, appId, checkId?} -> {jwt, user, ...}        PUBLIC
POST /v1/auth/logout                      Bearer <jwt> -> 204                                                 require_jwt
GET  /v1/auth/session                     Bearer <jwt> -> {user, appLicenses[]}                               require_jwt
POST /v1/auth/issue                       X-Bridge-Service-Token {userId} -> {jwt, expiresAt}                 require_service_token
POST /v1/auth/test-token                  {email} -> {jwt, user, appLicenses[]}                               service-token, test tenants only
POST /v1/auth/forgot-password             {email} -> 204                                                      PUBLIC, anti-enumeration
POST /v1/auth/reset-password-with-token   {token, newPassword} -> 204                                         PUBLIC
POST /v1/auth/resend-verification         {email} -> 204                                                      PUBLIC, anti-enumeration
POST /v1/auth/verify-email                {token} -> 204                                                      PUBLIC
"""
import hashlib
import logging
import os
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import asyncpg
import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Security
from pydantic import BaseModel, EmailStr, Field

from src.api_auth import require_jwt, require_service_token, AuthClaims
from src.db.client import get_pool
from src.identity.password import hash_password, verify_password
from src.identity.jwt_utils import sign_jwt, verify_jwt
from src.identity.webhook_config import BRIDGE_AUTH_APP_IDS, get_webhook_config

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

                # Role: 'owner'. Self-service registration creates a new
                # personal tenant where this user is the only member. Anything
                # less than owner blocks them from buying plans / managing
                # subscriptions in the customer portal (ADMIN_ROLES in
                # packages/usage-billing-admin SubscriptionSection.tsx).
                user_row = await conn.fetchrow(
                    """
                    INSERT INTO users (email, name, tenant_id, role, password_hash, created_at, updated_at)
                    VALUES ($1, $2, $3, 'owner', $4, $5, $5)
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

                # Trial subscription — the customer portal's plan-purchase
                # gate (SubscriptionSection.tsx) reads isTrial from the
                # subscriptions table. Without this row, the freshly
                # registered user can't see the "Standard kaufen"-CTA — the
                # forced-trial funnel was broken end-to-end.
                # mollie_customer_id stays NULL until the first paid
                # checkout creates a Mollie customer (see migration 024
                # for the CHECK constraint enforcing trial-only NULL).
                # trial_ends_at controls the forced-trial-period expiry —
                # list_subscriptions lazy-expires past-due rows. 7 days
                # matches the registration stage in required-fields.yaml.
                await conn.execute(
                    """
                    INSERT INTO subscriptions
                        (user_id, app_id, plan_id, status, mollie_customer_id, seats, started_at, trial_ends_at)
                    VALUES
                        ($1, $2::app_id, 'trial'::plan_id, 'active'::subscription_status, NULL, 1, $3, $3 + INTERVAL '7 days')
                    """,
                    user_id,
                    body.appId,
                    now,
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


# ---------------------------------------------------------------------------
# Password reset + email verification — single-use auth_tokens (migration 018)
#
# Two endpoints per token-type:
#   forgot-password / resend-verification → mint + persist + log cleartext
#   reset-password-with-token / verify-email → consume + apply effect
#
# Anti-enumeration is enforced on the mint endpoints: same 204 response for
# unknown user / verified user / rate-limited / success. Cleartext is logged
# to stdout for the app layer to pick up (mailer is out-of-scope per the task).
# ---------------------------------------------------------------------------

# Token byte length — 32 bytes ≈ 256 bits of entropy, hex-encoded to 64 chars.
_TOKEN_BYTES = 32

# Rate limit on mint endpoints: refuses to issue more than N tokens of the
# same type to the same user inside a rolling 1-hour window. Silent at the
# HTTP layer (still 204) to preserve anti-enumeration; the failure is visible
# only in server logs.
_TOKEN_RATE_LIMIT_PER_HOUR = 3


def _token_expiry_hours(token_type: str) -> int:
    """
    TTL per token type, env-overridable. Hard fail-loud on a non-integer
    override — silent fallback to a default would let a typo ship a token
    that never expires (or expires immediately).
    """
    if token_type == "password_reset":
        raw = os.getenv("TOKEN_EXPIRES_HOURS_RESET", "24")
    elif token_type == "email_verification":
        raw = os.getenv("TOKEN_EXPIRES_HOURS_VERIFY", "72")
    else:
        raise ValueError(f"Unknown token_type '{token_type}'")
    try:
        hours = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid TOKEN_EXPIRES_HOURS for {token_type}: {raw!r}"
        ) from exc
    if hours <= 0:
        raise RuntimeError(
            f"TOKEN_EXPIRES_HOURS for {token_type} must be > 0, got {hours}"
        )
    return hours


def _hash_token(cleartext: str) -> str:
    """sha256 hex of the cleartext token. Stable across processes — no salt
    needed because the token itself is high-entropy random and the hash is
    the lookup key (must be deterministic)."""
    return hashlib.sha256(cleartext.encode("utf-8")).hexdigest()


async def _issue_auth_token(
    conn: Any, user_id: uuid.UUID, token_type: str
) -> Optional[Tuple[str, uuid.UUID]]:
    """
    Mint, persist and return (cleartext, token_id).

    Returns None when rate-limited (>= _TOKEN_RATE_LIMIT_PER_HOUR tokens of
    the same type issued in the last hour). Returning None lets the route
    handler stay anti-enumeration-shaped (still 204) while suppressing the
    cleartext from the structured log.

    Always invalidates any prior unused token of the same type for the user —
    keeps the (user_id, token_type) partial-unique index from rejecting the
    new insert. Wrapped in a transaction so a half-insert never leaves the
    table without an active token after we've already invalidated the old.

    `token_id` is the DB row UUID and is used by the route handler to
    enqueue a webhook delivery (auth_token_webhook_deliveries FK).
    """
    recent_count = await conn.fetchval(
        """
        SELECT COUNT(*) FROM auth_tokens
         WHERE user_id    = $1
           AND token_type = $2::auth_token_type
           AND created_at > NOW() - INTERVAL '1 hour'
        """,
        user_id,
        token_type,
    )
    if recent_count is not None and int(recent_count) >= _TOKEN_RATE_LIMIT_PER_HOUR:
        logger.info(
            "identity.auth_token: rate-limited user_id=%s token_type=%s recent=%s",
            user_id,
            token_type,
            recent_count,
        )
        return None

    cleartext = secrets.token_hex(_TOKEN_BYTES)
    token_hash = _hash_token(cleartext)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=_token_expiry_hours(token_type))

    async with conn.transaction():
        # Invalidate every existing unused token of the same type for the
        # user so the partial-unique index lets the new INSERT through.
        # Using NOW() (not `now`) so the marker reflects DB clock — the row
        # is an audit artefact, server clock skew is acceptable here.
        await conn.execute(
            """
            UPDATE auth_tokens
               SET used_at = NOW()
             WHERE user_id    = $1
               AND token_type = $2::auth_token_type
               AND used_at IS NULL
            """,
            user_id,
            token_type,
        )
        token_id = await conn.fetchval(
            """
            INSERT INTO auth_tokens (user_id, token_hash, token_type, expires_at, created_at)
            VALUES ($1, $2, $3::auth_token_type, $4, $5)
            RETURNING id
            """,
            user_id,
            token_hash,
            token_type,
            expires_at,
            now,
        )
    return cleartext, token_id


# Token-type → webhook kind. The route handler uses this to enqueue the
# delivery row with the operator-facing `kind` field (the table CHECK
# enforces these values).
_TOKEN_TYPE_TO_KIND_RESET: str = "reset"
_TOKEN_TYPE_TO_KIND_RESEND: str = "resend"
_TOKEN_TYPE_TO_KIND_VERIFY: str = "verify"


async def _enqueue_webhook_delivery(
    conn: Any,
    token_id: uuid.UUID,
    app_id: str,
    kind: str,
    cleartext: str,
) -> None:
    """
    INSERT a pending row that the background dispatcher will pick up.

    Caller has already verified that `app_id` is in BRIDGE_AUTH_APP_IDS
    and a webhook config exists for it (the route did this BEFORE the
    DB lookup so the existence-check is anti-enumeration-safe).

    `cleartext` is the user-facing token string. It is required while
    the row is pending so the dispatcher can put it in the webhook
    payload (see migration 021 for the cleartext-lifecycle invariant).
    """
    await conn.execute(
        """
        INSERT INTO auth_token_webhook_deliveries
               (token_id, app_id, kind, status, attempts, token_cleartext)
        VALUES ($1, $2::app_id, $3, 'pending', 0, $4)
        """,
        token_id,
        app_id,
        kind,
        cleartext,
    )


def _require_app_id_header(x_app_id: Optional[str]) -> str:
    """
    Validate the X-App-ID header for token-issuing endpoints.

    Called BEFORE any email/user lookup so a missing/unknown app_id does
    not double as an oracle for user existence.

    Returns the normalised app_id on success. Raises 400 with an explicit
    message for missing / unknown / non-Bridge-Auth app_ids (engelmann is
    on Supabase — see ADR cross-app/0002).
    """
    if not x_app_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Missing X-App-ID header. Token-issuing endpoints require "
                "the calling app to identify itself so the Bridge can route "
                "the resulting webhook to the correct receiver."
            ),
        )
    if x_app_id not in BRIDGE_AUTH_APP_IDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown or unsupported X-App-ID {x_app_id!r}. Bridge-Auth "
                f"apps: {sorted(BRIDGE_AUTH_APP_IDS)}. Engelmann is on "
                f"Supabase and does not issue Bridge tokens."
            ),
        )
    # Probe the webhook config — if it's missing the Bridge should NEVER have
    # accepted startup. Re-raise as 503 to make a config drift visible
    # operationally instead of silently dropping deliveries.
    try:
        get_webhook_config(x_app_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Webhook config for app_id={x_app_id!r} is missing at "
                f"runtime — Bridge startup must have skipped validation. "
                f"Refuse to issue a token that cannot be delivered."
            ),
        ) from exc
    return x_app_id


# ---------------------------------------------------------------------------
# POST /v1/auth/forgot-password
# ---------------------------------------------------------------------------

class ForgotPasswordRequest(BaseModel):
    email: EmailStr


@router.post("/forgot-password", status_code=204, response_class=Response)
async def forgot_password(
    body: ForgotPasswordRequest,
    x_app_id: Optional[str] = Header(default=None, alias="X-App-ID"),
) -> Response:
    """
    Request a password-reset token. Anti-enumeration: always 204, regardless
    of whether the email exists, the account is anonymized, or the user is
    rate-limited.

    On success, a webhook-delivery row is enqueued for the app identified by
    X-App-ID; the background dispatcher (see src/identity/webhook_dispatcher.py)
    POSTs the cleartext token to the app's receiver. The cleartext is still
    logged at debug level for operator forensics — never at info, to keep
    prod logs from leaking issuance details.

    X-App-ID is validated UPFRONT (before the email lookup) so a missing
    header cannot double as an oracle for user existence.
    """
    app_id = _require_app_id_header(x_app_id)

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, anonymized_at FROM users WHERE email = $1",
            body.email,
        )

        # Anti-enumeration: unknown user / closed account => 204 silent.
        if not row or row["anonymized_at"] is not None:
            logger.info(
                "identity.forgot_password: silent-skip email=%s known=%s anonymized=%s app_id=%s",
                body.email,
                bool(row),
                bool(row and row["anonymized_at"]),
                app_id,
            )
            return Response(status_code=204)

        issued = await _issue_auth_token(conn, row["id"], "password_reset")

        if issued is None:
            # Rate-limited — _issue_auth_token already logged it.
            return Response(status_code=204)

        cleartext, token_id = issued

        # Enqueue the webhook in the SAME connection / transaction context as
        # the token itself: if the INSERT into auth_token_webhook_deliveries
        # fails, the row should be rolled back together with the token issue,
        # leaving the system consistent (no orphan token without a delivery
        # attempt). asyncpg auto-commits per `execute()` outside an explicit
        # transaction; wrap to be safe.
        async with conn.transaction():
            await _enqueue_webhook_delivery(
                conn, token_id, app_id, _TOKEN_TYPE_TO_KIND_RESET, cleartext,
            )

    logger.debug(
        "identity.forgot_password: token issued email=%s user_id=%s app_id=%s token=%s",
        body.email,
        row["id"],
        app_id,
        cleartext,
    )
    logger.info(
        "identity.forgot_password: token enqueued email=%s user_id=%s app_id=%s token_id=%s",
        body.email,
        row["id"],
        app_id,
        token_id,
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /v1/auth/reset-password-with-token
# ---------------------------------------------------------------------------

class ResetPasswordWithTokenRequest(BaseModel):
    token: str = Field(min_length=1)
    newPassword: str = Field(min_length=8)


@router.post("/reset-password-with-token", status_code=204, response_class=Response)
async def reset_password_with_token(body: ResetPasswordWithTokenRequest) -> Response:
    """
    Consume a password-reset token and set a new password.

    All token-validation failures collapse to the same 400 message — the
    caller learns "no, try again" without learning *why* (used vs expired
    vs invalid). This is anti-enumeration at the consume-side; legitimate
    users issued the token in the last 24h, they don't need a diagnosis.

    On success: password is rotated AND every existing session for the user
    is revoked. The reset itself is the "I have lost control of this account"
    moment — old sessions should not survive it.
    """
    token_hash = _hash_token(body.token)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            tok_row = await conn.fetchrow(
                """
                SELECT t.id, t.user_id, t.expires_at, t.used_at, u.anonymized_at
                  FROM auth_tokens t
                  JOIN users u ON u.id = t.user_id
                 WHERE t.token_hash = $1
                   AND t.token_type = 'password_reset'::auth_token_type
                """,
                token_hash,
            )

            if (
                tok_row is None
                or tok_row["used_at"] is not None
                or tok_row["anonymized_at"] is not None
                or tok_row["expires_at"].replace(tzinfo=timezone.utc)
                <= datetime.now(timezone.utc)
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid or expired password-reset token",
                )

            user_id = tok_row["user_id"]
            new_hash = hash_password(body.newPassword)

            await conn.execute(
                "UPDATE users SET password_hash = $1, updated_at = NOW() WHERE id = $2",
                new_hash,
                user_id,
            )
            await conn.execute(
                "UPDATE auth_tokens SET used_at = NOW() WHERE id = $1",
                tok_row["id"],
            )
            # Revoke every active session — the Bridge analogue of
            # bumpSessionGeneration (close_account uses the same pattern).
            await conn.execute(
                "DELETE FROM sessions WHERE user_id = $1",
                user_id,
            )

    logger.info(
        "identity.reset_password: password rotated + sessions revoked user_id=%s",
        user_id,
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /v1/auth/resend-verification
# ---------------------------------------------------------------------------

class ResendVerificationRequest(BaseModel):
    email: EmailStr


@router.post("/resend-verification", status_code=204, response_class=Response)
async def resend_verification(
    body: ResendVerificationRequest,
    x_app_id: Optional[str] = Header(default=None, alias="X-App-ID"),
) -> Response:
    """
    Request a new email-verification token. Anti-enumeration: 204 on every
    branch — unknown user, already-verified, rate-limited, closed account,
    success. The structured log distinguishes the cases for operators.

    On success the cleartext is delivered via webhook (see forgot_password
    for the design rationale). X-App-ID is validated before the email
    lookup so a missing header cannot leak existence.
    """
    app_id = _require_app_id_header(x_app_id)

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email_verified, anonymized_at FROM users WHERE email = $1",
            body.email,
        )

        if not row or row["anonymized_at"] is not None or row["email_verified"]:
            logger.info(
                "identity.resend_verification: silent-skip email=%s "
                "known=%s anonymized=%s already_verified=%s app_id=%s",
                body.email,
                bool(row),
                bool(row and row["anonymized_at"]),
                bool(row and row["email_verified"]),
                app_id,
            )
            return Response(status_code=204)

        issued = await _issue_auth_token(conn, row["id"], "email_verification")

        if issued is None:
            return Response(status_code=204)

        cleartext, token_id = issued

        async with conn.transaction():
            await _enqueue_webhook_delivery(
                conn, token_id, app_id, _TOKEN_TYPE_TO_KIND_RESEND, cleartext,
            )

    logger.debug(
        "identity.resend_verification: token issued email=%s user_id=%s app_id=%s token=%s",
        body.email,
        row["id"],
        app_id,
        cleartext,
    )
    logger.info(
        "identity.resend_verification: token enqueued email=%s user_id=%s app_id=%s token_id=%s",
        body.email,
        row["id"],
        app_id,
        token_id,
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /v1/auth/verify-email
# ---------------------------------------------------------------------------

class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=1)


@router.post("/verify-email", status_code=204, response_class=Response)
async def verify_email(body: VerifyEmailRequest) -> Response:
    """
    Consume an email-verification token and flip users.email_verified=true.

    Same 400-on-any-failure shape as reset-password-with-token: caller learns
    invalid/expired/used as one outcome. Idempotent at the user-row level —
    a second call with a (now-used) token returns 400, but the user_row is
    already verified, so the system state is consistent either way.
    """
    token_hash = _hash_token(body.token)
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            tok_row = await conn.fetchrow(
                """
                SELECT t.id, t.user_id, t.expires_at, t.used_at, u.anonymized_at
                  FROM auth_tokens t
                  JOIN users u ON u.id = t.user_id
                 WHERE t.token_hash = $1
                   AND t.token_type = 'email_verification'::auth_token_type
                """,
                token_hash,
            )

            if (
                tok_row is None
                or tok_row["used_at"] is not None
                or tok_row["anonymized_at"] is not None
                or tok_row["expires_at"].replace(tzinfo=timezone.utc)
                <= datetime.now(timezone.utc)
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Invalid or expired email-verification token",
                )

            user_id = tok_row["user_id"]

            await conn.execute(
                "UPDATE users SET email_verified = TRUE, updated_at = NOW() WHERE id = $1",
                user_id,
            )
            await conn.execute(
                "UPDATE auth_tokens SET used_at = NOW() WHERE id = $1",
                tok_row["id"],
            )

    logger.info("identity.verify_email: email marked verified user_id=%s", user_id)
    return Response(status_code=204)
