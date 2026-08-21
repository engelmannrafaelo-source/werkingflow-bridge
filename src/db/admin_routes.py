"""
Admin CRUD routes — Identity + DB-health.
Only mounted when BRIDGE_DB_URL is set.

Auth model:
  GET    /v1/db/health                                       — public liveness probe (no PII)
  GET    /v1/users                                           — admin only
  POST   /v1/users                                           — admin only (creates accounts)
  GET    /v1/users/{user_id}                                 — require_self_or_admin
  PATCH  /v1/users/{user_id}                                 — require_self_or_admin (name; role/password operator-only)
  DELETE /v1/users/{user_id}                                 — admin only (hard delete, refuses on billing-record FK)
  POST   /v1/users/{user_id}/anonymize                       — admin only (GDPR Art. 17 anonymize-with-retention; the
                                                                 explicit escape valve when hard-delete 409s on retained
                                                                 billing rows — never triggered implicitly by DELETE)
  GET    /v1/users/{user_id}/stammdaten                      — require_self_or_admin
  PATCH  /v1/users/{user_id}/stammdaten                      — require_self_or_admin
  POST   /v1/users/{user_id}/app-licenses                    — admin only (grant/update license with explicit dates)
  DELETE /v1/users/{user_id}/app-licenses/{app_id}           — admin only (revoke license)
  GET    /v1/tenants                                         — admin only
  POST   /v1/tenants                                         — admin only
  PATCH  /v1/tenants/{tenant_id}                             — admin only
  GET    /v1/tenants/{tenant_id}/billing-address             — self (own tenant) or admin
  PATCH  /v1/tenants/{tenant_id}/billing-address             — self (own tenant) or admin
  GET    /v1/tenants/{tenant_id}/stammdaten                  — own-tenant member or admin
  PATCH  /v1/tenants/{tenant_id}/stammdaten                  — own-tenant tenant_admin role or operator
  GET    /v1/app-licenses?userId=                            — require_self_or_admin (if userId given) else admin
  GET    /v1/admin/users/lookup?email=                       — admin only
  POST   /v1/admin/users/{user_id}/app-licenses              — admin only (legacy drift-correction path, no date control)
  POST   /v1/admin/users/{user_id}/subscriptions             — admin only (GRANT ACCESS: subscription = entitlement-SSoT + license in lockstep)
"""
import json
import logging
import uuid
from datetime import date, datetime, timezone
from typing import Optional, List, Any, Dict

import asyncpg
from src.identity.password import hash_password
from src.identity.jwt_utils import VALID_ROLES

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from src.api_auth import require_admin, require_jwt_or_service, require_self_or_admin, AuthClaims, get_tenant_of_user
from src.billing import billing_service
from src.billing import vat_id as vat_id_module
from src.db.client import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-db"])


# ---------------------------------------------------------------------------
# /v1/db/health  — public liveness probe. Does not leak schema details.
# ---------------------------------------------------------------------------

@router.get("/v1/db/health")
async def db_health() -> Dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT version() AS pg_version, NOW() AS server_time")
    return {
        "status": "healthy",
        "pg_version": row["pg_version"],
        "server_time": row["server_time"].isoformat(),
    }


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

_VALID_ROLES_LIST = sorted(VALID_ROLES)


class UserCreateRequest(BaseModel):
    # EmailStr enforces RFC-5322-ish format at the API boundary so we never
    # persist garbage like "" or "not-an-email" into users.email (which is
    # UNIQUE — a bad value blocks the slot forever).
    email: EmailStr
    name: str
    tenant_id: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = Field(default=None, description=f"Platform role. One of: {sorted(VALID_ROLES)}. Defaults to 'user'.")


class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    tenant_id: str
    role: str
    app_licenses: List[Dict[str, Any]]
    created_at: str
    updated_at: str


def _user_row_to_dict(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "name": row["name"],
        "tenant_id": row["tenant_id"],
        "role": row["role"],
        "provider_config": row["provider_config"],  # JSONB or None; None = inherit tenant default
        "app_licenses": [],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


_ALLOWED_APP_IDS = {
    "werking-report", "werking-energy", "werking-safety",
    "werking-noise", "engelmann",
}


@router.get("/v1/users")
async def list_users(
    limit: int = Query(default=100, le=1000),
    offset: int = Query(default=0, ge=0),
    account_type: Optional[str] = Query(
        default=None,
        description="Filter by tenant.account_type: customer|test|internal",
    ),
    app: Optional[str] = Query(
        default=None,
        description="Filter by app_licenses.app_id — only users with a license for this app",
    ),
    _claims: AuthClaims = Depends(require_admin),
) -> List[Dict[str, Any]]:
    """
    List users. Optional `account_type` filters by the user's tenant.
    Optional `app` filters to users holding an app_license for that app
    (any license row, active or expired — app association is the signal).
    """
    if account_type and account_type not in ("customer", "test", "internal"):
        raise HTTPException(status_code=400, detail=f"Invalid account_type: {account_type}")
    if app and app not in _ALLOWED_APP_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown app: {app}")

    where: List[str] = []
    args: List[Any] = []

    def _add(cond: str, val: Any, cast: str = "") -> None:
        args.append(val)
        where.append(cond.replace("$$", f"${len(args)}{cast}"))

    if account_type:
        _add("t.account_type = $$", account_type, "::account_type")
    if app:
        # EXISTS subquery is cheaper than DISTINCT + JOIN when a user has many
        # license rows. We want "is this user licensed for app X" — a single
        # row hit suffices.
        _add(
            "EXISTS (SELECT 1 FROM app_licenses al WHERE al.user_id = u.id AND al.app_id = $$::app_id)",
            app,
        )

    sql = """
        SELECT u.id, u.email, u.name, u.tenant_id, u.role,
               u.provider_config, u.created_at, u.updated_at,
               t.account_type::text AS tenant_account_type
        FROM users u
        LEFT JOIN tenants t ON t.id = u.tenant_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY u.created_at DESC LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}"
    args.append(limit)
    args.append(offset)

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
        # Batch-fetch licenses for all returned users in one query.
        # The list endpoint must not return app_licenses=[] for users that have
        # licenses — the UI uses this to display the license count column.
        user_ids = [r["id"] for r in rows]
        if user_ids:
            lic_rows = await conn.fetch(
                "SELECT user_id, app_id, plan_id, start_date, end_date, seats "
                "FROM app_licenses WHERE user_id = ANY($1::uuid[])",
                user_ids,
            )
        else:
            lic_rows = []

    licenses_by_user: Dict[Any, List[Dict[str, Any]]] = {}
    for lr in lic_rows:
        uid = lr["user_id"]
        licenses_by_user.setdefault(uid, []).append({
            "app_id": lr["app_id"],
            "plan_id": lr["plan_id"],
            "start_date": lr["start_date"].isoformat(),
            "end_date": lr["end_date"].isoformat() if lr["end_date"] else None,
            "seats": lr["seats"],
        })

    result = []
    for r in rows:
        u = {**_user_row_to_dict(r), "tenant_account_type": r["tenant_account_type"]}
        u["app_licenses"] = licenses_by_user.get(r["id"], [])
        result.append(u)
    return result


@router.post("/v1/users", status_code=201)
async def create_user(
    body: UserCreateRequest,
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    role = body.role or "user"
    if role not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role '{role}'. Must be one of: {_VALID_ROLES_LIST}",
        )

    pool = get_pool()
    now = datetime.now(timezone.utc)

    # Auto-create tenant if not supplied
    tenant_id = body.tenant_id or str(uuid.uuid4())

    async with pool.acquire() as conn:
        # Ensure tenant exists
        existing_tenant = await conn.fetchrow("SELECT id FROM tenants WHERE id = $1", tenant_id)
        if not existing_tenant:
            await conn.execute(
                "INSERT INTO tenants (id, name, created_at) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                tenant_id,
                f"Auto-tenant for {body.email}",
                now,
            )

        pw_hash = hash_password(body.password) if body.password else None

        # We catch UniqueViolationError explicitly and translate to 409. Any
        # other DB error is a server bug — log it server-side with full detail,
        # return a generic 500 to the caller to avoid leaking schema details
        # (table/constraint names, PG error codes) to internet-facing clients.
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO users (email, name, tenant_id, role, password_hash, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id, email, name, tenant_id, role, provider_config, created_at, updated_at
                """,
                body.email,
                body.name,
                tenant_id,
                role,
                pw_hash,
                now,
                now,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail=f"User with email '{body.email}' already exists",
            )
        except asyncpg.PostgresError as exc:
            logger.exception("create_user failed: %s", exc)
            raise HTTPException(status_code=500, detail="Database error")

    return _user_row_to_dict(row)


