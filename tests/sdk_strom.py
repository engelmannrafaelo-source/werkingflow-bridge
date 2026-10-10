"""The stream shape run_completion really hands on, for test fakes.

claude_code_sdk only lives in the worker image, so these are stand-ins with the
SDK's class NAMES and fields (claude_code_sdk 0.0.25 types.py; 0.0.22 has the
same shape). tests/unit/test_sdk_strom_form.py compares them with the real
dataclasses wherever the SDK is installed.

`chunk()` runs a message through the same conversion as run_completion
(claude_cli.sdk_message_to_dict). A real run starts with the system init
message; the CLI also sends system messages mid-run (compact_boundary, status).
Converted, a system message is {'subtype', 'data'} and a result message has no
'type' key either — fakes that only yield {"type": "result", ...} miss both.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from src.claude_cli import sdk_message_to_dict


@dataclass
class TextBlock:
    text: str


@dataclass
class AssistantMessage:
    content: list
    model: str
    parent_tool_use_id: Optional[str] = None


@dataclass
class SystemMessage:
    subtype: str
    data: dict


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


def chunk(message: Any) -> Any:
    return sdk_message_to_dict(message)


def init(session_id: str = "cli-sess-1", model: str = "claude-sonnet-4-5") -> SystemMessage:
    return SystemMessage(subtype="init", data={
        "type": "system", "subtype": "init", "session_id": session_id, "model": model,
        "cwd": "/w", "tools": ["Read", "Write", "Edit", "WebSearch"], "permissionMode": "bypassPermissions",
    })


def compact_boundary(session_id: str = "cli-sess-1") -> SystemMessage:
    return SystemMessage(subtype="compact_boundary", data={
        "type": "system", "subtype": "compact_boundary", "session_id": session_id,
        "compact_metadata": {"trigger": "auto", "pre_tokens": 150000},
    })


def result(subtype: str = "success", *, is_error: bool = False, session_id: str = "cli-sess-1",
           usage: Optional[dict] = None, num_turns: int = 3) -> ResultMessage:
    return ResultMessage(
        subtype=subtype, duration_ms=1200, duration_api_ms=1100, is_error=is_error,
        num_turns=num_turns, session_id=session_id, total_cost_usd=0.01,
        usage=usage, result=None,
    )


def stream_chunks(*body: Any, session_id: str = "cli-sess-1") -> list:
    """init first, the body with a system message after its first element —
    all converted like run_completion does. Dicts in `body` pass unchanged."""
    body = list(body)
    mitte = [compact_boundary(session_id)]
    return [chunk(m) for m in [init(session_id), *body[:1], *mitte, *body[1:]]]
