"""
Admin CRUD routes — Identity + DB-health.
Only mounted when BRIDGE_DB_URL is set.

Auth model:
  GET    /v1/db/health                           — public liveness probe (no PII)
  GET    /v1/users                               — admin only
  POST   /v1/users                               — admin only (creates accounts)
  GET    /v1/users/{user_id}                     — require_self_or_admin
  PATCH  /v1/users/{user_id}                     — require_self_or_admin (name; role/password operator-only)
  DELETE /v1/users/{user_id}                     — admin only (hard delete, refuses on billing-record FK)
  GET    /v1/users/{user_id}/stammdaten          — require_self_or_admin
  PATCH  /v1/users/{user_id}/stammdaten          — require_self_or_admin
  GET    /v1/tenants                             — admin only
  POST   /v1/tenants                             — admin only
  PATCH  /v1/tenants/{tenant_id}                 — admin only
  GET    /v1/tenants/{tenant_id}/billing-address — self (own tenant) or admin
  PATCH  /v1/tenants/{tenant_id}/billing-address — self (own tenant) or admin
  GET    /v1/tenants/{tenant_id}/stammdaten      — own-tenant member or admin
  PATCH  /v1/tenants/{tenant_id}/stammdaten      — own-tenant tenant_admin role or operator
  GET    /v1/app-licenses?userId=                — require_self_or_admin (if userId given) else admin
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, List, Any, Dict

import asyncpg
from src.identity.password import hash_password
from src.identity.jwt_utils import VALID_ROLES

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from src.api_auth import require_admin, require_jwt_or_service, require_self_or_admin, AuthClaims, get_tenant_of_user
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
        SELECT u.id, u.email, u.name, u.tenant_id, u.role, u.created_at, u.updated_at,
               t.account_type::text AS tenant_account_type
        FROM users u
        LEFT JOIN tenants t ON t.id = u.tenant_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY u.created_at DESC LIMIT ${len(args) + 1}"
    args.append(limit)

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [
        {**_user_row_to_dict(r), "tenant_account_type": r["tenant_account_type"]}
        for r in rows
    ]


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
                RETURNING id, email, name, tenant_id, role, created_at, updated_at
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
            "SELECT id, email, name, tenant_id, role, created_at, updated_at FROM users WHERE id = $1",
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
    # verification flow. `role`, `password`, and `tenant_id` are operator-only
    # — applied only when the caller is an admin / service token.
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

    if body.role is not None and body.role not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role '{body.role}'. Must be one of: {_VALID_ROLES_LIST}",
        )

    if body.name is None and body.role is None and body.password is None and body.tenant_id is None:
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

    args.append(uuid.UUID(user_id))
    where_pos = len(args)

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            UPDATE users
               SET {", ".join(set_clauses)}
             WHERE id = ${where_pos}
         RETURNING id, email, name, tenant_id, role, created_at, updated_at
            """,
            *args,
        )
    if not row:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
    return _user_row_to_dict(row)


# ---------------------------------------------------------------------------
# Admin: hard-delete a user
# ---------------------------------------------------------------------------

@router.delete("/v1/users/{user_id}", status_code=204, response_class=Response)
async def delete_user(
    user_id: str,
    _claims: AuthClaims = Depends(require_admin),
) -> Response:
    """
    Hard-delete a user. Admin only (admin JWT or service token without X-User-ID).

    Schema cascades take care of dependent data:
      app_licenses, sessions, mollie_customers, pending_payments, user_budgets,
      user_topup_balances → ON DELETE CASCADE.
      tenants.owner_user_id → ON DELETE SET NULL.

    Refuses with 409 when the user still owns billing records that must survive
    for audit reasons:
      subscriptions      → ON DELETE RESTRICT
      credit_purchases   → ON DELETE RESTRICT
    Cancel / refund those first, then retry.

    Returns 204 on success, 404 if the user does not exist.
    """
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
                f"credit_purchases) that block deletion. Cancel/refund first."
            ),
        )

    # asyncpg returns 'DELETE N' — N=0 means the row was not there.
    if not result.endswith(" 1"):
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------

class TenantCreateRequest(BaseModel):
    id: Optional[str] = None
    name: str
    owner_user_id: Optional[str] = None
    # customer|test|internal — defaults to 'customer' if omitted.
    account_type: Optional[str] = None


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
                SELECT id, name, owner_user_id, created_at, account_type::text AS account_type
                FROM tenants WHERE account_type = $1::account_type
                ORDER BY created_at DESC LIMIT $2
                """,
                account_type, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, name, owner_user_id, created_at, account_type::text AS account_type
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
            "created_at": r["created_at"].isoformat(),
        }
        for r in rows
    ]


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
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO tenants (id, name, owner_user_id, account_type, created_at)
                VALUES ($1, $2, $3, $4::account_type, $5)
                RETURNING id, name, owner_user_id, account_type::text AS account_type, created_at
                """,
                tenant_id,
                body.name,
                owner_uid,
                account_type,
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
        "created_at": row["created_at"].isoformat(),
    }


# ---------------------------------------------------------------------------
# Tenant updates — account_type is the main mutable field for now.
# ---------------------------------------------------------------------------

class TenantUpdateRequest(BaseModel):
    name: Optional[str] = None
    account_type: Optional[str] = None
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

    sets: List[str] = []
    args: List[Any] = []

    def _add(col: str, val: Any, cast: str = "") -> None:
        args.append(val)
        sets.append(f"{col} = ${len(args)}{cast}")

    if body.name is not None:
        _add("name", body.name)
    if body.account_type is not None:
        _add("account_type", body.account_type, "::account_type")
    if body.owner_user_id is not None:
        _add("owner_user_id", uuid.UUID(body.owner_user_id) if body.owner_user_id else None)

    if not sets:
        raise HTTPException(status_code=400, detail="No fields to update")

    args.append(tenant_id)
    sql = (
        "UPDATE tenants SET " + ", ".join(sets) +
        f" WHERE id = ${len(args)} "
        "RETURNING id, name, owner_user_id, account_type::text AS account_type, created_at"
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
        "created_at": row["created_at"].isoformat(),
    }


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
    return _tenant_billing_address_row(row)


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
# PATCH: tenant_admin role (or operator) — verhindert dass normaler Mitarbeiter
#        Firmenadresse/Logo für alle ändert.
# ---------------------------------------------------------------------------

_TENANT_ADMIN_ROLES = frozenset({"tenant_admin", "admin", "super_admin"})


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
            sets.append(f"firma = COALESCE(firma, '{{}}'::jsonb) || ${len(args)}::jsonb")
        if body.logo is not None:
            args.append(body.logo)
            sets.append(f"logo = ${len(args)}")
        if style_json is not None:
            args.append(style_json)
            sets.append(f"style_settings = COALESCE(style_settings, '{{}}'::jsonb) || ${len(args)}::jsonb")

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
