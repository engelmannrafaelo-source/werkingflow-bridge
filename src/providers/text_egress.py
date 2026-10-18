"""The only non-Gemini provider transport for JSON model requests.

Callers build provider payloads; this boundary validates the final body and then
sends it. Injected transports (including test clients) cannot bypass validation.
Streaming and Bedrock use the same boundary and policy.
"""

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

from src.routing.bild_policy import (
    ImageEgressRefused,
    app_is_gemini_only,
    guard_provider_payload,
)

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
OPENAI_BASE_URL = "https://api.openai.com/v1"
PERPLEXITY_URL = "https://api.perplexity.ai/v1/responses"


async def post(
    client: httpx.AsyncClient, url: str, *, json: Any = None, **kwargs: Any
) -> httpx.Response:
    if json is None:
        if app_is_gemini_only():
            raise ImageEgressRefused(
                "Nichttextueller Anbietertransport ist fuer diese App nicht erlaubt."
            )
        return await client.post(url, **kwargs)
    if app_is_gemini_only() and any(k in kwargs for k in ("content", "data", "files")):
        raise ImageEgressRefused(
            "Zusaetzlicher Anbieter-Body ausserhalb des Textvertrags."
        )
    guard_provider_payload(str(url), json)
    return await client.post(url, json=json, **kwargs)


@asynccontextmanager
async def stream(
    client: httpx.AsyncClient, method: str, url: str, *, json: Any, **kwargs: Any
) -> AsyncIterator[httpx.Response]:
    if app_is_gemini_only() and any(k in kwargs for k in ("content", "data", "files")):
        raise ImageEgressRefused(
            "Zusaetzlicher Anbieter-Body ausserhalb des Textvertrags."
        )
    guard_provider_payload(str(url), json)
    async with client.stream(method, url, json=json, **kwargs) as response:
        yield response


def invoke(client: Any, *, streaming: bool = False, **kwargs: Any) -> Any:
    guard_provider_payload("bedrock", json.loads(kwargs["body"]))
    method = (
        client.invoke_model_with_response_stream if streaming else client.invoke_model
    )
    return method(**kwargs)


def invoke_endpoint(client: Any, **kwargs: Any) -> Any:
    if app_is_gemini_only():
        raise ImageEgressRefused(
            "Binaerer Anbietertransport ist fuer diese App nicht erlaubt."
        )
    return client.invoke_endpoint(**kwargs)
