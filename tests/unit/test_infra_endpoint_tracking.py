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

import httpx
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


def _make_httpx_status_error(status_code: int, detail: str) -> httpx.HTTPStatusError:
    """A real httpx.HTTPStatusError carrying a JSON {"detail": ...} body, the
    shape FastAPI's HTTPException produces (e.g. the smart-anonymize
    admission-control 503)."""
    request = httpx.Request("POST", "http://privacy-service:8100/smart-anonymize")
    response = httpx.Response(status_code, json={"detail": detail}, request=request)
    return httpx.HTTPStatusError(f"{status_code} error", request=request, response=response)


class _FakeTrackCall:
    """Stand-in for PrivacyServiceClient.track_call(): an async context
    manager yielding a fixed concurrency count (0 = no overlap seen)."""
    def __init__(self, concurrent_before=0):
        self._concurrent_before = concurrent_before

    async def __aenter__(self):
        return self._concurrent_before

    async def __aexit__(self, *exc_info):
        return False


def _privacy_client_ctx(response):
    """Return a mock privacy_client whose _get_client() gives an async ctx manager."""
    inner_client = AsyncMock()
    inner_client.post = AsyncMock(return_value=response)
    pc = AsyncMock()
    pc._get_client = AsyncMock(return_value=inner_client)
    pc.track_call = MagicMock(return_value=_FakeTrackCall())
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
            patch("src.main.verify_api_key", new=AsyncMock()),
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
            patch("src.main.verify_api_key", new=AsyncMock()),
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
            patch("src.main.verify_api_key", new=AsyncMock()),
            patch.dict("os.environ", {"BRIDGE_ANONYMIZE_ENABLED": "true"}),
        ):
            result = await src.main.smart_anonymize_endpoint(
                request=_mock_request(),
                request_body=_mock_request_body(),
            )

        # response must still come through despite persist failing
        assert result.status != "error" or result.error is None or "DB" not in (result.error or "")
        mock_persist.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_downstream_503_surfaces_privacy_service_detail(self):
        """A privacy-service HTTPException (e.g. admission-control 503) must
        surface its actual `detail` message, not a generic httpx status string —
        otherwise a caller sees 'Client error 503 ...' instead of the reason."""
        mock_persist = AsyncMock()
        fake_resp = _make_httpx_response(503)
        fake_resp.raise_for_status = MagicMock(
            side_effect=_make_httpx_status_error(
                503,
                "smart-anonymize at capacity: no free analysis slot after 600s "
                "(max 2 concurrent). Retry later.",
            )
        )
        pc = _privacy_client_ctx(fake_resp)

        with (
            patch("src.main.get_privacy_client", return_value=pc),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
            patch("src.main.verify_api_key", new=AsyncMock()),
            patch.dict("os.environ", {"BRIDGE_ANONYMIZE_ENABLED": "true"}),
        ):
            result = await src.main.smart_anonymize_endpoint(
                request=_mock_request(),
                request_body=_mock_request_body(),
            )

        assert result.status == "error"
        assert "at capacity" in result.error
        assert "503" in result.error


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

    @pytest.mark.asyncio
    async def test_openai_stt_model_override(self):
        """OPENAI_STT_MODEL overrides the app-sent model id for OpenAI-compatible EU
        providers (e.g. Scaleway wants whisper-large-v3; the apps send whisper-1)."""
        mock_persist = AsyncMock()
        form = self._make_form(model="whisper-1")
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
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test",
                                      "OPENAI_STT_MODEL": "whisper-large-v3"}),
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
        ):
            await src.main.audio_transcriptions(request=mock_request, credentials=None)

        posted = mock_client.post.call_args.kwargs["data"]
        assert posted["model"] == "whisper-large-v3"  # override, not the app's whisper-1

    @pytest.mark.asyncio
    async def test_no_override_keeps_app_model(self):
        """Without OPENAI_STT_MODEL the app's model id is forwarded unchanged (default US)."""
        mock_persist = AsyncMock()
        form = self._make_form(model="whisper-1")
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

        import os as _os
        _os.environ.pop("OPENAI_STT_MODEL", None)
        with (
            patch("src.main.verify_api_key", new=AsyncMock()),
            patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}),
            patch("httpx.AsyncClient", return_value=mock_client),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
        ):
            await src.main.audio_transcriptions(request=mock_request, credentials=None)

        posted = mock_client.post.call_args.kwargs["data"]
        assert posted["model"] == "whisper-1"


# ---------------------------------------------------------------------------
# STT EU data-residency: provider resolution + SageMaker Whisper (aws-sagemaker)
# ---------------------------------------------------------------------------
import src.auth  # noqa: E402 — singleton bedrock_credential_manager, already loaded via src.main


