"""
Tenant Middleware for AI Bridge

Routes requests with per-tenant configuration:
- Privacy mode selection (none, basic, full)
- Rate limiting per tenant
- Model restrictions
- Usage tracking for billing

Headers:
- X-Tenant-ID: Tenant UUID (optional, for context)
- X-Tenant-API-Key: Tenant API key (required for tenant features)
"""

import os
import time
import logging
import asyncio
from typing import Optional, Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse

from .client import (
    SupabaseTenantClient,
    TenantSettings,
    get_tenant_client
)

logger = logging.getLogger(__name__)


class TenantMiddleware(BaseHTTPMiddleware):
    """
    Tenant-aware middleware for AI Bridge.

    Extracts tenant information from headers and configures:
    - Privacy mode for Presidio anonymization
    - Model access control
    - Rate limiting (per-tenant)
    - Usage tracking

    If no tenant headers: Uses default DSGVO-compliant settings (full privacy).
    """

    def __init__(
        self,
        app,
        default_privacy_mode: str = "full",
        require_tenant_auth: bool = False
    ):
        """
        Initialize tenant middleware.

        Args:
            app: FastAPI/Starlette app
            default_privacy_mode: Privacy mode when no tenant specified
            require_tenant_auth: If True, reject requests without valid tenant API key
        """
        super().__init__(app)

        self.default_privacy_mode = os.getenv(
            "DEFAULT_PRIVACY_MODE",
            default_privacy_mode
        )
        self.require_tenant_auth = os.getenv(
            "REQUIRE_TENANT_AUTH",
            str(require_tenant_auth)
        ).lower() in ("true", "1", "yes")

        self.tenant_client = get_tenant_client()

        logger.info(
            f"Tenant middleware initialized "
            f"(default_privacy={self.default_privacy_mode}, "
            f"require_auth={self.require_tenant_auth}, "
            f"supabase_enabled={self.tenant_client.enabled})"
        )

    async def dispatch(
        self,
        request: Request,
        call_next: Callable
    ) -> Response:
        """Process request with tenant context."""
        start_time = time.time()

        # Extract tenant headers
        tenant_api_key = request.headers.get("X-Tenant-API-Key")
        tenant_id_header = request.headers.get("X-Tenant-ID")

        # Initialize request state defaults
        request.state.tenant = None
        request.state.privacy_mode = self.default_privacy_mode
        request.state.tenant_validated = False

        # Skip tenant validation for non-AI endpoints
        if self._is_exempt_path(request.url.path):
            return await call_next(request)

        # Validate tenant if API key provided
        if tenant_api_key and self.tenant_client.enabled:
            settings = await self.tenant_client.validate_api_key(tenant_api_key)

            if settings:
                # Valid tenant - apply settings
                request.state.tenant = settings
                request.state.privacy_mode = settings.privacy_mode
                request.state.tenant_validated = True

                # Check if tenant is enabled
                if not settings.is_enabled:
                    return JSONResponse(
                        status_code=403,
                        content={
                            "error": {
                                "message": "Tenant is disabled",
                                "type": "tenant_disabled",
                                "code": "tenant_disabled"
                            }
                        }
                    )

                # Validate model if specified in request
                # (Model validation happens later in the request flow)

                logger.debug(
                    f"Tenant context set: {settings.tenant_slug} "
                    f"(privacy={settings.privacy_mode})"
                )

                # Fire-and-forget: Update API key last used
                asyncio.create_task(
                    self.tenant_client.update_key_last_used(tenant_api_key)
                )

            else:
                # Invalid API key
                if self.require_tenant_auth:
                    return JSONResponse(
                        status_code=401,
                        content={
                            "error": {
                                "message": "Invalid tenant API key",
                                "type": "authentication_error",
                                "code": "invalid_api_key"
                            }
                        }
                    )
                else:
                    # Log warning but continue with defaults
                    logger.warning(
                        f"Invalid tenant API key provided, using defaults"
                    )

        elif self.require_tenant_auth and self._requires_tenant_auth(request.url.path):
            # Tenant auth required but no key provided
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "message": "Tenant API key required",
                        "type": "authentication_error",
                        "code": "missing_api_key",
                        "help": "Set X-Tenant-API-Key header with your tenant API key"
                    }
                }
            )

        # Call next middleware/endpoint
        response = await call_next(request)

        # Log request duration for tenant
        duration_ms = int((time.time() - start_time) * 1000)
        if request.state.tenant:
            logger.debug(
                f"Tenant request completed: {request.state.tenant.tenant_slug} "
                f"({duration_ms}ms)"
            )

        return response

    def _is_exempt_path(self, path: str) -> bool:
        """Check if path is exempt from tenant validation."""
        exempt_paths = [
            "/health",
            "/stats",
            "/v1/models",
            "/v1/auth/status",
            "/v1/privacy/status",
            "/v1/debug/",
            "/v1/compatibility",
            "/docs",
            "/openapi.json",
            "/redoc"
        ]
        return any(path.startswith(p) for p in exempt_paths)

    def _requires_tenant_auth(self, path: str) -> bool:
        """Check if path requires tenant authentication."""
        auth_required_paths = [
            "/v1/chat/completions",
            "/v1/research"
        ]
        return any(path.startswith(p) for p in auth_required_paths)


# Singleton instance
_tenant_middleware: Optional[TenantMiddleware] = None


def get_tenant_middleware(app=None) -> Optional[TenantMiddleware]:
    """
    Get or create tenant middleware singleton.

    Args:
        app: FastAPI app (required on first call)

    Returns:
        TenantMiddleware instance or None if not configured
    """
    global _tenant_middleware

    if _tenant_middleware is None and app is not None:
        _tenant_middleware = TenantMiddleware(app)

    return _tenant_middleware


def get_tenant_from_request(request: Request) -> Optional[TenantSettings]:
    """
    Get tenant settings from request state.

    Usage in endpoint:
        tenant = get_tenant_from_request(request)
        if tenant:
            privacy_mode = tenant.privacy_mode
    """
    return getattr(request.state, "tenant", None)


def get_privacy_mode_from_request(request: Request) -> str:
    """
    Get privacy mode from request state.

    Returns "full" if no tenant context (DSGVO-safe default).
    """
    return getattr(request.state, "privacy_mode", "full")
