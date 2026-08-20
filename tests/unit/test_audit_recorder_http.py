"""Tests for the worker-side audit event writer (src/audit/recorder.py),
ADR-0009 Schritt 2a, C4 — now an HTTP client of platform-api's
POST /v1/internal/audit-events instead of a direct DB writer.

Contract under test: record_audit_event POSTs the given fields to
call_platform, and NEVER raises — a PlatformUnavailable is caught and logged,
never propagated (it must not fail the anonymization call it accompanies).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.audit import recorder
from src.platform_client import PlatformResponse, PlatformUnavailable


@pytest.mark.asyncio
async def test_posts_to_internal_audit_events_endpoint():
    mock_call = AsyncMock(return_value=PlatformResponse(status_code=204, json=None))
    with patch.object(recorder, "call_platform", mock_call):
        await recorder.record_audit_event(
            "pii.pseudonymized",
            actor_user_id="11111111-1111-1111-1111-111111111111",
            actor_label="engelmann",
            target_kind="anonymization",
            target_id="wf-1",
            metadata={"total_entities": 4},
        )

    mock_call.assert_awaited_once()
    method, path = mock_call.call_args.args
    assert method == "POST"
    assert path == "/v1/internal/audit-events"
    body = mock_call.call_args.kwargs["json"]
    assert body == {
        "action": "pii.pseudonymized",
        "actor_user_id": "11111111-1111-1111-1111-111111111111",
        "actor_label": "engelmann",
        "target_kind": "anonymization",
        "target_id": "wf-1",
        "metadata": {"total_entities": 4},
    }


@pytest.mark.asyncio
async def test_missing_metadata_defaults_to_empty_dict():
    mock_call = AsyncMock(return_value=PlatformResponse(status_code=204, json=None))
    with patch.object(recorder, "call_platform", mock_call):
        await recorder.record_audit_event("pii.pseudonymized")

    body = mock_call.call_args.kwargs["json"]
    assert body["metadata"] == {}


@pytest.mark.asyncio
async def test_platform_unavailable_is_swallowed_never_raises():
    mock_call = AsyncMock(side_effect=PlatformUnavailable("platform-api down"))
    with patch.object(recorder, "call_platform", mock_call):
        # Must not raise — a failed audit write must never fail the caller.
        await recorder.record_audit_event("pii.pseudonymized")

    mock_call.assert_awaited_once()
