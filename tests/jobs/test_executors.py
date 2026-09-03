"""Tests für die Built-in Job-Executors (src/jobs/executors.py).

Kein Live-Call: httpx + auth_manager werden gemockt. Geprüft wird das Verhalten
des chat_executors — Self-Call-URL, erzwungenes stream=False, Attribution- und
Auth-Header, Fehler-Propagation — sowie der ping_executor.
"""
from __future__ import annotations

import os
os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

from unittest.mock import AsyncMock, patch

import pytest

from src.jobs.executors import (
    chat_executor,
    convert_html_to_pdf_executor,
    ping_executor,
    research_executor,
)


class _FakeResp:
    def __init__(self, status, json_data=None, text="", headers=None):
        self.status_code = status
        self._json = json_data or {}
        self.text = text
        # A real httpx.Response always has headers; the executor reads
        # Retry-After off an error response to schedule a capacity retry
        # (ADR-0012). A fake without them is not standing in for anything.
        self.headers = headers or {}

    def json(self):
        return self._json


class _FakeClient:
    """Stands in for httpx.AsyncClient — async context manager capturing the post."""
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self._resp


async def test_ping_executor_echoes():
    out = await ping_executor({"a": 1}, {"app_id": "x"}, AsyncMock())
    assert out["echo"] == {"a": 1}
    assert out["attribution"] == {"app_id": "x"}


