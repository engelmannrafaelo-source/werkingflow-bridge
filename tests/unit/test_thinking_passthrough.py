"""
Tests: extended-thinking (`thinking` / `output_config`) passthrough (2026-07-24).

Verified live against Bedrock (see memory reference_energy_kfr_mapping_scaling):
Sonnet 5 needs thinking={"type": "adaptive"} + output_config={"effort": ...} to
control reasoning depth, but the Bridge did not forward either field on any
path. This closes that gap on the two raw-Anthropic-Messages-API-format
backends the Bridge builds request bodies for: Bedrock (bedrock_service.py)
and the direct-Anthropic fallback/vision path (vision_provider.py).

Cases:
  1. Unset (default None) → field absent from the outgoing body on both paths
     (byte-identical to pre-change behavior — no default flip).
  2. Set → forwarded verbatim in the outgoing body on both paths.
  3. An incompatible/invalid combination surfaces as the provider's own clean
     error (HTTPException), not a swallowed exception — for both paths.
  4. Setting thinking/output_config on the default (Claude Code SDK) backend
     logs a warning instead of silently doing nothing.
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

os.environ.setdefault("ANTHROPIC_VISION_API_KEY", "test-key")
os.environ.setdefault("AWS_ACCESS_KEY_ID_BEDROCK", "test-access-key")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY_BEDROCK", "test-secret-key")

from src.models import ChatCompletionRequest, Message  # noqa: E402
from src.vision_provider import VisionProvider  # noqa: E402
import src.bedrock_service as bedrock_service  # noqa: E402


SAMPLE_MESSAGES = [{"role": "user", "content": "Analyze this."}]


def _make_request(**overrides) -> ChatCompletionRequest:
    payload = dict(
        model="claude-sonnet-5-20260101",
        messages=[Message(role="user", content="hi")],
    )
    payload.update(overrides)
    return ChatCompletionRequest(**payload)


def _mock_httpx_client(status_code: int, body):
    body_text = json.dumps(body) if isinstance(body, dict) else str(body)
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_response.text = body_text
    mock_response.json.return_value = body if isinstance(body, dict) else {}

    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


class TestModelDefaults:
    def test_thinking_and_output_config_default_to_none(self):
        req = _make_request()
        assert req.thinking is None
        assert req.output_config is None

    def test_thinking_and_output_config_accept_arbitrary_dicts(self):
        req = _make_request(
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
        )
        assert req.thinking == {"type": "adaptive"}
        assert req.output_config == {"effort": "low"}


class TestVisionProviderPassthrough:
    """Direct-Anthropic path (vision_provider.py)."""

    @pytest.mark.asyncio
    async def test_unset_omits_fields_from_body(self):
        ok_body = {
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-sonnet-5-20260101",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        }
        mock_client = _mock_httpx_client(200, ok_body)
        with patch("httpx.AsyncClient", return_value=mock_client):
            await VisionProvider().analyze(SAMPLE_MESSAGES)

        sent_body = mock_client.post.call_args.kwargs["json"]
        assert "thinking" not in sent_body
        assert "output_config" not in sent_body

    @pytest.mark.asyncio
    async def test_set_forwards_verbatim_in_body(self):
        ok_body = {
            "content": [{"type": "text", "text": "ok"}],
            "model": "claude-sonnet-5-20260101",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        }
        mock_client = _mock_httpx_client(200, ok_body)
        with patch("httpx.AsyncClient", return_value=mock_client):
            await VisionProvider().analyze(
                SAMPLE_MESSAGES,
                thinking={"type": "adaptive"},
                output_config={"effort": "low"},
            )

        sent_body = mock_client.post.call_args.kwargs["json"]
        assert sent_body["thinking"] == {"type": "adaptive"}
        assert sent_body["output_config"] == {"effort": "low"}

    @pytest.mark.asyncio
    async def test_incompatible_combination_surfaces_as_clean_400(self):
        """Anthropic rejects an invalid thinking config with a 400 — must not
        be swallowed just because 'thinking' is now a forwarded field."""
        error_body = {"error": {"type": "invalid_request_error", "message": "thinking.type: invalid"}}
        mock_client = _mock_httpx_client(400, error_body)
        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(HTTPException) as exc_info:
                await VisionProvider().analyze(
                    SAMPLE_MESSAGES,
                    thinking={"type": "not-a-real-type"},
                )

        assert exc_info.value.status_code == 400


class TestBedrockPassthrough:
    """Bedrock path (bedrock_service.py) — non-streaming call_bedrock."""

    def _patch_client(self, monkeypatch, invoke_response_body):
        boto_client = MagicMock()
        boto_client.invoke_model.return_value = {
            "ResponseMetadata": {"RequestId": "req-1"},
            "body": MagicMock(read=lambda: json.dumps(invoke_response_body).encode()),
        }

        bedrock_client = MagicMock()
        bedrock_client.default_region = "eu-central-1"
        bedrock_client.get_client.return_value = boto_client

        monkeypatch.setattr(bedrock_service, "get_bedrock_client", lambda: bedrock_client)
        return boto_client

    @pytest.mark.asyncio
    async def test_unset_omits_fields_from_body(self, monkeypatch):
        ok_body = {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        }
        boto_client = self._patch_client(monkeypatch, ok_body)
        req = _make_request()

        await bedrock_service.call_bedrock(req)

        sent_body = json.loads(boto_client.invoke_model.call_args.kwargs["body"])
        assert "thinking" not in sent_body
        assert "output_config" not in sent_body

    @pytest.mark.asyncio
    async def test_set_forwards_verbatim_in_body(self, monkeypatch):
        ok_body = {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        }
        boto_client = self._patch_client(monkeypatch, ok_body)
        req = _make_request(
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
        )

        await bedrock_service.call_bedrock(req)

        sent_body = json.loads(boto_client.invoke_model.call_args.kwargs["body"])
        assert sent_body["thinking"] == {"type": "adaptive"}
        assert sent_body["output_config"] == {"effort": "high"}

    @pytest.mark.asyncio
    async def test_incompatible_combination_surfaces_as_clean_400(self, monkeypatch):
        """Bedrock rejects an invalid/incompatible thinking config with
        ValidationException — the existing ClientError→400 mapping must still
        fire once 'thinking' is a forwarded field, not a swallowed 500."""
        bedrock_client = MagicMock()
        bedrock_client.default_region = "eu-central-1"
        boto_client = MagicMock()
        boto_client.invoke_model.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "thinking.type: invalid"}},
            "InvokeModel",
        )
        bedrock_client.get_client.return_value = boto_client
        monkeypatch.setattr(bedrock_service, "get_bedrock_client", lambda: bedrock_client)

        req = _make_request(thinking={"type": "not-a-real-type"})

        with pytest.raises(HTTPException) as exc_info:
            await bedrock_service.call_bedrock(req)

        assert exc_info.value.status_code == 400


class TestLogUnsupportedParameters:
    def test_thinking_on_default_backend_logs_warning_not_silence(self, caplog):
        req = _make_request(thinking={"type": "adaptive"})
        with caplog.at_level("WARNING"):
            req.log_unsupported_parameters()

        assert any("thinking" in record.message for record in caplog.records)

    def test_output_config_on_default_backend_logs_warning_not_silence(self, caplog):
        req = _make_request(output_config={"effort": "low"})
        with caplog.at_level("WARNING"):
            req.log_unsupported_parameters()

        assert any("output_config" in record.message for record in caplog.records)

    def test_no_warning_when_unset(self, caplog):
        req = _make_request()
        with caplog.at_level("WARNING"):
            req.log_unsupported_parameters()

        assert not any(
            "thinking" in record.message or "output_config" in record.message
            for record in caplog.records
        )
