"""
FastAPI auth dependencies for Bridge endpoints.

Two distinct credential types:

1. **User JWT** (Authorization: Bearer <hs256-jwt>)
   Issued by POST /v1/auth/login. Carries user_id + tenant_id + app_licenses.
   Used by app frontends acting on behalf of an end-user.

2. **Service principal token** (X-Bridge-Service-Token)
   DB-backed, with an explicit app or isolated bridge:admin grant. The shared
   infrastructure secret is bridge:internal only, never an app/admin credential.

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

from dataclasses import asdict, dataclass, field
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
      - Service token without X-User-ID → acting_user_id is None (grants unchanged)
    """

    kind: str  # "user" | "service"
    user_id: Optional[str]  # set when kind == "user"
    email: Optional[str]
    tenant_id: Optional[str]
    is_admin: bool  # raw privilege claim — gate authz on is_operator, never on this
    acting_user_id: Optional[str] = field(default=None)
    scopes: tuple[str, ...] = ()
    allowed_apps: tuple[str, ...] = ()
    allowed_paths: tuple[str, ...] = ("*",)
    principal_name: Optional[str] = None
    role: Optional[str] = field(
        default=None
    )  # platform role from JWT; None for service tokens

    @property
    def is_user(self) -> bool:
        return self.kind == "user"

    @property
    def is_service(self) -> bool:
        return self.kind == "service"

    @property
    def is_operator(self) -> bool:
        """Only the isolated admin service principal can administer the platform.

        Neither a legacy infrastructure secret, user JWT role nor absence of
        X-User-ID creates an admin grant. Proxied admin calls are self-scoped.
        """
        return (
            self.is_service
            and self.acting_user_id is None
            and self.scopes == ("bridge:admin",)
            and not self.allowed_apps
        )

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
        role=payload.get("role"),
    )


async def _verify_service_token(
    token: str, x_user_id: Optional[str] = None
) -> AuthClaims:
    """Resolve immutable service grants. Shared infrastructure is never admin."""
    import hmac

    from src.principals import (
        get_principal_row_by_hash,
        hash_token,
        resolve_principal_by_token,
        validate_principal_scopes,
    )

    if hmac.compare_digest(token, config.service_token):
        return AuthClaims(
            kind="service",
            user_id=None,
            email=None,
            tenant_id=None,
            is_admin=False,
            scopes=("bridge:internal",),
            principal_name="bridge-internal",
            acting_user_id=x_user_id,
        )
    # Platform uses its own DB; DB-less workers resolve through the internal seam.
    if config.db_enabled:
        row = await get_principal_row_by_hash(hash_token(token))
    else:
        principal = await resolve_principal_by_token(token)
        row = asdict(principal) if principal is not None else None
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid service token")
    try:
        scopes = validate_principal_scopes(row["allowed_apps"], row["scopes"])
    except ValueError:
        raise HTTPException(
            status_code=403, detail="Principal has invalid service scopes"
        )
    return AuthClaims(
        kind="service",
        user_id=None,
        email=None,
        tenant_id=None,
        is_admin=scopes == ["bridge:admin"],
        scopes=tuple(scopes),
        allowed_apps=tuple(row["allowed_apps"]),
        allowed_paths=tuple(row["allowed_paths"]),
        principal_name=row["name"],
        acting_user_id=x_user_id,
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


async def require_service_token(
    request: Request,
    x_bridge_service_token: Optional[str] = Header(default=None),
) -> AuthClaims:
    """Require the internal service token. Use for service-to-service endpoints."""
    if not x_bridge_service_token:
        raise HTTPException(
            status_code=401, detail="Missing X-Bridge-Service-Token header"
        )
    # Machine callers keep their explicit grants; no implicit operator role.
    claims = await _verify_service_token(x_bridge_service_token)
    await enforce_app_request(claims, request)
    return claims


async def require_jwt_or_service(
    request: Request,
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
        claims = await _verify_service_token(
            x_bridge_service_token, x_user_id=x_user_id
        )
        await enforce_app_request(claims, request)
        return claims
    if credentials and credentials.credentials:
        # x_user_id is intentionally NOT passed — JWT sub is the acting identity.
        return _decode_user_jwt(credentials.credentials)
    raise HTTPException(
        status_code=401,
        detail="Missing credentials (need Authorization: Bearer <jwt> or X-Bridge-Service-Token)",
    )


def require_admin(claims: AuthClaims = Depends(require_jwt_or_service)) -> AuthClaims:
    """Require the isolated bridge:admin principal, never an app or user role."""
    if not claims.is_operator:
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
    (which would otherwise be treated as an operator). This check MUST precede
    the operator check for that reason.

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
    if claims.is_operator:
        return claims
    # User JWT: must be own user_id.
    if claims.user_id == user_id:
        return claims
    raise HTTPException(status_code=403, detail="Forbidden: can only act on own user")


async def require_internal_service(
    claims: AuthClaims = Depends(require_service_token),
) -> AuthClaims:
    """Bridge workers only; app principals cannot use internal data/mutation APIs."""
    if claims.scopes != ("bridge:internal",):
        raise HTTPException(status_code=403, detail="Bridge internal scope required")
    return claims


async def require_credential_admin_or_user(
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> AuthClaims:
    """Users manage their own credentials; machine callers require admin scope."""
    if claims.is_service and not claims.is_operator:
        raise HTTPException(
            status_code=403, detail="Admin scope required for credential management"
        )
    return claims


async def enforce_app_request(claims: AuthClaims, request: Request) -> None:
    """An app grant cannot be relabelled by header, route, query or body."""
    if not claims.allowed_apps:
        return
    if (
        request.url.path != "/v1/auth/service-scope"
        and claims.allowed_paths
        and "*" not in claims.allowed_paths
        and request.url.path not in claims.allowed_paths
    ):
        raise HTTPException(
            status_code=403, detail="Service principal path scope mismatch"
        )
    app = claims.allowed_apps[0]
    candidates = [request.headers.get("x-app-id")]
    for source in (request.query_params, request.path_params):
        candidates.extend(source.get(key) for key in ("app", "app_id", "appId"))
    if "application/json" in request.headers.get("content-type", ""):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        if isinstance(body, dict):
            candidates.extend(body.get(key) for key in ("app", "app_id", "appId"))
    if any(value is not None and value != app for value in candidates):
        raise HTTPException(
            status_code=403, detail="Service principal app scope mismatch"
        )
