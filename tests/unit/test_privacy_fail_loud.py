"""
Regression guard: the anonymization path must FAIL LOUD, never silently
forward raw content downstream to the LLM.

Historically several call sites "failed open" (returned the original
messages/content when anonymization errored) to keep the service available.
For a DSGVO anonymization gate that is a silent PII leak — the exact failure
mode this pipeline exists to prevent. These tests lock in fail-closed behavior
so a future refactor cannot quietly re-introduce the leak.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.mark.asyncio
async def test_privacy_client_fails_closed_when_service_errors():
    """Main LLM-proxy path: if the privacy service call fails while privacy is
    enabled, the client must RAISE — never return the raw messages (which
    main.py would forward verbatim to Claude)."""
    from src.privacy_client import PrivacyServiceClient

    client = PrivacyServiceClient()
    client.enabled = True  # force privacy on regardless of env

    class _BoomClient:
        async def post(self, *a, **k):
            raise RuntimeError("privacy service down")

    async def _boom_get_client():
        return _BoomClient()

    client._get_client = _boom_get_client  # type: ignore[assignment]

    raw = [{"role": "user", "content": "Max Mustermann wohnt in der Pelzgasse 18"}]
    with pytest.raises(RuntimeError):
        await client.anonymize_messages(raw, privacy_mode="full")


@pytest.mark.asyncio
async def test_privacy_client_passthrough_when_disabled_is_not_a_leak():
    """The legitimate 'privacy off' config (disabled / mode=none) still returns
    the input unchanged — that is a deliberate no-op, not the failure path."""
    from src.privacy_client import PrivacyServiceClient

    client = PrivacyServiceClient()
    client.enabled = False

    raw = [{"role": "user", "content": "Max Mustermann"}]
    msgs, mapping = await client.anonymize_messages(raw, privacy_mode="full")
    assert msgs == raw
    assert mapping == {}


def test_middleware_anonymize_message_fails_loud_on_error():
    """PrivacyMiddleware.anonymize_message must propagate anonymizer errors,
    not swallow them and return raw content."""
    from src.privacy.middleware import PrivacyMiddleware

    mw = PrivacyMiddleware(enabled=True)

    class _BoomAnonymizer:
        def anonymize(self, *a, **k):
            raise RuntimeError("presidio boom")

    mw._anonymizer = _BoomAnonymizer()  # type: ignore[assignment]

    with pytest.raises(RuntimeError):
        mw.anonymize_message("Max Mustermann", privacy_mode="full")
