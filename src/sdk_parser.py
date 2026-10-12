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


def _retain_cli_end_fields(parsed: Any, data: dict[str, Any]) -> None:
    """Keep the CLI fields that say HOW a message or run ended.

    claude-code-sdk 0.0.25 builds AssistantMessage(content, model,
    parent_tool_use_id) and ResultMessage without them, so they would be gone
    before any bridge code sees the chunk. Measured in CLI 2.1.296 (BRTRUNC,
    11.10.2026): an answer cut at the output limit arrives as an assistant
    message with top-level ``"error": "max_output_tokens"``, and the result
    carries ``stop_reason`` and ``api_error``. sdk_message_to_dict hands them
    on as ``cli_error`` / ``stop_reason`` / ``api_error``."""
    kept: dict[str, Any] = {}
    if data.get("type") == "assistant":
        if data.get("error"):
            kept["_bridge_cli_error"] = data["error"]
        # CACHEFIX: the CLI's flag for a turn that wraps an API error
        # (internal AssistantMessage.isApiErrorMessage, CLI 2.1.285).
        if data.get("is_api_error_message") is True:
            kept["_bridge_is_api_error_message"] = True
        message = data.get("message")
        if isinstance(message, dict) and "stop_reason" in message:
            kept["_bridge_stop_reason"] = message["stop_reason"]
        # BRREF (12.10.2026): a safety classifier stop arrives as stop_reason
        # "refusal" with stop_details {type, category, explanation}; usage and
        # id are per model call — the result's usage is the sum over calls.
        if isinstance(message, dict):
            if isinstance(message.get("stop_details"), dict):
                kept["_bridge_stop_details"] = message["stop_details"]
            if isinstance(message.get("usage"), dict):
                kept["_bridge_message_usage"] = message["usage"]
            if message.get("id"):
                kept["_bridge_message_id"] = message["id"]
    elif data.get("type") == "result":
        if "stop_reason" in data:
            kept["_bridge_stop_reason"] = data["stop_reason"]
        if data.get("api_error"):
            kept["_bridge_api_error"] = data["api_error"]
        if isinstance(data.get("api_error_status"), int):
            kept["_bridge_api_error_status"] = data["api_error_status"]
        if data.get("terminal_reason"):
            kept["_bridge_terminal_reason"] = data["terminal_reason"]
    for name, value in kept.items():
        try:
            setattr(parsed, name, value)
        except (AttributeError, TypeError):
            LOG.error("Could not retain the CLI field %s on an SDK message", name)


def resilient_parse_message(
    data: Any, original_parse_message: Callable[[Any], Any]
) -> Any:
    """Ignore informational unknown SDK events, but preserve real parse failures."""
    try:
        parsed = original_parse_message(data)
        if parsed is not None and isinstance(data, dict):
            _retain_cli_end_fields(parsed, data)
            # FK25B2: usage of completed model calls, for a job cancel's row.
            from src.jobs.teilverbrauch import aus_cli_nachricht

            aus_cli_nachricht(data)
        return parsed
    except MessageParseError as error:
        if "unknown message type" not in str(error).lower():
            raise
        message_type = (
            data.get("type", "unknown") if isinstance(data, dict) else "unknown"
        )
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
