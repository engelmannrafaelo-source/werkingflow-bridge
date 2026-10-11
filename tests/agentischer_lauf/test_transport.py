import json
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.models import ResearchRequest, ResearchResponse
from src.agentischer_lauf import http
from src.error_contract import JobFailure, job_error_fields
from src.jobs.executors import research_executor
from src.jobs import store, store_client
from src.internal_routes import InternalJobError, internal_mark_error
from .test_contract import payload
from .test_runtime import runtime, reply


async def test_error_executor_through_storage(tmp_path):
    result = {
        "status": "error",
        "error": "technical failure",
        "fehler_code": "antwort_abgeschnitten",
        "antwort_ende": "max_tokens_fortgesetzt",
        "retryable": False,
        "retry_after_s": None,
        "usage": {"input_tokens": 3},
        "diagnose_ref": "/v1/research/run-1/content?lauf_artifact=x",
    }
    with patch("src.jobs.executors._self_post_json", AsyncMock(return_value=result)):
        with pytest.raises(JobFailure) as caught:
            await research_executor(payload(), {}, AsyncMock())
    fields = job_error_fields(caught.value)
    assert fields["code"] == "EXECUTOR_ERROR"
    assert fields["fehler_code"] == result["fehler_code"]
    assert fields["antwort_ende"] == result["antwort_ende"]
    assert fields["usage"] == result["usage"]
    assert fields["retryable"] is False

    async def platform(method, path, json, timeout_s):
        body = InternalJobError(**json)
        result = await internal_mark_error("job-test", body, _claims=None)
        return type("Response", (), {"status_code": 200, "json": result})()

    conn = AsyncMock()
    conn.execute.return_value = "UPDATE 1"
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    with (
        patch.object(store_client, "call_platform", platform),
        patch.object(store, "get_pool", return_value=pool),
    ):
        await store_client.mark_error("job-test", "technical failure", **fields)
    saved = json.loads(conn.execute.call_args.args[2])
    assert saved == {"message": "technical failure", **fields}


def app():
    server = FastAPI()

    @server.post("/v1/research", response_model=ResearchResponse)
    @http.research_dispatch
    async def research(request_body: ResearchRequest, request: Request, credentials=None):
        return {
            "status": "success",
            "query": request_body.query,
            "model": "legacy",
            "content": "legacy",
        }

    @server.get("/v1/research/{session_id}/content")
    @http.content_dispatch
    async def content(session_id: str, request: Request, credentials=None):
        return "legacy"

    return server


def test_http_schema_and_capability_no_legacy_fallback(monkeypatch):
    monkeypatch.setattr(http, "authorized", AsyncMock())
    monkeypatch.setattr(http, "_runtime", None)
    with TestClient(app()) as client:
        assert client.post("/v1/research", json={"query": "legacy"}).status_code == 200
        out = client.post("/v1/research", json=payload())
        assert out.status_code == 422
        assert out.json()["detail"]["code"] == "faehigkeit_nicht_verfuegbar"
        invalid = payload(max_tokens=True)
        assert client.post("/v1/research", json=invalid).status_code == 422


def test_http_success_auth_conflict(monkeypatch, tmp_path):
    rt, sdk = runtime(tmp_path, [reply()])
    monkeypatch.setattr(http, "authorized", AsyncMock())
    monkeypatch.setattr(http, "_runtime", rt)
    monkeypatch.setattr(http, "_admit", AsyncMock(return_value="verified-tenant"))
    with TestClient(app()) as client:
        response = client.post("/v1/research", json=payload())
        assert response.status_code == 200 and response.json()["content"] == "alpha"
        diagnostic = response.json()["diagnose_ref"]
        assert client.get(diagnostic).status_code == 200
        http._admit.return_value = "other-tenant"
        assert client.get(diagnostic).status_code == 404
        http._admit.return_value = "verified-tenant"
        assert client.post("/v1/research", json=payload(query="changed")).status_code == 409
