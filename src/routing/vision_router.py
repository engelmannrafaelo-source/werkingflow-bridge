"""
Vision Router - Handles image/multimodal request routing

Centralizes vision detection and API call logic to avoid duplication
between streaming and non-streaming endpoints.
"""

import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

from src.vision_provider import VisionProvider, get_vision_provider

logger = logging.getLogger(__name__)


@dataclass
class VisionResult:
    """Result from vision analysis"""
    content: str
    model: str
    usage: Dict[str, int]


def serialize_message_content(content) -> Any:
    """Convert Pydantic content parts to dicts for VisionProvider compatibility"""
    if isinstance(content, str):
        return content
    # List of ContentPart Pydantic models -> list of dicts
    return [
        part.model_dump() if hasattr(part, 'model_dump') else part
        for part in content
    ]


def prepare_messages_for_vision(messages: List[Any]) -> List[Dict[str, Any]]:
    """Convert request messages to vision-compatible format"""
    return [
        {'role': m.role, 'content': serialize_message_content(m.content)}
        for m in messages
    ]


def has_vision_content(messages: List[Dict[str, Any]]) -> bool:
    """Check if messages contain images requiring vision routing"""
    return VisionProvider.has_images(messages)


async def route_to_vision(
    messages: List[Dict[str, Any]],
    model: str,
    max_tokens: int = 4096,
    temperature: float = 0.7
) -> VisionResult:
    """
    Route request to Vision API

    Args:
        messages: Messages in vision-compatible format (use prepare_messages_for_vision)
        model: Model to use
        max_tokens: Maximum tokens for response
        temperature: Temperature for generation

    Returns:
        VisionResult with content, model, and usage info

    Raises:
        Exception: If vision analysis fails
    """
    logger.info("🖼️ Routing to Vision API (direct Anthropic)")

    vision_provider = get_vision_provider()

    vision_response = await vision_provider.analyze(
        messages=messages,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature
    )

    return VisionResult(
        content=vision_response.content,
        model=vision_response.model,
        usage=vision_response.usage
    )


async def check_and_route_vision(
    messages: List[Any],
    model: str,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None
) -> Optional[VisionResult]:
    """
    Check if messages need vision routing, and route if needed

    Convenience function that combines has_vision_content check with routing.

    Args:
        messages: Original request messages (Pydantic models)
        model: Model to use
        max_tokens: Maximum tokens (default 4096)
        temperature: Temperature (default 0.7)

    Returns:
        VisionResult if vision was needed, None otherwise

    Raises:
        Exception: If vision analysis fails
    """
    messages_for_vision = prepare_messages_for_vision(messages)

    if not has_vision_content(messages_for_vision):
        return None

    return await route_to_vision(
        messages=messages_for_vision,
        model=model,
        max_tokens=max_tokens or 4096,
        temperature=temperature or 0.7
    )
