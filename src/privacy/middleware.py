"""
Privacy Middleware for FastAPI

Transparent middleware that automatically anonymizes user messages before
sending to Claude and de-anonymizes responses before returning to client.

Flow:
  User Message → [Anonymize] → Claude → [De-Anonymize] → Response
"""

import os
import logging
import asyncio
from typing import Dict, Optional, List, Any, Callable
from contextvars import ContextVar
from functools import wraps

from .anonymizer import PresidioAnonymizer, AnonymizationResult

logger = logging.getLogger(__name__)

# Context variable to store anonymization mapping per request
_anonymization_context: ContextVar[Optional[Dict[str, str]]] = ContextVar(
    'anonymization_mapping',
    default=None
)


class PrivacyMiddleware:
    """
    DSGVO-compliant privacy middleware for message anonymization.

    This middleware transparently processes all messages:
    1. BEFORE Claude: Anonymizes user messages (replaces PII with ANON_XXX)
    2. AFTER Claude: De-anonymizes response (restores original PII)

    Configuration via environment variables:
    - PRIVACY_ENABLED: Enable/disable privacy middleware (default: true)
    - PRIVACY_LANGUAGE: Default language for PII detection (default: de)
    - PRIVACY_LOG_DETECTIONS: Log detected entities (default: false, for debugging)
    """

    def __init__(
        self,
        enabled: Optional[bool] = None,
        language: str = 'de',
        log_detections: bool = False
    ):
        """
        Initialize privacy middleware.

        Args:
            enabled: Enable/disable middleware. If None, reads from PRIVACY_ENABLED env var.
            language: Default language for PII detection ('de' or 'en')
            log_detections: Log detected entities (useful for debugging, disable in production)
        """
        # Read from environment if not explicitly set
        # WICHTIG: Default MUSS auf 'false' stehen!
        # Presidio anonymisiert technische Begriffe, Variablennamen, Pfade etc. falsch.
        # Viele Features (Workflows, Code-Generierung, File-Paths) funktionieren nicht
        # wenn Privacy aktiviert ist. Nur für explizite DSGVO-Workloads aktivieren.
        if enabled is None:
            enabled = os.getenv('PRIVACY_ENABLED', 'false').lower() in ('true', '1', 'yes', 'on')

        self.enabled = enabled
        self.language = os.getenv('PRIVACY_LANGUAGE', language)
        self.log_detections = os.getenv('PRIVACY_LOG_DETECTIONS', str(log_detections)).lower() in ('true', '1', 'yes')

        # Lazy-initialize anonymizer
        self._anonymizer: Optional[PresidioAnonymizer] = None

        if self.enabled:
            logger.info(f"Privacy middleware enabled (language={self.language})")
        else:
            logger.info("Privacy middleware disabled")

    @property
    def anonymizer(self) -> PresidioAnonymizer:
        """Get or create anonymizer instance."""
        if self._anonymizer is None:
            self._anonymizer = PresidioAnonymizer(language=self.language)
        return self._anonymizer

    def is_available(self) -> bool:
        """Check if privacy middleware is available and enabled."""
        if not self.enabled:
            return False
        return self.anonymizer.is_available

    def should_anonymize(self, privacy_mode: Optional[str] = None) -> bool:
        """
        Check if anonymization should be performed.

        Privacy modes (from tenant settings):
        - "none": No anonymization (fast, for internal/technical data)
        - "basic": Basic anonymization (names, emails only)
        - "full": Full DSGVO-compliant anonymization (default)

        Args:
            privacy_mode: Per-request privacy mode override

        Returns:
            True if anonymization should be performed
        """
        # If middleware globally disabled, never anonymize
        if not self.enabled:
            return False

        # Check per-request privacy mode
        if privacy_mode == "none":
            return False

        return True

    def anonymize_message(
        self,
        content: str,
        privacy_mode: Optional[str] = None
    ) -> tuple[str, Dict[str, str]]:
        """
        Anonymize a single message content (synchronous version).

        Args:
            content: Message content to anonymize
            privacy_mode: Per-request privacy mode ("none", "basic", "full")

        Returns:
            Tuple of (anonymized_content, mapping)
        """
        if not self.should_anonymize(privacy_mode) or not content:
            return content, {}

        try:
            result = self.anonymizer.anonymize(content, self.language)

            if self.log_detections and result.detected_entities:
                logger.info(
                    f"PII detected: {result.entity_count} entities (mode={privacy_mode or 'default'})",
                    extra={
                        'entity_types': [e.entity_type for e in result.detected_entities],
                        'language': result.language,
                        'privacy_mode': privacy_mode
                    }
                )

            return result.anonymized_text, result.mapping

        except Exception as e:
            logger.error(f"Anonymization failed: {e}", exc_info=True)
            # Fail open: return original content if anonymization fails
            # This ensures the service remains available
            return content, {}

    async def anonymize_message_async(
        self,
        content: str,
        privacy_mode: Optional[str] = None
    ) -> tuple[str, Dict[str, str]]:
        """
        Anonymize a single message content (async version - non-blocking).

        Runs Presidio NLP in a thread pool to avoid blocking the event loop.

        Args:
            content: Message content to anonymize
            privacy_mode: Per-request privacy mode ("none", "basic", "full")

        Returns:
            Tuple of (anonymized_content, mapping)
        """
        if not self.should_anonymize(privacy_mode) or not content:
            return content, {}

        try:
            result = await self.anonymizer.anonymize_async(content, self.language)

            if self.log_detections and result.detected_entities:
                logger.info(
                    f"PII detected: {result.entity_count} entities (mode={privacy_mode or 'default'})",
                    extra={
                        'entity_types': [e.entity_type for e in result.detected_entities],
                        'language': result.language,
                        'privacy_mode': privacy_mode
                    }
                )

            return result.anonymized_text, result.mapping

        except Exception as e:
            logger.error(f"Anonymization failed: {e}", exc_info=True)
            # Fail open: return original content if anonymization fails
            return content, {}

    def anonymize_messages(
        self,
        messages: List[Dict[str, Any]],
        privacy_mode: Optional[str] = None
    ) -> tuple[List[Dict[str, Any]], Dict[str, str]]:
        """
        Anonymize all user messages in a message list.

        Only user messages are anonymized. System and assistant messages
        are passed through unchanged.

        Args:
            messages: List of message dicts with 'role' and 'content'
            privacy_mode: Per-request privacy mode ("none", "basic", "full")

        Returns:
            Tuple of (anonymized_messages, combined_mapping)
        """
        if not self.should_anonymize(privacy_mode):
            return messages, {}

        combined_mapping: Dict[str, str] = {}
        anonymized_messages = []

        for msg in messages:
            role = msg.get('role', '')
            content = msg.get('content', '')

            # Only anonymize user messages
            if role == 'user' and isinstance(content, str):
                anon_content, mapping = self.anonymize_message(content, privacy_mode)
                combined_mapping.update(mapping)

                anonymized_messages.append({
                    **msg,
                    'content': anon_content
                })
            else:
                anonymized_messages.append(msg)

        if combined_mapping:
            logger.debug(f"Anonymized {len(combined_mapping)} entities (mode={privacy_mode or 'default'})")

        return anonymized_messages, combined_mapping

    async def anonymize_messages_async(
        self,
        messages: List[Dict[str, Any]],
        privacy_mode: Optional[str] = None
    ) -> tuple[List[Dict[str, Any]], Dict[str, str]]:
        """
        Anonymize all user messages (async version - non-blocking).

        Uses asyncio.gather() to parallelize anonymization of multiple messages.

        Args:
            messages: List of message dicts with 'role' and 'content'
            privacy_mode: Per-request privacy mode ("none", "basic", "full")

        Returns:
            Tuple of (anonymized_messages, combined_mapping)
        """
        if not self.should_anonymize(privacy_mode):
            return messages, {}

        # Collect user messages that need anonymization
        user_message_indices = []
        for i, msg in enumerate(messages):
            if msg.get('role') == 'user' and isinstance(msg.get('content', ''), str):
                user_message_indices.append(i)

        if not user_message_indices:
            return messages, {}

        # Anonymize all user messages in parallel
        tasks = [
            self.anonymize_message_async(messages[i]['content'], privacy_mode)
            for i in user_message_indices
        ]
        results = await asyncio.gather(*tasks)

        # Build result
        combined_mapping: Dict[str, str] = {}
        anonymized_messages = list(messages)  # Copy

        for idx, (anon_content, mapping) in zip(user_message_indices, results):
            combined_mapping.update(mapping)
            anonymized_messages[idx] = {
                **messages[idx],
                'content': anon_content
            }

        if combined_mapping:
            logger.debug(f"Anonymized {len(combined_mapping)} entities async (mode={privacy_mode or 'default'})")

        return anonymized_messages, combined_mapping

    def deanonymize_response(self, content: str, mapping: Optional[Dict[str, str]] = None) -> str:
        """
        De-anonymize response content using stored or provided mapping.

        Args:
            content: Response content with ANON_XXX placeholders
            mapping: Mapping dict. If None, uses context variable.

        Returns:
            Original content with PII restored
        """
        if not self.enabled or not content:
            return content

        # Use provided mapping or get from context
        if mapping is None:
            mapping = _anonymization_context.get()

        if not mapping:
            return content

        try:
            return self.anonymizer.deanonymize(content, mapping)
        except Exception as e:
            logger.error(f"De-anonymization failed: {e}", exc_info=True)
            return content

    def deanonymize_streaming_chunk(
        self,
        chunk: str,
        buffer: str,
        mapping: Optional[Dict[str, str]] = None
    ) -> tuple[str, str]:
        """
        De-anonymize a streaming chunk with buffering for split placeholders.

        Handles the case where ANON_XXX placeholders are split across chunks:
        - Chunk 1: "text with ANON_IP_"
        - Chunk 2: "ADDRESS_001 more text"

        Args:
            chunk: Current streaming chunk
            buffer: Buffer from previous chunks (may contain partial placeholder)
            mapping: Mapping dict for de-anonymization

        Returns:
            Tuple of (text_to_yield, new_buffer)
            - text_to_yield: De-anonymized text safe to send to client
            - new_buffer: Text to buffer for next chunk (may contain partial placeholder)
        """
        import re

        if not self.enabled:
            return chunk, ""

        if mapping is None:
            mapping = _anonymization_context.get()

        if not mapping:
            return chunk, ""

        # Combine buffer with new chunk
        combined = buffer + chunk

        # Pattern to detect partial ANON_ placeholders at end of text
        # Full pattern: ANON_ENTITYTYPE_NNN (e.g., ANON_IP_ADDRESS_001, ANON_PERSON_001)
        # We need to buffer if text ends with partial pattern
        partial_pattern = r'ANON_[A-Z_]*\d*$'

        # Find if there's a partial placeholder at the end
        match = re.search(partial_pattern, combined)

        if match:
            # Check if this is a COMPLETE placeholder (exists in mapping)
            potential_placeholder = match.group(0)
            if potential_placeholder in mapping:
                # It's complete - de-anonymize everything
                result = self.anonymizer.deanonymize(combined, mapping)
                return result, ""
            else:
                # It's partial - buffer it for next chunk
                safe_text = combined[:match.start()]
                new_buffer = combined[match.start():]

                # De-anonymize the safe part
                if safe_text:
                    safe_text = self.anonymizer.deanonymize(safe_text, mapping)

                return safe_text, new_buffer
        else:
            # No partial placeholder - de-anonymize everything
            result = self.anonymizer.deanonymize(combined, mapping)
            return result, ""

    def flush_streaming_buffer(self, buffer: str, mapping: Optional[Dict[str, str]] = None) -> str:
        """
        Flush remaining buffer at end of stream.

        Called when stream ends to ensure any buffered content is sent.

        Args:
            buffer: Remaining buffer content
            mapping: Mapping dict for de-anonymization

        Returns:
            De-anonymized buffer content
        """
        if not buffer:
            return ""

        if not self.enabled:
            return buffer

        if mapping is None:
            mapping = _anonymization_context.get()

        if not mapping:
            return buffer

        try:
            return self.anonymizer.deanonymize(buffer, mapping)
        except Exception as e:
            logger.error(f"Buffer flush de-anonymization failed: {e}", exc_info=True)
            return buffer

    def set_context_mapping(self, mapping: Dict[str, str]) -> None:
        """Store mapping in request context for later de-anonymization."""
        _anonymization_context.set(mapping)

    def get_context_mapping(self) -> Optional[Dict[str, str]]:
        """Get mapping from request context."""
        return _anonymization_context.get()

    def clear_context_mapping(self) -> None:
        """Clear mapping from request context."""
        _anonymization_context.set(None)


