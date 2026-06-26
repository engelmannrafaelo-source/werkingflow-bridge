"""
Unit tests for activity tracking in the 5 infra endpoints that bypass
chat_completions / research but still appear in the Platform Admin overview.

Endpoints covered:
  - /v1/privacy/smart-anonymize  → agent_id="anonymisierung", model="privacy-service"
  - /v1/convert-html-to-pdf      → agent_id="pdf-export",    model="html-renderer"
  - /v1/convert-html-to-screenshot → agent_id="screenshot",  model="html-renderer"
  - /v1/document/convert         → agent_id="dokument-konvertierung", model="docling"
  - /v1/audio/transcriptions     → agent_id="transkription",  model=whisper model from form

Each test patches:
  - persist_ai_call_activity → AsyncMock (assert called / not called)
  - The downstream client (privacy_client / httpx) → MagicMock/AsyncMock
  - extract_attribution_context → returns a fixed dict
  - verify_api_key → AsyncMock no-op

Tests verify:
  - Success path: persist called with status="success", correct agent_id/model
  - Error path:   persist called with status="error"
  - Non-blocking: if persist raises, the handler response is still returned
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock as _MM

# ---------------------------------------------------------------------------
# Stub ALL heavy deps before any src.* import
# ---------------------------------------------------------------------------
for _mod in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
    "src.identity.routes",
    "src.db.client",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = _MM()

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import src.main  # noqa: E402 — safe after stubs above


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _fake_attr(**overrides):
    base = {
        "user_id": "user-uuid-999",
        "app_id": "werking-report",
        "agent_id": None,
        "workflow_id": None,
        "app_env": "dev",
        "session_id": None,
        "job_id": None,
    }
    base.update(overrides)
    return base


def _mock_request(headers=None):
    req = MagicMock()
    req.headers = headers or {}
    return req


def _mock_request_body(text="hello", language="de"):
    rb = MagicMock()
    rb.text = text
    rb.language = language
    rb.context_hint = None
    rb.prefix = None
    return rb


def _make_httpx_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_data or {"status": "ok"})
    resp.raise_for_status = MagicMock()
    return resp


def _privacy_client_ctx(response):
    """Return a mock privacy_client whose _get_client() gives an async ctx manager."""
    inner_client = AsyncMock()
    inner_client.post = AsyncMock(return_value=response)
    pc = AsyncMock()
    pc._get_client = AsyncMock(return_value=inner_client)
    return pc


# ---------------------------------------------------------------------------
# /v1/privacy/smart-anonymize
# ---------------------------------------------------------------------------

class TestSmartAnonymizeTracking:
    @pytest.mark.asyncio
    async def test_success_logs_anonymisierung(self):
        mock_persist = AsyncMock()
        fake_resp = _make_httpx_response(200, {"status": "success", "text": "anon", "mapping": {}, "entities": []})
        pc = _privacy_client_ctx(fake_resp)

        with (
            patch("src.main.get_privacy_client", return_value=pc),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
            patch.dict("os.environ", {"BRIDGE_ANONYMIZE_ENABLED": "true"}),
        ):
            await src.main.smart_anonymize_endpoint(
                request=_mock_request(),
                request_body=_mock_request_body(),
            )

        mock_persist.assert_awaited_once()
        call_kwargs = mock_persist.call_args.kwargs
        assert call_kwargs["agent_id"] == "anonymisierung"
        assert call_kwargs["model"] == "privacy-service"
        assert call_kwargs["status"] == "success"
        assert call_kwargs["input_tokens"] == 0
        assert call_kwargs["output_tokens"] == 0

    @pytest.mark.asyncio
    async def test_error_logs_anonymisierung_error(self):
        mock_persist = AsyncMock()
        pc = AsyncMock()
        pc._get_client = AsyncMock(side_effect=RuntimeError("privacy-service down"))

        with (
            patch("src.main.get_privacy_client", return_value=pc),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
            patch.dict("os.environ", {"BRIDGE_ANONYMIZE_ENABLED": "true"}),
        ):
            result = await src.main.smart_anonymize_endpoint(
                request=_mock_request(),
                request_body=_mock_request_body(),
            )

        assert result.status == "error"
        mock_persist.assert_awaited_once()
        assert mock_persist.call_args.kwargs["status"] == "error"
        assert mock_persist.call_args.kwargs["agent_id"] == "anonymisierung"

    @pytest.mark.asyncio
    async def test_tracking_failure_does_not_break_response(self):
        """If persist raises, the response is still returned (non-blocking contract)."""
        mock_persist = AsyncMock(side_effect=Exception("DB exploded"))
        fake_resp = _make_httpx_response(200, {"status": "success", "text": "x", "mapping": {}, "entities": []})
        pc = _privacy_client_ctx(fake_resp)

        with (
            patch("src.main.get_privacy_client", return_value=pc),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
            patch.dict("os.environ", {"BRIDGE_ANONYMIZE_ENABLED": "true"}),
        ):
            result = await src.main.smart_anonymize_endpoint(
                request=_mock_request(),
                request_body=_mock_request_body(),
            )

        # response must still come through despite persist failing
        assert result.status != "error" or result.error is None or "DB" not in (result.error or "")
        mock_persist.assert_awaited_once()


# ---------------------------------------------------------------------------
# /v1/convert-html-to-pdf
# ---------------------------------------------------------------------------

class TestConvertHtmlToPdfTracking:
    @pytest.mark.asyncio
    async def test_success_logs_pdf_export(self):
        mock_persist = AsyncMock()
        fake_resp = _make_httpx_response(200, {"status": "ok", "pdf_base64": "abc"})
        pc = _privacy_client_ctx(fake_resp)

        mock_request = AsyncMock()
        mock_request.json = AsyncMock(return_value={"html": "<p>hi</p>"})
        mock_request.headers = {}

        with (
            patch("src.main.verify_api_key", new=AsyncMock()),
            patch("src.main.get_privacy_client", return_value=pc),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
        ):
            await src.main.convert_html_to_pdf_endpoint(
                request=mock_request,
                credentials=None,
            )

        mock_persist.assert_awaited_once()
        kw = mock_persist.call_args.kwargs
        assert kw["agent_id"] == "pdf-export"
        assert kw["model"] == "html-renderer"
        assert kw["status"] == "success"
        assert kw["input_tokens"] == 0
        assert kw["output_tokens"] == 0

    @pytest.mark.asyncio
    async def test_error_logs_pdf_export_error(self):
        mock_persist = AsyncMock()
        pc = AsyncMock()
        pc._get_client = AsyncMock(side_effect=RuntimeError("chromium crashed"))

        mock_request = AsyncMock()
        mock_request.json = AsyncMock(return_value={"html": "<p>hi</p>"})
        mock_request.headers = {}

        with (
            patch("src.main.verify_api_key", new=AsyncMock()),
            patch("src.main.get_privacy_client", return_value=pc),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
        ):
            from starlette.responses import JSONResponse
            resp = await src.main.convert_html_to_pdf_endpoint(
                request=mock_request,
                credentials=None,
            )

        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 500
        mock_persist.assert_awaited_once()
        assert mock_persist.call_args.kwargs["status"] == "error"

    @pytest.mark.asyncio
    async def test_tracking_failure_does_not_break_response(self):
        mock_persist = AsyncMock(side_effect=Exception("tracking broke"))
        fake_resp = _make_httpx_response(200, {"status": "ok"})
        pc = _privacy_client_ctx(fake_resp)

        mock_request = AsyncMock()
        mock_request.json = AsyncMock(return_value={"html": "<p>hi</p>"})
        mock_request.headers = {}

        with (
            patch("src.main.verify_api_key", new=AsyncMock()),
            patch("src.main.get_privacy_client", return_value=pc),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
        ):
            from starlette.responses import JSONResponse
            resp = await src.main.convert_html_to_pdf_endpoint(
                request=mock_request,
                credentials=None,
            )

        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 200
        mock_persist.assert_awaited_once()


# ---------------------------------------------------------------------------
# /v1/convert-html-to-screenshot
# ---------------------------------------------------------------------------

class TestConvertHtmlToScreenshotTracking:
    @pytest.mark.asyncio
    async def test_success_logs_screenshot(self):
        mock_persist = AsyncMock()
        fake_resp = _make_httpx_response(200, {"status": "ok", "image_base64": "xyz"})
        pc = _privacy_client_ctx(fake_resp)

        mock_request = AsyncMock()
        mock_request.json = AsyncMock(return_value={"html": "<p>hi</p>"})
        mock_request.headers = {}

        with (
            patch("src.main.verify_api_key", new=AsyncMock()),
            patch("src.main.get_privacy_client", return_value=pc),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
        ):
            await src.main.convert_html_to_screenshot_endpoint(
                request=mock_request,
                credentials=None,
            )

        mock_persist.assert_awaited_once()
        kw = mock_persist.call_args.kwargs
        assert kw["agent_id"] == "screenshot"
        assert kw["model"] == "html-renderer"
        assert kw["status"] == "success"

    @pytest.mark.asyncio
    async def test_error_logs_screenshot_error(self):
        mock_persist = AsyncMock()
        pc = AsyncMock()
        pc._get_client = AsyncMock(side_effect=RuntimeError("chromium timeout"))

        mock_request = AsyncMock()
        mock_request.json = AsyncMock(return_value={"html": "<p>hi</p>"})
        mock_request.headers = {}

        with (
            patch("src.main.verify_api_key", new=AsyncMock()),
            patch("src.main.get_privacy_client", return_value=pc),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
        ):
            from starlette.responses import JSONResponse
            resp = await src.main.convert_html_to_screenshot_endpoint(
                request=mock_request,
                credentials=None,
            )

        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 500
        mock_persist.assert_awaited_once()
        assert mock_persist.call_args.kwargs["status"] == "error"
        assert mock_persist.call_args.kwargs["agent_id"] == "screenshot"


# ---------------------------------------------------------------------------
# /v1/document/convert
# ---------------------------------------------------------------------------

class TestDocumentConvertTracking:
    @pytest.mark.asyncio
    async def test_success_logs_dokument_konvertierung(self):
        mock_persist = AsyncMock()

        from starlette.responses import JSONResponse as _JSONResponse
        fake_result = _JSONResponse(content={"success": True, "markdown": "# Hi"}, status_code=200)

        mock_request = MagicMock()
        mock_request.headers = {}

        with (
            patch("src.main.verify_api_key", new=AsyncMock()),
            patch("src.main._proxy_document_endpoint", new=AsyncMock(return_value=fake_result)),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
        ):
            result = await src.main.convert_document_endpoint(
                request=mock_request,
                credentials=None,
            )

        assert result.status_code == 200
        mock_persist.assert_awaited_once()
        kw = mock_persist.call_args.kwargs
        assert kw["agent_id"] == "dokument-konvertierung"
        assert kw["model"] == "docling"
        assert kw["status"] == "success"
        assert kw["input_tokens"] == 0
        assert kw["output_tokens"] == 0

    @pytest.mark.asyncio
    async def test_error_status_code_logs_error(self):
        mock_persist = AsyncMock()

        from starlette.responses import JSONResponse as _JSONResponse
        fake_result = _JSONResponse(content={"error": "unsupported"}, status_code=415)

        mock_request = MagicMock()
        mock_request.headers = {}

        with (
            patch("src.main.verify_api_key", new=AsyncMock()),
            patch("src.main._proxy_document_endpoint", new=AsyncMock(return_value=fake_result)),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
        ):
            result = await src.main.convert_document_endpoint(
                request=mock_request,
                credentials=None,
            )

        assert result.status_code == 415
        mock_persist.assert_awaited_once()
        kw = mock_persist.call_args.kwargs
        assert kw["status"] == "error"
        assert kw["error_code"] == "415"
        assert kw["agent_id"] == "dokument-konvertierung"

    @pytest.mark.asyncio
    async def test_tracking_failure_does_not_break_response(self):
        mock_persist = AsyncMock(side_effect=Exception("DB gone"))

        from starlette.responses import JSONResponse as _JSONResponse
        fake_result = _JSONResponse(content={"success": True}, status_code=200)

        mock_request = MagicMock()
        mock_request.headers = {}

        with (
            patch("src.main.verify_api_key", new=AsyncMock()),
            patch("src.main._proxy_document_endpoint", new=AsyncMock(return_value=fake_result)),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
        ):
            result = await src.main.convert_document_endpoint(
                request=mock_request,
                credentials=None,
            )

        assert result.status_code == 200
        mock_persist.assert_awaited_once()


# ---------------------------------------------------------------------------
# /v1/audio/transcriptions
# ---------------------------------------------------------------------------

class TestAudioTranscriptionsTracking:
    def _make_form(self, model="whisper-1"):
        audio_file = MagicMock()
        audio_file.filename = "test.mp3"
        audio_file.content_type = "audio/mpeg"
        audio_file.read = AsyncMock(return_value=b"audio-bytes")

        form = MagicMock()
        form.get = MagicMock(side_effect=lambda key, default=None: {
            "file": audio_file,
            "model": model,
            "response_format": "json",
            "language": None,
            "prompt": None,
            "temperature": None,
        }.get(key, default))
        return form

    @pytest.mark.asyncio
    async def test_success_logs_transkription(self):
        mock_persist = AsyncMock()
        form = self._make_form(model="whisper-1")

        mock_request = AsyncMock()
        mock_request.form = AsyncMock(return_value=form)
        mock_request.headers = {}

        fake_http_resp = MagicMock()
        fake_http_resp.status_code = 200
        fake_http_resp.json = MagicMock(return_value={"text": "Hello world"})
        fake_http_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_http_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("src.main.verify_api_key", new=AsyncMock()),
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}),
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
        ):
            from starlette.responses import JSONResponse
            resp = await src.main.audio_transcriptions(
                request=mock_request,
                credentials=None,
            )

        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 200
        mock_persist.assert_awaited_once()
        kw = mock_persist.call_args.kwargs
        assert kw["agent_id"] == "transkription"
        assert kw["model"] == "whisper-1"
        assert kw["status"] == "success"
        assert kw["input_tokens"] == 0
        assert kw["output_tokens"] == 0

    @pytest.mark.asyncio
    async def test_httpx_error_logs_transkription_error(self):
        import httpx
        mock_persist = AsyncMock()
        form = self._make_form()

        mock_request = AsyncMock()
        mock_request.form = AsyncMock(return_value=form)
        mock_request.headers = {}

        fake_error_resp = MagicMock()
        fake_error_resp.status_code = 401
        fake_error_resp.text = "Unauthorized"
        httpx_error = httpx.HTTPStatusError(
            "401", request=MagicMock(), response=fake_error_resp
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx_error)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        from fastapi import HTTPException as _HTTPException
        with (
            patch("src.main.verify_api_key", new=AsyncMock()),
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}),
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
            pytest.raises(_HTTPException),
        ):
            await src.main.audio_transcriptions(
                request=mock_request,
                credentials=None,
            )

        mock_persist.assert_awaited_once()
        kw = mock_persist.call_args.kwargs
        assert kw["status"] == "error"
        assert kw["agent_id"] == "transkription"
        assert kw["error_code"] == "401"

    @pytest.mark.asyncio
    async def test_tracking_failure_does_not_break_success_response(self):
        mock_persist = AsyncMock(side_effect=Exception("DB gone"))
        form = self._make_form()

        mock_request = AsyncMock()
        mock_request.form = AsyncMock(return_value=form)
        mock_request.headers = {}

        fake_http_resp = MagicMock()
        fake_http_resp.status_code = 200
        fake_http_resp.json = MagicMock(return_value={"text": "ok"})
        fake_http_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=fake_http_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("src.main.verify_api_key", new=AsyncMock()),
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}),
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
        ):
            from starlette.responses import JSONResponse
            resp = await src.main.audio_transcriptions(
                request=mock_request,
                credentials=None,
            )

        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 200
        mock_persist.assert_awaited_once()
