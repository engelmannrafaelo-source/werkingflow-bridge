"""
Vision Provider - Direct Anthropic API for Image Analysis

The Claude Code SDK doesn't support multimodal (image) inputs.
This provider routes image-containing requests directly to the Anthropic API,
bypassing the SDK while maintaining the same response format.

Usage:
    provider = VisionProvider()
    response = await provider.analyze(messages, model="claude-sonnet-4-20250514")
"""

import os
import base64
import re
import httpx
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from config.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class VisionResponse:
    """Response from vision analysis."""
    content: str
    model: str
    usage: Dict[str, int]
    stop_reason: str


class VisionProvider:
    """
    Direct Anthropic API provider for image/vision requests.

    Bypasses Claude Code SDK which doesn't support multimodal inputs.
    """

    ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
    ANTHROPIC_VERSION = "2023-06-01"

    # Supported image formats
    SUPPORTED_MIME_TYPES = [
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp"
    ]

    def __init__(self):
        # Use ANTHROPIC_VISION_API_KEY (renamed by auth.py to prevent OAuth fallback)
        # Falls back to ANTHROPIC_API_KEY for backwards compatibility
        self.api_key = os.getenv("ANTHROPIC_VISION_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            logger.warning("ANTHROPIC_VISION_API_KEY not set - vision requests will fail")

    @staticmethod
    def has_images(messages: List[Dict[str, Any]]) -> bool:
        """
        Check if any USER message contains image content.

        Only checks user messages - assistant messages may contain base64 data
        from API responses (e.g., QR codes from MFA setup) which should NOT
        trigger vision routing.

        Supports multiple formats:
        - OpenAI format: {"type": "image_url", "image_url": {"url": "data:image/..."}}
        - Anthropic format: {"type": "image", "source": {"type": "base64", ...}}
        - Inline base64: "data:image/jpeg;base64,..." in text content
        - Bracketed format: "[data:image/jpeg;base64,...]" in text content
        """
        for message in messages:
            # Skip non-user messages - assistant responses may contain base64
            # API data that shouldn't trigger vision processing
            role = message.get("role", "")
            if role != "user":
                continue

            content = message.get("content")

            # Check array content (OpenAI/Anthropic multimodal format)
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        # OpenAI image_url format
                        if block.get("type") == "image_url":
                            return True
                        # Anthropic image format
                        if block.get("type") == "image":
                            return True

            # Check string content for inline base64 images
            elif isinstance(content, str):
                # Pattern: data:image/xxx;base64,...
                if "data:image/" in content and ";base64," in content:
                    return True
                # Pattern: [data:image/xxx;base64,...]
                if re.search(r'\[data:image/[^;]+;base64,[^\]]+\]', content):
                    return True

        return False

    def _extract_images_from_content(self, content: Any) -> Tuple[List[Dict], str]:
        """
        Extract images and text from message content.

        Returns:
            Tuple of (image_blocks, remaining_text)
        """
        images = []
        text_parts = []

        if isinstance(content, list):
            # Already in multimodal format
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "image_url":
                        # OpenAI format -> Anthropic format
                        url = block.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            # Parse data URL - use regex that only matches valid base64 chars
                            # Base64 alphabet: A-Z, a-z, 0-9, +, /, = (padding)
                            match = re.match(r'data:(image/[^;]+);base64,([A-Za-z0-9+/=]+)', url)
                            if match:
                                base64_data = match.group(2).strip()
                                # Ensure proper padding
                                padding_needed = len(base64_data) % 4
                                if padding_needed:
                                    base64_data += '=' * (4 - padding_needed)
                                images.append({
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": match.group(1),
                                        "data": base64_data
                                    }
                                })
                        elif url.startswith("http://") or url.startswith("https://"):
                            # External URL - download and convert to base64
                            try:
                                import httpx
                                import base64
                                headers = {"User-Agent": "Mozilla/5.0 (compatible; AIBridge/1.0)"}
                                with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
                                    response = client.get(url)
                                    response.raise_for_status()
                                    image_data = base64.b64encode(response.content).decode('utf-8')
                                    # Determine media type from content-type header or URL
                                    content_type = response.headers.get('content-type', 'image/png')
                                    if ';' in content_type:
                                        content_type = content_type.split(';')[0].strip()
                                    images.append({
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": content_type,
                                            "data": image_data
                                        }
                                    })
                            except Exception as e:
                                logger.warning(f"Failed to download image from {url}: {e}")
                    elif block.get("type") == "image":
                        # Already Anthropic format
                        images.append(block)
                    elif block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_parts.append(block)

        elif isinstance(content, str):
            # Extract inline base64 images from text
            # Pattern 1: [data:image/xxx;base64,...] - ONLY valid base64 chars
            pattern = r'\[data:(image/[^;]+);base64,([A-Za-z0-9+/=]+)\]'

            last_end = 0
            for match in re.finditer(pattern, content):
                # Add text before this image
                text_before = content[last_end:match.start()]
                if text_before.strip():
                    text_parts.append(text_before)

                # Add image
                images.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": match.group(1),
                        "data": match.group(2)
                    }
                })

                last_end = match.end()

            # Add remaining text
            remaining = content[last_end:]
            if remaining.strip():
                text_parts.append(remaining)

            # If no bracketed images found, check for raw data URLs
            if not images:
                # ONLY valid base64 chars: A-Z, a-z, 0-9, +, /, =
                pattern2 = r'data:(image/[^;]+);base64,([A-Za-z0-9+/=]+)'
                for match in re.finditer(pattern2, content):
                    images.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": match.group(1),
                            "data": match.group(2)
                        }
                    })
                # Remove image data from text
                text_parts = [re.sub(pattern2, '[Image]', content)]

        combined_text = "\n".join(text_parts).strip()
        return images, combined_text

    def _convert_to_anthropic_messages(
        self,
        messages: List[Dict[str, Any]]
    ) -> Tuple[List[Dict], Optional[str]]:
        """
        Convert OpenAI-format messages to Anthropic format with image support.

        Returns:
            Tuple of (messages, system_prompt)
        """
        anthropic_messages = []
        system_prompt = None

        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")

            # Extract system prompt
            if role == "system":
                system_prompt = content if isinstance(content, str) else str(content)
                continue

            # Map assistant role
            if role == "assistant":
                anthropic_messages.append({
                    "role": "assistant",
                    "content": content if isinstance(content, str) else str(content)
                })
                continue

            # Process user messages (may contain images)
            images, text = self._extract_images_from_content(content)

            if images:
                # Build multimodal content array
                content_blocks = []

                # Add images first
                for img in images:
                    content_blocks.append(img)

                # Add text if present
                if text:
                    content_blocks.append({
                        "type": "text",
                        "text": text
                    })

                anthropic_messages.append({
                    "role": "user",
                    "content": content_blocks
                })
            else:
                # Plain text message
                anthropic_messages.append({
                    "role": "user",
                    "content": text or content
                })

        return anthropic_messages, system_prompt

    async def analyze(
        self,
        messages: List[Dict[str, Any]],
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        system_prompt: Optional[str] = None
    ) -> VisionResponse:
        """
        Analyze images using direct Anthropic API.

        Args:
            messages: OpenAI-format messages (may contain images)
            model: Claude model to use
            max_tokens: Maximum response tokens
            temperature: Sampling temperature
            system_prompt: Optional system prompt override

        Returns:
            VisionResponse with analysis result
        """
        if not self.api_key:
            raise ValueError(
                "ANTHROPIC_VISION_API_KEY environment variable not set. "
                "Vision/Image requests require direct API access (not OAuth)."
            )

        # Convert messages to Anthropic format
        anthropic_messages, extracted_system = self._convert_to_anthropic_messages(messages)

        # Use provided system prompt or extracted one
        final_system = system_prompt or extracted_system

        # Count images for logging
        image_count = sum(
            1 for msg in anthropic_messages
            if isinstance(msg.get("content"), list)
            for block in msg["content"]
            if isinstance(block, dict) and block.get("type") == "image"
        )

        logger.info(
            f"Vision request: {image_count} images, model={model}",
            extra={
                "image_count": image_count,
                "model": model,
                "message_count": len(anthropic_messages)
            }
        )

        # Build request body
        request_body = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": anthropic_messages
        }

        if final_system:
            request_body["system"] = final_system

        # Make API request
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                self.ANTHROPIC_API_URL,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                    "anthropic-version": self.ANTHROPIC_VERSION
                },
                json=request_body
            )

        if response.status_code != 200:
            error_body = response.text
            logger.error(
                f"Anthropic API error: {response.status_code}",
                extra={
                    "status_code": response.status_code,
                    "error": error_body[:500]
                }
            )
            raise RuntimeError(
                f"Anthropic API error ({response.status_code}): {error_body[:200]}"
            )

        data = response.json()

        # Extract response content
        content_blocks = data.get("content", [])
        response_text = ""
        for block in content_blocks:
            if block.get("type") == "text":
                response_text += block.get("text", "")

        usage = data.get("usage", {})

        logger.info(
            f"Vision response: {len(response_text)} chars",
            extra={
                "response_length": len(response_text),
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0)
            }
        )

        return VisionResponse(
            content=response_text,
            model=data.get("model", model),
            usage={
                "prompt_tokens": usage.get("input_tokens", 0),
                "completion_tokens": usage.get("output_tokens", 0),
                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
            },
            stop_reason=data.get("stop_reason", "end_turn")
        )


# Singleton instance
_vision_provider: Optional[VisionProvider] = None


def get_vision_provider() -> VisionProvider:
    """Get or create the vision provider singleton."""
    global _vision_provider
    if _vision_provider is None:
        _vision_provider = VisionProvider()
    return _vision_provider
