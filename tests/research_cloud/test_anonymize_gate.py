"""Tests for the research-cloud fail-closed anonymize gate.

Verifies the gate reuses src.main._smart_anonymize_core (the same fail-closed
path + attestation persistence as /v1/privacy/smart-anonymize) and raises
CloudAnonymizeError — never returning unanonymized text — for every failure
shape that core function can produce.
"""
# Stub claude_code_sdk and other heavy deps BEFORE any src.* import — same
# pattern as tests/test_research_tracking.py: src.main needs to be imported
# (unstubbed) so patch("src.main....") can resolve the dotted path, but it
# transitively imports claude_code_sdk / DB / identity, none of which this
# unit test needs or has available.
import sys
from unittest.mock import MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
    "src.identity.routes",
    "src.db.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.main  # noqa: E402 — import after stubs, before patch("src.main....")

from src.research_cloud.anonymize_gate import CloudAnonymizeError, anonymize_query_for_cloud  # noqa: E402


def _fake_request() -> MagicMock:
    return MagicMock()


@pytest.mark.asyncio
async def test_success_returns_anonymized_text():
    fake_result = MagicMock(
        status="success",
        anonymization_performed=True,
        smart_anonymized_text="ANON_PERSON_001 fragt nach Normen",
        error=None,
    )
    with patch("src.main._smart_anonymize_core", new=AsyncMock(return_value=fake_result)) as core:
        text = await anonymize_query_for_cloud(_fake_request(), "Max Mustermann fragt nach Normen")

    assert text == "ANON_PERSON_001 fragt nach Normen"
    core.assert_awaited_once()


@pytest.mark.asyncio
async def test_status_error_aborts():
    fake_result = MagicMock(
        status="error", anonymization_performed=False, smart_anonymized_text=None, error="privacy-service 503: ..."
    )
    with patch("src.main._smart_anonymize_core", new=AsyncMock(return_value=fake_result)):
        with pytest.raises(CloudAnonymizeError, match="did not attest success"):
            await anonymize_query_for_cloud(_fake_request(), "query text")


@pytest.mark.asyncio
async def test_anonymization_not_performed_aborts_even_if_status_success():
    """Defense in depth: a status=success without anonymization_performed=True
    must still abort (mirrors the fail-loud check in the convert-and-anonymize
    call site)."""
    fake_result = MagicMock(
        status="success", anonymization_performed=False, smart_anonymized_text="query text", error=None
    )
    with patch("src.main._smart_anonymize_core", new=AsyncMock(return_value=fake_result)):
        with pytest.raises(CloudAnonymizeError, match="did not attest success"):
            await anonymize_query_for_cloud(_fake_request(), "query text")


@pytest.mark.asyncio
async def test_empty_anonymized_text_for_nonempty_query_aborts():
    fake_result = MagicMock(
        status="success", anonymization_performed=True, smart_anonymized_text="", error=None
    )
    with patch("src.main._smart_anonymize_core", new=AsyncMock(return_value=fake_result)):
        with pytest.raises(CloudAnonymizeError, match="empty text"):
            await anonymize_query_for_cloud(_fake_request(), "a real query")


@pytest.mark.asyncio
async def test_underlying_exception_is_wrapped_and_aborts():
    """BRIDGE_ANONYMIZE_ENABLED disabled -> _smart_anonymize_core raises
    BridgeError; the gate must not swallow it into a silent pass-through."""
    with patch(
        "src.main._smart_anonymize_core", new=AsyncMock(side_effect=RuntimeError("disabled"))
    ):
        with pytest.raises(CloudAnonymizeError, match="refusing research-cloud call"):
            await anonymize_query_for_cloud(_fake_request(), "query text")
