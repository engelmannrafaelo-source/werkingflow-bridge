"""Minimal Bedrock Service - Direct boto3 calls, no Claude Code SDK.

This is a lightweight service that handles backend="bedrock" requests directly.
Nginx routes these requests here before they reach the main wrapper.

DSGVO-compliant: Default region is eu-central-1 (Frankfurt).
"""

import os
import json
import uuid
from datetime import datetime
from typing import List, Dict, Any, Optional, AsyncGenerator

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from config.logging_config import get_logger
from src.models import (
    ChatCompletionRequest, ChatCompletionResponse, Choice, Message, Usage,
    BackendInfo, BackendType, PrivacyMode,
    ChatCompletionStreamResponse, StreamChoice
)
from src.model_registry import resolve_model, to_bedrock_model_id, model_supports_temperature
from src.routing.backend_router import _resolve_privacy_mode

logger = get_logger(__name__)

# botocore defaults to a 60s read_timeout — far too short for a large-prompt
# generation. The non-streaming invoke_model must receive the WHOLE response
# within read_timeout, so a long generation (e.g. the wizard's analyze_narrative
# step: ~176k-char prompt) tripped "Read timeout on endpoint URL" at 60s, and
# botocore then retried it several times (~5×60s ≈ 300s) before failing.
# Anthropic's flat-rate pool never hit this — the cap is Bedrock-path only.
# Give generations room (10 min), fail fast on genuine connect problems, and cap
# retries so a real hang does not multiply the wait (callers have their own retry
# loop). Applies to BOTH invoke_model and invoke_model_with_response_stream.
_BEDROCK_BOTO_CONFIG = Config(
    connect_timeout=10,
    read_timeout=600,
    retries={"max_attempts": 2, "mode": "standard"},
)

# AWS error codes that are identical on every worker (account/IAM/model-access
# misconfiguration). Raised as 424 so nginx does NOT cross-worker-retry and
# mask the real cause as "capacity_busy" — the client must see the message.
_BEDROCK_CONFIG_ERROR_CODES = frozenset({
    "ResourceNotFoundException",      # model access / use-case form missing
    "AccessDeniedException",          # IAM policy missing
    "UnrecognizedClientException",    # bad access key
    "InvalidSignatureException",      # bad secret key
    "ExpiredTokenException",          # expired STS token
})

# AWS-side rate limiting — retryable, surfaced as 429.
_BEDROCK_THROTTLE_ERROR_CODES = frozenset({
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceQuotaExceededException",
})


def _map_bedrock_stop_reason(bedrock_reason: Optional[str]) -> str:
    """Map Bedrock stop_reason to OpenAI finish_reason.

    Bedrock uses different values than OpenAI:
    - end_turn → stop
    - max_tokens → length
    - stop_sequence → stop
    - content_filtered → content_filter
    """
    mapping = {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "content_filtered": "content_filter",
    }
    return mapping.get(bedrock_reason, "stop")


# FastAPI app for Bedrock-only service
app = FastAPI(
    title="Bedrock Service",
    description="Lightweight DSGVO-compliant Bedrock backend",
    version="1.0.0"
)


class BedrockClient:
    """Boto3 client for Bedrock Runtime API."""

    def __init__(self):
        self.aws_access_key = os.getenv("AWS_ACCESS_KEY_ID_BEDROCK") or os.getenv("AWS_ACCESS_KEY_ID")
        self.aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY_BEDROCK") or os.getenv("AWS_SECRET_ACCESS_KEY")
        self.default_region = os.getenv("AWS_REGION_BEDROCK") or os.getenv("AWS_REGION") or "eu-central-1"

        if not self.aws_access_key or not self.aws_secret_key:
            raise RuntimeError(
                "AWS credentials not configured. "
                "Set AWS_ACCESS_KEY_ID_BEDROCK and AWS_SECRET_ACCESS_KEY_BEDROCK."
            )

        logger.info(f"Bedrock client initialized (region={self.default_region})")

    def get_client(self, region: Optional[str] = None):
        """Get boto3 bedrock-runtime client for specified region."""
        return boto3.client(
            "bedrock-runtime",
            region_name=region or self.default_region,
            aws_access_key_id=self.aws_access_key,
            aws_secret_access_key=self.aws_secret_key,
            config=_BEDROCK_BOTO_CONFIG,
        )


