"""
Admission control for privacy_service.app's /smart-anonymize endpoint.

Root-caused 2026-07-22: with no admission control, concurrent large-text
callers (e.g. werking-report's batched anonymizeMany) all funneled into the
same 2-thread Presidio/Flair executor with an unbounded, silent wait — the
caller either hung past its own client timeout or (compounded by a since-
fixed nginx cross-worker retry bug) saw an opaque connection reset. These
tests exercise the fix directly against the FastAPI app (real HTTP layer via
TestClient), mocking out smart_anonymize() itself so no real Presidio/Flair
models are needed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture()
def client():
    pytest.importorskip("fastapi")
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient
    from src.privacy_service.app import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_semaphore():
    """Every test starts with a fresh, fully-available semaphore."""
    from src.privacy_service import app as app_mod

    app_mod._SMART_ANONYMIZE_SEMAPHORE = asyncio.Semaphore(
        app_mod._SMART_ANONYMIZE_MAX_CONCURRENT
    )
    yield


def test_smart_anonymize_happy_path_releases_slot(client):
    """A normal call acquires and releases its slot; a second call still succeeds."""
    fake_result = {
        "status": "success",
        "anonymization_performed": True,
        "raw_anonymized_text": "Hallo ANON_PERSON_001",
        "raw_entity_count": 1,
        "smart_anonymized_text": "Hallo ANON_PERSON_001",
        "smart_entity_count": 1,
        "restored_entities": [],
        "mapping": {"ANON_PERSON_001": "Max"},
        "detected_entities": [],
    }
    with patch(
        "src.privacy.smart_anonymizer.smart_anonymize",
        new=AsyncMock(return_value=fake_result),
    ):
        resp1 = client.post("/smart-anonymize", json={"text": "Hallo Max"})
        resp2 = client.post("/smart-anonymize", json={"text": "Hallo Max"})

    assert resp1.status_code == 200, resp1.text
    assert resp2.status_code == 200, resp2.text
    assert resp1.json()["status"] == "success"

    from src.privacy_service import app as app_mod
    assert app_mod._SMART_ANONYMIZE_SEMAPHORE._value == app_mod._SMART_ANONYMIZE_MAX_CONCURRENT


def test_smart_anonymize_at_capacity_returns_503_with_clear_reason(client, monkeypatch):
    """No free slot within the queue timeout → fail loud, not hang."""
    from src.privacy_service import app as app_mod

    monkeypatch.setattr(app_mod, "_SMART_ANONYMIZE_QUEUE_TIMEOUT_S", 0.05)

    # Exhaust every configured slot up front so the request under test has
    # nothing to acquire and must hit the queue timeout deterministically.
    for _ in range(app_mod._SMART_ANONYMIZE_MAX_CONCURRENT):
        asyncio.run(app_mod._SMART_ANONYMIZE_SEMAPHORE.acquire())

    resp = client.post("/smart-anonymize", json={"text": "Hallo Max"})

    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "at capacity" in detail
    assert "0" in detail  # queue-timeout seconds echoed back for diagnosability


def test_smart_anonymize_timeout_does_not_leak_the_semaphore(client, monkeypatch):
    """A timed-out waiter must not have decremented the semaphore it never acquired."""
    from src.privacy_service import app as app_mod

    monkeypatch.setattr(app_mod, "_SMART_ANONYMIZE_QUEUE_TIMEOUT_S", 0.05)
    for _ in range(app_mod._SMART_ANONYMIZE_MAX_CONCURRENT):
        asyncio.run(app_mod._SMART_ANONYMIZE_SEMAPHORE.acquire())
    value_before = app_mod._SMART_ANONYMIZE_SEMAPHORE._value

    client.post("/smart-anonymize", json={"text": "Hallo Max"})

    assert app_mod._SMART_ANONYMIZE_SEMAPHORE._value == value_before

    # Release back what this test manually acquired.
    for _ in range(app_mod._SMART_ANONYMIZE_MAX_CONCURRENT):
        app_mod._SMART_ANONYMIZE_SEMAPHORE.release()
