import hashlib
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI

from src.erkunder import routes as erkunder_routes
from src.jobs import routes as job_routes


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(job_routes, "verify_api_key", AsyncMock())
    monkeypatch.setattr(erkunder_routes, "verify_api_key", AsyncMock())
    monkeypatch.setattr(job_routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(job_routes, "get_executor", lambda kind: object())
    monkeypatch.setattr(job_routes, "new_job_id", lambda: "fake-job-id")
    monkeypatch.setattr(job_routes.store_client, "create_job", AsyncMock())
    monkeypatch.setattr(job_routes, "run_generic_job", Mock(return_value=None))
    monkeypatch.setattr(job_routes, "spawn", Mock())
    from src.middleware import capacity_lock

    monkeypatch.setattr(
        capacity_lock, "get_capacity_lock", lambda: Mock(is_locked=lambda _: False)
    )
    monkeypatch.delenv("ERKUNDER_ALLOWED_KEY_SHA256", raising=False)
    app = FastAPI()
    app.include_router(job_routes.router)
    app.include_router(erkunder_routes.router)
    return app


@pytest.mark.parametrize("allowed", ["", "0" * 64])
async def test_disallowed(app, monkeypatch, auftrag, allowed):
    monkeypatch.setenv("ERKUNDER_ALLOWED_KEY_SHA256", allowed)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/v1/jobs",
            json={"kind": "erkunder", "payload": auftrag},
            headers={"Authorization": "Bearer synthetic-key"},
        )
    assert r.status_code == 403
    assert r.json() == {"detail": "API-Schlüssel ist für Erkunder nicht freigegeben"}
    job_routes.store_client.create_job.assert_not_awaited()


async def test_allowed(app, monkeypatch, auftrag):
    monkeypatch.setenv(
        "ERKUNDER_ALLOWED_KEY_SHA256", hashlib.sha256(b"synthetic-key").hexdigest()
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/v1/jobs",
            json={"kind": "erkunder", "payload": auftrag},
            headers={"Authorization": "Bearer synthetic-key"},
        )
    assert r.status_code == 200
    job_routes.store_client.create_job.assert_awaited_once()


async def test_invalid_is_400(app, monkeypatch, auftrag):
    monkeypatch.setenv(
        "ERKUNDER_ALLOWED_KEY_SHA256", hashlib.sha256(b"synthetic-key").hexdigest()
    )
    auftrag["bericht_id"] = "bad"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/v1/jobs",
            json={"kind": "erkunder", "payload": auftrag},
            headers={"Authorization": "Bearer synthetic-key"},
        )
    assert r.status_code == 400
    job_routes.store_client.create_job.assert_not_awaited()


async def test_chat_unchanged(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as c:
        r = await c.post("/v1/jobs", json={"kind": "chat", "payload": {}})
    assert r.status_code == 200
    job_routes.store_client.create_job.assert_awaited_once()


@pytest.mark.parametrize("method,action", [("GET", "ergebnis"), ("POST", "aufraeumen")])
async def test_worker_routes_forbidden(app, method, action):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as c:
        r = await c.request(
            method,
            f"/v1/erkunder/bericht/bericht-test-123/{action}",
            headers={"Authorization": "Bearer synthetic-key"},
        )
    assert r.status_code == 403


@pytest.mark.parametrize("allowed", [False, True])
async def test_library_is_authenticated_and_hashes_fulltext(app, monkeypatch, allowed):
    from src.research_cloud import library

    key = hashlib.sha256(b"synthetic-key").hexdigest() if allowed else ""
    monkeypatch.setenv("ERKUNDER_ALLOWED_KEY_SHA256", key)
    monkeypatch.setattr(library, "load_library_config", Mock())
    fetch = AsyncMock(return_value={"documents": [
        {"id": "kw-stoerung-pumpen", "title": "Pumpen"}, {"id": "unrelated", "title": "Andere"},
    ]})
    monkeypatch.setattr(library, "fetch_library_index", fetch)
    monkeypatch.setattr(library, "fetch_library_document", AsyncMock(return_value={"text": "Prüfwissen"}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as c:
        response = await c.get("/v1/erkunder/bibliothek", headers={"Authorization": "Bearer synthetic-key"})
    assert response.status_code == (200 if allowed else 403)
    if allowed:
        documents = response.json()["dokumente"]
        assert len(documents) == 1
        assert documents[0]["sha256"] == hashlib.sha256("Prüfwissen".encode()).hexdigest()
    else:
        fetch.assert_not_awaited()
