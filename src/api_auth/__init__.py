"""Auth dependencies for Bridge HTTP routes."""
from src.api_auth.deps import (
    require_jwt,
    require_service_token,
    require_jwt_or_service,
    require_admin,
    require_self_or_admin,
    AuthClaims,
)

__all__ = [
    "require_jwt",
    "require_service_token",
    "require_jwt_or_service",
    "require_admin",
    "require_self_or_admin",
    "AuthClaims",
]
