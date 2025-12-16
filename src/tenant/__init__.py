"""
Tenant-Aware Routing for AI Bridge (WerkingFlow Platform)

Provides per-tenant configuration for:
- Privacy modes (none, basic, full)
- Rate limiting
- Budget tracking
- Model restrictions
- Usage logging for billing

Headers:
- X-Tenant-ID: Tenant UUID (optional context)
- X-Tenant-API-Key: Tenant API key (required for tenant features)
"""

from .middleware import (
    TenantMiddleware,
    get_tenant_middleware,
    get_tenant_from_request,
    get_privacy_mode_from_request
)
from .client import SupabaseTenantClient, TenantSettings, get_tenant_client
from .usage_tracker import (
    UsageTracker,
    UsageRecord,
    get_usage_tracker,
    track_request_usage
)

__all__ = [
    # Middleware
    'TenantMiddleware',
    'get_tenant_middleware',
    'get_tenant_from_request',
    'get_privacy_mode_from_request',
    # Client
    'SupabaseTenantClient',
    'TenantSettings',
    'get_tenant_client',
    # Usage Tracking
    'UsageTracker',
    'UsageRecord',
    'get_usage_tracker',
    'track_request_usage'
]
