"""
Tests: SDK-Disconnect als retryable 503 (Spec: 03-sdk-disconnect-as-503.md)

Cases:
  1. SDK-Disconnect → cross_worker_retry → 2nd worker 200 (transparent retry)
  2. SDK-Disconnect → no alternative worker → 503 capacity_busy (not 500)
  3. Regression: RateLimitError handler still triggers cross_worker_retry

The tests exercise the exception handlers directly with a mocked Request object,
avoiding the need for a full Docker stack.
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse

os.environ.setdefault("INSTANCE_NAME", "worker-test")
os.environ.setdefault("ANTHROPIC_VISION_API_KEY", "test-key")

from src.middleware.bridge_error import SDKDisconnectError  # noqa: E402

# RateLimitError lives in claude_cli but imports claude_code_sdk at module level.
# Stub that dependency so we can import RateLimitError for regression tests.
import sys
import types

_sdk_stub = types.ModuleType("claude_code_sdk")
_sdk_stub.query = None
_sdk_stub.ClaudeCodeOptions = object
_sdk_stub.Message = object
_sdk_stub._errors = types.ModuleType("claude_code_sdk._errors")
_sdk_stub._errors.MessageParseError = Exception
sys.modules.setdefault("claude_code_sdk", _sdk_stub)
sys.modules.setdefault("claude_code_sdk._errors", _sdk_stub._errors)

from src.claude_cli import RateLimitError  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(path: str = "/v1/chat/completions", stream: bool = False) -> Request:
    """Build a minimal FastAPI Request stub with cached_body_dict set."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": b"",
        "headers": [],
    }
    request = Request(scope)
    request.state.cached_body_dict = {
        "model": "claude-sonnet-4-6",
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": stream,
    }
    request.state.arrival_recorded = False
    return request


