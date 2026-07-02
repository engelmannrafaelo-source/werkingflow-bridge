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

from src.jobs.executors import chat_executor, convert_html_to_pdf_executor, ping_executor


class _FakeResp:
    def __init__(self, status, json_data=None, text=""):
        self.status_code = status
        self._json = json_data or {}
        self.text = text

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
