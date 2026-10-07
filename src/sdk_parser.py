"""Tolerant Claude Code SDK message parsing shared by workers and Erkunder."""

import logging
from collections.abc import Callable
from typing import Any

from claude_code_sdk._errors import MessageParseError

LOG = logging.getLogger(__name__)
_SKIPPED_TYPES_LOG: set[str] = set()


class RateLimitEvent:
    """A rate-limit notification emitted by Claude Code while it retries."""

    def __init__(self, data: dict[str, Any]):
        self.type = "rate_limit_event"
        self.retry_after = data.get("retry_after")
        self.reset_at = data.get("reset_at")
        self.message = data.get("message", "")
        self.raw = data

    def __repr__(self) -> str:
        return (
            "RateLimitEvent("
            f"retry_after={self.retry_after}, message={self.message[:80]})"
        )


def resilient_parse_message(
    data: Any, original_parse_message: Callable[[Any], Any]
) -> Any:
    """Ignore informational unknown SDK events, but preserve real parse failures."""
    try:
        parsed = original_parse_message(data)
        if (
            parsed is not None
            and isinstance(data, dict)
            and data.get("type") == "assistant"
            and data.get("error")
        ):
            try:
                parsed._bridge_cli_error = data["error"]
            except (AttributeError, TypeError):
                LOG.error("Could not retain the CLI error field on an SDK message")
        return parsed
    except MessageParseError as error:
        if "unknown message type" not in str(error).lower():
            raise
        message_type = data.get("type", "unknown") if isinstance(data, dict) else "unknown"
        if message_type == "rate_limit_event":
            LOG.info("Skipping informational SDK message type: rate_limit_event")
            return RateLimitEvent(data)
        if message_type not in _SKIPPED_TYPES_LOG:
            LOG.info("Skipping unrecognized SDK message type: %s", message_type)
            _SKIPPED_TYPES_LOG.add(message_type)
        return None


def install_resilient_parser() -> Callable[[Any], Any]:
    """Patch the SDK client once and return the parser used by its stream."""
    import claude_code_sdk._internal.client as sdk_client
    from claude_code_sdk._internal.message_parser import parse_message

    current = sdk_client.parse_message
    if getattr(current, "_bridge_resilient_parser", False):
        return current

    def parser(data: Any) -> Any:
        return resilient_parse_message(data, parse_message)

    parser._bridge_resilient_parser = True  # type: ignore[attr-defined]
    sdk_client.parse_message = parser
    return parser
