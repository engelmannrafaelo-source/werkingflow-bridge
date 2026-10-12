"""BRREFUSAL (FK32 b): a refused job ends in ai_jobs.error with its reason —
against a real Postgres (BRIDGE_TEST_PG_URL, real migrations).

Chain: the endpoint's real answer (chat: the 422 envelope of
CliModellAbbruch; research / doc-agent: status=error with fehler_code) →
executor → runner → store_client → platform-api internal route (ASGI) →
store.mark_error → the row → GET /v1/jobs/{id}. A capacity 429 on the same
chain is still parked, never written as an error."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.jobs import executors, registry, store_client
from tests.jobs.test_error_verdict_br9 import _main, _platform_app, _poll
from tests.jobs.test_job_cancel_br10 import JOB, _insert, pg_store  # noqa: F401

_main()  # claude_code_sdk stand-ins before src.middleware imports main's deps

from src.middleware.bridge_error import (  # noqa: E402
    CliApiError,
    CliModellAbbruch,
    classify_exception,
)

REFUSAL = {"stop_reason": "refusal", "category": "reasoning_extraction",
           "quelle": "model_refusal_no_fallback"}


def _envelope_response(resp) -> httpx.Response:
    return httpx.Response(resp.status_code, content=resp.body,
                          headers={"content-type": "application/json", **dict(resp.headers)})


async def _lauf(kind: str, answer: httpx.Response, pool):
    app = _platform_app()

    async def call(method, path, json=None, timeout_s=None):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.request(method, path, json=json)
        return SimpleNamespace(status_code=r.status_code,
                               json=r.json() if r.content else None)

    async def fake_post(client, path, body, headers):
        return answer, None

    defer = AsyncMock()
    with patch.object(store_client, "call_platform", call), \
         patch.object(executors, "_post_with_capacity_redispatch", fake_post), \
         patch("src.auth.auth_manager.get_api_key", return_value="k"), \
         patch.multiple(store_client, mark_running=AsyncMock(), heartbeat=AsyncMock(),
                        update_progress=AsyncMock(), defer_job=defer,
                        get_job=AsyncMock(return_value={"defer_count": 0})):
        registry.register_executor(kind, {
            "chat": executors.chat_executor,
            "research": executors.research_executor,
            "doc-agent": executors.doc_agent_executor,
        }[kind])
        await registry.run_generic_job(JOB, kind, {"query": "q"}, None)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status, error FROM ai_jobs WHERE job_id = $1", JOB)
    return row, defer


@pytest.mark.parametrize("kind", ["chat", "research", "doc-agent"])
async def test_refusal_endet_im_ai_jobs_error_mit_grund(pg_store, kind, monkeypatch):  # noqa: F811
    await _insert(pg_store, JOB, "running")
    if kind == "chat":
        answer = _envelope_response(classify_exception(
            CliModellAbbruch(None, "unknown", "safeguards flagged", "w1", REFUSAL)))
        code = "UPSTREAM_HTTP_422"
    else:
        answer = httpx.Response(200, json={
            "status": "error", "query": "q", "model": "m", "question": "q",
            "error": "refused", "fehler_code": "modell_abbruch",
            "retryable": False, "retry_after_s": None})
        code = "EXECUTOR_ERROR"
    row, defer = await _lauf(kind, answer, pg_store)
    assert not defer.called
    assert row["status"] == "error"
    error = json.loads(row["error"]) if isinstance(row["error"], str) else row["error"]
    assert error["code"] == code
    assert error["retryable"] is False and error["retry_after_s"] is None
    assert error["fehler_code"] == "modell_abbruch"
    out = await _poll({"kind": kind, "status": "error", "error": error,
                       "created_at": None, "updated_at": None, "progress": None,
                       "result": None, "deferred_until": None, "defer_count": 0,
                       "defer_reason": None}, monkeypatch)
    assert out["error"]["fehler_code"] == "modell_abbruch"
    assert out["error"]["retryable"] is False


async def test_kapazitaet_bleibt_zurueckgestellt(pg_store):  # noqa: F811
    await _insert(pg_store, JOB, "running")
    answer = _envelope_response(
        classify_exception(CliApiError(529, "unknown", "API Error: 529", "w1")))
    row, defer = await _lauf("chat", answer, pg_store)
    assert defer.called
    assert row["status"] == "running" and row["error"] is None