@router.get("/v1/users/{user_id}")
async def get_user(
    user_id: str,
    _claims: AuthClaims = Depends(require_self_or_admin),
) -> Dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, email, name, tenant_id, role, provider_config, created_at, updated_at FROM users WHERE id = $1",
            uuid.UUID(user_id),
        )
    if not row:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")

    # Fetch app_licenses
    async with pool.acquire() as conn:
        license_rows = await conn.fetch(
            "SELECT app_id, plan_id, start_date, end_date, seats FROM app_licenses WHERE user_id = $1",
            uuid.UUID(user_id),
        )

    result = _user_row_to_dict(row)
    result["app_licenses"] = [
        {
            "app_id": lr["app_id"],
            "plan_id": lr["plan_id"],
            "start_date": lr["start_date"].isoformat(),
            "end_date": lr["end_date"].isoformat() if lr["end_date"] else None,
            "seats": lr["seats"],
        }
        for lr in license_rows
    ]
    return result


# ---------------------------------------------------------------------------
# User self-update
# ---------------------------------------------------------------------------

class UserUpdateRequest(BaseModel):
    # Only `name` is exposed for self-edit. Email requires a separate
    # verification flow. `role`, `password`, `tenant_id`, and `provider_config`
    # are operator-only — applied only when the caller is an admin / service token.
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    role: Optional[str] = Field(default=None, description=f"Admin-only. One of: {sorted(VALID_ROLES)}.")
    password: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Admin-only. Replaces the user's password (stored bcrypt-hashed).",
    )
    tenant_id: Optional[str] = Field(
        default=None,
        description=(
            "Admin-only. Moves the user to a different tenant. Used by the test-user "
            "seeder to reconcile stale tenants from the pre-ADR-0006 era where the "
            "seeder only patched password+role on 409, leaving tenant frozen at "
            "first-seeded value. One-time correction per ADR-0006 Phase 1."
        ),
    )
    provider_config: Optional[Any] = Field(
        default=None,
        description=(
            "Admin-only. Per-user provider pin, ENFORCED by "
            "src/routing/user_provider_override.py on chat completions and "
            "research. NULL = inherit default (anthropic). Shape: "
            '{"provider": "bedrock"|"anthropic", "region": "eu-central-1"}. '
            "Send {} to clear an existing pin. A bedrock pin with missing AWS "
            "credentials yields 503 on every call — no silent fallback. "
            "Takes effect within the 60s routing cache TTL on all workers."
        ),
    )
    email_verified: Optional[bool] = Field(
        default=None,
        description=(
            "Admin-only. Flips users.email_verified without a verification "
            "token. Exists for the test-user seeder: login hard-blocks "
            "unverified users (identity/routes.py), and seeded test users "
            "have no reachable inbox for the verification link — without this "
            "every fresh Bridge re-seed produces login-blocked test users "
            "(same durability class as the app-license grants). Real customer "
            "verification MUST keep the token flow; self-callers get 403."
        ),
    )


# Keep the old name as an alias so existing code referencing UserSelfUpdateRequest still works.
UserSelfUpdateRequest = UserUpdateRequest


