"""Operator admin routes for service principals (Stufe 2).

Create / list / rotate / deactivate the per-caller identities defined in
migration 034. Operator-only (require_admin — same gate as the user/tenant admin
routes). The cleartext token is generated here and returned EXACTLY ONCE at
create/rotate time; only its hash + prefix are persisted, so it can never be
read back (password-hash discipline).
"""
from __future__ import annotations

import logging
import secrets
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api_auth import require_admin, AuthClaims
from src.db.client import is_db_enabled
from src import principals as principals_repo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-principals"])


def _require_db() -> None:
    if not is_db_enabled():
        raise HTTPException(status_code=503, detail="principal store unavailable (no DB)")


class CreatePrincipalRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    allowed_apps: List[str] = Field(default_factory=list)
    allowed_paths: Optional[List[str]] = None
    monthly_cap_eur: Optional[float] = Field(default=None, ge=0)


class RotatePrincipalRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)


class SetActiveRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    active: bool


def _new_token() -> str:
    """64 hex chars — same shape/length as the AI_BRIDGE_API_KEY it replaces."""
    return secrets.token_hex(32)


@router.post("/v1/admin/service-principals", status_code=201)
async def create_principal(
    body: CreatePrincipalRequest,
    _claims: AuthClaims = Depends(require_admin),
):
    """Create a principal and return its cleartext token ONCE. Store the token in
    the caller's Infisical workspace immediately — it is not recoverable."""
    _require_db()
    token = _new_token()
    try:
        principal = await principals_repo.create_principal(
            name=body.name,
            token=token,
            allowed_apps=body.allowed_apps,
            allowed_paths=body.allowed_paths,
            monthly_cap_eur=body.monthly_cap_eur,
        )
    except Exception as e:  # unique-name violation etc. — surface, don't swallow
        raise HTTPException(status_code=409, detail=f"could not create principal: {e}")
    logger.info("service-principal created: name=%s apps=%s", principal.name, principal.allowed_apps)
    return {
        "id": principal.id,
        "name": principal.name,
        "allowed_apps": principal.allowed_apps,
        "allowed_paths": principal.allowed_paths,
        "monthly_cap_eur": principal.monthly_cap_eur,
        "token": token,  # shown ONCE
    }


@router.get("/v1/admin/service-principals")
async def list_principals(_claims: AuthClaims = Depends(require_admin)):
    """List all principals (never any token material)."""
    _require_db()
    return {"principals": await principals_repo.list_principals()}


@router.post("/v1/admin/service-principals/rotate")
async def rotate_principal(
    body: RotatePrincipalRequest,
    _claims: AuthClaims = Depends(require_admin),
):
    """Issue a new token for an existing principal and return it ONCE. The old
    token stops working immediately (resolution cache is invalidated)."""
    _require_db()
    token = _new_token()
    ok = await principals_repo.rotate_principal_token(body.name, token)
    if not ok:
        raise HTTPException(status_code=404, detail=f"no principal named '{body.name}'")
    logger.info("service-principal rotated: name=%s", body.name)
    return {"name": body.name, "token": token}  # shown ONCE


@router.post("/v1/admin/service-principals/set-active")
async def set_active(
    body: SetActiveRequest,
    _claims: AuthClaims = Depends(require_admin),
):
    """Activate/deactivate a principal. Deactivation kills its token at once."""
    _require_db()
    ok = await principals_repo.set_principal_active(body.name, body.active)
    if not ok:
        raise HTTPException(status_code=404, detail=f"no principal named '{body.name}'")
    logger.info("service-principal set active=%s: name=%s", body.active, body.name)
    return {"name": body.name, "active": body.active}
