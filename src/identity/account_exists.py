"""Scoped machine-only identity existence read; never an anonymous email oracle."""
import logging
import time
from collections import OrderedDict
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, EmailStr

from src.principals import get_principal_row_by_hash, hash_token
from src.identity.user_resolver import lookup_user_id_by_email

logger = logging.getLogger(__name__)

router = APIRouter()
PATH = "/v1/auth/account-exists"
APP = "werking-schema-generator"
# Per-platform-process burst limit. Auth is rechecked against the canonical
# active principal on every call; restart never restores a revoked credential.
_windows: OrderedDict[str, tuple[float, int]] = OrderedDict()


class AccountExistsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr


async def scoped_reader(
    authorization: Optional[str] = Header(default=None),
    x_app_id: Optional[str] = Header(default=None),
) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing reader credential")
    token = authorization[7:]
    if not token or len(token) > 4096:
        raise HTTPException(401, "Invalid reader credential")
    try:
        principal = await get_principal_row_by_hash(hash_token(token))
    except Exception as e:
        logger.warning("Identity reader authorization failed: %s", type(e).__name__)
        raise HTTPException(503, "Identity service unavailable") from None
    if not principal:
        raise HTTPException(401, "Invalid reader credential")
    # Explicit app/path enrollment: legacy shared keys and wildcard principals
    # do not inherit this lookup just because they can perform model calls.
    if (x_app_id != APP or APP not in principal["allowed_apps"]
            or PATH not in principal["allowed_paths"]
            or "*" in principal["allowed_apps"] or "*" in principal["allowed_paths"]):
        raise HTTPException(403, "Reader scope denied")
    return principal["id"]


@router.post("/account-exists")
async def account_exists(body: AccountExistsRequest, response: Response,
                         principal_id: str = Depends(scoped_reader)) -> dict[str, bool]:
    now = time.monotonic()
    start, count = _windows.get(principal_id, (now, 0))
    if now - start >= 60:
        start, count = now, 0
    if count >= 30:
        raise HTTPException(429, "Reader rate limit", headers={"Retry-After": "60"})
    _windows[principal_id] = (start, count + 1)
    _windows.move_to_end(principal_id)
    while len(_windows) > 1024:
        _windows.popitem(last=False)
    try:
        exists = await lookup_user_id_by_email(str(body.email).strip().lower()) is not None
    except Exception as e:
        logger.warning("Account existence lookup failed: %s", type(e).__name__)
        raise HTTPException(503, "Identity service unavailable") from None
    response.headers["Cache-Control"] = "no-store"
    return {"exists": exists}
