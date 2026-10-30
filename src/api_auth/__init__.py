"""Auth dependencies for Bridge HTTP routes."""
from src.api_auth.deps import (
    require_jwt,
    require_service_token,
    require_jwt_or_service,
    require_admin,
    require_self_or_admin,
    require_auftragsbudget_token,
    enforce_auftragsbudget_binding,
    RECHT_ANLEGEN,
    RECHT_LESEN,
    AuthClaims,
)
from src.api_auth.tenant_resolver import (
    resolve_tenant_id,
    resolve_tenant_for_user,
    get_tenant_of_user,
)

__all__ = [
    "require_jwt",
    "require_service_token",
    "require_jwt_or_service",
    "require_admin",
    "require_self_or_admin",
    "require_auftragsbudget_token",
    "enforce_auftragsbudget_binding",
    "RECHT_ANLEGEN",
    "RECHT_LESEN",
    "AuthClaims",
    "resolve_tenant_id",
    "resolve_tenant_for_user",
    "get_tenant_of_user",
]
