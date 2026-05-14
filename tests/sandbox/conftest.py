"""
Shared fixtures for sandbox tests.

Uses unittest.mock to avoid the circular import chain that exists in production.
All DB calls are exercised against an in-memory mock connection (AsyncMock).
"""
import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

# Stub out modules that trigger heavy import chains in test isolation
for mod in [
    "src.identity.routes",
    "src.billing.routes",
    "src.activity.routes",
]:
    if mod not in sys.modules:
        stub = MagicMock()
        stub.router = MagicMock()
        sys.modules[mod] = stub


TENANT_ID = "tenant-sandbox-test"
USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
LEASE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


def make_mock_conn(
    *,
    billing_mode: str = "subscription",
    tenant_id: str = TENANT_ID,
    lease_returns_row: bool = True,
    already_released: bool = False,
    session_tokens: int = 0,
    session_cost: float = 0.0,
):
    """
    Build an AsyncMock connection pre-wired with sensible defaults.
    Tests can override individual methods after calling this factory.
    """
    conn = AsyncMock()

    # get_tenant_info path
    conn.fetchrow.return_value = {
        "tenant_id": tenant_id,
        "billing_mode": billing_mode,
    }

    # create_lease: execute returns nothing
    conn.execute.return_value = None

    # heartbeat
    conn.fetchrow.return_value = {
        "expires_at": datetime(2099, 1, 1, tzinfo=timezone.utc),
        "tenant_id": tenant_id,
        "billing_mode": billing_mode,
    }

    # get_session_aggregate
    conn.fetch.return_value = []

    return conn