def _make_200_json_response() -> MagicMock:
    """Mock httpx response that represents a successful worker answer."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "Hello back!"}, "finish_reason": "stop"}],
        "model": "claude-sonnet-4-6",
    }
    return resp


def _httpx_client_mock(response: MagicMock) -> MagicMock:
    """Build httpx.AsyncClient mock that returns the given response from .post()."""
    mock_instance = MagicMock()
    mock_instance.post = AsyncMock(return_value=response)
    mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_instance.__aexit__ = AsyncMock(return_value=False)
    mock_cls = MagicMock(return_value=mock_instance)
    return mock_cls


# ---------------------------------------------------------------------------
# Test 1 + 4 (same scenario): 1× disconnect, 2nd worker 200 → client 200
# ---------------------------------------------------------------------------

class TestSDKDisconnectRetrySuccess:
    """SDK disconnect on first worker → retry to second worker → client sees 200."""

    @pytest.mark.asyncio
    async def test_retry_to_second_worker_returns_200(self):
        """
        SDKDisconnectError raised → handler finds alternative worker →
        httpx call returns 200 → client sees 200 (transparent retry).
        """
        from src.main import sdk_disconnect_handler

        request = _make_request()
        exc = SDKDisconnectError({
            "message": "No response from Claude Code SDK",
            "chunks_received": 0,
            "prompt_length": 500,
            "model": "claude-sonnet-4-6",
            "max_turns": 1,
            "tools_enabled": False,
        })

        mock_http_resp = _make_200_json_response()

        with patch("src.main._pick_alternative_worker", new=AsyncMock(return_value="worker2")), \
             patch("httpx.AsyncClient", new=_httpx_client_mock(mock_http_resp)):
            response = await sdk_disconnect_handler(request, exc)

        assert response.status_code == 200, (
            f"Expected 200 from retry, got {response.status_code}. "
            "cross_worker_retry must forward the alternative worker's response."
        )

    @pytest.mark.asyncio
    async def test_streaming_requests_skip_retry(self):
        """Streaming requests cannot be retried — handler must return 503 directly."""
        from src.main import sdk_disconnect_handler

        request = _make_request(stream=True)
        exc = SDKDisconnectError({"chunks_received": 0, "prompt_length": 100, "model": "x", "max_turns": 1, "tools_enabled": False, "message": "x"})

        with patch("src.main._pick_alternative_worker", new=AsyncMock(return_value="worker2")):
            response = await sdk_disconnect_handler(request, exc)

        # Streaming retries are skipped inside _cross_worker_retry — handler
        # must fall through to the 503 fallback.
        assert response.status_code == 503, (
            f"Streaming request: expected 503, got {response.status_code}"
        )
        body = json.loads(response.body)
        assert body["error"]["bridge_type"] == "capacity_busy"


# ---------------------------------------------------------------------------
# Test 2 (+ Test 3 in spec): both workers disconnect → 503 capacity_busy
# ---------------------------------------------------------------------------

class TestSDKDisconnectBothWorkersFail:
    """No alternative worker available → 503 capacity_busy (not 500)."""

    @pytest.mark.asyncio
    async def test_no_alternative_returns_503_capacity_busy(self):
        """
        SDKDisconnectError raised → _pick_alternative_worker returns None →
        handler falls back to 503 capacity_busy (not 500).
        """
        from src.main import sdk_disconnect_handler

        request = _make_request()
        exc = SDKDisconnectError({
            "message": "No response from Claude Code SDK",
            "chunks_received": 0,
            "prompt_length": 200,
            "model": "claude-sonnet-4-6",
            "max_turns": 1,
            "tools_enabled": False,
        })

        with patch("src.main._pick_alternative_worker", new=AsyncMock(return_value=None)):
            response = await sdk_disconnect_handler(request, exc)

        assert response.status_code == 503, (
            f"Expected 503 capacity_busy when no alternative worker, got {response.status_code}. "
            "SDK disconnect must never surface as 500."
        )
        body = json.loads(response.body)
        assert body["error"]["bridge_type"] == "capacity_busy", (
            f"Expected bridge_type=capacity_busy, got {body['error'].get('bridge_type')}"
        )
        assert body["error"]["reason"] == "sdk_disconnect"
        assert body["error"]["retryable"] is True
        assert "Retry-After" in response.headers

    @pytest.mark.asyncio
    async def test_non_chat_path_returns_503_without_retry(self):
        """Non chat/research paths skip cross-worker retry → immediate 503."""
        from src.main import sdk_disconnect_handler

        request = _make_request(path="/v1/health")
        exc = SDKDisconnectError({"chunks_received": 0, "prompt_length": 0, "model": "x", "max_turns": 1, "tools_enabled": False, "message": "x"})

        with patch("src.main._pick_alternative_worker", new=AsyncMock(return_value="worker2")):
            response = await sdk_disconnect_handler(request, exc)

        assert response.status_code == 503
        body = json.loads(response.body)
        assert body["error"]["bridge_type"] == "capacity_busy"


# ---------------------------------------------------------------------------
# Regression: SDKDisconnectError class is importable and correctly shaped
# ---------------------------------------------------------------------------

class TestSDKDisconnectErrorClass:
    """SDKDisconnectError has the right shape for the handler to consume."""

    def test_error_detail_accessible(self):
        detail = {"chunks_received": 6, "prompt_length": 1000, "model": "x", "max_turns": 2, "tools_enabled": True, "message": "no response"}
        exc = SDKDisconnectError(detail)
        assert exc.error_detail == detail
        assert str(exc) == "sdk_disconnect"
        assert exc.error_detail["chunks_received"] == 6

    def test_importable_from_claude_cli(self):
        from src.claude_cli import SDKDisconnectError as SDE
        assert issubclass(SDE, Exception)


# ---------------------------------------------------------------------------
# Regression: RateLimitError handler still works (cross_worker_retry path)
# ---------------------------------------------------------------------------

class TestRateLimitErrorRegression:
    """RateLimitError handler must still call cross_worker_retry (regression check)."""

    @pytest.mark.asyncio
    async def test_rate_limit_handler_still_triggers_retry(self):
        """RateLimitError on chat path still calls _cross_worker_retry."""
        from src.main import rate_limit_handler

        request = _make_request()
        exc = RateLimitError("account exhausted", retry_after_seconds=3600)

        mock_http_resp = _make_200_json_response()

        with patch("src.main._pick_alternative_worker", new=AsyncMock(return_value="worker2")), \
             patch("httpx.AsyncClient", new=_httpx_client_mock(mock_http_resp)), \
             patch("src.middleware.rolling_metrics.get_rolling_metrics") as mock_metrics:
            mock_metrics.return_value = MagicMock()
            response = await rate_limit_handler(request, exc)

        assert response.status_code == 200, (
            f"RateLimitError regression: expected 200 from retry, got {response.status_code}"
        )

    @pytest.mark.asyncio
    async def test_rate_limit_handler_returns_429_when_no_alternative(self):
        """RateLimitError with no alternative worker → 429 (unchanged behavior)."""
        from src.main import rate_limit_handler

        request = _make_request()
        exc = RateLimitError("account exhausted", retry_after_seconds=3600)

        with patch("src.main._pick_alternative_worker", new=AsyncMock(return_value=None)), \
             patch("src.middleware.rolling_metrics.get_rolling_metrics") as mock_metrics:
            mock_metrics.return_value = MagicMock()
            response = await rate_limit_handler(request, exc)

        assert response.status_code == 429, (
            f"RateLimitError regression: expected 429 when no alternative, got {response.status_code}"
        )
