"""
Tests: VisionProvider 4xx passthrough behavior.

Spec: specs/bridge-phase-b/01-vision-4xx-passthrough.md

Cases:
  1. Anthropic 400 → HTTPException(400), not RuntimeError/500
  2. Anthropic 422 → HTTPException(422)
  3. Anthropic 500 → RuntimeError (unchanged)
  4. Anthropic 200 → VisionResponse (regression check)
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

os.environ.setdefault("ANTHROPIC_VISION_API_KEY", "test-key")

from src.vision_provider import VisionProvider, VisionResponse  # noqa: E402


SAMPLE_MESSAGES = [{"role": "user", "content": "Analyze this."}]


def _mock_client(status_code: int, body):
    """Return a mock httpx.AsyncClient that yields a fixed response."""
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


@pytest.mark.asyncio
async def test_400_raises_http_exception():
    """Anthropic 400 invalid_request_error → HTTPException(400), not 500."""
    error_body = {"error": {"type": "invalid_request_error", "message": "invalid base64 data"}}

    with patch("httpx.AsyncClient", return_value=_mock_client(400, error_body)):
        with pytest.raises(HTTPException) as exc_info:
            await VisionProvider().analyze(SAMPLE_MESSAGES)

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_422_raises_http_exception():
    """Anthropic 422 → HTTPException(422)."""
    error_body = {"error": {"type": "invalid_request_error", "message": "unprocessable entity"}}

    with patch("httpx.AsyncClient", return_value=_mock_client(422, error_body)):
        with pytest.raises(HTTPException) as exc_info:
            await VisionProvider().analyze(SAMPLE_MESSAGES)

    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_500_raises_runtime_error():
    """Anthropic 500 → RuntimeError (nginx maps to 503, unchanged)."""
    with patch("httpx.AsyncClient", return_value=_mock_client(500, "Internal Server Error")):
        with pytest.raises(RuntimeError):
            await VisionProvider().analyze(SAMPLE_MESSAGES)


@pytest.mark.asyncio
async def test_200_returns_vision_response():
    """Anthropic 200 → VisionResponse (regression check)."""
    ok_body = {
        "content": [{"type": "text", "text": "Analysis complete."}],
        "model": "claude-sonnet-4-5-20250929",
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "stop_reason": "end_turn",
    }

    with patch("httpx.AsyncClient", return_value=_mock_client(200, ok_body)):
        result = await VisionProvider().analyze(SAMPLE_MESSAGES)

    assert isinstance(result, VisionResponse)
    assert result.content == "Analysis complete."
