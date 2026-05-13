"""
FastAPI auth dependencies for Bridge endpoints.

Two distinct credential types:

1. **User JWT** (Authorization: Bearer <hs256-jwt>)
   Issued by POST /v1/auth/login. Carries user_id + tenant_id + app_licenses.
   Used by app frontends acting on behalf of an end-user.

2. **Service token** (X-Bridge-Service-Token: <shared-secret>)
   Shared secret from BRIDGE_SERVICE_TOKEN env. Used by app backends for
   server-to-server calls (e.g. budget-deduct, topup-credit-webhook from
   internal Mollie processor). Never sent from a browser.

Each dependency raises HTTPException(401/403) on rejection — defensive,
fail-loud, no silent pass-through.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config import config
from src.identity.jwt_utils import verify_jwt

_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# AuthClaims — uniform shape passed downstream
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuthClaims:
    """Resolved auth context. `kind` is either 'user' or 'service'."""
    kind: str                   # "user" | "service"
    user_id: Optional[str]      # set when kind == "user"
    email: Optional[str]
    tenant_id: Optional[str]
    is_admin: bool

    @property
    def is_user(self) -> bool:
        return self.kind == "user"

    @property
    def is_service(self) -> bool:
        return self.kind == "service"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _decode_user_jwt(token: str) -> AuthClaims:
    """Decode a Bearer JWT into AuthClaims. Raises 401 on failure."""
    try:
        payload = verify_jwt(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Malformed token: missing sub")

    return AuthClaims(
        kind="user",
        user_id=str(user_id),
        email=payload.get("email"),
        tenant_id=payload.get("tenantId"),
        is_admin=bool(payload.get("isAdmin", False)),
    )


def _verify_service_token(token: str) -> AuthClaims:
    """Constant-time compare against configured service token. Raises 401 on mismatch."""
    import hmac
    expected = config.service_token
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid service token")
    return AuthClaims(
        kind="service",
        user_id=None,
        email=None,
        tenant_id=None,
        is_admin=True,  # service tokens implicitly admin
    )


# ---------------------------------------------------------------------------
# Dependencies — wire these into routes via Depends()
# ---------------------------------------------------------------------------

def require_jwt(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> AuthClaims:
    """Require a valid user JWT. Use for user-facing endpoints."""
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    return _decode_user_jwt(credentials.credentials)


def require_service_token(
    x_bridge_service_token: Optional[str] = Header(default=None),
) -> AuthClaims:
    """Require the internal service token. Use for service-to-service endpoints."""
    if not x_bridge_service_token:
        raise HTTPException(status_code=401, detail="Missing X-Bridge-Service-Token header")
    return _verify_service_token(x_bridge_service_token)


def require_jwt_or_service(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    x_bridge_service_token: Optional[str] = Header(default=None),
) -> AuthClaims:
    """
    Accept EITHER a user JWT OR a service token.

    Use for endpoints that can be called both:
      - by an app frontend on behalf of a user (JWT)
      - by an app backend in a server-to-server context (service token)

    Service-token check is preferred when both are provided (less surprise:
    explicit X-header > implicit Authorization fallback).
    """
    if x_bridge_service_token:
        return _verify_service_token(x_bridge_service_token)
    if credentials and credentials.credentials:
        return _decode_user_jwt(credentials.credentials)
    raise HTTPException(
        status_code=401,
        detail="Missing credentials (need Authorization: Bearer <jwt> or X-Bridge-Service-Token)",
    )


def require_admin(claims: AuthClaims = Depends(require_jwt_or_service)) -> AuthClaims:
    """Admin-only endpoints (list all users, list all tenants, etc.)."""
    if not claims.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return claims


def require_self_or_admin(
    user_id: str,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> AuthClaims:
    """
    Authorize endpoints with a `{user_id}` path param.

    Allowed if:
      - the JWT subject matches the path's user_id, OR
      - the caller is admin (admin JWT or service token).

    Rejects 403 otherwise — never silently downgrades.
    """
    if claims.is_admin:
        return claims
    if claims.user_id == user_id:
        return claims
    raise HTTPException(status_code=403, detail="Forbidden: can only act on own user")