@router.patch("/v1/users/{user_id}")
async def update_user(
    user_id: str,
    body: UserUpdateRequest,
    claims: AuthClaims = Depends(require_self_or_admin),
) -> Dict[str, Any]:
    """
    Update a user profile. Self-scoped: a user may only patch their own record.
    Admins may patch any user.

    Editable fields:
      - name: all callers
      - role: operator (admin service token or admin JWT) only; 403 for self-callers
      - password: operator only; 403 for self-callers. Used by the test-user
        seeder to reconcile a pre-existing account whose password has drifted
        from test-credentials.json. Self-service password changes go through a
        dedicated flow with old-password verification — not this endpoint.
      - tenant_id: operator only. Moves the user to a different tenant. Used by
        the test-user seeder (ADR-0006 Phase 1) to correct stale tenants that
        were frozen at first-seeded value before tenant reconciliation was added.
    """
    if body.role is not None and not claims.is_operator:
        raise HTTPException(status_code=403, detail="Only admins may change role")

    if body.password is not None and not claims.is_operator:
        raise HTTPException(status_code=403, detail="Only admins may change a password")

    if body.tenant_id is not None and not claims.is_operator:
        raise HTTPException(status_code=403, detail="Only admins may change tenant")

    if body.provider_config is not None and not claims.is_operator:
        raise HTTPException(status_code=403, detail="Only admins may change provider_config")

    if body.email_verified is not None and not claims.is_operator:
        raise HTTPException(status_code=403, detail="Only admins may change email_verified")

    if body.role is not None and body.role not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role '{body.role}'. Must be one of: {_VALID_ROLES_LIST}",
        )

    # Validate provider_config at WRITE time — a typo must fail here, not as a
    # 503 on the pinned user's next AI call. {} clears the pin.
    if body.provider_config is not None:
        from src.routing.user_provider_override import SUPPORTED_PROVIDERS
        if not isinstance(body.provider_config, dict):
            raise HTTPException(
                status_code=400,
                detail="provider_config must be a JSON object (use {} to clear the pin)",
            )
        _pc_provider = body.provider_config.get("provider")
        if _pc_provider is not None and _pc_provider not in SUPPORTED_PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"provider_config.provider '{_pc_provider}' not supported. "
                    f"Must be one of: {sorted(SUPPORTED_PROVIDERS)}"
                ),
            )

    if (body.name is None and body.role is None and body.password is None
            and body.tenant_id is None and body.provider_config is None
            and body.email_verified is None):
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clauses: List[str] = ["updated_at = NOW()"]
    args: List[Any] = []

    if body.name is not None:
        args.append(body.name)
        set_clauses.append(f"name = ${len(args)}")

    if body.role is not None:
        args.append(body.role)
        set_clauses.append(f"role = ${len(args)}")

    if body.password is not None:
        args.append(hash_password(body.password))
        set_clauses.append(f"password_hash = ${len(args)}")

    if body.tenant_id is not None:
        args.append(body.tenant_id)
        set_clauses.append(f"tenant_id = ${len(args)}")

    if body.provider_config is not None:
        args.append(json.dumps(body.provider_config))
        set_clauses.append(f"provider_config = ${len(args)}::jsonb")

    if body.email_verified is not None:
        args.append(body.email_verified)
        set_clauses.append(f"email_verified = ${len(args)}")

    args.append(uuid.UUID(user_id))
    where_pos = len(args)

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE users
               SET {", ".join(set_clauses)}
             WHERE id = ${where_pos}
         RETURNING id, email, name, tenant_id, role, provider_config, created_at, updated_at
            """,
            *args,
        )
    if not row:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")

    # Drop this process's routing cache for the user; other workers converge
    # via the 60s TTL (each worker holds its own in-memory cache).
    if body.provider_config is not None:
        from src.routing.user_provider_override import invalidate_cache
        invalidate_cache(str(row["id"]))
        invalidate_cache(row["email"])

    # Same reasoning for the tenant cache (ADR-0009 Schritt 2): moving a user
    # between tenants must not keep writing to the old one for a whole TTL.
    # This clears platform-api's own cache, which is where tenant resolution
    # actually happens today; anything else converges via the TTL.
    if body.tenant_id is not None:
        from src.api_auth.tenant_resolver import invalidate_tenant_cache
        invalidate_tenant_cache(str(row["id"]))

    return _user_row_to_dict(row)


# ---------------------------------------------------------------------------
# Admin: hard-delete a user
# ---------------------------------------------------------------------------

@router.delete("/v1/users/{user_id}", status_code=204, response_class=Response)
async def delete_user(
    user_id: str,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Response:
    """
    Operator: hard-delete. Customer self-service: GDPR anonymize (delegated).

    ROUTING SHADOW — this route is registered BEFORE self_service.close_account
    on the identical path (admin_db_router precedes self_service_router in
    platform_main.py), so close_account is unreachable over HTTP. Until
    2026-07-03 this handler was require_admin-only, which meant every customer
    portal "Konto löschen" (service token WITH X-User-ID → not an operator)
    died here with 403 and the GDPR anonymize path never ran. Non-operator
    callers are therefore explicitly delegated to close_account below —
    operator behaviour (hard-delete, 204/404/409) is unchanged.

    Hard-delete semantics (operator: admin JWT or service token without X-User-ID):

    Schema cascades take care of dependent data:
      app_licenses, sessions, mollie_customers, pending_payments, user_budgets,
      user_topup_balances → ON DELETE CASCADE.
      tenants.owner_user_id → ON DELETE SET NULL.

    Refuses with 409 when the user still owns billing records that must survive
    for audit reasons:
      subscriptions      → ON DELETE RESTRICT
      credit_purchases   → ON DELETE RESTRICT
    This is BY DESIGN and permanent — cancelling a subscription changes its
    status but not its existence, so the row (and the RESTRICT) survives even
    after every subscription is cancelled/refunded. There is no hard-delete
    path around it. An operator who needs to close such an account (disposable
    test account, or a real customer's Art. 17 request filed via support) must
    use POST /v1/users/{user_id}/anonymize instead — a distinct, explicitly
    called endpoint. DELETE never falls back to it on its own.

    Returns 204 on success, 404 if the user does not exist.
    """
    if not claims.is_operator:
        # Customer self-service (portal proxy / user JWT): GDPR Art. 17
        # anonymize-with-retention, NOT hard-delete. Enforce the same authz the
        # shadowed route would have applied (path user == acting user), then
        # delegate. Deferred import: keeps module-load order independent.
        require_self_or_admin(user_id, claims)
        from src.identity.self_service import close_account
        return JSONResponse(content=await close_account(user_id, claims))

    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid userId (must be UUID)")

    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            result = await conn.execute("DELETE FROM users WHERE id = $1", uid)
    except asyncpg.ForeignKeyViolationError:
        raise HTTPException(
            status_code=409,
            detail=(
                f"User '{user_id}' has billing records (subscriptions or "
                f"credit_purchases) that block deletion — retained permanently "
                f"for audit/tax reasons, cancelling/refunding them does not "
                f"remove the row. Hard-delete is refused by design; use "
                f"POST /v1/users/{user_id}/anonymize (operator GDPR Art. 17 "
                f"anonymize-with-retention) instead."
            ),
        )

    # asyncpg returns 'DELETE N' — N=0 means the row was not there.
    if not result.endswith(" 1"):
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /v1/users/{user_id}/anonymize — operator-only GDPR anonymize-with-retention
#
# The escape valve delete_user's 409 above points to. Hard-delete stays a dead
# end (by design) for any user with billing history; before this route existed
# an operator had no way at all to close such an account — not a disposable
# e2e test account, not a real customer's Art. 17 erasure request filed via
# support instead of the self-service portal. This is a SEPARATE, explicitly
# called endpoint on purpose: DELETE /v1/users/{user_id} never falls back to
# it, and calling it requires a deliberate second API call — no silent
# downgrade from "delete" to "anonymize" ever happens.
#
# Reuses identity.self_service.close_account verbatim rather than
# reimplementing anonymization — same PII-clearing, session-revocation,
# and billing-retention semantics as the customer DSGVO path. close_account
# carries no self-vs-operator assumption: it only ever reads `user_id`, never
# `claims` (the customer-scoping in delete_user's delegation happens one layer
# up, in the require_self_or_admin() call made before delegating). Swapping
# that outer gate for require_admin is therefore sufficient to generalize it
# to the operator case — no change to close_account itself was needed.
# ---------------------------------------------------------------------------

@router.post("/v1/users/{user_id}/anonymize")
async def anonymize_user(
    user_id: str,
    claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Operator-only: GDPR Art. 17 anonymize-with-retention.

    Identical effect to the customer self-service DELETE /v1/users/{user_id}
    path — see identity.self_service.close_account for exactly what gets
    cleared (PII, sessions, developer tokens, tenant ownership) versus
    retained (invoices, subscriptions, credit_purchases, billing_events).

    Idempotent: calling it again on an already-anonymized user returns the
    existing anonymizedAt with `alreadyAnonymized: true`, no error.

    Status mapping:
      200 → anonymized now, or already anonymized (idempotent)
      403 → caller is not an operator credential
      404 → user_id does not exist
    """
    from src.identity.self_service import close_account
    return await close_account(user_id, claims)


# ---------------------------------------------------------------------------
# Admin: lookup user by email + assign app_license
#
# Drift-correction primitives for the app-side seed-users routes. The flow:
#   1. App posts to /v1/auth/register → 409 (email already taken)
#   2. App calls GET /v1/admin/users/lookup?email=... → 200 with user_id +
#      existing app_licenses, or 404 (race: user deleted between 409 and lookup)
#   3. If the user already holds a license for the caller's app_id: no-op.
#      Otherwise POST /v1/admin/users/{user_id}/app-licenses to assign it.
#
# Both endpoints are operator-only (require_admin → require_jwt_or_service +
# is_operator). Service token without X-User-ID is the intended caller
# (apps holding BRIDGE_SERVICE_TOKEN). Admin JWTs work too — same path used
# by future operator dashboards.
# ---------------------------------------------------------------------------


# plan_id ENUM from 001_initial_schema.sql. Kept here as a Python-side allowlist
# so a typo at the API boundary surfaces as a 400 with a helpful message
# instead of a confusing 500 from Postgres on an unknown enum literal.
_ALLOWED_PLAN_IDS = {
    "trial", "report-standard", "energy-project", "safety-project",
    "noise-tbd", "engelmann-custom",
}


class AppLicensePayload(BaseModel):
    """Single app_license row, shared by the lookup and assign responses."""
    app_id: str
    plan_id: str
    start_date: str
    end_date: Optional[str] = None
    seats: int


class EntitlementVerdict(BaseModel):
    """
    Authorization verdict for one app — the SAME four fields the login
    response's `entitlements` claim carries (identity/routes.py
    _entitlements_for). camelCase on purpose: it mirrors the JWT claim
    shape apps already consume, so an operator comparing lookup output
    against a captured token sees identical keys.
    """
    appId: str
    status: str
    planId: str
    trialEndsAt: Optional[str] = None


class UserLookupResponse(BaseModel):
    """
    Operator-scoped lookup result. Explicit fields — no JsonValueSchema or
    z.unknown wrapper. The drift-correction caller reads `user_id` + walks
    `app_licenses` to decide whether to assign a new license.

    `entitlements` is the field that answers "does this user get in?" —
    app middlewares gate EXCLUSIVELY on these subscription-derived verdicts;
    `app_licenses` is provisioning/portfolio metadata and grants nothing.
    Before 2026-07-06 the lookup showed only licenses, which sent operators
    (and agents) down the wrong diagnosis path when a customer bounced with
    reason=no-license despite a "valid license".

    `anonymized_at` is included so the caller can detect the "row is closed"
    edge case. (In practice anonymization rewrites the email to a placeholder,
    so a real-email lookup will not hit an anonymized row — but exposing the
    field lets the caller be explicit instead of silently assuming.)
    """
    user_id: str
    email: str
    name: str
    tenant_id: str
    role: str
    email_verified: bool
    anonymized_at: Optional[str] = None
    app_licenses: List[AppLicensePayload]
    entitlements: List[EntitlementVerdict]
    created_at: str
    updated_at: str


@router.get("/v1/admin/users/lookup", response_model=UserLookupResponse)
async def lookup_user_by_email(
    email: EmailStr = Query(..., description="Email to look up. EmailStr-validated."),
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Operator-only: look up a user by email.

    Returns the full identity payload (user_id, profile, app_licenses) on hit,
    404 on miss. The 200-vs-404 distinction is NOT a public oracle because
    `require_admin` gates the endpoint; the caller is already authenticated.

    Public-facing anti-enumeration lives in the /v1/auth/forgot-password and
    /v1/auth/resend-verification flows — operators querying user state by
    email is the intended use here.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, name, tenant_id, role, email_verified,
                   anonymized_at, created_at, updated_at
              FROM users
             WHERE email = $1
            """,
            email,
        )
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"User with email '{email}' not found",
            )

        license_rows = await conn.fetch(
            """
            SELECT app_id, plan_id, start_date, end_date, seats
              FROM app_licenses
             WHERE user_id = $1
             ORDER BY start_date DESC
            """,
            row["id"],
        )

    # Subscription-derived verdicts — the field app gates actually check.
    # Goes through billing_service.list_subscriptions (NOT raw SQL) so the
    # lazy-expiry of past-due trials runs and the lookup never shows an
    # entitlement the login would deny.
    subscriptions = await billing_service.list_subscriptions(str(row["id"]))
    entitlements = [
        {
            "appId": s["appId"],
            "status": s["status"],
            "planId": s["planId"],
            "trialEndsAt": s.get("trialEndsAt"),
        }
        for s in subscriptions
    ]

    return {
        "user_id": str(row["id"]),
        "email": row["email"],
        "name": row["name"],
        "tenant_id": row["tenant_id"],
        "role": row["role"],
        "email_verified": bool(row["email_verified"]),
        "anonymized_at": (
            row["anonymized_at"].isoformat() if row["anonymized_at"] else None
        ),
        "app_licenses": [
            {
                "app_id": lr["app_id"],
                "plan_id": lr["plan_id"],
                "start_date": lr["start_date"].isoformat(),
                "end_date": lr["end_date"].isoformat() if lr["end_date"] else None,
                "seats": lr["seats"],
            }
            for lr in license_rows
        ],
        "entitlements": entitlements,
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


async def _upsert_app_license(
    conn: Any, uid: uuid.UUID, app_id: str, plan_id: str, start_date: date, seats: int
) -> Any:
    """UPSERT one app_license row on (user_id, app_id) — the UNIQUE constraint
    from 001_initial_schema.sql. The `xmax = 0` predicate distinguishes the
    insert path (xmax stays at the row's natural value, here 0) from the
    update path (xmax is set to the updating txn id) — the canonical
    PostgreSQL idiom for "tell me if my UPSERT created vs updated"; no extra
    round-trip. Shared by the license-assign and subscription-grant routes."""
    return await conn.fetchrow(
        """
        INSERT INTO app_licenses (user_id, app_id, plan_id, start_date, end_date, seats)
        VALUES ($1, $2::app_id, $3::plan_id, $4, NULL, $5)
        ON CONFLICT (user_id, app_id) DO UPDATE
          SET plan_id    = EXCLUDED.plan_id,
              start_date = EXCLUDED.start_date,
              seats      = EXCLUDED.seats
        RETURNING user_id, app_id, plan_id, start_date, end_date, seats,
                  (xmax = 0) AS created
        """,
        uid,
        app_id,
        plan_id,
        start_date,
        seats,
    )


class AppLicenseAssignRequest(BaseModel):
    app_id: str = Field(description="Target app_id. Must be one of the known app_id enum values.")
    plan_id: str = Field(
        default="trial",
        description="Plan identifier. Defaults to 'trial' to match register-flow.",
    )
    seats: int = Field(default=1, ge=1, description="Seat count. Must be ≥ 1.")


class AppLicenseAssignResponse(AppLicensePayload):
    """Echo the assigned license + a `created` flag distinguishing insert vs update."""
    user_id: str
    created: bool


@router.post(
    "/v1/admin/users/{user_id}/app-licenses",
    response_model=AppLicenseAssignResponse,
)
async def assign_app_license(
    user_id: str,
    body: AppLicenseAssignRequest,
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Operator-only: assign (or refresh) an app_license to a user.

    Idempotent on (user_id, app_id) — second call updates plan_id/seats and
    refreshes start_date, returning `created: false`. Used by the drift-correction
    path of the seed-users routes after a 409 + lookup confirms the user exists
    but holds no license for the caller's app.

    Status mapping:
      200  → success ({created: true} on insert, {created: false} on update)
      400  → invalid user_id (not a UUID), unknown app_id, unknown plan_id
      404  → user_id does not exist
    """
    if body.app_id not in _ALLOWED_APP_IDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown app_id '{body.app_id}'. Must be one of: "
                f"{sorted(_ALLOWED_APP_IDS)}"
            ),
        )
    if body.plan_id not in _ALLOWED_PLAN_IDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown plan_id '{body.plan_id}'. Must be one of: "
                f"{sorted(_ALLOWED_PLAN_IDS)}"
            ),
        )
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid userId (must be UUID)")

    pool = get_pool()
    today = datetime.now(timezone.utc).date()

    async with pool.acquire() as conn:
        # Verify the user exists BEFORE the UPSERT so a missing user 404s
        # instead of failing on the FK with a confusing 5xx.
        user_exists = await conn.fetchval(
            "SELECT 1 FROM users WHERE id = $1", uid
        )
        if not user_exists:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")

        row = await _upsert_app_license(conn, uid, body.app_id, body.plan_id, today, body.seats)

    logger.info(
        "admin.assign_app_license: user_id=%s app_id=%s plan_id=%s seats=%s created=%s",
        row["user_id"],
        row["app_id"],
        row["plan_id"],
        row["seats"],
        row["created"],
    )

    return {
        "user_id": str(row["user_id"]),
        "app_id": row["app_id"],
        "plan_id": row["plan_id"],
        "start_date": row["start_date"].isoformat(),
        "end_date": row["end_date"].isoformat() if row["end_date"] else None,
        "seats": row["seats"],
        "created": bool(row["created"]),
    }