# Singleton instance
_privacy_middleware: Optional[PrivacyMiddleware] = None


def get_privacy_middleware() -> PrivacyMiddleware:
    """
    Get the singleton privacy middleware instance.

    Returns:
        PrivacyMiddleware instance (created on first call)
    """
    global _privacy_middleware
    if _privacy_middleware is None:
        _privacy_middleware = PrivacyMiddleware()
    return _privacy_middleware


def anonymize_request(func: Callable) -> Callable:
    """
    Decorator for endpoint functions that handles anonymization/de-anonymization.

    Usage:
        @app.post("/v1/chat/completions")
        @anonymize_request
        async def chat_completions(request_body: ChatCompletionRequest, ...):
            ...
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        middleware = get_privacy_middleware()

        if not middleware.enabled:
            return await func(*args, **kwargs)

        # Find request_body in kwargs or args
        request_body = kwargs.get('request_body')
        if request_body is None and len(args) > 0:
            request_body = args[0]

        # Anonymize messages if present (using async for non-blocking)
        if hasattr(request_body, 'messages') and request_body.messages:
            original_messages = [
                {'role': m.role, 'content': m.content}
                for m in request_body.messages
            ]
            anon_messages, mapping = await middleware.anonymize_messages_async(original_messages)

            # Store mapping for response de-anonymization
            middleware.set_context_mapping(mapping)

            # Update request messages (create new Message objects)
            from src.models import Message
            request_body.messages = [
                Message(role=m['role'], content=m['content'])
                for m in anon_messages
            ]

        try:
            # Call original function
            result = await func(*args, **kwargs)

            # De-anonymize response if needed
            # This is handled in the streaming generator or response processing
            return result

        finally:
            # Clean up context
            middleware.clear_context_mapping()

    return wrapper
