"""
Stammdaten endpoints — per-tenant per-app key/value config in JSONB.

Apps used to keep their own /api/admin/stammdaten with app-local storage.
Now bridge owns the canonical copy; apps read via PlatformClient.

GET   /v1/stammdaten/{tenantId}/{appId}        require_self_or_admin (tenant-bound) or admin
PUT   /v1/stammdaten/{tenantId}/{appId}        upsert (admin only — Stammdaten ops)
DELETE /v1/stammdaten/{tenantId}/{appId}       reset
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.api_auth import require_admin, require_jwt_or_service, AuthClaims
from src.db.client import get_pool

router = APIRouter(prefix="/v1/stammdaten", tags=["stammdaten"])

_ALLOWED_APPS = {"werking-report", "werking-energy", "werking-safety", "werking-noise", "engelmann"}


def _row(r: Any) -> Dict[str, Any]:
    data_raw = r["data"]
    if isinstance(data_raw, str):
        try:
            data_raw = json.loads(data_raw)
        except Exception:
            data_raw = {}
    return {
        "tenantId": r["tenant_id"],
        "appId": r["app_id"],
        "data": data_raw or {},
        "updatedAt": r["updated_at"].isoformat(),
        "updatedBy": str(r["updated_by"]) if r["updated_by"] else None,
    }


def _check_app(app_id: str) -> None:
    if app_id not in _ALLOWED_APPS:
        raise HTTPException(status_code=400, detail=f"Unknown appId: {app_id}")


@router.get("/{tenant_id}/{app_id}")
async def get_stammdaten(
    tenant_id: str,
    app_id: str,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    _check_app(app_id)
    # Scoped callers may only read stammdaten for their own tenant; operators
    # any. A customer self-service proxy (service token + X-User-ID) has no
    # tenant binding and is rejected here — stammdaten is not a portal endpoint.
    if not claims.is_operator and claims.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Forbidden: foreign tenant")

    pool = get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT tenant_id, app_id, data, updated_at, updated_by FROM stammdaten WHERE tenant_id = $1 AND app_id = $2",
            tenant_id, app_id,
        )
    if not r:
        # Return empty doc — caller can PUT to create.
        return {
            "tenantId": tenant_id,
            "appId": app_id,
            "data": {},
            "updatedAt": None,
            "updatedBy": None,
        }
    return _row(r)


class StammdatenUpsert(BaseModel):
    data: Dict[str, Any]


@router.put("/{tenant_id}/{app_id}")
async def upsert_stammdaten(
    tenant_id: str,
    app_id: str,
    body: StammdatenUpsert,
    claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    _check_app(app_id)
    actor_id = uuid.UUID(claims.user_id) if claims.is_user and claims.user_id else None

    pool = get_pool()
    async with pool.acquire() as conn:
        # Verify tenant exists — fail loud, no auto-create here.
        t = await conn.fetchval("SELECT 1 FROM tenants WHERE id = $1", tenant_id)
        if not t:
            raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")
        r = await conn.fetchrow(
            """
            INSERT INTO stammdaten (tenant_id, app_id, data, updated_at, updated_by)
            VALUES ($1, $2, $3::jsonb, NOW(), $4)
            ON CONFLICT (tenant_id, app_id) DO UPDATE
              SET data = EXCLUDED.data,
                  updated_at = NOW(),
                  updated_by = EXCLUDED.updated_by
            RETURNING tenant_id, app_id, data, updated_at, updated_by
            """,
            tenant_id, app_id, json.dumps(body.data or {}), actor_id,
        )
    return _row(r)


@router.delete("/{tenant_id}/{app_id}")
async def delete_stammdaten(
    tenant_id: str,
    app_id: str,
    _claims: AuthClaims = Depends(require_admin),
) -> None:
    _check_app(app_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM stammdaten WHERE tenant_id = $1 AND app_id = $2",
            tenant_id, app_id,
        )
    return {"deleted": True}