class SubscriptionPayload(BaseModel):
    """Serialized subscription row (billing_service._serialize_subscription).
    camelCase mirrors the billing service's canonical wire shape."""
    id: str
    userId: str
    appId: str
    planId: str
    status: str
    mollieCustomerId: Optional[str] = None
    mollieSubscriptionId: Optional[str] = None
    seats: int
    startedAt: Optional[str] = None
    cancelledAt: Optional[str] = None
    suspendedAt: Optional[str] = None
    expiredAt: Optional[str] = None
    trialEndsAt: Optional[str] = None


class SubscriptionGrantRequest(BaseModel):
    app_id: str = Field(description="Target app_id. Must be one of the known app_id enum values.")
    plan_id: str = Field(
        description=(
            "Plan identifier — EXPLICIT, no default. An operator granting "
            "access must state the plan; 'trial' re-issues the forced-trial "
            "window, everything else grants without expiry."
        ),
    )
    seats: int = Field(default=1, ge=1, description="Seat count. Must be ≥ 1.")


class SubscriptionGrantResponse(BaseModel):
    """The grant result: the subscription (SSoT for entitlements) plus the
    app_license kept in lockstep, each with its created-vs-existing flag."""
    subscription: SubscriptionPayload
    subscription_created: bool
    app_license: AppLicensePayload
    license_created: bool


