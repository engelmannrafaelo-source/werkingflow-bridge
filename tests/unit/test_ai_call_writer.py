"""
Unit tests for persist_ai_call_activity user_id resolution.

Tests the fix for production WARNING:
  "invalid input for query argument $1: 'office@heimbau.at'
   (invalid UUID ...)"

Covers:
  - 'system' string → warning logged with counter, no row requested
  - email with existing user → UUID resolved, row requested
  - email with no matching user → warning logged with counter, no row
  - valid UUID → passes through unchanged, row requested

Seam note (ADR-0009 Schritt 2c): the writer holds no database connection any
more. It states the finished row to platform-api, so these tests assert on what
it ASKS to be written (the `ledger_seam` fixture in tests/conftest.py) rather
than on SQL arguments. The SQL itself is tested one layer down, against
src/activity/ledger_db.py — see tests/billing/test_ledger_db_rows.py.
"""
from __future__ import annotations

import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

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
from src.activity.providers import PROVIDER_ANTHROPIC  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_UUID = str(uuid.uuid4())
TENANT_UUID = str(uuid.uuid4())


@pytest.fixture
def seam(ledger_seam):
    """The money seam with a resolvable tenant — the ordinary case."""
    ledger_seam.context = {"tenantId": TENANT_UUID, "billingMode": "subscription"}
    writer._skip_counts.clear()
    return ledger_seam


def _resolves_to(user_id):
    """Patch the shared identity resolver to answer with this UUID."""
    return patch(
        "src.identity.user_resolver.resolve_user_id",
        new=AsyncMock(return_value=uuid.UUID(user_id)),
    )


def _resolves_to_unknown():
    """Patch the shared identity resolver to report 'no such Bridge user'."""
    from src.identity.user_resolver import UnknownUserIdentity

    return patch(
        "src.identity.user_resolver.resolve_user_id",
        new=AsyncMock(side_effect=UnknownUserIdentity("no Bridge user")),
    )


