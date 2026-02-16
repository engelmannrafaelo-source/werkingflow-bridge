"""OpenAI-Compatible Provider — Generic httpx client for any OpenAI-compatible API.

Supports IONOS AI Model Hub, Mistral, Groq, Together AI, OpenRouter, and any other
provider that implements the OpenAI /v1/chat/completions endpoint.

Architecture:
    Request comes in OpenAI format → forward to provider → return response unchanged.
    No message conversion needed — the Bridge API is already OpenAI-compatible.
"""

import time
import logging
from typing import Optional, AsyncGenerator

import httpx

from src.models import ChatCompletionRequest

logger = logging.getLogger(__name__)

# Timeout: 5 min for generation (long prompts), 30s connect
TIMEOUT = httpx.Timeout(timeout=300.0, connect=30.0)


class ProviderError(RuntimeError):
    """Error from an OpenAI-compatible provider, carrying the HTTP status code."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(f"Provider returned {status_code}: {message}")


def _build_headers(base_url: str, api_key: str) -> dict[str, str]:
    """Build request headers, including provider-specific ones."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # OpenRouter requires app identification for analytics/routing
    if "openrouter.ai" in base_url:
        headers["HTTP-Referer"] = "https://werking.tools"
        headers["X-Title"] = "Werkingflow AI Bridge"

    return headers


async def call_openai_compatible(
    request: ChatCompletionRequest,
    base_url: str,
    api_key: str,
    model_override: Optional[str] = None,
) -> dict:
    """Call an OpenAI-compatible API endpoint.

    Args:
        request: The chat completion request (OpenAI format)
        base_url: Provider base URL (e.g. https://openai.inference.de-txl.ionos.com/v1)
        api_key: Provider API key
        model_override: Override the model name (provider may use different model IDs)

    Returns:
        OpenAI-compatible response dict with choices and usage
    """
    url = f"{base_url.rstrip('/')}/chat/completions"

    # Build request body — use provider's model ID, not the Bridge's
    body = {
        "model": model_override or request.model,
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "stream": False,
    }

    if request.top_p is not None and request.top_p != 1.0:
        body["top_p"] = request.top_p
    if request.stop:
        body["stop"] = request.stop

    headers = _build_headers(base_url, api_key)

    start = time.time()
    logger.info(f"🌐 OpenAI-compatible call: {url} (model: {body['model']})")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        response = await client.post(url, json=body, headers=headers)

    duration = time.time() - start

    if response.status_code != 200:
        error_text = response.text[:500]
        logger.error(f"❌ Provider error ({response.status_code}): {error_text}")
        error = ProviderError(response.status_code, error_text)
        raise error

    data = response.json()
    logger.info(
        f"✅ OpenAI-compatible response in {duration:.2f}s "
        f"(tokens: {data.get('usage', {}).get('total_tokens', '?')})"
    )

    return data


async def stream_openai_compatible(
    request: ChatCompletionRequest,
    base_url: str,
    api_key: str,
    model_override: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Stream from an OpenAI-compatible API endpoint.

    Yields SSE-formatted chunks (data: {...}\n\n).
    """
    url = f"{base_url.rstrip('/')}/chat/completions"

    body = {
        "model": model_override or request.model,
        "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "stream": True,
    }

    if request.top_p is not None and request.top_p != 1.0:
        body["top_p"] = request.top_p
    if request.stop:
        body["stop"] = request.stop

    headers = _build_headers(base_url, api_key)

    logger.info(f"🌐 OpenAI-compatible stream: {url} (model: {body['model']})")

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        async with client.stream("POST", url, json=body, headers=headers) as response:
            if response.status_code != 200:
                error_text = await response.aread()
                raise ProviderError(response.status_code, error_text.decode()[:500])

            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    yield f"{line}\n\n"
                elif line == "data: [DONE]":
                    yield "data: [DONE]\n\n"
                    break
