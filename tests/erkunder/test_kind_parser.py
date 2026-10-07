import sys
import types

import pytest
from claude_code_sdk._errors import MessageParseError

from src.erkunder import kind
from src.erkunder.platz import child_failure_reason
from src.sdk_parser import RateLimitEvent, resilient_parse_message


def unknown_message(_: object):
    raise MessageParseError("Unknown message type: synthetic")


@pytest.mark.parametrize("message_type", ["rate_limit_event", "future_event"])
def test_harmless_unknown_sdk_messages_are_skipped(message_type, caplog):
    parsed = resilient_parse_message({"type": message_type}, unknown_message)
    if message_type == "rate_limit_event":
        assert isinstance(parsed, RateLimitEvent)
    else:
        assert parsed is None
    assert f"{message_type}" in caplog.text


def test_real_sdk_parse_error_is_raised():
    def malformed(_: object):
        raise MessageParseError("invalid SDK message payload")

    with pytest.raises(MessageParseError, match="invalid SDK message payload"):
        resilient_parse_message({"type": "assistant"}, malformed)


@pytest.mark.asyncio
async def test_step_continues_after_harmless_sdk_messages(monkeypatch, tmp_path):
    class ResultMessage:
        def __init__(self):
            self.is_error = False
            self.usage = {"input_tokens": 2, "output_tokens": 3}
            self.num_turns = 1

    class ClaudeCodeOptions:
        def __init__(self, **_kwargs):
            pass

    async def query(**_kwargs):
        yield RateLimitEvent({"type": "rate_limit_event"})
        yield None  # An unknown type is intentionally skipped by the parser.
        yield ResultMessage()

    monkeypatch.setattr(kind, "install_resilient_parser", lambda: None)
    monkeypatch.setitem(
        sys.modules,
        "claude_code_sdk",
        types.SimpleNamespace(
            ClaudeCodeOptions=ClaudeCodeOptions,
            ResultMessage=ResultMessage,
            query=query,
        ),
    )
    output = await kind.run(
        {
            "ordner": str(tmp_path),
            "prompt": "synthetic",
            "max_turns": 1,
            "claude_token": "synthetic",
        }
    )
    assert output["zuege"] == 1


def test_real_parse_error_reason_reaches_step_status():
    error = MessageParseError("invalid SDK message payload")
    assert kind.failure_reason(error) == "cli_fehler: SDK-Lauf: MessageParseError"
    assert child_failure_reason(
        b'{"fehler":"cli_fehler: SDK-Lauf: MessageParseError"}'
    ) == "cli_fehler: SDK-Lauf: MessageParseError"
