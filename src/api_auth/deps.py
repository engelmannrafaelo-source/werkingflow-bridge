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

## Acting-user identity (X-User-ID)

A service-token caller may include an `X-User-ID` header to declare that it
is acting *on behalf of* an authenticated end-user (the customer self-service
proxy pattern). When present, `acting_user_id` is set and the request is
scoped to that user — self-service endpoints must not return data for any
other user, even though the credential is a service token.

Security invariant: `X-User-ID` is ONLY honoured for service-token callers.
With a user JWT the JWT `sub` is authoritative; an `X-User-ID` header is
silently ignored to prevent identity spoofing (same principle as ADR 0007's
"body tenantId is ignored for JWT callers").
"""
from __future__ import annotations

from dataclasses import dataclass, field
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
    """
    Resolved auth context. `kind` is either 'user' or 'service'.

    `acting_user_id` is the user identity this request is scoped to:
      - User JWT  → acting_user_id == user_id (JWT sub)
      - Service token + X-User-ID → acting_user_id == that header value
      - Service token without X-User-ID → acting_user_id is None (operator)
    """
    kind: str                       # "user" | "service"
    user_id: Optional[str]          # set when kind == "user"
    email: Optional[str]
    tenant_id: Optional[str]
    is_admin: bool
    acting_user_id: Optional[str] = field(default=None)

    @property
    def is_user(self) -> bool:
        return self.kind == "user"

    @property
    def is_service(self) -> bool:
        return self.kind == "service"

    @property
    def is_operator(self) -> bool:
        """
        True when this credential grants unrestricted cross-user access.

        Two cases:
          1. Service token without a proxied user (acting_user_id is None).
             The classic operator: CUI platform panel, background jobs, etc.
          2. User JWT with is_admin=True. Currently is_admin is never set in
             JWTs (known bug tracked separately), but is handled here for
             correctness when that changes.

        A service token WITH X-User-ID is NOT an operator: it is scoped to
        that specific user for all self-service endpoints.
        """
        if self.is_service:
            return self.acting_user_id is None
        # User JWT: operator only when explicitly flagged as admin.
        return self.is_admin

    @property
    def effective_user_id(self) -> Optional[str]:
        """
        The user identity this request is scoped to, if any.

        Returns acting_user_id (may come from JWT sub or X-User-ID),
        or None for operator service tokens (no user scope).
        """
        return self.acting_user_id


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

    uid = str(user_id)
    return AuthClaims(
        kind="user",
        user_id=uid,
        email=payload.get("email"),
        tenant_id=payload.get("tenantId"),
        is_admin=bool(payload.get("isAdmin", False)),
        acting_user_id=uid,  # JWT sub is always the acting identity for user JWTs
    )


def _verify_service_token(token: str, x_user_id: Optional[str] = None) -> AuthClaims:
    """
    Constant-time compare against configured service token. Raises 401 on mismatch.

    x_user_id, when given, sets acting_user_id — the request is then scoped
    to that user (customer self-service proxy). Without it, acting_user_id is
    None, meaning the caller has unrestricted operator access.
    """
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
        acting_user_id=x_user_id,  # None = operator, set = proxy for user
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
    # require_service_token is used for machine-to-machine calls that never
    # proxy for an end-user (e.g. budget top-up after Mollie webhook). No
    # X-User-ID handling here — the acting_user_id stays None (operator).
    return _verify_service_token(x_bridge_service_token)


def require_jwt_or_service(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    x_bridge_service_token: Optional[str] = Header(default=None),
    x_user_id: Optional[str] = Header(default=None),
) -> AuthClaims:
    """
    Accept EITHER a user JWT OR a service token.

    Use for endpoints that can be called both:
      - by an app frontend on behalf of a user (JWT)
      - by an app backend in a server-to-server context (service token)

    Service-token check is preferred when both are provided (less surprise:
    explicit X-header > implicit Authorization fallback).

    X-User-ID is only honoured for service-token callers (sets acting_user_id,
    scoping the request to that user). With a user JWT, X-User-ID is silently
    ignored — the JWT sub is authoritative and cannot be overridden by a header
    (that would be an identity-spoofing vector).
    """
    if x_bridge_service_token:
        return _verify_service_token(x_bridge_service_token, x_user_id=x_user_id)
    if credentials and credentials.credentials:
        # x_user_id is intentionally NOT passed — JWT sub is the acting identity.
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
      - the caller is an operator (service token without X-User-ID), OR
      - the caller is an admin JWT.

    Service token WITH X-User-ID (customer proxy): scoped to acting_user_id.
    The acting_user_id must equal the path user_id — access to any other user
    is rejected with 403, even though the credential is formally a service token
    (which would otherwise be treated as admin). This check MUST precede the
    is_admin check for that reason.

    Rejects 403 otherwise — never silently downgrades.
    """
    # Customer proxy: service token acting for a specific user. Hard scope.
    if claims.is_service and claims.acting_user_id is not None:
        if claims.acting_user_id == user_id:
            return claims
        raise HTTPException(
            status_code=403,
            detail="Forbidden: proxy token scoped to different user",
        )
    # Operator service token or admin JWT: unrestricted.
    if claims.is_admin:
        return claims
    # User JWT: must be own user_id.
    if claims.user_id == user_id:
        return claims
    raise HTTPException(status_code=403, detail="Forbidden: can only act on own user")
