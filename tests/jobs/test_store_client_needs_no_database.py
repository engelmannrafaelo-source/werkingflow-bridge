"""Abnahmekriterium von ADR-0009 Schritt 2d, direkt geprüft:

    Der komplette Job-Store-Pfad — anlegen, lesen, fortschreiben, Watchdog,
    TTL-Cleanup — läuft ohne BRIDGE_DB_URL, über platform-api.

Gleiche brutale Bauart wie tests/billing/test_worker_needs_no_database.py:
`get_pool` wird so verbogen, dass JEDER Griff zum Verbindungspool den Test
sprengt. Ein übersehener Direkt-DB-Aufruf kann sich damit nicht als "geht
doch" tarnen — auf einem Entwickler-Rechner MIT Datenbank wäre er unsichtbar
und fiele erst nach dem Umzug auf.

Zweiter Teil: die Fallback-Stufe. Ist platform-api weg UND BRIDGE_DB_URL
gesetzt, greift store.* direkt; ist BEIDES weg, kommt der benannte
JobStoreUnavailable — keine RuntimeError-Kaskade aus get_pool().
"""
from __future__ import annotations

import os
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

from src.jobs import store_client
from src.jobs.store_client import JobStoreUnavailable
from src.platform_client import PlatformResponse, PlatformUnavailable


def _boom(*a, **kw):
    raise AssertionError(
        "der Job-Store-Pfad hat get_pool() angefasst — genau das darf ein "
        "Worker ohne BRIDGE_DB_URL nie brauchen"
    )


class _NoPool:
    """Jeder Pool-Griff sprengt den Test — beide Bindungsarten abgedeckt."""

    _POOL_BINDINGS = [
        "src.db.client.get_pool",   # späte Importer (in Funktionen)
        "src.jobs.store.get_pool",  # store.py bindet beim Import
    ]

    def __enter__(self):
        self._patches = [patch(t, side_effect=_boom) for t in self._POOL_BINDINGS]
        for pt in self._patches:
            pt.start()
        return self

    def __exit__(self, *exc):
        for pt in reversed(self._patches):
            pt.stop()


