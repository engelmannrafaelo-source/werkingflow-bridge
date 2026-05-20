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
    get_privacy_mode_from_request,
    get_user_id_from_request,
    get_app_id_from_request,
    get_agent_id_from_request,
    get_session_id_from_request,
    get_workflow_id_from_request,
    get_job_id_from_request,
    get_app_env_from_request,
    normalize_app_env
)
from .client import SupabaseTenantClient, TenantSettings, get_tenant_client
from .usage_tracker import (
    UsageTracker,
    UsageRecord,
    get_usage_tracker,
    track_request_usage
)
from .budget_checker import (
    check_budget,
    BudgetCheckResult,
    get_budget_cache
)

__all__ = [
    # Middleware
    'TenantMiddleware',
    'get_tenant_middleware',
    'get_tenant_from_request',
    'get_privacy_mode_from_request',
    'get_user_id_from_request',
    'get_app_id_from_request',
    'get_agent_id_from_request',
    'get_session_id_from_request',
    'get_workflow_id_from_request',
    'get_job_id_from_request',
    'get_app_env_from_request',
    'normalize_app_env',
    # Client
    'SupabaseTenantClient',
    'TenantSettings',
    'get_tenant_client',
    # Usage Tracking
    'UsageTracker',
    'UsageRecord',
    'get_usage_tracker',
    'track_request_usage',
    # Budget Checking
    'check_budget',
    'BudgetCheckResult',
    'get_budget_cache',
]
