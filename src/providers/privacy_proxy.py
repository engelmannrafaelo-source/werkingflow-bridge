"""Internal credential-holding boundary for the keyless Privacy process."""

import hmac
import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from src.providers import text_egress
from src.routing.bild_policy import CURRENT_APP, ImageEgressRefused

router = APIRouter()


@router.post("/internal/privacy/messages")
async def messages(request: Request):
    secret = os.environ.get("PRIVACY_MODEL_PROXY_TOKEN", "")
    supplied = request.headers.get("x-privacy-model-token", "")
    if not secret or not hmac.compare_digest(secret, supplied):
        raise HTTPException(403, "privacy model proxy authentication required")
    body = await request.json()
    if not isinstance(body, dict) or set(body) != {"app", "payload"}:
        raise HTTPException(400, "privacy model proxy envelope invalid")
    if not isinstance(body["app"], str) or not isinstance(body["payload"], dict):
        raise HTTPException(400, "privacy model proxy envelope invalid")
    key = os.environ.get("ANTHROPIC_VISION_API_KEY", "")
    if not key:
        raise HTTPException(503, "privacy model provider unavailable")
    # The shared Privacy process cannot grant itself an allowlisted app.
    # This credential is text-only regardless of a supplied app label.
    context = CURRENT_APP.set("")
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await text_egress.post(
                client,
                text_egress.ANTHROPIC_MESSAGES_URL,
                json=body["payload"],
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
        return JSONResponse(status_code=response.status_code, content=response.json())
    except ImageEgressRefused as exc:
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )
    finally:
        CURRENT_APP.reset(context)


@router.post("/internal/privacy/vision")
async def vision(request: Request):
    secret = os.environ.get("PRIVACY_MODEL_PROXY_TOKEN", "")
    supplied = request.headers.get("x-privacy-model-token", "")
    if not secret or not hmac.compare_digest(secret, supplied):
        raise HTTPException(403, "privacy model proxy authentication required")
    body = await request.json()
    allowed = {"messages", "model", "max_tokens", "temperature", "system_prompt"}
    if not isinstance(body, dict) or set(body) != allowed:
        raise HTTPException(400, "privacy vision envelope invalid")
    # This separate capability can ONLY call Gemini, never a provider selected
    # by model name or app label supplied by the keyless process.
    from src.providers.gemini_vision import get_gemini_vision_provider

    result = await get_gemini_vision_provider().analyze(**body)
    return {"content": result.content, "stop_reason": result.stop_reason}
