"""/v1/research: how the final answer ended (BRTRUNC, 11.10.2026).

Fields antwort_ende and fehler_code.

Energy P4-AUSWAHL (B4) asked whether /v1/research says when the answer was cut
off. It did not: a run whose final message stopped at the output limit while the
run itself reported success went out under antwort_im_text as
antwort_quelle="text" — Energy then told the model "block missing" although the
answer had simply been too long.

How CLI 2.1.296 reports it (stream-json, read from the CLI bundle):
  * message_delta stop_reason "max_tokens" → a synthetic assistant message with
    top-level "error": "max_output_tokens" and the text
    "API Error: Claude's response exceeded the N output token maximum …";
  * non-interactive (-p, the bridge) it first withholds that message and
    continues up to 3 times with a meta user turn "Output token limit hit.
    Resume directly — …"; only when that is exhausted the error message is
    yielded; the result then carries stop_reason / api_error;
  * claude-code-sdk 0.0.25 drops "error", message.stop_reason, result
    stop_reason and api_error when it builds its dataclasses.

Guards:
  * the SDK parser keeps those fields, sdk_message_to_dict hands them on;
  * run_completion marks the assistant message that follows a CLI continuation;
  * antwort_ende(chunks) names the end of the final answer;
  * antwort_im_text + cut off → status=error, fehler_code="antwort_abgeschnitten",
    retryable=false — distinct from "no answer" and from error_max_turns;
  * file contract: the same field as information, the report file stays the answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.claude_cli as claude_cli
import src.main as main_module
from src.claude_cli import antwort_ende, sdk_message_to_dict

_CUT_TEXT = (
    "API Error: Claude's response exceeded the 64000 output token maximum. "
    "To configure this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable."
)
_META_TURN = (
    "Output token limit hit. Resume directly — no apology, no recap of what "
    "you were doing. Pick up mid-thought if that is where the cut happened. Break remaining work "
    "into smaller pieces."
)
_BLOCK_START = '```pruefwissen-auswahl\n{"dokument_ids": ["kw-1", "kw-2", '


def _assistant(text, **extra):
    return {"type": "assistant", "content": [{"type": "text", "text": text}], **extra}


def _tool(name="Read"):
    return {"type": "assistant", "content": [{"type": "tool_use", "name": name}]}


def _result(subtype="success", **extra):
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": False,
        "num_turns": 3,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        **extra,
    }


# --- SDK parser keeps the end fields --------------------------------------------


def test_parser_behaelt_cli_endfelder():
    sdk = pytest.importorskip("claude_code_sdk._internal.message_parser")
    from src.sdk_parser import resilient_parse_message

    assistant = resilient_parse_message(
        {
            "type": "assistant",
            "error": "max_output_tokens",
            "message": {
                "model": "m",
                "content": [{"type": "text", "text": _CUT_TEXT}],
                "stop_reason": "max_tokens",
            },
            "parent_tool_use_id": None,
            "session_id": "s",
        },
        sdk.parse_message,
    )
    d = sdk_message_to_dict(assistant)
    assert d["cli_error"] == "max_output_tokens"
    assert d["stop_reason"] == "max_tokens"

    result = resilient_parse_message(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "num_turns": 4,
            "duration_ms": 1,
            "duration_api_ms": 1,
            "session_id": "s",
            "result": _CUT_TEXT,
            "stop_reason": "max_tokens",
            "api_error": "max_output_tokens",
        },
        sdk.parse_message,
    )
    d = sdk_message_to_dict(result)
    assert d["stop_reason"] == "max_tokens"
    assert d["api_error"] == "max_output_tokens"
    assert claude_cli.is_result_chunk(d)


def test_parser_ohne_endfelder_fuegt_nichts_hinzu():
    sdk = pytest.importorskip("claude_code_sdk._internal.message_parser")
    from src.sdk_parser import resilient_parse_message

    msg = resilient_parse_message(
        {
            "type": "assistant",
            "message": {"model": "m", "content": []},
            "session_id": "s",
        },
        sdk.parse_message,
    )
    d = sdk_message_to_dict(msg)
    assert "cli_error" not in d and "stop_reason" not in d and "api_error" not in d


# --- run_completion marks the continued message ---------------------------------

# Stand-ins with the SDK's class NAMES (run_completion dispatches on them), as in
# tests/unit/test_cli_continuation_not_content.py.


@dataclass
class TextBlock:
    text: str


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


@pytest.mark.asyncio
async def test_run_completion_markiert_fortsetzung(monkeypatch, tmp_path):
    async def fake_query(prompt, options):
        for m in (
            AssistantMessage(
                content=[TextBlock(text="Hier die Auswahl:\n" + _BLOCK_START)],
                model="m",
            ),
            UserMessage(content=[TextBlock(text=_META_TURN)]),
            AssistantMessage(content=[TextBlock(text='"kw-3"]}\n```')], model="m"),
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=2,
                session_id="s",
                usage={"input_tokens": 1, "output_tokens": 1},
            ),
        ):
            yield m

    monkeypatch.setattr(claude_cli, "query", fake_query)
    cli = object.__new__(claude_cli.ClaudeCodeCLI)
    cli.timeout = 30
    cli.cwd = tmp_path
    cli.claude_env_vars = {}
    cli.cache_dir = tmp_path
    cli.max_cache_size_mb = 10
    cli.file_discovery = MagicMock()
    chunks = [c async for c in cli.run_completion(prompt="x", model="claude-sonnet-5")]

    assistants = [
        c for c in chunks if isinstance(c, dict) and isinstance(c.get("content"), list)
    ]
    assert len(assistants) == 2
    assert not assistants[0].get(claude_cli.FORTSETZUNG_KEY)
    assert assistants[1][claude_cli.FORTSETZUNG_KEY] is True
    assert antwort_ende(chunks) == "max_tokens_fortgesetzt"


# --- antwort_ende ---------------------------------------------------------------


def test_antwort_ende_normal():
    assert (
        antwort_ende([_assistant("fertig"), _result(stop_reason="end_turn")])
        == "end_turn"
    )


def test_antwort_ende_ohne_angabe_ist_none():
    assert antwort_ende([_assistant("fertig"), _result()]) is None


def test_antwort_ende_cli_fehler_an_der_schlussnachricht():
    chunks = [
        _assistant("Hier:\n" + _BLOCK_START),
        _assistant(_CUT_TEXT, cli_error="max_output_tokens"),
        _result(is_error=True),
    ]
    assert antwort_ende(chunks) == "max_tokens"


def test_antwort_ende_result_api_error():
    assert (
        antwort_ende([_assistant("x"), _result(api_error="max_output_tokens")])
        == "max_tokens"
    )


def test_fortsetzung_vor_letztem_werkzeug_zaehlt_nicht():
    """A continuation in an early working turn says nothing about the final answer."""
    chunks = [
        _assistant("Plan …", **{claude_cli.FORTSETZUNG_KEY: True}),
        _tool(),
        _assistant("Antwort"),
        _result(stop_reason="end_turn"),
    ]
    assert antwort_ende(chunks) == "end_turn"


# --- Handler ------------------------------------------------------------------------


def _req(**kw):
    d = dict(
        query="Wähle Prüfwissen aus.",
        model="claude-sonnet-4-5",
        depth="deep",
        strategy="planning",
        max_turns=45,
        max_hops=None,
        confidence_threshold=0.7,
        parallel_searches=5,
        source_filter=None,
        output_path=None,
        async_mode=False,
        backend=None,
        privacy=None,
        bedrock_region=None,
        research_mode=None,
        antwort_im_text=True,
    )
    d.update(kw)
    ns = MagicMock()
    for k, v in d.items():
        setattr(ns, k, v)
    return ns


async def _stream(*chunks):
    for c in chunks:
        yield c


async def _run(chunks, req):
    cli = MagicMock()
    cli.run_completion = MagicMock(return_value=_stream(*chunks))
    cli.parse_claude_message = MagicMock(return_value=None)
    cli.cwd = "/nirgends"
    with (
        patch("src.main.claude_cli", cli),
        patch("src.claude_cli.detect_quota_exhaustion", return_value=False),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock()),
    ):
        return await main_module._execute_research_impl(req, None, attribution_ctx=None)


def _metadata(tmp_path, name, text):
    d = tmp_path / "claudedocs"
    d.mkdir(exist_ok=True)
    (d / name).write_text(text, encoding="utf-8")
    return {
        "type": "x_claude_metadata",
        "files_created": [{"path": str(d / name)}],
        "session_tracking": {"research_dir": str(tmp_path)},
    }


@pytest.mark.asyncio
async def test_textvertrag_ausgabegrenze_erschoepft_scheitert_als_abgeschnitten():
    """CLI gave up continuing: error message last, result success flagged is_error."""
    chunks = [
        _assistant("Hier die Auswahl:\n" + _BLOCK_START),
        _assistant(_CUT_TEXT, cli_error="max_output_tokens"),
        _result(is_error=True, stop_reason="max_tokens", api_error="max_output_tokens"),
    ]
    res = await _run(chunks, _req())
    assert res.status == "error"
    assert res.fehler_code == "antwort_abgeschnitten"
    assert res.antwort_ende == "max_tokens"
    assert res.retryable is False
    assert "output limit" in res.error
    assert res.content is None


@pytest.mark.asyncio
async def test_textvertrag_fortgesetzte_antwort_ist_nur_der_rest():
    """CLI continued successfully: the final message is only the tail of the answer."""
    chunks = [
        _tool(),
        _assistant("Hier die Auswahl:\n" + _BLOCK_START),
        _assistant('"kw-3"]}\n```', **{claude_cli.FORTSETZUNG_KEY: True}),
        _result(stop_reason="end_turn"),
    ]
    res = await _run(chunks, _req())
    assert res.status == "error"
    assert res.fehler_code == "antwort_abgeschnitten"
    assert res.antwort_ende == "max_tokens_fortgesetzt"


@pytest.mark.asyncio
async def test_textvertrag_normales_ende_traegt_antwort_ende():
    res = await _run(
        [_assistant("Hier:\n```x\n{}\n```"), _result(stop_reason="end_turn")], _req()
    )
    assert res.status == "success", res.error
    assert res.antwort_quelle == "text"
    assert res.antwort_ende == "end_turn"
    assert res.fehler_code is None


@pytest.mark.asyncio
async def test_max_turns_ist_nicht_abgeschnitten():
    res = await _run(
        [_assistant("Ich rechne noch …"), _result("error_max_turns")], _req()
    )
    assert res.status == "error"
    assert res.fehler_code is None


@pytest.mark.asyncio
async def test_keine_antwort_ist_nicht_abgeschnitten():
    res = await _run([_tool(), _result(stop_reason="end_turn")], _req())
    assert res.status == "error"
    assert res.fehler_code is None


@pytest.mark.asyncio
async def test_dateivertrag_meldet_ende_als_auskunft(tmp_path):
    bericht = "# Research Report\n\n" + "x" * 800
    chunks = [
        _assistant("Bericht geschrieben, Zusammenfassung: " + _BLOCK_START),
        _assistant(_CUT_TEXT, cli_error="max_output_tokens"),
        _metadata(tmp_path, "output.md", bericht),
        _result(stop_reason="max_tokens"),
    ]
    res = await _run(chunks, _req(antwort_im_text=False))
    assert res.status == "success", res.error
    assert res.content == bericht
    assert res.antwort_quelle == "datei"
    assert res.antwort_ende == "max_tokens"
