"""Only the configured authority may serve/mutate policy; ordinary user proxies cannot."""
import os
from fastapi import APIRouter, Depends, HTTPException
from src.api_auth import require_admin, AuthClaims
from . import store

router = APIRouter(prefix="/v1/admin/bridge-account-policy", tags=["bridge-account-policy"])

def authority():
    if os.environ.get("BRIDGE_ACCOUNT_POLICY_AUTHORITY") != "1":
        raise HTTPException(503, detail="bridge_account_policy_not_authority")

@router.get("")
async def get_policy(claims: AuthClaims = Depends(require_admin)):
    authority()
    try:
        return await store.read_policy()
    except (RuntimeError, ValueError):
        raise HTTPException(503, detail="bridge_account_policy_unavailable") from None

@router.patch("")
async def patch_policy(body: dict, claims: AuthClaims = Depends(require_admin)):
    authority()
    try:
        return await store.replace_policy(body, claims.effective_user_id or "operator-service")
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except store.Conflict:
        raise HTTPException(409, detail="bridge_account_policy_revision_conflict") from None


@router.get("/runtime")
async def get_runtime(claims: AuthClaims = Depends(require_admin)):
    authority()
    from .runtime import runtime_snapshot
    try:
        return await runtime_snapshot(await store.read_policy())
    except (RuntimeError, ValueError):
        raise HTTPException(503, detail="bridge_account_policy_runtime_unavailable") from None
