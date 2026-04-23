"""OpenAI-Compatible Provider — Generic httpx client for any OpenAI-compatible API.

Supports IONOS AI Model Hub, Mistral, Groq, Together AI, OpenRouter, and any other
provider that implements the OpenAI /v1/chat/completions endpoint.

Architecture:
    Request comes in OpenAI format → forward to provider → return response unchanged.
    No message conversion needed — the Bridge API is already OpenAI-compatible.
    Transient upstream errors (5xx, network blips) are retried here with bounded
    exponential backoff BEFORE surfacing — this absorbs upstream glitches so
    fewer worker-level BridgeErrors leak out of the bridge.
"""

import asyncio
import random
import time
import logging
from typing import Optional, AsyncGenerator

import httpx

from src.models import ChatCompletionRequest

logger = logging.getLogger(__name__)

# Timeout: 5 min for generation (long prompts), 30s connect
TIMEOUT = httpx.Timeout(timeout=300.0, connect=30.0)

# Transient-error retry policy for upstream OpenAI-compatible providers.
# 429 is included because external providers' own rate-limits are often brief.
# 5xx are transient upstream issues. 4xx (400/401/403/404) are client-side
# mistakes and retrying won't help — surface immediately.
_RETRY_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
_MAX_RETRIES = 2  # i.e. up to 3 total attempts
_BASE_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 6.0


def _is_transient_httpx_exc(exc: Exception) -> bool:
    """Network-layer exceptions that are worth retrying once."""
    return isinstance(exc, (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
        httpx.RemoteProtocolError,
        httpx.NetworkError,
    ))


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter. attempt is 0-indexed (0 = first retry)."""
    raw = _BASE_BACKOFF_S * (2 ** attempt)
    capped = min(raw, _MAX_BACKOFF_S)
    # Full jitter keeps colliding retries from synchronizing under sustained 5xx.
    return random.uniform(0, capped)


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

    last_error: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await client.post(url, json=body, headers=headers)
            except Exception as net_exc:
                if _is_transient_httpx_exc(net_exc) and attempt < _MAX_RETRIES:
                    delay = _backoff_delay(attempt)
                    logger.warning(
                        f"🔁 Network error calling {base_url} "
                        f"(attempt {attempt + 1}/{_MAX_RETRIES + 1}): "
                        f"{type(net_exc).__name__}: {str(net_exc)[:120]} — "
                        f"retrying in {delay:.1f}s"
                    )
                    last_error = net_exc
                    await asyncio.sleep(delay)
                    continue
                raise

            if response.status_code == 200:
                data = response.json()
                duration = time.time() - start
                logger.info(
                    f"✅ OpenAI-compatible response in {duration:.2f}s "
                    f"(tokens: {data.get('usage', {}).get('total_tokens', '?')}, "
                    f"attempts: {attempt + 1})"
                )
                return data

            # Non-200. Retry transient upstream statuses; surface the rest.
            error_text = response.text[:500]
            if response.status_code in _RETRY_STATUS_CODES and attempt < _MAX_RETRIES:
                # Honour Retry-After when provided (bounded so we don't stall forever).
                retry_after_hdr = response.headers.get("Retry-After")
                try:
                    hinted = float(retry_after_hdr) if retry_after_hdr else None
                except ValueError:
                    hinted = None
                delay = min(hinted, _MAX_BACKOFF_S) if hinted else _backoff_delay(attempt)
                logger.warning(
                    f"🔁 Transient upstream {response.status_code} from {base_url} "
                    f"(attempt {attempt + 1}/{_MAX_RETRIES + 1}): "
                    f"{error_text[:160]} — retrying in {delay:.1f}s"
                )
                last_error = ProviderError(response.status_code, error_text)
                await asyncio.sleep(delay)
                continue

            logger.error(f"❌ Provider error ({response.status_code}): {error_text}")
            raise ProviderError(response.status_code, error_text)

    # Loop exhausted without returning — raise the last transient error.
    logger.error(
        f"❌ Exhausted {_MAX_RETRIES + 1} attempts against {base_url}: {last_error}"
    )
    if isinstance(last_error, ProviderError):
        raise last_error
    raise ProviderError(
        status_code=599,
        message=f"Transient network error after retries: {last_error}",
    )


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

    # Pre-stream retry: we can only retry BEFORE the first chunk is yielded.
    # Once streaming has started, a mid-stream failure must surface (the client
    # has already received partial output; re-running would duplicate content
    # and double-bill tokens).
    last_error: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with client.stream("POST", url, json=body, headers=headers) as response:
                    if response.status_code != 200:
                        error_bytes = await response.aread()
                        error_text = error_bytes.decode(errors="replace")[:500]
                        if (
                            response.status_code in _RETRY_STATUS_CODES
                            and attempt < _MAX_RETRIES
                        ):
                            delay = _backoff_delay(attempt)
                            logger.warning(
                                f"🔁 Transient upstream {response.status_code} on stream "
                                f"to {base_url} (attempt {attempt + 1}/{_MAX_RETRIES + 1}): "
                                f"{error_text[:160]} — retrying in {delay:.1f}s"
                            )
                            last_error = ProviderError(response.status_code, error_text)
                            await asyncio.sleep(delay)
                            continue
                        raise ProviderError(response.status_code, error_text)

                    # Success — stream through. Any error past this point must
                    # bubble as-is (can't safely restart a partially-delivered stream).
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            yield f"{line}\n\n"
                        elif line == "data: [DONE]":
                            yield "data: [DONE]\n\n"
                            break
                    return
            except Exception as net_exc:
                if _is_transient_httpx_exc(net_exc) and attempt < _MAX_RETRIES:
                    delay = _backoff_delay(attempt)
                    logger.warning(
                        f"🔁 Network error on stream setup to {base_url} "
                        f"(attempt {attempt + 1}/{_MAX_RETRIES + 1}): "
                        f"{type(net_exc).__name__}: {str(net_exc)[:120]} — "
                        f"retrying in {delay:.1f}s"
                    )
                    last_error = net_exc
                    await asyncio.sleep(delay)
                    continue
                raise

    # Stream retries exhausted without ever succeeding.
    logger.error(
        f"❌ Stream retries exhausted against {base_url}: {last_error}"
    )
    if isinstance(last_error, ProviderError):
        raise last_error
    raise ProviderError(
        status_code=599,
        message=f"Transient network error after retries: {last_error}",
    )
