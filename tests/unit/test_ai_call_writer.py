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


# ---------------------------------------------------------------------------
# Test: cache tokens land in the activity payload (UI display contract)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_tokens_in_activity_payload():
    """Cache read/creation tokens are part of the activities payload and
    totalTokens is the physical sum — otherwise cached agent calls display
    '10 input tokens' while the priced input is 100k+."""
    import json as _json

    writer._skip_counts.clear()

    t_row = _tenant_row(TENANT_UUID)
    conn = _make_mock_conn(tenant_row=t_row)
    pool = _pool_ctx(conn)

    with (
        patch.object(writer, "get_pool", return_value=pool),
        patch.object(writer, "_deduct_call_cost"),
    ):
        await writer.persist_ai_call_activity(
            app_id="werking-energy",
            user_id=VALID_UUID,
            agent_id="api/llm-client",
            workflow_id=None,
            model="claude-sonnet-4-5",
            input_tokens=10,
            output_tokens=39_133,
            status="success",
            duration_ms=648_000,
            app_env="prod",
            cache_read_tokens=80_000,
            cache_creation_tokens=5_000,
        )

    # First execute = activities INSERT; its jsonb arg is the payload.
    activities_call = conn.execute.call_args_list[0]
    payload_json = next(
        a for a in activities_call.args if isinstance(a, str) and "promptTokens" in a
    )
    payload = _json.loads(payload_json)

    assert payload["promptTokens"] == 10
    assert payload["completionTokens"] == 39_133
    assert payload["cacheReadTokens"] == 80_000
    assert payload["cacheCreationTokens"] == 5_000
    assert payload["totalTokens"] == 10 + 39_133 + 80_000 + 5_000


# ---------------------------------------------------------------------------
# Anonymous marker → dedicated accounting bucket (migration 032)
# ---------------------------------------------------------------------------

async def _call_writer_anonymous(user_id):
    """Wie _call_writer, aber ohne app_id-Default-Verwirrung — expliziter Marker."""
    await writer.persist_ai_call_activity(
        app_id="werking-report",
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


@pytest.mark.asyncio
async def test_anonymous_marker_books_to_anonymous_identity():
    """'anonymous:<grund>' bucht auf die synthetische Identität statt geskippt zu werden."""
    writer._skip_counts.clear()
    writer._anonymous_identity_verified = True  # Identität (Migration 032) vorhanden

    t_row = _tenant_row(TENANT_UUID)
    conn = _make_mock_conn(tenant_row=t_row)
    pool = _pool_ctx(conn)

    with (
        patch.object(writer, "get_pool", return_value=pool),
        patch.object(writer, "_deduct_call_cost") as mock_deduct,
        patch.object(writer.logger, "warning") as mock_warn,
    ):
        await _call_writer_anonymous("anonymous:public-check-funnel")

    # Kein Skip — es wurde gebucht
    skip_warnings = [c for c in mock_warn.call_args_list if "skipped" in str(c)]
    assert skip_warnings == []
    conn.execute.assert_called()

    # Auf die Anonymous-UUID gebucht, Grund in beiden Persist-Zielen
    all_args = [str(c) for c in conn.execute.call_args_list]
    assert any(writer.ANONYMOUS_USER_ID in a for a in all_args)
    assert any("public-check-funnel" in a for a in all_args)

    # Keine Budget-Deduction für den Anonym-Posten
    mock_deduct.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_underscore_anonymous_alias_books_too():
    """Der report-Übergangsalias '_anonymous' verhält sich wie ein anonymous-Marker."""
    writer._skip_counts.clear()
    writer._anonymous_identity_verified = True

    t_row = _tenant_row(TENANT_UUID)
    conn = _make_mock_conn(tenant_row=t_row)
    pool = _pool_ctx(conn)

    with (
        patch.object(writer, "get_pool", return_value=pool),
        patch.object(writer, "_deduct_call_cost") as mock_deduct,
    ):
        await _call_writer_anonymous("_anonymous")

    conn.execute.assert_called()
    all_args = [str(c) for c in conn.execute.call_args_list]
    assert any(writer.ANONYMOUS_USER_ID in a for a in all_args)
    mock_deduct.assert_not_called()


@pytest.mark.asyncio
async def test_anonymous_without_identity_row_skips_loudly():
    """Fehlt die Migration-032-Identität, wird geskippt + laut gewarnt (kein FK-Crash)."""
    writer._skip_counts.clear()
    writer._anonymous_identity_verified = False

    conn = _make_mock_conn(tenant_row=None)  # identity lookup → None
    pool = _pool_ctx(conn)

    with (
        patch.object(writer, "get_pool", return_value=pool),
        patch.object(writer.logger, "warning") as mock_warn,
    ):
        await _call_writer_anonymous("anonymous:funnel")

    assert any("migration 032" in str(c) for c in mock_warn.call_args_list)
    conn.execute.assert_not_called()