@router.post(
    "/v1/admin/users/{user_id}/subscriptions",
    response_model=SubscriptionGrantResponse,
)
async def grant_subscription(
    user_id: str,
    body: SubscriptionGrantRequest,
    claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Operator-only: grant a user ACCESS to an app — subscription + license in
    one call.

    This is the endpoint operators actually need: app middlewares gate on the
    subscription-derived `entitlements` claim, so assigning an app_license
    alone (the sibling endpoint) changes NOTHING about access. Before this
    endpoint existed, granting access outside a Mollie checkout meant
    hand-written SQL against the production database (2026-07-06, Kurt
    Engelmann: valid energy license, bounced with reason=no-license).

    Order of operations: license first (portfolio metadata), then the
    subscription (the actual grant). If the subscription step fails the
    caller retries idempotently; the intermediate "license without
    subscription" state is exactly the harmless pre-existing kind.

    Idempotency: an existing ACTIVE subscription for (user, app) is returned
    unchanged (`subscription_created: false`) — billing rows are never
    silently mutated. Changing a live subscription is a deliberate separate
    operation (cancel + re-grant).

    Status mapping:
      200 → success (flags distinguish created vs already-present)
      400 → invalid user_id / unknown app_id / unknown plan_id
      404 → user_id does not exist
    """
    if body.app_id not in _ALLOWED_APP_IDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown app_id '{body.app_id}'. Must be one of: "
                f"{sorted(_ALLOWED_APP_IDS)}"
            ),
        )
    if body.plan_id not in _ALLOWED_PLAN_IDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown plan_id '{body.plan_id}'. Must be one of: "
                f"{sorted(_ALLOWED_PLAN_IDS)}"
            ),
        )
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid userId (must be UUID)")

    pool = get_pool()
    today = datetime.now(timezone.utc).date()

    async with pool.acquire() as conn:
        user_exists = await conn.fetchval("SELECT 1 FROM users WHERE id = $1", uid)
        if not user_exists:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")

        license_row = await _upsert_app_license(
            conn, uid, body.app_id, body.plan_id, today, body.seats
        )

    granted_by = "service-token" if claims.user_id is None else f"user:{claims.user_id}"
    subscription, sub_created = await billing_service.grant_subscription(
        user_id=user_id,
        app_id=body.app_id,
        plan_id=body.plan_id,
        seats=body.seats,
        granted_by=granted_by,
    )

    logger.info(
        "admin.grant_subscription: user_id=%s app_id=%s plan_id=%s seats=%s "
        "subscription_created=%s license_created=%s granted_by=%s",
        user_id,
        body.app_id,
        body.plan_id,
        body.seats,
        sub_created,
        bool(license_row["created"]),
        granted_by,
    )

    return {
        "subscription": subscription,
        "subscription_created": sub_created,
        "app_license": {
            "app_id": license_row["app_id"],
            "plan_id": license_row["plan_id"],
            "start_date": license_row["start_date"].isoformat(),
            "end_date": license_row["end_date"].isoformat() if license_row["end_date"] else None,
            "seats": license_row["seats"],
        },
        "license_created": bool(license_row["created"]),
    }


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------

_ALLOWED_BILLING_TYPES = {"self_service", "managed"}


class TenantCreateRequest(BaseModel):
    id: Optional[str] = None
    name: str
    owner_user_id: Optional[str] = None
    # customer|test|internal — defaults to 'customer' if omitted.
    account_type: Optional[str] = None
    # self_service|managed — defaults to 'self_service' if omitted.
    # managed = Betreiber stellt Rechnung manuell (Sondervereinbarungen).
    billing_type: Optional[str] = None


@router.get("/v1/tenants")
async def list_tenants(
    limit: int = Query(default=100, le=1000),
    account_type: Optional[str] = Query(
        default=None,
        description="Filter by account_type: customer|test|internal",
    ),
    _claims: AuthClaims = Depends(require_admin),
) -> List[Dict[str, Any]]:
    if account_type and account_type not in ("customer", "test", "internal"):
        raise HTTPException(status_code=400, detail=f"Invalid account_type: {account_type}")

    pool = get_pool()
    async with pool.acquire() as conn:
        if account_type:
            rows = await conn.fetch(
                """
                SELECT id, name, owner_user_id, created_at,
                       account_type::text AS account_type, billing_type::text AS billing_type
                FROM tenants WHERE account_type = $1::account_type
                ORDER BY created_at DESC LIMIT $2
                """,
                account_type, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, name, owner_user_id, created_at,
                       account_type::text AS account_type, billing_type::text AS billing_type
                FROM tenants ORDER BY created_at DESC LIMIT $1
                """,
                limit,
            )
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "owner_user_id": str(r["owner_user_id"]) if r["owner_user_id"] else None,
            "account_type": r["account_type"],
            "billing_type": r["billing_type"],
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


# Pflichtfelder per stage. Must stay in lockstep with required-fields.yaml
# (packages/api-validation/required-fields.yaml) and the provision_subscription
# gate in src/billing/billing_service.py (commit 7400895). When the SSoT YAML
# adds/removes a field for upgrade-stage, update this list AND bump the
# Validator in werkingflow-production (tests/unified-tester/validators/fe/
# required_fields_coverage.mjs).
_REQUIRED_FIELDS_BY_STAGE: Dict[str, List[str]] = {
    "upgrade": [
        "billing_name",
        "billing_street",
        "billing_city",
        "billing_postcode",
        "billing_country",
    ],
    "registration": [],  # placeholder: tightened later via YAML sync
}


@router.get("/v1/tenants/incomplete")
async def list_incomplete_tenants(
    account_type: str = Query(
        default="customer",
        description="Filter by account_type: customer|test|internal",
    ),
    stage: str = Query(
        default="upgrade",
        description="Lifecycle stage to evaluate: upgrade|registration|all",
    ),
    limit: int = Query(default=200, le=1000),
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Tenants that fail required-field checks for a given lifecycle stage.

    Counterpart on the live DB to the static YAML/Validator in
    werkingflow-production. A tenant that fails 'upgrade' here would
    hard-fail the provision_subscription gate; surfacing it lets ops
    backfill BEFORE the customer hits the gate at Mollie-checkout.

    stage='all' OR's the required-field sets across stages.
    test/internal account_types bypass the gate at runtime (354b383) but
    are still listable here so seeders can be inspected.
    """
    if account_type not in ("customer", "test", "internal"):
        raise HTTPException(status_code=400, detail=f"Invalid account_type: {account_type}")
    if stage not in ("upgrade", "registration", "all"):
        raise HTTPException(status_code=400, detail=f"Invalid stage: {stage}")

    if stage == "all":
        required = sorted({f for fs in _REQUIRED_FIELDS_BY_STAGE.values() for f in fs})
    else:
        required = _REQUIRED_FIELDS_BY_STAGE.get(stage, [])

    if not required:
        return {
            "account_type": account_type,
            "stage": stage,
            "required_fields": [],
            "tenants": [],
            "note": "no required fields defined for this stage — nothing to check",
        }

    null_clause = " OR ".join(f"{f} IS NULL" for f in required)
    select_cols = ", ".join(required)
    sql = (
        f"SELECT id, name, account_type::text AS account_type, created_at, "
        f"{select_cols} "
        f"FROM tenants "
        f"WHERE account_type = $1::account_type AND ({null_clause}) "
        f"ORDER BY created_at DESC LIMIT $2"
    )

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, account_type, limit)

    out = []
    for r in rows:
        missing = [f for f in required if r[f] is None]
        out.append({
            "id": r["id"],
            "name": r["name"],
            "account_type": r["account_type"],
            "missing_fields": missing,
            "created_at": r["created_at"].isoformat(),
        })

    return {
        "account_type": account_type,
        "stage": stage,
        "required_fields": required,
        "count": len(out),
        "tenants": out,
    }


@router.post("/v1/tenants", status_code=201)
async def create_tenant(
    body: TenantCreateRequest,
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    pool = get_pool()
    tenant_id = body.id or str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    owner_uid = uuid.UUID(body.owner_user_id) if body.owner_user_id else None

    async with pool.acquire() as conn:
        account_type = body.account_type or "customer"
        if account_type not in ("customer", "test", "internal"):
            raise HTTPException(
                status_code=400, detail=f"Invalid account_type: {account_type}",
            )
        billing_type = body.billing_type or "self_service"
        if billing_type not in _ALLOWED_BILLING_TYPES:
            raise HTTPException(
                status_code=400, detail=f"Invalid billing_type: {billing_type}",
            )
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO tenants (id, name, owner_user_id, account_type, billing_type, created_at)
                VALUES ($1, $2, $3, $4::account_type, $5::billing_type, $6)
                RETURNING id, name, owner_user_id, account_type::text AS account_type,
                          billing_type::text AS billing_type, created_at
                """,
                tenant_id,
                body.name,
                owner_uid,
                account_type,
                billing_type,
                now,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail=f"Tenant '{tenant_id}' already exists",
            )
        except asyncpg.PostgresError as exc:
            logger.exception("create_tenant failed: %s", exc)
            raise HTTPException(status_code=500, detail="Database error")

    return {
        "id": row["id"],
        "name": row["name"],
        "owner_user_id": str(row["owner_user_id"]) if row["owner_user_id"] else None,
        "account_type": row["account_type"],
        "billing_type": row["billing_type"],
        "created_at": row["created_at"].isoformat(),
    }


# ---------------------------------------------------------------------------
# Tenant updates — account_type + billing_type are the mutable fields.
# ---------------------------------------------------------------------------

class TenantUpdateRequest(BaseModel):
    name: Optional[str] = None
    account_type: Optional[str] = None
    billing_type: Optional[str] = None
    owner_user_id: Optional[str] = None


