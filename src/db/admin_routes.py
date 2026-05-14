"""
Admin CRUD routes — Identity + DB-health.
Only mounted when BRIDGE_DB_URL is set.

Auth model:
  GET  /v1/db/health             — public liveness probe (no PII)
  GET  /v1/users                 — admin only
  POST /v1/users                 — admin only (creates accounts)
  GET  /v1/users/{user_id}       — require_self_or_admin
  GET  /v1/tenants               — admin only
  POST /v1/tenants               — admin only
  GET  /v1/app-licenses?userId=  — require_self_or_admin (if userId given) else admin
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, List, Any, Dict

import asyncpg
from src.identity.password import hash_password

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr

from src.api_auth import require_admin, require_jwt_or_service, require_self_or_admin, AuthClaims
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

class UserCreateRequest(BaseModel):
    # EmailStr enforces RFC-5322-ish format at the API boundary so we never
    # persist garbage like "" or "not-an-email" into users.email (which is
    # UNIQUE — a bad value blocks the slot forever).
    email: EmailStr
    name: str
    tenant_id: Optional[str] = None
    password: Optional[str] = None


class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    tenant_id: str
    app_licenses: List[Dict[str, Any]]
    created_at: str
    updated_at: str


def _user_row_to_dict(row: Any) -> Dict[str, Any]:
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "name": row["name"],
        "tenant_id": row["tenant_id"],
        "app_licenses": [],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


@router.get("/v1/users")
async def list_users(
    limit: int = Query(default=100, le=1000),
    mode: Optional[str] = Query(default=None, description="Filter by tenant category: prod|staging|local"),
    _claims: AuthClaims = Depends(require_admin),
) -> List[Dict[str, Any]]:
    """List users. Optional `mode` filters by tenant.category."""
    if mode and mode not in ("prod", "staging", "local"):
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")

    pool = get_pool()
    async with pool.acquire() as conn:
        if mode:
            rows = await conn.fetch(
                """
                SELECT u.id, u.email, u.name, u.tenant_id, u.created_at, u.updated_at, t.category::text AS tenant_category
                FROM users u
                LEFT JOIN tenants t ON t.id = u.tenant_id
                WHERE t.category = $1::tenant_category
                ORDER BY u.created_at DESC
                LIMIT $2
                """,
                mode, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT u.id, u.email, u.name, u.tenant_id, u.created_at, u.updated_at, t.category::text AS tenant_category
                FROM users u
                LEFT JOIN tenants t ON t.id = u.tenant_id
                ORDER BY u.created_at DESC
                LIMIT $1
                """,
                limit,
            )
    return [
        {**_user_row_to_dict(r), "tenant_category": r["tenant_category"]}
        for r in rows
    ]


@router.post("/v1/users", status_code=201)
async def create_user(
    body: UserCreateRequest,
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
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
                INSERT INTO users (email, name, tenant_id, password_hash, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id, email, name, tenant_id, created_at, updated_at
                """,
                body.email,
                body.name,
                tenant_id,
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
            "SELECT id, email, name, tenant_id, created_at, updated_at FROM users WHERE id = $1",
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
# Tenants
# ---------------------------------------------------------------------------

class TenantCreateRequest(BaseModel):
    id: Optional[str] = None
    name: str
    owner_user_id: Optional[str] = None
    category: Optional[str] = None  # prod|staging|local — default 'prod' if omitted


@router.get("/v1/tenants")
async def list_tenants(
    limit: int = Query(default=100, le=1000),
    mode: Optional[str] = Query(default=None, description="Filter by category: prod|staging|local"),
    _claims: AuthClaims = Depends(require_admin),
) -> List[Dict[str, Any]]:
    if mode and mode not in ("prod", "staging", "local"):
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")

    pool = get_pool()
    async with pool.acquire() as conn:
        if mode:
            rows = await conn.fetch(
                """
                SELECT id, name, owner_user_id, created_at, category::text AS category
                FROM tenants WHERE category = $1::tenant_category
                ORDER BY created_at DESC LIMIT $2
                """,
                mode, limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT id, name, owner_user_id, created_at, category::text AS category
                FROM tenants ORDER BY created_at DESC LIMIT $1
                """,
                limit,
            )
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "owner_user_id": str(r["owner_user_id"]) if r["owner_user_id"] else None,
            "category": r["category"],
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
        category = body.category or "prod"
        if category not in ("prod", "staging", "local"):
            raise HTTPException(status_code=400, detail=f"Invalid category: {category}")
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO tenants (id, name, owner_user_id, category, created_at)
                VALUES ($1, $2, $3, $4::tenant_category, $5)
                RETURNING id, name, owner_user_id, category::text AS category, created_at
                """,
                tenant_id,
                body.name,
                owner_uid,
                category,
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
        "category": row["category"],
        "created_at": row["created_at"].isoformat(),
    }


# ---------------------------------------------------------------------------
# Tenant updates — category is the main mutable field for now.
# ---------------------------------------------------------------------------

class TenantUpdateRequest(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    owner_user_id: Optional[str] = None


@router.patch("/v1/tenants/{tenant_id}")
async def update_tenant(
    tenant_id: str,
    body: TenantUpdateRequest,
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    if body.category and body.category not in ("prod", "staging", "local"):
        raise HTTPException(status_code=400, detail=f"Invalid category: {body.category}")

    sets: List[str] = []
    args: List[Any] = []

    def _add(col: str, val: Any, cast: str = "") -> None:
        args.append(val)
        sets.append(f"{col} = ${len(args)}{cast}")

    if body.name is not None:
        _add("name", body.name)
    if body.category is not None:
        _add("category", body.category, "::tenant_category")
    if body.owner_user_id is not None:
        _add("owner_user_id", uuid.UUID(body.owner_user_id) if body.owner_user_id else None)

    if not sets:
        raise HTTPException(status_code=400, detail="No fields to update")

    args.append(tenant_id)
    sql = (
        "UPDATE tenants SET " + ", ".join(sets) +
        f" WHERE id = ${len(args)} RETURNING id, name, owner_user_id, category::text AS category, created_at"
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
        "category": row["category"],
        "created_at": row["created_at"].isoformat(),
    }


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
    if userId is None:
        if not claims.is_admin:
            raise HTTPException(status_code=403, detail="Admin privileges required for unfiltered list")
    else:
        if not claims.is_admin and claims.user_id != userId:
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
