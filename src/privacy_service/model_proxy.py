"""Privacy owns only an internal Bridge credential, never a provider key."""

import os
from types import SimpleNamespace

import httpx

from src.providers import text_egress
from src.routing.bild_policy import CURRENT_APP, guard_provider_payload


def vision_available() -> bool:
    return bool(
        os.environ.get("PRIVACY_MODEL_PROXY_URL")
        and os.environ.get("PRIVACY_MODEL_PROXY_TOKEN")
    )


async def post_messages(payload):
    guard_provider_payload(text_egress.ANTHROPIC_MESSAGES_URL, payload)
    if not vision_available():
        raise ValueError("Privacy model proxy URL/token required")
    async with httpx.AsyncClient(timeout=300) as client:
        return await client.post(
            os.environ["PRIVACY_MODEL_PROXY_URL"].rstrip("/") + "/messages",
            headers={"x-privacy-model-token": os.environ["PRIVACY_MODEL_PROXY_TOKEN"]},
            json={"app": CURRENT_APP.get() or "", "payload": payload},
        )


class PrivacyVisionProxy:
    async def analyze(self, messages, *, model, max_tokens, temperature, system_prompt):
        if not vision_available():
            raise ValueError("Privacy model proxy URL/token required")
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(
                os.environ["PRIVACY_MODEL_PROXY_URL"].rstrip("/") + "/vision",
                headers={
                    "x-privacy-model-token": os.environ["PRIVACY_MODEL_PROXY_TOKEN"]
                },
                json={
                    "messages": messages,
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "system_prompt": system_prompt,
                },
            )
        response.raise_for_status()
        result = response.json()
        return SimpleNamespace(
            content=result["content"], stop_reason=result["stop_reason"]
        )


def get_vision_provider():
    if not vision_available():
        raise ValueError("Privacy model proxy URL/token required")
    return PrivacyVisionProxy()