async def _call_writer(user_id, app_id="test-app"):
    return await writer.persist_ai_call_activity(
        provider=PROVIDER_ANTHROPIC,
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
async def test_system_user_skipped_with_warning(seam):
    """'system' X-User-ID logs a warning with skip counter and writes nothing."""
    with patch.object(writer.logger, "warning") as mock_warn:
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

    assert seam.ledger_calls == []


# ---------------------------------------------------------------------------
# Test: email with matching user → UUID resolved, row written
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_email_resolved_to_uuid_and_inserted(seam):
    """Valid email that matches a user row → UUID resolved, row written."""
    with (
        _resolves_to(VALID_UUID),
        patch.object(writer.logger, "warning") as mock_warn,
    ):
        await _call_writer(user_id="office@heimbau.at")

    # No skip warning
    skip_warnings = [
        c for c in mock_warn.call_args_list
        if "activity skipped" in str(c)
    ]
    assert skip_warnings == [], f"Unexpected skip warning: {skip_warnings}"

    assert seam.row["actor_user_id"] == VALID_UUID


# ---------------------------------------------------------------------------
# Test: email with no matching user → skip with counter warning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_email_not_found_skipped_with_counter(seam):
    """Email that has no user row → warning with counter, no row written."""
    with (
        _resolves_to_unknown(),
        patch.object(writer.logger, "warning") as mock_warn,
    ):
        await _call_writer(user_id="unknown@example.com")
        await _call_writer(user_id="unknown@example.com")

    # Two warnings with incrementing counter
    assert mock_warn.call_count >= 2
    counts = [c.args[1] for c in mock_warn.call_args_list]  # counter is arg[1]
    assert 1 in counts
    assert 2 in counts

    assert seam.ledger_calls == []


@pytest.mark.asyncio
async def test_unresolvable_email_identity_is_not_a_skip(seam):
    """An identity lookup that could not be ANSWERED is not the same as "no
    such user". The first is transient (the row stays owed and is replayed),
    the second is a definitive skip. Collapsing them would file a real billing
    row as correctly-not-metered — see load_billing_context's docstring for the
    same distinction one step later."""
    from src.platform_client import PlatformUnavailable

    with patch(
        "src.identity.user_resolver.resolve_user_id",
        new=AsyncMock(side_effect=PlatformUnavailable("platform-api down")),
    ):
        outcome = await _call_writer(user_id="office@heimbau.at")

    assert seam.ledger_calls == []
    assert outcome == writer.OUTCOME_FAILED, (
        "eine unbeantwortbare Identitaetsaufloesung muss die Zeile geschuldet "
        f"lassen, nicht als Skip abhaken (war: {outcome!r})"
    )


# ---------------------------------------------------------------------------
# Test: valid UUID → passes through, row written
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_uuid_inserts_activity(seam):
    """Valid UUID user_id bypasses all resolution logic and writes the row."""
    with patch.object(writer.logger, "warning") as mock_warn:
        await _call_writer(user_id=VALID_UUID)

    # No skip warning
    skip_warnings = [c for c in mock_warn.call_args_list if "skipped" in str(c)]
    assert skip_warnings == []

    assert seam.row["actor_user_id"] == VALID_UUID


# ---------------------------------------------------------------------------
# Test: cache tokens land in the activity payload (UI display contract)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cache_tokens_in_activity_payload(seam):
    """Cache read/creation tokens are part of the activities payload and
    totalTokens is the physical sum — otherwise cached agent calls display
    '10 input tokens' while the priced input is 100k+."""
    with patch.object(writer, "_deduct_call_cost", new=AsyncMock()):
        await writer.persist_ai_call_activity(
            provider=PROVIDER_ANTHROPIC,
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

    payload = seam.row["audit_payload"]

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
    return await writer.persist_ai_call_activity(
        provider=PROVIDER_ANTHROPIC,
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
async def test_anonymous_marker_books_to_anonymous_identity(seam):
    """'anonymous:<grund>' bucht auf die synthetische Identität statt geskippt zu werden."""
    seam.anonymous_present = True  # Identität (Migration 032) vorhanden

    with (
        patch.object(writer, "_deduct_call_cost") as mock_deduct,
        patch.object(writer.logger, "warning") as mock_warn,
    ):
        await _call_writer_anonymous("anonymous:public-check-funnel")

    # Kein Skip — es wurde gebucht
    skip_warnings = [c for c in mock_warn.call_args_list if "skipped" in str(c)]
    assert skip_warnings == []

    # Auf die Anonymous-UUID gebucht, Grund in beiden Persist-Zielen
    assert seam.row["actor_user_id"] == writer.ANONYMOUS_USER_ID
    assert seam.row["provider_metadata"]["anonymous_reason"] == "public-check-funnel"
    assert seam.row["audit_payload"]["anonymousReason"] == "public-check-funnel"

    # Keine Budget-Deduction für den Anonym-Posten
    mock_deduct.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_underscore_anonymous_alias_books_too(seam):
    """Der report-Übergangsalias '_anonymous' verhält sich wie ein anonymous-Marker."""
    seam.anonymous_present = True

    with patch.object(writer, "_deduct_call_cost") as mock_deduct:
        await _call_writer_anonymous("_anonymous")

    assert seam.row["actor_user_id"] == writer.ANONYMOUS_USER_ID
    mock_deduct.assert_not_called()


@pytest.mark.asyncio
async def test_anonymous_without_identity_row_is_held_not_dropped(seam):
    """Fehlt die Migration-032-Identität, wird NICHT geschrieben (kein FK-Crash)
    und NICHT stillgelegt: der Ausgang ist "failed", die Zeile bleibt geschuldet
    und wird nach der Migration nachgeholt. Ton ist ERROR, nicht WARNING —
    die Nutzung ist bis dahin nicht abgerechnet."""
    seam.anonymous_present = False

    with patch.object(writer.logger, "error") as mock_err:
        outcome = await _call_writer_anonymous("anonymous:funnel")

    assert any("migration 032" in str(c) for c in mock_err.call_args_list)
    assert seam.ledger_calls == []
    assert outcome == writer.OUTCOME_FAILED


@pytest.mark.asyncio
async def test_anonymous_probe_outage_is_held_not_dropped(seam):
    """Und wenn die Frage gar nicht beantwortet werden konnte, ebenso: eine
    unerreichbare Gegenseite darf nicht wie "Identität fehlt" *oder* wie
    "alles gut" aussehen. Die Zeile bleibt geschuldet."""
    seam.explode_on = "anonymous"

    outcome = await _call_writer_anonymous("anonymous:funnel")

    assert seam.ledger_calls == []
    assert outcome == writer.OUTCOME_FAILED


# ---------------------------------------------------------------------------
# Test: error_message persisted (truncated) into activities + usage_events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_error_message_persisted_truncated(seam):
    """Error calls carry the provider error text (capped at 500 chars) in BOTH
    the activities payload (errorMessage) and usage_events provider_metadata
    (error_message) — a bare errorCode is undiagnosable after log rotation."""
    long_msg = "Bedrock API error (ValidationException): " + "x" * 600

    with patch.object(writer, "_deduct_call_cost", new=AsyncMock()):
        await writer.persist_ai_call_activity(
            provider=PROVIDER_ANTHROPIC,
            app_id="werking-energy",
            user_id=VALID_UUID,
            agent_id="upload-verarbeitung",
            workflow_id="wf-1",
            model="claude-sonnet-5",
            input_tokens=0,
            output_tokens=0,
            status="error",
            duration_ms=300,
            error_code="400",
            error_message=long_msg,
            app_env="prod",
        )

    # One request carrying BOTH rows — the audit half rides with the money row
    # and is written only if that row was created (see ledger_db.insert_ai_call).
    assert len(seam.ledger_calls) == 1
    activities_payload = seam.row["audit_payload"]
    usage_metadata = seam.row["provider_metadata"]

    assert activities_payload["errorCode"] == "400"
    assert activities_payload["errorMessage"] == long_msg[:500]
    assert len(activities_payload["errorMessage"]) == 500

    assert usage_metadata["status"] == "error"
    assert usage_metadata["error_code"] == "400"
    assert usage_metadata["error_message"] == long_msg[:500]


@pytest.mark.asyncio
async def test_success_rows_carry_no_error_fields(seam):
    """Success rows must not grow empty error keys."""
    with patch.object(writer, "_deduct_call_cost", new=AsyncMock()):
        await _call_writer(user_id=VALID_UUID)

    activities_payload = seam.row["audit_payload"]
    usage_metadata = seam.row["provider_metadata"]
    assert "errorMessage" not in activities_payload
    assert "error_message" not in usage_metadata
    assert "error_code" not in usage_metadata
