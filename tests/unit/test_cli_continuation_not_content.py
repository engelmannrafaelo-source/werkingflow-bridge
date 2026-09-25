"""User/meta turns from the CLI must never reach callers as content.

Befund 24.09.2026 (werking-energy, Mühl-Bericht): Claude Code recovers from a
max_output_tokens stop by appending a META user turn "Output token limit hit.
Resume directly — no apology, no recap …". The SDK surfaces it as
UserMessage(content=[TextBlock]). run_completion converted it into a type-less
dict {"content": [TextBlock]} — the same shape every consumer reads as
assistant text — and the instruction was streamed into a customer report.

These tests drive the real run_completion loop with a fake SDK stream.
"""
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from dataclasses import dataclass, field
from typing import Any, Optional

# Local stand-ins with the SDK's class NAMES and fields (claude_code_sdk 0.0.22).
# run_completion dispatches on type(message).__name__ and attributes, so these
# behave exactly like the real dataclasses — and the test runs where the SDK
# is not installed (it only lives in the worker image).


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any = None
    is_error: Optional[bool] = None


@dataclass
class UserMessage:
    content: Any


@dataclass
class AssistantMessage:
    content: list
    model: str


@dataclass
class ResultMessage:
    subtype: str
    duration_ms: int
    duration_api_ms: int
    is_error: bool
    num_turns: int
    session_id: str
    total_cost_usd: Optional[float] = None
    usage: Optional[dict] = field(default=None)
    result: Optional[str] = None


import src.claude_cli as claude_cli
from src.claude_cli import ClaudeCodeCLI

# Deliberately a local literal, not claude_cli.CLI_CONTINUATION_SENTENCE:
# the behaviour tests must fail on the pre-fix code for the right reason.
CLI_CONTINUATION_SENTENCE = "Output token limit hit. Resume directly"

CLI_META_TURN = (
    "Output token limit hit. Resume directly — no apology, no recap of what you "
    "were doing. Pick up mid-thought if that is where the cut happened. "
    "Break remaining work into smaller pieces."
)


def _result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=2,
        session_id="s",
        total_cost_usd=0.0,
        usage={"input_tokens": 1, "output_tokens": 64000},
        result="",
    )


def _make_cli(tmp_path: Path) -> ClaudeCodeCLI:
    cli = object.__new__(ClaudeCodeCLI)
    cli.timeout = 30
    cli.cwd = tmp_path
    cli.claude_env_vars = {}
    cli.cache_dir = tmp_path
    cli.max_cache_size_mb = 10
    cli.file_discovery = MagicMock()
    return cli


async def _drive(monkeypatch, tmp_path, sdk_messages):
    async def fake_query(prompt, options):
        for m in sdk_messages:
            yield m

    monkeypatch.setattr(claude_cli, "query", fake_query)
    cli = _make_cli(tmp_path)
    return [c async for c in cli.run_completion(prompt="Schreib den Text", model="claude-sonnet-5")]


def _streamed_text(chunks) -> str:
    """Exactly what main.generate_streaming_response turns into SSE deltas."""
    out = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        content = chunk.get("content")
        if isinstance(content, list):
            for block in content:
                if hasattr(block, "text"):
                    out.append(block.text)
                elif isinstance(block, dict) and block.get("type") == "text":
                    out.append(block.get("text", ""))
    return "".join(out)


@pytest.mark.asyncio
async def test_cli_continuation_turn_is_not_streamed_as_content(monkeypatch, tmp_path):
    chunks = await _drive(
        monkeypatch,
        tmp_path,
        [
            AssistantMessage(content=[TextBlock(text="Montag, Dienstag, Mittwoch")], model="m"),
            UserMessage(content=[TextBlock(text=CLI_META_TURN)]),
            AssistantMessage(content=[TextBlock(text=", Donnerstag")], model="m"),
            _result(),
        ],
    )
    text = _streamed_text(chunks)
    assert CLI_CONTINUATION_SENTENCE not in text
    assert text == "Montag, Dienstag, Mittwoch, Donnerstag"


@pytest.mark.asyncio
async def test_no_user_turn_is_yielded_at_all(monkeypatch, tmp_path):
    """Tool results are user turns as well — none of them is content."""
    chunks = await _drive(
        monkeypatch,
        tmp_path,
        [
            UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="x", is_error=False)]),
            UserMessage(content="plain string user turn"),
            AssistantMessage(content=[TextBlock(text="ok")], model="m"),
            _result(),
        ],
    )
    assert _streamed_text(chunks) == "ok"
    for c in chunks:
        assert not (isinstance(c, dict) and c.get("content") in ("plain string user turn",))
        assert not (
            isinstance(c, dict)
            and isinstance(c.get("content"), list)
            and any(type(b).__name__ == "ToolResultBlock" for b in c["content"])
        )


@pytest.mark.asyncio
async def test_sentence_in_assistant_text_warns_loud(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger=claude_cli.logger.name)
    await _drive(
        monkeypatch,
        tmp_path,
        [AssistantMessage(content=[TextBlock(text="x " + CLI_META_TURN)], model="m"), _result()],
    )
    assert any("continuation instruction" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)


def test_warn_helper_carries_app_and_job(caplog):
    from src.claude_cli import warn_if_continuation_leak

    caplog.set_level(logging.WARNING, logger=claude_cli.logger.name)
    assert warn_if_continuation_leak(
        "a " + CLI_META_TURN, where="chat_stream", app="werking-energy", job="j-1"
    )
    msg = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING][-1]
    assert "app=werking-energy" in msg and "job=j-1" in msg
    assert not warn_if_continuation_leak("normaler Kundentext", where="chat_stream")


def test_is_user_turn_shapes():
    from src.claude_cli import is_user_turn

    assert is_user_turn(UserMessage(content="x"))
    assert is_user_turn({"type": "user", "message": {}})
    assert not is_user_turn(AssistantMessage(content=[TextBlock(text="x")], model="m"))
    assert not is_user_turn({"content": [TextBlock(text="x")]})
    assert not is_user_turn(None)
