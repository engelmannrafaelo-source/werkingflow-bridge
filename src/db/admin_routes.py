"""
Admin CRUD routes — Identity + DB-health.
Only mounted when BRIDGE_DB_URL is set.

Auth model:
  GET    /v1/db/health                           — public liveness probe (no PII)
  GET    /v1/users                               — admin only
  POST   /v1/users                               — admin only (creates accounts)
  GET    /v1/users/{user_id}                     — require_self_or_admin
  PATCH  /v1/users/{user_id}                     — require_self_or_admin (name only)
  GET    /v1/tenants                             — admin only
  POST   /v1/tenants                             — admin only
  PATCH  /v1/tenants/{tenant_id}                 — admin only
  GET    /v1/tenants/{tenant_id}/billing-address — self (own tenant) or admin
  PATCH  /v1/tenants/{tenant_id}/billing-address — self (own tenant) or admin
  GET    /v1/app-licenses?userId=                — require_self_or_admin (if userId given) else admin
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, List, Any, Dict

import asyncpg
from src.identity.password import hash_password
from src.identity.jwt_utils import VALID_ROLES

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field

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
    # verification flow. tenant_id and password_hash are admin or dedicated-endpoint only.
    # `role` is accepted in the request body but only applied when the caller is an operator.
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    role: Optional[str] = Field(default=None, description=f"Admin-only. One of: {sorted(VALID_ROLES)}.")


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
    """
    if body.role is not None and not claims.is_operator:
        raise HTTPException(status_code=403, detail="Only admins may change role")

    if body.role is not None and body.role not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role '{body.role}'. Must be one of: {_VALID_ROLES_LIST}",
        )

    if body.name is None and body.role is None:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_clauses: List[str] = ["updated_at = NOW()"]
    args: List[Any] = []

    if body.name is not None:
        args.append(body.name)
        set_clauses.append(f"name = ${len(args)}")

    if body.role is not None:
        args.append(body.role)
        set_clauses.append(f"role = ${len(args)}")

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