# Lazy-initialized client (initialized on first use)
_bedrock_client: Optional[BedrockClient] = None


def get_bedrock_client() -> BedrockClient:
    """Get or create Bedrock client (lazy initialization)."""
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = BedrockClient()
    return _bedrock_client


@app.on_event("startup")
async def startup():
    """Pre-initialize client on standalone service startup."""
    try:
        get_bedrock_client()
    except RuntimeError as e:
        logger.error(f"Failed to initialize Bedrock client: {e}")


def convert_messages_to_anthropic(messages: List[Message]) -> tuple[Optional[str], List[Dict]]:
    """Convert OpenAI messages to Anthropic format.

    Returns:
        Tuple of (system_prompt, messages_list)
    """
    system_prompt = None
    anthropic_messages = []

    for msg in messages:
        if msg.role == "system":
            # Anthropic uses separate system parameter
            system_prompt = msg.content if isinstance(msg.content, str) else str(msg.content)
        else:
            content = msg.content
            if isinstance(content, list):
                # Handle multimodal content
                anthropic_content = []
                for part in content:
                    if hasattr(part, 'type'):
                        if part.type == "text":
                            anthropic_content.append({"type": "text", "text": part.text})
                        elif part.type == "image_url":
                            # Convert OpenAI image_url to Anthropic format
                            # This requires base64 data extraction
                            url = part.image_url.url
                            if url.startswith("data:"):
                                # Parse data URL: data:image/jpeg;base64,xxxxx
                                media_type = url.split(";")[0].split(":")[1]
                                data = url.split(",")[1]
                                anthropic_content.append({
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": data
                                    }
                                })
                        elif part.type == "image":
                            # Already in Anthropic format
                            anthropic_content.append({
                                "type": "image",
                                "source": {
                                    "type": part.source.type,
                                    "media_type": part.source.media_type,
                                    "data": part.source.data
                                }
                            })
                    elif isinstance(part, dict):
                        anthropic_content.append(part)
                content = anthropic_content

            anthropic_messages.append({
                "role": msg.role,
                "content": content
            })

    return system_prompt, anthropic_messages


