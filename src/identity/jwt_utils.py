"""HS256 JWT sign/verify. Secret from BRIDGE_JWT_SECRET env var."""
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

import jwt

_ALGORITHM = "HS256"
_EXPIRY_HOURS = 8

# Mirrors UserRoleSchema in packages/api-validation/src/common-schemas.ts
VALID_ROLES = frozenset({"super_admin", "tenant_admin", "admin", "owner", "member", "user"})


def _secret() -> str:
    s = os.getenv("BRIDGE_JWT_SECRET")
    if not s:
        raise RuntimeError("BRIDGE_JWT_SECRET not set")
    return s


def sign_jwt(
    user_id: str,
    email: str,
    tenant_id: str,
    app_licenses: list[Dict[str, Any]],
    role: str = "user",
) -> str:
    if role not in VALID_ROLES:
        raise ValueError(f"Invalid role '{role}'. Must be one of: {sorted(VALID_ROLES)}")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "email": email,
        "tenantId": tenant_id,
        "appLicenses": app_licenses,
        "role": role,
        "iat": now,
        "exp": now + timedelta(hours=_EXPIRY_HOURS),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, _secret(), algorithm=_ALGORITHM)


def verify_jwt(token: str) -> Dict[str, Any]:
    """Raises jwt.InvalidTokenError if invalid/expired."""
    return jwt.decode(token, _secret(), algorithms=[_ALGORITHM])