class TestSTTProviderResolution:
    """_resolve_stt_provider() — env-driven EU provider dispatch. Fail-loud, NO
    silent US fallback (GDPR). Default stays OpenAI-US (migration off-by-default)."""

    def test_openai_default_us(self, monkeypatch):
        monkeypatch.delenv("STT_PROVIDER", raising=False)
        monkeypatch.delenv("OPENAI_STT_BASE_URL", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        cfg = src.main._resolve_stt_provider()
        assert cfg == {
            "kind": "proxy", "provider": "openai",
            "url": "https://api.openai.com/v1/audio/transcriptions",
            "headers": {"Authorization": "Bearer sk-x"},
        }

    def test_openai_eu_base_url_override(self, monkeypatch):
        monkeypatch.delenv("STT_PROVIDER", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-eu")
        monkeypatch.setenv("OPENAI_STT_BASE_URL", "https://eu.api.openai.com/v1")
        cfg = src.main._resolve_stt_provider()
        assert cfg["url"] == "https://eu.api.openai.com/v1/audio/transcriptions"

    def test_openai_missing_key_fails_loud(self, monkeypatch):
        monkeypatch.delenv("STT_PROVIDER", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(src.main.BridgeError) as ei:
            src.main._resolve_stt_provider()
        assert b"OPENAI_API_KEY" in ei.value.response.body

    def test_aws_sagemaker_missing_all_fails_loud(self, monkeypatch):
        monkeypatch.setenv("STT_PROVIDER", "aws-sagemaker")
        monkeypatch.delenv("AWS_STT_SAGEMAKER_ENDPOINT", raising=False)
        with (
            patch.object(src.auth.bedrock_credential_manager, "aws_access_key", None),
            patch.object(src.auth.bedrock_credential_manager, "aws_secret_key", None),
        ):
            with pytest.raises(src.main.BridgeError) as ei:
                src.main._resolve_stt_provider()
        body = ei.value.response.body
        assert b"AWS_STT_SAGEMAKER_ENDPOINT" in body and b"AWS credentials" in body

    def test_aws_sagemaker_missing_endpoint_only(self, monkeypatch):
        monkeypatch.setenv("STT_PROVIDER", "aws-sagemaker")
        monkeypatch.delenv("AWS_STT_SAGEMAKER_ENDPOINT", raising=False)
        with (
            patch.object(src.auth.bedrock_credential_manager, "aws_access_key", "AK"),
            patch.object(src.auth.bedrock_credential_manager, "aws_secret_key", "SK"),
        ):
            with pytest.raises(src.main.BridgeError) as ei:
                src.main._resolve_stt_provider()
        body = ei.value.response.body
        assert b"AWS_STT_SAGEMAKER_ENDPOINT" in body and b"AWS credentials" not in body

    def test_aws_sagemaker_configured_default_region(self, monkeypatch):
        monkeypatch.setenv("STT_PROVIDER", "aws-sagemaker")
        monkeypatch.setenv("AWS_STT_SAGEMAKER_ENDPOINT", "whisper-eu")
        monkeypatch.delenv("AWS_STT_REGION", raising=False)
        with (
            patch.object(src.auth.bedrock_credential_manager, "aws_access_key", "AK"),
            patch.object(src.auth.bedrock_credential_manager, "aws_secret_key", "SK"),
            patch.object(src.auth.bedrock_credential_manager, "default_region", "eu-central-1"),
        ):
            cfg = src.main._resolve_stt_provider()
        assert cfg["kind"] == "aws-sagemaker"
        assert cfg["provider"] == "aws-sagemaker"
        assert cfg["endpoint_name"] == "whisper-eu"
        assert cfg["region"] == "eu-central-1"
        assert cfg["access_key"] == "AK" and cfg["secret_key"] == "SK"

    def test_aws_sagemaker_region_override(self, monkeypatch):
        monkeypatch.setenv("STT_PROVIDER", "aws-sagemaker")
        monkeypatch.setenv("AWS_STT_SAGEMAKER_ENDPOINT", "whisper-eu")
        monkeypatch.setenv("AWS_STT_REGION", "eu-west-1")
        with (
            patch.object(src.auth.bedrock_credential_manager, "aws_access_key", "AK"),
            patch.object(src.auth.bedrock_credential_manager, "aws_secret_key", "SK"),
        ):
            cfg = src.main._resolve_stt_provider()
        assert cfg["region"] == "eu-west-1"

    def test_unknown_provider_fails_loud(self, monkeypatch):
        monkeypatch.setenv("STT_PROVIDER", "bogus")
        with pytest.raises(src.main.BridgeError) as ei:
            src.main._resolve_stt_provider()
        assert b"aws-sagemaker" in ei.value.response.body


class TestSagemakerTranscribeSync:
    """_sagemaker_transcribe_sync() — synchronous SageMaker Whisper invoke, reuses
    the Bedrock AWS creds (SigV4 via boto3), normalizes to OpenAI-shaped {"text": ...},
    fail-loud on any contract mismatch (never a silent wrong result)."""

    @staticmethod
    def _fake_client(payload_bytes):
        body = MagicMock()
        body.read = MagicMock(return_value=payload_bytes)
        client = MagicMock()
        client.invoke_endpoint = MagicMock(return_value={"Body": body})
        return client

    def test_dict_text_normalized_and_creds_forwarded(self):
        client = self._fake_client(b'{"text": "hallo welt"}')
        with patch("boto3.client", return_value=client) as mk:
            out = src.main._sagemaker_transcribe_sync(
                endpoint_name="whisper-eu", region="eu-central-1",
                access_key="AK", secret_key="SK",
                audio_bytes=b"AUDIO", content_type="audio/webm",
            )
        assert out == {"text": "hallo welt"}
        mk.assert_called_once()
        ck = mk.call_args.kwargs
        assert ck["region_name"] == "eu-central-1"
        assert ck["aws_access_key_id"] == "AK" and ck["aws_secret_access_key"] == "SK"
        iek = client.invoke_endpoint.call_args.kwargs
        assert iek["EndpointName"] == "whisper-eu"
        assert iek["Body"] == b"AUDIO"
        assert iek["ContentType"] == "audio/webm"

    def test_list_wrapped_text_normalized(self):
        client = self._fake_client(b'[{"text": "aus liste"}]')
        with patch("boto3.client", return_value=client):
            out = src.main._sagemaker_transcribe_sync(
                endpoint_name="e", region="eu-central-1", access_key="AK",
                secret_key="SK", audio_bytes=b"A", content_type="audio/wav",
            )
        assert out == {"text": "aus liste"}

    def test_default_content_type_when_missing(self):
        client = self._fake_client(b'{"text": "x"}')
        with patch("boto3.client", return_value=client):
            src.main._sagemaker_transcribe_sync(
                endpoint_name="e", region="eu-central-1", access_key="AK",
                secret_key="SK", audio_bytes=b"A", content_type="",
            )
        assert client.invoke_endpoint.call_args.kwargs["ContentType"] == "audio/wav"

    @pytest.mark.parametrize("payload", [
        b"<html>not json</html>",   # non-JSON body
        b'{"foo": 1}',              # JSON without text
        b'{"text": 123}',           # text not a string
    ])
    def test_contract_mismatch_fails_loud(self, payload):
        client = self._fake_client(payload)
        with patch("boto3.client", return_value=client):
            with pytest.raises(RuntimeError):
                src.main._sagemaker_transcribe_sync(
                    endpoint_name="e", region="eu-central-1", access_key="AK",
                    secret_key="SK", audio_bytes=b"A", content_type="audio/wav",
                )


class TestAudioTranscriptionsAwsSagemaker:
    """Endpoint dispatch for STT_PROVIDER=aws-sagemaker: routes to SageMaker Whisper
    (EU) and still tracks activity as agent_id=transkription."""

    def _make_form(self, model="whisper-1"):
        audio_file = MagicMock()
        audio_file.filename = "test.mp3"
        audio_file.content_type = "audio/mpeg"
        audio_file.read = AsyncMock(return_value=b"audio-bytes")
        form = MagicMock()
        form.get = MagicMock(side_effect=lambda key, default=None: {
            "file": audio_file, "model": model, "response_format": "json",
            "language": None, "prompt": None, "temperature": None,
        }.get(key, default))
        return form

    @pytest.mark.asyncio
    async def test_dispatch_logs_success(self):
        mock_persist = AsyncMock()
        form = self._make_form(model="whisper-1")
        mock_request = AsyncMock()
        mock_request.form = AsyncMock(return_value=form)
        mock_request.headers = {}

        with (
            patch("src.main.verify_api_key", new=AsyncMock()),
            patch.dict("os.environ", {"STT_PROVIDER": "aws-sagemaker",
                                      "AWS_STT_SAGEMAKER_ENDPOINT": "whisper-eu"}),
            patch.object(src.auth.bedrock_credential_manager, "aws_access_key", "AK"),
            patch.object(src.auth.bedrock_credential_manager, "aws_secret_key", "SK"),
            patch("src.main._sagemaker_transcribe_sync", return_value={"text": "hallo welt"}),
            patch("src.main.extract_attribution_context", return_value=_fake_attr()),
            patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
        ):
            from starlette.responses import JSONResponse
            resp = await src.main.audio_transcriptions(request=mock_request, credentials=None)

        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 200
        assert b"hallo welt" in resp.body
        mock_persist.assert_awaited_once()
        kw = mock_persist.call_args.kwargs
        assert kw["agent_id"] == "transkription"
        assert kw["model"] == "whisper-1"
        assert kw["status"] == "success"