async def call_bedrock(
    request: ChatCompletionRequest,
    region: Optional[str] = None
) -> ChatCompletionResponse:
    """Call Bedrock API and return OpenAI-compatible response."""

    try:
        client = get_bedrock_client()
    except RuntimeError as e:
        # Bedrock client init failed = missing AWS credentials / region = config issue.
        # Surface as 500 config_error (non-retryable) so operators see it in the
        # contract-violation feed rather than as a silent transient 503.
        from src.middleware.bridge_error import BridgeError, config_error
        raise BridgeError(config_error(detail=str(e)))

    # Resolve model
    resolved_model, _ = resolve_model(request.model)
    if not resolved_model:
        raise HTTPException(status_code=400, detail=f"Unknown model: {request.model}")

    # Determine region first (needed for model ID)
    actual_region = region or request.bedrock_region or client.default_region
    bedrock_model_id = to_bedrock_model_id(resolved_model, actual_region)

    # Convert messages
    system_prompt, messages = convert_messages_to_anthropic(request.messages)

    # Build Anthropic request body
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "messages": messages,
        "max_tokens": request.max_tokens or 4096,
    }

    if system_prompt:
        body["system"] = system_prompt

    if request.temperature is not None and request.temperature != 1.0:
        # The Bridge may have force-upgraded the requested model to a newer one
        # (resolve_model). Only forward `temperature` if the model ACTUALLY sent
        # to Bedrock accepts it — newer (4.5+) models reject the field with a 400.
        # Stripping it here is the single boundary that keeps callers oblivious to
        # provider quirks (see model_registry.model_supports_temperature).
        if model_supports_temperature(resolved_model):
            body["temperature"] = request.temperature
        else:
            logger.warning(
                "Dropping deprecated `temperature`=%s: requested model %r resolved "
                "to %r, which rejects `temperature` on Bedrock.",
                request.temperature, request.model, resolved_model,
            )

    if request.top_p is not None and request.top_p != 1.0:
        body["top_p"] = request.top_p

    if request.stop:
        body["stop_sequences"] = request.stop if isinstance(request.stop, list) else [request.stop]

    logger.info(f"Calling Bedrock: model={bedrock_model_id}, region={actual_region}")

    try:
        boto_client = client.get_client(actual_region)
        response = boto_client.invoke_model(
            modelId=bedrock_model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body)
        )

        # AWS request ID — the join key between our usage_events row and the
        # AWS invocation log entry (1:1 billing reconciliation).
        aws_request_id = (response.get("ResponseMetadata") or {}).get("RequestId")

        response_body = json.loads(response["body"].read())

        # Extract response content
        content = ""
        if "content" in response_body:
            for block in response_body["content"]:
                if block.get("type") == "text":
                    content += block.get("text", "")

        # Build OpenAI-compatible response
        finish_reason = _map_bedrock_stop_reason(response_body.get("stop_reason"))
        return ChatCompletionResponse(
            id=f"chatcmpl-{uuid.uuid4().hex[:16]}",
            model=resolved_model,
            choices=[
                Choice(
                    index=0,
                    message=Message(role="assistant", content=content),
                    finish_reason=finish_reason
                )
            ],
            usage=Usage(
                prompt_tokens=response_body.get("usage", {}).get("input_tokens", 0),
                completion_tokens=response_body.get("usage", {}).get("output_tokens", 0),
                total_tokens=(
                    response_body.get("usage", {}).get("input_tokens", 0) +
                    response_body.get("usage", {}).get("output_tokens", 0)
                )
            ),
            x_backend_info=BackendInfo(
                backend="bedrock",
                region=actual_region,
                privacy_applied=_resolve_privacy_mode(
                    request.privacy or PrivacyMode.AUTO,
                    BackendType.BEDROCK,
                    actual_region
                ),
                model_id_used=bedrock_model_id,
                aws_request_id=aws_request_id
            )
        )

    except ClientError as e:
        code = (e.response.get("Error") or {}).get("Code", "")
        detail = f"Bedrock API error ({code or 'unknown'}): {e}"
        logger.error(detail)
        if code in _BEDROCK_CONFIG_ERROR_CODES:
            raise HTTPException(status_code=424, detail=detail)
        if code == "ValidationException":
            raise HTTPException(status_code=400, detail=detail)
        if code in _BEDROCK_THROTTLE_ERROR_CODES:
            raise HTTPException(status_code=429, detail=detail)
        raise HTTPException(status_code=500, detail=detail)
    except Exception as e:
        logger.error(f"Bedrock API error: {e}")
        raise HTTPException(status_code=500, detail=f"Bedrock API error: {str(e)}")


