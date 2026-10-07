"""Exercise the existing chat entry point without authentication or model I/O."""

import inspect

import pytest
from starlette.requests import Request

from src import main
from src.models import ChatCompletionRequest


@pytest.mark.parametrize("header", [None, "", "   "])
@pytest.mark.asyncio
async def test_chat_tools_stay_disabled_without_research_header(monkeypatch, header):
    class ReachedAuthentication(Exception):
        """Stop after the actual entry-point policy, before external operations."""

    body = ChatCompletionRequest(
        model="claude-sonnet-5-5",
        messages=[{"role": "user", "content": "Test"}],
        enable_tools=True,
    )
    headers = [] if header is None else [(b"x-claude-allowed-tools", header.encode())]
    request = Request({
        "type": "http", "method": "POST", "path": "/v1/chat/completions",
        "headers": headers,
    })

    async def stop_at_authentication(request, credentials):
        # The real route must normalize the header and apply its policy first.
        assert body.enable_tools is False
        raise ReachedAuthentication

    monkeypatch.setattr(main, "verify_api_key", stop_at_authentication)
    endpoint = next(
        route.endpoint for route in main.app.routes
        if getattr(route, "path", None) == "/v1/chat/completions"
        and "POST" in getattr(route, "methods", set())
    )
    # Unwrap the rate limiter; retain the actual registered route implementation.
    with pytest.raises(ReachedAuthentication):
        await inspect.unwrap(endpoint)(body, request, credentials=None, _adaptive=None)
    assert body.enable_tools is False