class _PlatformApi:
    """Beantwortet jeden Innen-API-Aufruf und merkt sich die Pfade."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: dict = {}

    async def __call__(self, method, path, *, json=None, params=None, **kw):
        self.calls.append((method, path))
        if json is not None:
            self.bodies[path] = json

        if path == "/v1/internal/jobs" and method == "POST":
            return PlatformResponse(204, None)
        if path == "/v1/internal/jobs" and method == "GET":
            return PlatformResponse(200, {"jobs": [{"job_id": "job_1", "created_at": "2026-08-31T04:00:00+00:00"}]})
        if path == "/v1/internal/jobs/job_x":
            return PlatformResponse(200, {"job": {
                "job_id": "job_x", "kind": "ping", "status": "pending",
                "payload": {"a": 1}, "attribution": {"app_id": "werking-energy"},
                "attempts": 0, "defer_count": 0, "defer_reason": None,
                "progress": None, "result": None, "error": None,
                "created_at": "2026-08-31T04:00:00+00:00",
                "updated_at": "2026-08-31T04:00:05+00:00",
                "heartbeat_at": None, "deferred_until": None,
            }})
        if path == "/v1/internal/jobs/job_gone":
            return PlatformResponse(404, {"detail": "job not found: job_gone"})
        if path.startswith("/v1/internal/jobs/") and method == "POST":
            return PlatformResponse(204, None)
        if path == "/v1/internal/jobs-maintenance/claim-stale":
            return PlatformResponse(200, {"job": None})
        if path == "/v1/internal/jobs-maintenance/abandoned":
            return PlatformResponse(200, {"jobs": []})
        if path == "/v1/internal/jobs-maintenance/cleanup":
            return PlatformResponse(200, {"removed": 3})
        raise AssertionError(f"unerwarteter Innen-API-Aufruf: {method} {path}")


@pytest.mark.asyncio
async def test_every_store_operation_runs_without_a_database(monkeypatch):
    monkeypatch.delenv("BRIDGE_DB_URL", raising=False)
    api = _PlatformApi()

    with _NoPool(), patch.object(store_client, "call_platform", new=api):
        await store_client.create_job("job_x", "ping", {"a": 1}, {"app_id": "werking-energy"})
        job = await store_client.get_job("job_x")
        assert await store_client.get_job("job_gone") is None
        await store_client.mark_running("job_x")
        await store_client.heartbeat("job_x")
        await store_client.update_progress("job_x", {"step": 1})
        await store_client.mark_done("job_x", {"ok": True})
        await store_client.mark_error("job_x", "kaputt", code="X")
        await store_client.defer_job("job_x", 60, "dependency down")
        assert await store_client.claim_stale_job(90, 3) is None
        assert await store_client.find_abandoned(90, 3) == []
        assert await store_client.cleanup_old(7200) == 3
        jobs = await store_client.list_jobs(app_id="werking-energy")

    # Die Datetime-Felder kommen als ISO-Strings über den Draht und MÜSSEN als
    # datetime bei den Aufrufern ankommen (elapsed-Berechnung in routes.py).
    assert isinstance(job["created_at"], datetime)
    assert isinstance(job["updated_at"], datetime)
    assert job["heartbeat_at"] is None
    # Die List-Projektion gibt created_at schon in store.py als ISO-String aus —
    # sie bleibt String (beide Stufen identisch).
    assert jobs[0]["created_at"] == "2026-08-31T04:00:00+00:00"

    # Zähl-Operationen liefen über die Innen-API, nicht irgendwo lokal vorbei.
    paths = [p for _, p in api.calls]
    assert "/v1/internal/jobs-maintenance/claim-stale" in paths
    assert "/v1/internal/jobs/job_x/defer" in paths
    assert api.bodies["/v1/internal/jobs"]["job_id"] == "job_x"


@pytest.mark.asyncio
async def test_platform_down_without_db_raises_the_named_error(monkeypatch):
    monkeypatch.delenv("BRIDGE_DB_URL", raising=False)

    async def _down(*a, **kw):
        raise PlatformUnavailable("connect timeout")

    with _NoPool(), patch.object(store_client, "call_platform", new=_down):
        with pytest.raises(JobStoreUnavailable) as exc:
            await store_client.get_job("job_x")
    assert "no direct-DB fallback" in str(exc.value)


@pytest.mark.asyncio
async def test_platform_down_with_db_falls_back_to_direct_store(monkeypatch):
    monkeypatch.setenv("BRIDGE_DB_URL", "postgresql://x")

    async def _down(*a, **kw):
        raise PlatformUnavailable("connect timeout")

    direct = AsyncMock(return_value={"job_id": "job_x", "status": "pending"})
    with patch.object(store_client, "call_platform", new=_down), \
         patch.object(store_client.store, "get_job", new=direct):
        job = await store_client.get_job("job_x")

    assert job["job_id"] == "job_x"
    direct.assert_awaited_once_with("job_x")


@pytest.mark.asyncio
async def test_list_jobs_maps_the_contract_400_back_to_valueerror(monkeypatch):
    monkeypatch.delenv("BRIDGE_DB_URL", raising=False)

    async def _bad_request(*a, **kw):
        return PlatformResponse(400, {"detail": "list_jobs requires at least one of app_id or user_id"})

    with _NoPool(), patch.object(store_client, "call_platform", new=_bad_request):
        with pytest.raises(ValueError, match="app_id or user_id"):
            await store_client.list_jobs(limit=10)


@pytest.mark.asyncio
async def test_unexpected_platform_answer_fails_loud_not_silent(monkeypatch):
    """Ein Status außerhalb des Vertrags ist ein gebrochener Vertrag — kein
    stiller Fallback (die Plattform HAT geantwortet), kein geratenes Ergebnis."""
    monkeypatch.setenv("BRIDGE_DB_URL", "postgresql://x")

    async def _weird(*a, **kw):
        return PlatformResponse(418, {"detail": "teapot"})

    direct = AsyncMock()
    with patch.object(store_client, "call_platform", new=_weird), \
         patch.object(store_client.store, "get_job", new=direct):
        with pytest.raises(JobStoreUnavailable, match="unexpectedly"):
            await store_client.get_job("job_x")
    direct.assert_not_awaited()


def test_store_availability_predicate(monkeypatch):
    monkeypatch.delenv("BRIDGE_DB_URL", raising=False)
    monkeypatch.setenv("BRIDGE_SERVICE_TOKEN", "t")
    assert store_client.is_store_available() is True
    monkeypatch.delenv("BRIDGE_SERVICE_TOKEN", raising=False)
    assert store_client.is_store_available() is False
    monkeypatch.setenv("BRIDGE_DB_URL", "postgresql://x")
    assert store_client.is_store_available() is True