async def stream_bedrock(
    request: ChatCompletionRequest,
    region: Optional[str] = None,
    usage_sink: Optional[Dict[str, Any]] = None
) -> AsyncGenerator[str, None]:
    """Stream response from Bedrock API.

    usage_sink: caller-owned dict, filled while streaming so usage tracking
        survives the generator: input_tokens (message_start), output_tokens
        (message_delta, cumulative), bedrock_model_id, region, aws_request_id,
        and status ('success' once the stream finished, 'error' otherwise).
        Without it, streamed Bedrock usage would be untrackable = unbillable.
    """
    if usage_sink is None:
        usage_sink = {}
    usage_sink.setdefault("status", "error")  # flipped to success at stream end

    try:
        client = get_bedrock_client()
    except RuntimeError as e:
        yield f"data: {{\"error\": \"{str(e)}\"}}\n\n"
        return

    # Resolve model
    resolved_model, _ = resolve_model(request.model)
    if not resolved_model:
        yield f"data: {{\"error\": \"Unknown model: {request.model}\"}}\n\n"
        return

    # Determine region first (needed for model ID)
    actual_region = region or request.bedrock_region or client.default_region
    bedrock_model_id = to_bedrock_model_id(resolved_model, actual_region)
    usage_sink["bedrock_model_id"] = bedrock_model_id
    usage_sink["region"] = actual_region

    # Convert messages
    system_prompt, messages = convert_messages_to_anthropic(request.messages)

    # Build request body (same parameter surface as the non-streaming path)
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "messages": messages,
        "max_tokens": request.max_tokens or 4096,
    }

    if system_prompt:
        body["system"] = system_prompt

    if request.temperature is not None and request.temperature != 1.0:
        # The Bridge may have force-upgraded the requested model to a newer one
        # (resolve_model). Only forward `temperature` if the model ACTUALLY sent
        # to Bedrock accepts it — newer (4.5+) models reject the field with a 400.
        # Stripping it here is the single boundary that keeps callers oblivious to
        # provider quirks (see model_registry.model_supports_temperature).
        if model_supports_temperature(resolved_model):
            body["temperature"] = request.temperature
        else:
            logger.warning(
                "Dropping deprecated `temperature`=%s: requested model %r resolved "
                "to %r, which rejects `temperature` on Bedrock.",
                request.temperature, request.model, resolved_model,
            )

    if request.top_p is not None and request.top_p != 1.0:
        body["top_p"] = request.top_p

    if request.stop:
        body["stop_sequences"] = request.stop if isinstance(request.stop, list) else [request.stop]

    logger.info(f"Streaming from Bedrock: model={bedrock_model_id}, region={actual_region}")

    response_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"

    try:
        boto_client = client.get_client(actual_region)
        response = boto_client.invoke_model_with_response_stream(
            modelId=bedrock_model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(body)
        )

        usage_sink["aws_request_id"] = (response.get("ResponseMetadata") or {}).get("RequestId")

        for event in response["body"]:
            chunk = json.loads(event["chunk"]["bytes"])

            if chunk.get("type") == "message_start":
                # Input tokens are only reported here.
                _msg_usage = (chunk.get("message") or {}).get("usage") or {}
                if "input_tokens" in _msg_usage:
                    usage_sink["input_tokens"] = _msg_usage["input_tokens"]

            elif chunk.get("type") == "message_delta":
                # Cumulative output token count — the last delta wins.
                _delta_usage = chunk.get("usage") or {}
                if "output_tokens" in _delta_usage:
                    usage_sink["output_tokens"] = _delta_usage["output_tokens"]

            if chunk.get("type") == "content_block_delta":
                delta_text = chunk.get("delta", {}).get("text", "")
                if delta_text:
                    stream_response = ChatCompletionStreamResponse(
                        id=response_id,
                        model=resolved_model,
                        choices=[
                            StreamChoice(
                                index=0,
                                delta={"content": delta_text},
                                finish_reason=None
                            )
                        ]
                    )
                    yield f"data: {stream_response.model_dump_json()}\n\n"

            elif chunk.get("type") == "message_stop":
                # Final chunk
                stream_response = ChatCompletionStreamResponse(
                    id=response_id,
                    model=resolved_model,
                    choices=[
                        StreamChoice(
                            index=0,
                            delta={},
                            finish_reason="stop"
                        )
                    ]
                )
                yield f"data: {stream_response.model_dump_json()}\n\n"

        usage_sink["status"] = "success"
        yield "data: [DONE]\n\n"

    except Exception as e:
        logger.error(f"Bedrock streaming error: {e}")
        yield f"data: {{\"error\": \"{str(e)}\"}}\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """OpenAI-compatible chat completions via Bedrock."""

    if request.stream:
        return StreamingResponse(
            stream_bedrock(request),
            media_type="text/event-stream"
        )

    return await call_bedrock(request)


@app.get("/health")
async def health():
    """Health check endpoint."""
    try:
        client = get_bedrock_client()
        return {
            "status": "healthy",
            "service": "bedrock-service",
            "region": client.default_region
        }
    except RuntimeError:
        return {
            "status": "degraded",
            "service": "bedrock-service",
            "region": None
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