@router.patch("/v1/tenants/{tenant_id}")
async def update_tenant(
    tenant_id: str,
    body: TenantUpdateRequest,
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    if body.account_type and body.account_type not in ("customer", "test", "internal"):
        raise HTTPException(
            status_code=400, detail=f"Invalid account_type: {body.account_type}",
        )
    if body.billing_type and body.billing_type not in _ALLOWED_BILLING_TYPES:
        raise HTTPException(
            status_code=400, detail=f"Invalid billing_type: {body.billing_type}",
        )

    sets: List[str] = []
    args: List[Any] = []

    def _add(col: str, val: Any, cast: str = "") -> None:
        args.append(val)
        sets.append(f"{col} = ${len(args)}{cast}")

    if body.name is not None:
        _add("name", body.name)
    if body.account_type is not None:
        _add("account_type", body.account_type, "::account_type")
    if body.billing_type is not None:
        _add("billing_type", body.billing_type, "::billing_type")
    if body.owner_user_id is not None:
        _add("owner_user_id", uuid.UUID(body.owner_user_id) if body.owner_user_id else None)

    if not sets:
        raise HTTPException(status_code=400, detail="No fields to update")

    args.append(tenant_id)
    sql = (
        "UPDATE tenants SET " + ", ".join(sets) +
        f" WHERE id = ${len(args)} "
        "RETURNING id, name, owner_user_id, account_type::text AS account_type, "
        "billing_type::text AS billing_type, created_at"
    )

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
    if not row:
        raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")
    return {
        "id": row["id"],
        "name": row["name"],
        "owner_user_id": str(row["owner_user_id"]) if row["owner_user_id"] else None,
        "account_type": row["account_type"],
        "billing_type": row["billing_type"],
        "created_at": row["created_at"].isoformat(),
    }


# ---------------------------------------------------------------------------
# Platform-Config — globale Schalter (Self-Checkout). Admin only.
# ---------------------------------------------------------------------------

class SelfCheckoutConfig(BaseModel):
    active: bool


@router.get("/v1/platform-config/self-checkout")
async def get_self_checkout_config(
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    from src.platform_config import is_self_checkout_active
    return {"active": await is_self_checkout_active()}


@router.patch("/v1/platform-config/self-checkout")
async def set_self_checkout_config(
    body: SelfCheckoutConfig,
    claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    from src.platform_config import SELF_CHECKOUT_ACTIVE, set_config, is_self_checkout_active
    await set_config(SELF_CHECKOUT_ACTIVE, body.active, updated_by=claims.effective_user_id or "operator")
    return {"active": await is_self_checkout_active()}


# ---------------------------------------------------------------------------
# Einmalumsätze / Verträge — getrennt vom MRR. Pipeline = angebahnt (z.B.
# unterschriebener/anstehender EP-Vertrag), booked = Rechnung gestellt.
# ---------------------------------------------------------------------------

_CONTRACT_STATUS = {"pipeline", "booked"}


class ContractItem(BaseModel):
    id: str
    name: str
    amountEur: float          # netto
    status: str               # 'pipeline' | 'booked'
    note: Optional[str] = None


class ContractsPayload(BaseModel):
    contracts: List[ContractItem]


@router.get("/v1/platform-config/contracts")
async def get_contracts(_claims: AuthClaims = Depends(require_admin)) -> Dict[str, Any]:
    from src.platform_config import get_config
    return {"contracts": await get_config("contracts", [])}


@router.put("/v1/platform-config/contracts")
async def set_contracts(
    body: ContractsPayload,
    claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    from src.platform_config import set_config
    for c in body.contracts:
        if c.status not in _CONTRACT_STATUS:
            raise HTTPException(status_code=400, detail=f"Invalid contract status: {c.status!r} (pipeline|booked)")
        if c.amountEur < 0:
            raise HTTPException(status_code=400, detail="amountEur must be >= 0")
    data = [c.model_dump() for c in body.contracts]
    await set_config("contracts", data, updated_by=claims.effective_user_id or "operator")
    return {"contracts": data}


# ---------------------------------------------------------------------------
# Tenant billing address — self-service (user reads/writes own tenant only)
# ---------------------------------------------------------------------------

async def _check_tenant_access(claims: AuthClaims, tenant_id: str) -> None:
    """
    Raise 403 if the caller may not access this tenant.

    - Operator (service token without X-User-ID, admin JWT): unrestricted.
    - Service proxy (service token + X-User-ID): look up the acting user's
      tenant and require it to equal the path tenant_id.
    - User JWT: use claims.tenant_id directly (already resolved from JWT).
    """
    if claims.is_operator:
        return
    if claims.is_service and claims.acting_user_id is not None:
        acting_tenant = await get_tenant_of_user(claims.acting_user_id)
        if acting_tenant != tenant_id:
            raise HTTPException(
                status_code=403,
                detail="Forbidden: proxy token acting as user from a different tenant",
            )
        return
    # User JWT: tenant_id in JWT must match path
    if claims.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Forbidden: can only access own tenant")

class TenantBillingAddressRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=255)
    street: Optional[str] = Field(default=None, max_length=255)
    city: Optional[str] = Field(default=None, max_length=255)
    postcode: Optional[str] = Field(default=None, max_length=64)
    country: Optional[str] = Field(default=None, min_length=2, max_length=2, description="ISO 3166-1 alpha-2")
    vatId: Optional[str] = Field(default=None, max_length=64)


def _tenant_billing_address_row(row: Any) -> Dict[str, Any]:
    return {
        "name": row["billing_name"],
        "street": row["billing_street"],
        "city": row["billing_city"],
        "postcode": row["billing_postcode"],
        "country": row["billing_country"],
        "vatId": row["billing_vat_id"],
    }


@router.get("/v1/tenants/{tenant_id}/billing-address")
async def get_tenant_billing_address(
    tenant_id: str,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    """
    Read the billing address of a tenant.

    Self-scoped: a user JWT sees only their own tenant; a service proxy
    (X-User-ID) is scoped to that user's tenant. Operators may read any.
    """
    await _check_tenant_access(claims, tenant_id)

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT billing_name, billing_street, billing_city,
                   billing_postcode, billing_country, billing_vat_id
            FROM tenants WHERE id = $1
            """,
            tenant_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")
    return _tenant_billing_address_row(row)


@router.patch("/v1/tenants/{tenant_id}/billing-address")
async def update_tenant_billing_address(
    tenant_id: str,
    body: TenantBillingAddressRequest,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    """
    Update the billing address of a tenant (partial update — only provided fields change).

    Self-scoped: a user JWT sees only their own tenant; a service proxy
    (X-User-ID) is scoped to that user's tenant. Operators may update any.

    country must be an ISO 3166-1 alpha-2 code (e.g. "AT", "DE").
    """
    await _check_tenant_access(claims, tenant_id)

    sets: List[str] = []
    args: List[Any] = []

    def _add(col: str, val: Any) -> None:
        args.append(val)
        sets.append(f"{col} = ${len(args)}")

    if body.name is not None:
        _add("billing_name", body.name)
    if body.street is not None:
        _add("billing_street", body.street)
    if body.city is not None:
        _add("billing_city", body.city)
    if body.postcode is not None:
        _add("billing_postcode", body.postcode)
    if body.country is not None:
        _add("billing_country", body.country.upper())
    if body.vatId is not None:
        _add("billing_vat_id", body.vatId)

    if not sets:
        raise HTTPException(status_code=400, detail="No fields to update")

    args.append(tenant_id)
    sql = (
        "UPDATE tenants SET " + ", ".join(sets) +
        f" WHERE id = ${len(args)}"
        " RETURNING billing_name, billing_street, billing_city,"
        "   billing_postcode, billing_country, billing_vat_id"
    )

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
    if not row:
        raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")

    # UID gegen VIES pruefen, sobald eine erfasst ist. Das Ergebnis entscheidet
    # spaeter ueber Reverse Charge (_determine_tax_rate) — ohne bestaetigte
    # Pruefung wird mit 20 % USt fakturiert.
    #
    # Der Fehlschlag ist BEWUSST nicht fatal: die Adresse ist gespeichert, und
    # ein VIES-Ausfall darf den Kunden nicht am Weiterkommen hindern. Er fuehrt
    # lediglich dazu, dass keine Bestaetigung vorliegt — die teure Richtung
    # (Nullsteuer auf eine ungepruefte Nummer) ist damit ausgeschlossen.
    # Das Ergebnis geht an den Aufrufer zurueck, damit die Oberflaeche eine
    # ungueltige Nummer sofort melden kann statt sie stumm zu schlucken.
    result = _tenant_billing_address_row(row)
    raw_vat = (row["billing_vat_id"] or "").strip()
    if raw_vat:
        try:
            check = await vat_id_module.validate_and_store(
                conn, tenant_id=tenant_id, vat_id=raw_vat,
            )
            result["vatIdValid"] = check["isValid"]
            result["vatIdCheckedName"] = check["name"]
        except vat_id_module.VatIdCheckUnavailable as e:
            logger.warning("[billing-address] VIES unavailable for tenant=%s: %s", tenant_id, e)
            result["vatIdValid"] = None  # nicht pruefbar — weder gueltig noch ungueltig
        except ValueError as e:
            # Kein fuehrendes Laenderkuerzel o. ae. — das ist ein Eingabefehler
            # des Kunden und wird als "ungueltig" gemeldet, nicht als Ausfall.
            logger.info("[billing-address] malformed vat id for tenant=%s: %s", tenant_id, e)
            result["vatIdValid"] = False
    return result


# ---------------------------------------------------------------------------
# App Licenses
# ---------------------------------------------------------------------------

@router.get("/v1/app-licenses")
async def list_app_licenses(
    userId: Optional[str] = Query(default=None),
    # Two auth paths converge on this endpoint:
    #   - userId given → owner or admin may read
    #   - userId omitted → admin only (full table dump)
    # We inject require_jwt_or_service and enforce ownership inline because
    # FastAPI Depends does not naturally branch on a query param.
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> List[Dict[str, Any]]:
    if claims.is_operator:
        # Operator: userId=None → full list; userId set → any user's licenses.
        pass
    else:
        # User JWT or service proxy: scope to own licenses.
        if userId is None:
            userId = claims.effective_user_id
        elif userId != claims.effective_user_id:
            raise HTTPException(status_code=403, detail="Forbidden: can only read own app-licenses")

    pool = get_pool()
    async with pool.acquire() as conn:
        if userId:
            rows = await conn.fetch(
                """
                SELECT id, user_id, app_id, plan_id, start_date, end_date, seats
                FROM app_licenses WHERE user_id = $1 ORDER BY start_date DESC
                """,
                uuid.UUID(userId),
            )
        else:
            rows = await conn.fetch(
                "SELECT id, user_id, app_id, plan_id, start_date, end_date, seats FROM app_licenses ORDER BY start_date DESC LIMIT 500"
            )
    return [
        {
            "id": str(r["id"]),
            "user_id": str(r["user_id"]),
            "app_id": r["app_id"],
            "plan_id": r["plan_id"],
            "start_date": r["start_date"].isoformat(),
            "end_date": r["end_date"].isoformat() if r["end_date"] else None,
            "seats": r["seats"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Tenant stammdaten — Firmen-Identität (Bridge validiert Schema)
#
# GET:   any tenant member or operator
# PATCH: tenant-admin-equivalent role (or operator) — verhindert dass normaler
#        Mitarbeiter (role 'member'/'user') Firmenadresse/Logo für alle ändert.
#
# 'owner' MUSS enthalten sein: Self-Service-Registrierung (identity/routes.py)
# vergibt jedem selbst-registrierten Tenant-Chef die Rolle 'owner' — ohne owner
# hier wäre JEDER Selbst-Signup von der Firmen-Stammdaten-Bearbeitung ausgesperrt
# (403), obwohl das FE (isTenantAdmin) ihn als Admin behandelt. Diese Allowlist
# ist die Bridge-Seite des FE↔Bridge-Rollen-Contracts (role_admin_contract).
# ---------------------------------------------------------------------------

_TENANT_ADMIN_ROLES = frozenset({"owner", "tenant_admin", "admin", "super_admin"})


async def _check_tenant_admin_role(claims: AuthClaims, tenant_id: str, conn: Any) -> None:
    """
    Raises 403 if the acting user is not a tenant_admin (or operator).
    Must be called AFTER _check_tenant_access has confirmed tenant membership.
    """
    if claims.is_operator:
        return

    # Determine acting user and their role.
    if claims.is_service and claims.acting_user_id is not None:
        # Service proxy: look up role from DB.
        row = await conn.fetchrow(
            "SELECT role FROM users WHERE id = $1",
            uuid.UUID(claims.acting_user_id),
        )
        if not row:
            raise HTTPException(status_code=403, detail="Forbidden: acting user not found")
        role = row["role"]
    else:
        # User JWT: role is in claims (may be None for legacy JWTs without role claim).
        role = claims.role or "user"

    if role not in _TENANT_ADMIN_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Forbidden: tenant_admin role required to update tenant stammdaten",
        )


class _AdresseField(BaseModel):
    strasse: Optional[str] = Field(default=None, max_length=200)
    hausnummer: Optional[str] = Field(default=None, max_length=20)
    plz: Optional[str] = Field(default=None, max_length=10)
    ort: Optional[str] = Field(default=None, max_length=200)


class _FirmaField(BaseModel):
    name: Optional[str] = Field(default=None, max_length=300)
    rechtsform: Optional[str] = Field(default=None, max_length=100)
    adresse: Optional[_AdresseField] = None


class TenantStammdatenPatch(BaseModel):
    firma: Optional[_FirmaField] = None
    logo: Optional[str] = Field(
        default=None,
        max_length=2_097_152,  # ~1.5 MB base64; switch to a Blob endpoint if this stings
        description="Opaker String — data-URI (Base64) oder URL",
    )
    styleSettings: Optional[Dict[str, Any]] = None


def _jsonb(raw: Any) -> Dict[str, Any]:
    """Normalise an asyncpg JSONB value — may arrive as str or dict."""
    if isinstance(raw, str):
        try:
            return json.loads(raw) or {}
        except Exception:
            return {}
    return raw or {}


def _tenant_stammdaten_row(r: Any) -> Dict[str, Any]:
    return {
        "firma": _jsonb(r["firma"]),
        "logo": r["logo"],
        "styleSettings": _jsonb(r["style_settings"]),
        "updatedAt": r["updated_at"].isoformat() if r["updated_at"] else None,
        "updatedBy": str(r["updated_by"]) if r["updated_by"] else None,
    }


_TENANT_STAMMDATEN_DEFAULT: Dict[str, Any] = {
    "firma": {},
    "logo": None,
    "styleSettings": {},
    "updatedAt": None,
    "updatedBy": None,
}


@router.get("/v1/tenants/{tenant_id}/stammdaten")
async def get_tenant_stammdaten(
    tenant_id: str,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    await _check_tenant_access(claims, tenant_id)

    pool = get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT firma, logo, style_settings, updated_at, updated_by FROM tenant_stammdaten WHERE tenant_id = $1",
            tenant_id,
        )
    if not r:
        return dict(_TENANT_STAMMDATEN_DEFAULT)
    return _tenant_stammdaten_row(r)


@router.patch("/v1/tenants/{tenant_id}/stammdaten")
async def patch_tenant_stammdaten(
    tenant_id: str,
    body: TenantStammdatenPatch,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        await _check_tenant_access(claims, tenant_id)
        await _check_tenant_admin_role(claims, tenant_id, conn)

        # Verify tenant exists — fail loud.
        t = await conn.fetchval("SELECT 1 FROM tenants WHERE id = $1", tenant_id)
        if not t:
            raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")

        actor_id = uuid.UUID(claims.acting_user_id) if claims.acting_user_id else None

        # Build the upsert with merge semantics per field.
        # Only fields present in the request body change; absent fields keep their current value.
        firma_json = (
            json.dumps(body.firma.model_dump(exclude_none=True)) if body.firma is not None else None
        )
        style_json = (
            json.dumps(body.styleSettings) if body.styleSettings is not None else None
        )

        # We need COALESCE(existing, '{}') || patch for merge — done in SQL.
        sets = ["updated_at = NOW()", "updated_by = $1"]
        args: List[Any] = [actor_id]

        if firma_json is not None:
            args.append(firma_json)
            sets.append(f"firma = COALESCE(tenant_stammdaten.firma, '{{}}'::jsonb) || ${len(args)}::jsonb")
        if body.logo is not None:
            args.append(body.logo)
            sets.append(f"logo = ${len(args)}")
        if style_json is not None:
            args.append(style_json)
            sets.append(f"style_settings = COALESCE(tenant_stammdaten.style_settings, '{{}}'::jsonb) || ${len(args)}::jsonb")

        args.append(tenant_id)
        tenant_pos = len(args)

        r = await conn.fetchrow(
            f"""
            INSERT INTO tenant_stammdaten (tenant_id, firma, logo, style_settings, updated_at, updated_by)
            VALUES (${tenant_pos}, '{{}}'::jsonb, NULL, '{{}}'::jsonb, NOW(), $1)
            ON CONFLICT (tenant_id) DO UPDATE SET {", ".join(sets)}
            RETURNING firma, logo, style_settings, updated_at, updated_by
            """,
            *args,
        )
    return _tenant_stammdaten_row(r)


# ---------------------------------------------------------------------------
# User stammdaten — Gutachter-Identität (opaker JSONB-Blob)
#
# Bridge speichert, werking-report validiert das Schema.
# Keine tiefe Feld-Validierung hier — nur "ist ein Objekt, vernünftige Größe".
# ---------------------------------------------------------------------------

_USER_STAMMDATEN_MAX_KEYS = 100  # sanity guard against pathological payloads


class UserStammdatenPatch(BaseModel):
    model_config = ConfigDict(extra="allow")


def _user_stammdaten_row(r: Any) -> Dict[str, Any]:
    data = _jsonb(r["data"])
    result = dict(data)
    result["updatedAt"] = r["updated_at"].isoformat() if r["updated_at"] else None
    return result


@router.get("/v1/users/{user_id}/stammdaten")
async def get_user_stammdaten(
    user_id: str,
    claims: AuthClaims = Depends(require_self_or_admin),
) -> Dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT data, updated_at FROM user_stammdaten WHERE user_id = $1",
            uuid.UUID(user_id),
        )
    if not r:
        return {"updatedAt": None}
    return _user_stammdaten_row(r)


@router.patch("/v1/users/{user_id}/stammdaten")
async def patch_user_stammdaten(
    user_id: str,
    body: UserStammdatenPatch,
    claims: AuthClaims = Depends(require_self_or_admin),
) -> Dict[str, Any]:
    patch_data = body.model_dump()
    if len(patch_data) > _USER_STAMMDATEN_MAX_KEYS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many fields in stammdaten patch (max {_USER_STAMMDATEN_MAX_KEYS})",
        )

    patch_json = json.dumps(patch_data)

    pool = get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            INSERT INTO user_stammdaten (user_id, data, updated_at)
            VALUES ($1, $2::jsonb, NOW())
            ON CONFLICT (user_id) DO UPDATE
              SET data = COALESCE(user_stammdaten.data, '{}'::jsonb) || EXCLUDED.data,
                  updated_at = NOW()
            RETURNING data, updated_at
            """,
            uuid.UUID(user_id),
            patch_json,
        )
    return _user_stammdaten_row(r)


# ---------------------------------------------------------------------------
# App-License Grant + Revoke — admin-only, date-aware
#
# POST /v1/users/{user_id}/app-licenses
#   Grant (or update) a license for a specific app. Idempotent: second call
#   on the same (user_id, app_id) pair updates plan_id / end_date / seats and
#   returns `created: false`. The caller passes explicit start/end dates so
#   werking.tools and other orchestrators can issue paid licenses with known
#   validity windows.
#
# DELETE /v1/users/{user_id}/app-licenses/{app_id}
#   Revoke the license for a specific app. 204 on success, 404 if not found.
#
# Companion to the legacy POST /v1/admin/users/{user_id}/app-licenses which
# auto-sets start_date=today and end_date=NULL (drift-correction path).
# ---------------------------------------------------------------------------


def _parse_date(value: Optional[str], field: str) -> Optional[date]:
    """Parse an ISO date string ('YYYY-MM-DD'). Raises 400 on invalid format."""
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date format for '{field}': {value!r}. Expected YYYY-MM-DD.",
        )


class GrantAppLicenseRequest(BaseModel):
    appId: str = Field(description="Target app_id. Must be one of the known app_id enum values.")
    planId: str = Field(
        default="trial",
        description="Plan identifier. Defaults to 'trial'.",
    )
    startDate: str = Field(description="License start date — ISO format YYYY-MM-DD.")
    endDate: Optional[str] = Field(
        default=None,
        description="License end date — ISO format YYYY-MM-DD, or null for open-ended.",
    )
    seats: int = Field(default=1, ge=1, description="Seat count. Must be ≥ 1.")


class GrantAppLicenseResponse(BaseModel):
    userId: str
    appId: str
    planId: str
    startDate: str
    endDate: Optional[str] = None
    seats: int
    created: bool


@router.post(
    "/v1/users/{user_id}/app-licenses",
    response_model=GrantAppLicenseResponse,
)
async def grant_app_license(
    user_id: str,
    body: GrantAppLicenseRequest,
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Admin-only: grant (or update) an app_license for a user, with explicit dates.

    Idempotent on (user_id, app_id) — a second call updates plan_id, end_date,
    and seats and returns `created: false`.

    Status mapping:
      200  → success ({created: true} on insert, {created: false} on update)
      400  → invalid user_id, unknown app_id, unknown plan_id, bad date format
      404  → user_id does not exist
    """
    if body.appId not in _ALLOWED_APP_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown appId '{body.appId}'. Must be one of: {sorted(_ALLOWED_APP_IDS)}",
        )
    if body.planId not in _ALLOWED_PLAN_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown planId '{body.planId}'. Must be one of: {sorted(_ALLOWED_PLAN_IDS)}",
        )
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid userId (must be UUID)")

    start_date = _parse_date(body.startDate, "startDate")
    end_date = _parse_date(body.endDate, "endDate")

    if end_date is not None and end_date < start_date:
        raise HTTPException(
            status_code=400,
            detail=f"endDate ({body.endDate}) must not be before startDate ({body.startDate})",
        )

    pool = get_pool()
    async with pool.acquire() as conn:
        user_exists = await conn.fetchval("SELECT 1 FROM users WHERE id = $1", uid)
        if not user_exists:
            raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")

        row = await conn.fetchrow(
            """
            INSERT INTO app_licenses (user_id, app_id, plan_id, start_date, end_date, seats)
            VALUES ($1, $2::app_id, $3::plan_id, $4, $5, $6)
            ON CONFLICT (user_id, app_id) DO UPDATE
              SET plan_id    = EXCLUDED.plan_id,
                  start_date = EXCLUDED.start_date,
                  end_date   = EXCLUDED.end_date,
                  seats      = EXCLUDED.seats
            RETURNING user_id, app_id, plan_id, start_date, end_date, seats,
                      (xmax = 0) AS created
            """,
            uid,
            body.appId,
            body.planId,
            start_date,
            end_date,
            body.seats,
        )

    logger.info(
        "admin.grant_app_license: user_id=%s app_id=%s plan_id=%s seats=%s "
        "start_date=%s end_date=%s created=%s",
        row["user_id"],
        row["app_id"],
        row["plan_id"],
        row["seats"],
        row["start_date"],
        row["end_date"],
        row["created"],
    )

    return {
        "userId": str(row["user_id"]),
        "appId": row["app_id"],
        "planId": row["plan_id"],
        "startDate": row["start_date"].isoformat(),
        "endDate": row["end_date"].isoformat() if row["end_date"] else None,
        "seats": row["seats"],
        "created": bool(row["created"]),
    }


@router.delete(
    "/v1/users/{user_id}/app-licenses/{app_id}",
    status_code=204,
    response_class=Response,
)
async def revoke_app_license(
    user_id: str,
    app_id: str,
    _claims: AuthClaims = Depends(require_admin),
) -> Response:
    """
    Admin-only: revoke an app_license for a user.

    Status mapping:
      204 → success
      400 → invalid user_id or unknown app_id
      404 → license not found (user doesn't hold this app license)
    """
    if app_id not in _ALLOWED_APP_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown app_id '{app_id}'. Must be one of: {sorted(_ALLOWED_APP_IDS)}",
        )
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid userId (must be UUID)")

    pool = get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM app_licenses WHERE user_id = $1 AND app_id = $2::app_id",
            uid,
            app_id,
        )

    if not result.endswith(" 1"):
        raise HTTPException(
            status_code=404,
            detail=f"No license found for user '{user_id}' and app '{app_id}'",
        )
    return Response(status_code=204)