async def test_chat_executor_success_forces_nonstream_and_propagates():
    resp = _FakeResp(200, {"id": "chatcmpl-x", "choices": [{"message": {"content": "hi"}}]})
    fake = _FakeClient(resp)
    progress = []

    async def rp(p):
        progress.append(p)

    with patch("httpx.AsyncClient", return_value=fake), \
         patch("src.auth.auth_manager.get_api_key", return_value="k-123"):
        out = await chat_executor(
            {"model": "claude", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            {"app_id": "werking-report", "user_id": "u1", "workflow_id": "wf"},
            rp,
        )

    assert out == resp._json
    call = fake.calls[0]
    assert call["url"].endswith("/v1/chat/completions")
    assert call["json"]["stream"] is False              # forced non-streaming
    assert call["headers"]["Authorization"] == "Bearer k-123"
    assert call["headers"]["X-App-ID"] == "werking-report"
    assert call["headers"]["X-User-ID"] == "u1"
    assert call["headers"]["X-Workflow-ID"] == "wf"
    assert any(p.get("phase") == "llm" for p in progress)


async def test_chat_executor_http_error_fails_loud():
    fake = _FakeClient(_FakeResp(503, text="Bridge at capacity"))
    with patch("httpx.AsyncClient", return_value=fake), \
         patch("src.auth.auth_manager.get_api_key", return_value="k"):
        with pytest.raises(RuntimeError) as ei:
            await chat_executor({"messages": []}, None, AsyncMock())
    assert "503" in str(ei.value)


async def test_chat_executor_no_api_key_omits_auth_header():
    fake = _FakeClient(_FakeResp(200, {"ok": True}))
    with patch("httpx.AsyncClient", return_value=fake), \
         patch("src.auth.auth_manager.get_api_key", return_value=None):
        await chat_executor({"messages": []}, None, AsyncMock())
    assert "Authorization" not in fake.calls[0]["headers"]


async def test_pdf_executor_success_passes_result_through():
    resp = _FakeResp(200, {"status": "success", "pdf_base64": "JVBERi0=", "size_bytes": 6, "cost": 0})
    fake = _FakeClient(resp)
    progress = []

    async def rp(p):
        progress.append(p)

    with patch("httpx.AsyncClient", return_value=fake), \
         patch("src.auth.auth_manager.get_api_key", return_value="k-123"):
        out = await convert_html_to_pdf_executor(
            {"html": "<html><body>hi</body></html>"},
            {"app_id": "engelmann", "user_id": "u1"},
            rp,
        )

    assert out == resp._json                             # renderer JSON is the job result, 1:1
    call = fake.calls[0]
    assert call["url"].endswith("/v1/convert-html-to-pdf")
    assert call["json"] == {"html": "<html><body>hi</body></html>"}
    assert call["headers"]["Authorization"] == "Bearer k-123"
    assert call["headers"]["X-App-ID"] == "engelmann"
    assert call["headers"]["X-User-ID"] == "u1"
    assert any(p.get("phase") == "render-pdf" for p in progress)


async def test_pdf_executor_rejects_missing_html():
    with pytest.raises(RuntimeError) as ei:
        await convert_html_to_pdf_executor({}, None, AsyncMock())
    assert "html" in str(ei.value)


async def test_pdf_executor_http_error_fails_loud():
    fake = _FakeClient(_FakeResp(502, text="PDF render failed: boom"))
    with patch("httpx.AsyncClient", return_value=fake), \
         patch("src.auth.auth_manager.get_api_key", return_value="k"):
        with pytest.raises(RuntimeError) as ei:
            await convert_html_to_pdf_executor({"html": "<p>x</p>"}, None, AsyncMock())
    assert "502" in str(ei.value)


async def test_pdf_executor_2xx_without_pdf_fails_loud():
    fake = _FakeClient(_FakeResp(200, {"status": "error", "error": "weird"}))
    with patch("httpx.AsyncClient", return_value=fake), \
         patch("src.auth.auth_manager.get_api_key", return_value="k"):
        with pytest.raises(RuntimeError) as ei:
            await convert_html_to_pdf_executor({"html": "<p>x</p>"}, None, AsyncMock())
    assert "no PDF" in str(ei.value)


# ---------------------------------------------------------------------------
# Attribution-Durchreichung im Self-Call
# ---------------------------------------------------------------------------

async def test_selfcall_forwards_full_attribution_incl_job_id():
    """Alle Attribution-Dimensionen des auslösenden Jobs erreichen den Self-Call —
    inkl. job_id (X-Job-ID); ein 'anonymous:<grund>'-Marker bleibt intakt."""
    fake = _FakeClient(_FakeResp(200, {"status": "success", "pdf_base64": "JVBERi0=", "size_bytes": 6}))
    with patch("httpx.AsyncClient", return_value=fake), \
         patch("src.auth.auth_manager.get_api_key", return_value="k"):
        await convert_html_to_pdf_executor(
            {"html": "<p>x</p>"},
            {
                "app_id": "werking-report",
                "user_id": "anonymous:public-check-funnel",
                "agent_id": "check",
                "session_id": "s-1",
                "workflow_id": "wf-1",
                "app_env": "prod",
                "job_id": "job-outer-42",
            },
            AsyncMock(),
        )
    h = fake.calls[0]["headers"]
    assert h["X-App-ID"] == "werking-report"
    assert h["X-User-ID"] == "anonymous:public-check-funnel"
    assert h["X-Agent-ID"] == "check"
    assert h["X-Session-ID"] == "s-1"
    assert h["X-Workflow-ID"] == "wf-1"
    assert h["X-App-Env"] == "prod"
    assert h["X-Job-ID"] == "job-outer-42"
    assert "X-Client-ID" not in h                        # echte App-Identität wird nie überlagert


async def test_selfcall_without_app_id_names_the_job_layer():
    """Job ohne app_id: Self-Call trägt den bridge-jobs-Selfcall-Marker als
    X-Client-ID, damit das Echo nicht als 'unknown' auf dem Zielpfad bucht.
    Der Leak bleibt sichtbar (kein X-User-ID wird fabriziert)."""
    fake = _FakeClient(_FakeResp(200, {"status": "success", "pdf_base64": "JVBERi0=", "size_bytes": 6}))
    with patch("httpx.AsyncClient", return_value=fake), \
         patch("src.auth.auth_manager.get_api_key", return_value="k"):
        await convert_html_to_pdf_executor({"html": "<p>x</p>"}, None, AsyncMock())
    h = fake.calls[0]["headers"]
    assert h["X-Client-ID"] == "bridge-jobs/selfcall"
    assert "X-App-ID" not in h
    assert "X-User-ID" not in h


async def test_chat_executor_4xx_carries_original_status():
    """Ein deterministisches Upstream-400 muss als ExecutorHTTPError mit
    Original-Status hochkommen — sonst kollabiert es beim Client zu einem
    retryablen 502 (Retry-Sturm 2026-07-20)."""
    from src.jobs.executors import ExecutorHTTPError

    fake = _FakeClient(_FakeResp(400, text="Bedrock API error (ValidationException): temperature"))
    with patch("httpx.AsyncClient", return_value=fake), \
         patch("src.auth.auth_manager.get_api_key", return_value="k"):
        with pytest.raises(ExecutorHTTPError) as ei:
            await chat_executor({"messages": []}, None, AsyncMock())
    assert ei.value.status_code == 400
    assert "ValidationException" in str(ei.value)


# ---------------------------------------------------------------------------
# research_executor — /v1/research self-call always answers HTTP 200
# (ResearchResponse.status carries the outcome, never the wire status), so
# the executor must inspect the body itself instead of trusting status<400.
# ---------------------------------------------------------------------------

async def test_research_executor_success_passes_result_through():
    resp = _FakeResp(200, {"status": "success", "content": "# Report", "query": "q", "model": "m"})
    fake = _FakeClient(resp)
    with patch("httpx.AsyncClient", return_value=fake), \
         patch("src.auth.auth_manager.get_api_key", return_value="k"):
        out = await research_executor({"query": "q"}, {"app_id": "werking-report"}, AsyncMock())

    assert out == resp._json
    call = fake.calls[0]
    assert call["url"].endswith("/v1/research")
    assert call["json"]["async_mode"] is False  # forced blocking regardless of caller input


async def test_research_executor_body_status_error_raises_not_marked_done():
    """The defect this guards: a 200 response carrying status='error' was
    previously returned as-is, so registry._run_body called store.mark_done()
    on a FAILED research run — the job read back as 'done' with no content,
    and the real error message (incl. any retryable marker) never reached
    the job's error field at all."""
    resp = _FakeResp(200, {
        "status": "error",
        "query": "q",
        "model": "m",
        "error": 'research-cloud exhausted same-path retries (bridge: HTTP 503, "retryable": true)',
    })
    fake = _FakeClient(resp)
    with patch("httpx.AsyncClient", return_value=fake), \
         patch("src.auth.auth_manager.get_api_key", return_value="k"):
        with pytest.raises(RuntimeError) as ei:
            await research_executor({"query": "q"}, None, AsyncMock())
    assert "retryable" in str(ei.value)
    assert "503" in str(ei.value)


async def test_research_executor_http_error_still_fails_loud():
    fake = _FakeClient(_FakeResp(503, text="Bridge at capacity"))
    with patch("httpx.AsyncClient", return_value=fake), \
         patch("src.auth.auth_manager.get_api_key", return_value="k"):
        with pytest.raises(RuntimeError) as ei:
            await research_executor({"query": "q"}, None, AsyncMock())
    assert "503" in str(ei.value)


async def test_research_executor_body_status_error_reaches_registry_as_job_error():
    """End-to-end through registry._run_body: a status='error' self-call body
    must land the job in status='error' (not 'done'), with the message
    carrying the original error text — this is what lets a retryable marker
    actually reach the polling app."""
    from src.jobs import registry

    resp = _FakeResp(200, {"status": "error", "query": "q", "model": "m", "error": "cap reached (HTTP 503)"})
    fake = _FakeClient(resp)
    recorded = {}

    async def _mark_error(job_id, message, code=None):
        recorded.update({"job_id": job_id, "message": message, "code": code})

    async def _mark_done(job_id, result):
        recorded["wrongly_marked_done"] = True

    with patch("httpx.AsyncClient", return_value=fake), \
         patch("src.auth.auth_manager.get_api_key", return_value="k"), \
         patch.object(registry, "get_executor", return_value=research_executor), \
         patch.object(registry.store_client, "mark_error", _mark_error), \
         patch.object(registry.store_client, "mark_done", _mark_done), \
         patch.object(registry.store_client, "heartbeat", AsyncMock()), \
         patch.object(registry.store_client, "update_progress", AsyncMock()):
        await registry._run_body("job-1", "research", {"query": "q"}, None)

    assert "wrongly_marked_done" not in recorded
    assert recorded["code"] == "EXECUTOR_ERROR"
    assert "HTTP 503" in recorded["message"]


async def test_registry_persists_upstream_status_code():
    """registry._run_body schreibt UPSTREAM_HTTP_<status> statt EXECUTOR_ERROR,
    wenn der Executor einen ExecutorHTTPError wirft."""
    from src.jobs import registry
    from src.jobs.executors import ExecutorHTTPError

    async def _failing_executor(payload, attribution, report_progress):
        raise ExecutorHTTPError(400, "chat self-call failed HTTP 400: nope")

    recorded = {}

    async def _mark_error(job_id, message, code=None):
        recorded.update({"job_id": job_id, "message": message, "code": code})

    with patch.object(registry, "get_executor", return_value=_failing_executor), \
         patch.object(registry.store_client, "mark_error", _mark_error), \
         patch.object(registry.store_client, "heartbeat", AsyncMock()), \
         patch.object(registry.store_client, "update_progress", AsyncMock()):
        await registry._run_body("job-1", "chat", {}, None)

    assert recorded["code"] == "UPSTREAM_HTTP_400"
    assert "HTTP 400" in recorded["message"]
