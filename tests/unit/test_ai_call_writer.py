"""
Unit tests for persist_ai_call_activity user_id resolution.

Tests the fix for production WARNING:
  "invalid input for query argument $1: 'office@heimbau.at'
   (invalid UUID ...)"

Covers:
  - 'system' string → warning logged with counter, no DB insert
  - email with existing user → UUID resolved, activity written
  - email with no matching user → warning logged with counter, no DB insert
  - valid UUID → passes through unchanged, activity written
"""
from __future__ import annotations

import sys
import uuid
from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# Stub heavy deps before any src.* import
for _mod in ["src.db.client", "src.pricing"]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Stub pricing constants used by the module
import src.pricing as _pricing_stub  # noqa: E402
_pricing_stub.cost_eur = MagicMock(return_value=0.01)
_pricing_stub.PRICING_VERSION = "test-v1"

import src.activity.ai_call_writer as writer  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_UUID = str(uuid.uuid4())
TENANT_UUID = str(uuid.uuid4())


def _make_mock_conn(*, tenant_row=None, email_row=None):
    """
    Build a mock asyncpg connection whose fetchrow returns different
    values depending on the query (tenant lookup vs email lookup).
    """
    conn = AsyncMock()
    call_count = {"n": 0}

    async def _fetchrow(query, *args):
        call_count["n"] += 1
        # First call might be email lookup (SELECT id FROM users WHERE email)
        # Subsequent call is tenant lookup (SELECT u.tenant_id ...)
        if "email" in query.lower():
            return email_row
        return tenant_row

    conn.fetchrow = _fetchrow
    conn.execute = AsyncMock(return_value=None)
    return conn


def _pool_ctx(conn):
    """Return a mock pool whose acquire() returns the given connection."""
    pool = MagicMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=cm)
    return pool


def _tenant_row(tenant_id=TENANT_UUID):
    row = MagicMock()
    row.__getitem__ = lambda self, key: {
        "tenant_id": tenant_id,
        "billing_mode": "subscription",
    }[key]
    return row


def _email_row(user_id=VALID_UUID):
    row = MagicMock()
    row.__getitem__ = lambda self, key: {"id": uuid.UUID(user_id)}[key]
    return row


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _call_writer(user_id, app_id="test-app"):
    await writer.persist_ai_call_activity(
        app_id=app_id,
        user_id=user_id,
        agent_id=None,
        workflow_id=None,
        model="claude-sonnet-4-5",
        input_tokens=100,
        output_tokens=50,
        status="success",
        duration_ms=1000,
        app_env="prod",
    )


# ---------------------------------------------------------------------------
# Test: 'system' string → skip with counter warning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_system_user_skipped_with_warning():
    """'system' X-User-ID logs a warning with skip counter and does not insert."""
    # Reset counter for isolation
    writer._skip_counts.clear()

    conn = _make_mock_conn()  # should not be reached for the insert path
    pool = _pool_ctx(conn)

    with (
        patch.object(writer, "get_pool", return_value=pool),
        patch.object(writer.logger, "warning") as mock_warn,
    ):
        await _call_writer(user_id="system")
        # Call again — counter should increment
        await _call_writer(user_id="system")

    # Two warnings, each with incrementing counter
    # Format: warning(fmt, user_id, app_id, counter) → args[3] is the counter
    assert mock_warn.call_count >= 2
    first_msg = mock_warn.call_args_list[0]
    second_msg = mock_warn.call_args_list[1]
    assert first_msg.args[3] == 1   # skip #1
    assert second_msg.args[3] == 2  # skip #2

    # No DB inserts
    conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Test: email with matching user → UUID resolved, activity inserted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_email_resolved_to_uuid_and_inserted():
    """Valid email that matches a user row → UUID resolved, activity written."""
    writer._skip_counts.clear()

    e_row = _email_row(VALID_UUID)
    t_row = _tenant_row(TENANT_UUID)
    conn = _make_mock_conn(tenant_row=t_row, email_row=e_row)
    pool = _pool_ctx(conn)

    with (
        patch.object(writer, "get_pool", return_value=pool),
        patch.object(writer.logger, "warning") as mock_warn,
    ):
        await _call_writer(user_id="office@heimbau.at")

    # No skip warning
    skip_warnings = [
        c for c in mock_warn.call_args_list
        if "activity skipped" in str(c)
    ]
    assert skip_warnings == [], f"Unexpected skip warning: {skip_warnings}"

    # DB insert was attempted
    conn.execute.assert_called()


# ---------------------------------------------------------------------------
# Test: email with no matching user → skip with counter warning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_email_not_found_skipped_with_counter():
    """Email that has no user row → warning with counter, no insert."""
    writer._skip_counts.clear()

    conn = _make_mock_conn(tenant_row=None, email_row=None)
    pool = _pool_ctx(conn)

    with (
        patch.object(writer, "get_pool", return_value=pool),
        patch.object(writer.logger, "warning") as mock_warn,
    ):
        await _call_writer(user_id="unknown@example.com")
        await _call_writer(user_id="unknown@example.com")

    # Two warnings with incrementing counter
    assert mock_warn.call_count >= 2
    counts = [c.args[1] for c in mock_warn.call_args_list]  # counter is arg[1]
    assert 1 in counts
    assert 2 in counts

    # No insert
    conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Test: valid UUID → passes through, activity written
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_uuid_inserts_activity():
    """Valid UUID user_id bypasses all resolution logic and writes activity."""
    writer._skip_counts.clear()

    t_row = _tenant_row(TENANT_UUID)
    conn = _make_mock_conn(tenant_row=t_row)
    pool = _pool_ctx(conn)

    with (
        patch.object(writer, "get_pool", return_value=pool),
        patch.object(writer.logger, "warning") as mock_warn,
    ):
        await _call_writer(user_id=VALID_UUID)

    # No skip warning
    skip_warnings = [c for c in mock_warn.call_args_list if "skipped" in str(c)]
    assert skip_warnings == []

    # Insert was called
    conn.execute.assert_called()
