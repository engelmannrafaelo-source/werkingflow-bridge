"""
Tenant Middleware for AI Bridge

Routes requests with per-tenant configuration:
- Privacy mode selection (none, basic, full)
- Rate limiting per tenant
- Model restrictions
- Usage tracking for billing

Headers (in priority order):
1. X-Tenant-API-Key: Tenant API key (legacy, validates against Supabase)
2. X-Tenant-ID + X-Tenant-Timestamp + X-Tenant-Signature: Signed tenant (new, HMAC)
"""

import os
import time
import hmac
import hashlib
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

# Shared secret for signed tenant headers
BRIDGE_TENANT_SECRET = os.getenv("BRIDGE_TENANT_SECRET")

logger = logging.getLogger(__name__)


def validate_signed_tenant(request: Request, max_age_seconds: int = 300) -> Optional[str]:
    """
    Validate signed tenant headers (HMAC-SHA256).

    Headers required:
    - X-Tenant-ID: Tenant identifier
    - X-Tenant-Timestamp: Unix timestamp (seconds)
    - X-Tenant-Signature: HMAC-SHA256(tenant_id:timestamp, secret)

    Args:
        request: Starlette request
        max_age_seconds: Maximum age of timestamp (default: 5 minutes)

    Returns:
        tenant_id if valid, None otherwise
    """
    if not BRIDGE_TENANT_SECRET:
        return None

    tenant_id = request.headers.get("X-Tenant-ID")
    timestamp = request.headers.get("X-Tenant-Timestamp")
    signature = request.headers.get("X-Tenant-Signature")

    if not all([tenant_id, timestamp, signature]):
        return None

    # Check timestamp age
    try:
        ts = int(timestamp)
        now = int(time.time())
        if abs(now - ts) > max_age_seconds:
            logger.warning(f"Signed tenant timestamp too old: {abs(now - ts)}s")
            return None
    except ValueError:
        logger.warning(f"Invalid tenant timestamp: {timestamp}")
        return None

    # Validate signature
    payload = f"{tenant_id}:{timestamp}"
    expected = hmac.new(
        BRIDGE_TENANT_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        logger.warning(f"Invalid tenant signature for {tenant_id}")
        return None

    return tenant_id


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
        privacy_mode_header = request.headers.get("X-Privacy-Mode")

        # Initialize request state defaults
        request.state.tenant = None
        request.state.privacy_mode = self.default_privacy_mode
        request.state.tenant_validated = False

        # Allow per-request privacy mode override via header
        # Valid values: "none", "basic", "full"
        if privacy_mode_header in ("none", "basic", "full"):
            request.state.privacy_mode = privacy_mode_header

        # Skip tenant validation for non-AI endpoints
        if self._is_exempt_path(request.url.path):
            return await call_next(request)

        # Method 1: Validate tenant via API key (legacy, Supabase lookup)
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

                logger.debug(
                    f"Tenant context set (API key): {settings.tenant_slug} "
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
                    logger.warning(
                        f"Invalid tenant API key provided, using defaults"
                    )

        # Method 2: Validate tenant via signed headers (new, HMAC)
        elif not request.state.tenant_validated:
            signed_tenant_id = validate_signed_tenant(request)

            if signed_tenant_id:
                # Valid signed tenant - create minimal settings
                # Note: We trust the signature, so we don't need full Supabase lookup
                # The tenant_id is used for usage tracking
                request.state.tenant = TenantSettings(
                    tenant_id=signed_tenant_id,
                    tenant_slug=signed_tenant_id,
                    tenant_name=signed_tenant_id,
                    privacy_mode=self.default_privacy_mode,
                    allowed_models=[],  # No model restrictions for signed tenants
                    rate_limit_rpm=60,
                    budget_limit_eur=None,
                    budget_alert_threshold=0.8,
                    is_enabled=True,
                    billing_mode="platform_managed",
                    monthly_token_limit=None,
                    monthly_vision_limit=None,
                    billing_margin=1.5,
                    plan="pro"
                )
                request.state.tenant_validated = True

                logger.debug(
                    f"Tenant context set (signed): {signed_tenant_id}"
                )

        # No valid tenant authentication
        if not request.state.tenant_validated and self.require_tenant_auth and self._requires_tenant_auth(request.url.path):
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
