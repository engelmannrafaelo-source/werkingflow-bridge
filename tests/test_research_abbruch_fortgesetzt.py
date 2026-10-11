"""/v1/research: the CLI's second automatic continuation (BRTRUNC2, 11.10.2026).

BRTRUNC recognised the continuation after the output limit ("Output token
limit hit. Resume directly"). The worker CLI (docker/Dockerfile.worker pin
2.1.285) has a second one, truncated_response_recovery: when the API stream
dies after some output it withholds its "Connection lost" error
(error "server_error") and appends a meta user turn "Your response above was
cut off mid-stream. Resume directly from where it stops …" — on the same
counter, up to 3 times. The last assistant message is then only the tail of
the answer, exactly as after the output limit; on 276d146 it went out as the
answer with antwort_ende "end_turn".

What the CLI hands out (read from the 2.1.285 bundle, nothing executed):
  * both meta turns are ``Ce({content: …, isMeta: true})``; on the wire a user
    message with ``isSynthetic: isMeta || …`` → true;
  * the kind (``transition.reason`` max_output_tokens_recovery /
    truncated_response_recovery) stays inside the CLI — no field carries it.
So: structure decides "a CLI turn" (user turn, not isSynthetic=false), the
CLI's own sentence decides the kind. The pin test below fails when the worker
CLI changes, so the sentences get re-read against the new version.

Guards:
  * antwort_ende "abbruch_fortgesetzt", fehler_code antwort_abgeschnitten,
    retryable=false under antwort_im_text;
  * the continuation marker is reset after it was set once (BRTRUNCR mutant MC);
  * teilantwort carries the joined parts next to the error, never as content.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import src.claude_cli as claude_cli
from src.claude_cli import antwort_ende, cli_fortsetzung, teilantwort
from tests.test_research_antwort_ende import _assistant, _req, _result, _run, _tool

sdk = pytest.importorskip("claude_code_sdk._internal.message_parser")
from src.sdk_parser import resilient_parse_message  # noqa: E402

# Verbatim from CLI 2.1.285 (NUt, LUt).
_ABBRUCH_TURN = (
    "Your response above was cut off mid-stream. Resume directly from where it "
    "stops — no apology, no recap. If none of it survived, answer the request "
    "from the start."
)
_GRENZE_TURN = (
    "Output token limit hit. Resume directly — no apology, no recap of what you "
    "were doing. Pick up mid-thought if that is where the cut happened. Break "
    "remaining work into smaller pieces."
)
# Verbatim from the real CLI 2.1.285 run with exhausted continuations (BRTRUNC2R).
_LOST_TEXT = "API Error: Connection lost mid-response. The response above may be incomplete."
_BLOCK_START = '```pruefwissen-auswahl\n{"dokument_ids": ["kw-1", "kw-2", '
_BLOCK_REST = '"kw-3"]}\n```'

WORKER_DOCKERFILE = (
    Path(__file__).resolve().parent.parent / "docker" / "Dockerfile.worker"
)
# Same CLI, own image (BRTRUNC2R note): both must move together.
CLI_DOCKERFILES = (
    WORKER_DOCKERFILE,
    Path(__file__).resolve().parent.parent / "docker" / "Dockerfile.erkunder",
)


# --- raw stream-json, through the real SDK parser and run_completion --------------


def _a(text, error=None):
    d = {
        "type": "assistant",
        "message": {"model": "m", "content": [{"type": "text", "text": text}]},
        "parent_tool_use_id": None,
        "session_id": "s",
    }
    if error:
        d["error"] = error
    return d


def _u(text, synthetic=True):
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        "parent_tool_use_id": None,
        "session_id": "s",
        "isSynthetic": synthetic,
    }


def _tool_raw():
    return {
        "type": "assistant",
        "message": {
            "model": "m",
            "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}],
        },
        "parent_tool_use_id": None,
        "session_id": "s",
    }


def _tool_result_raw():
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
        "parent_tool_use_id": None,
        "session_id": "s",
    }


def _r(**kw):
    d = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 2,
        "duration_ms": 1,
        "duration_api_ms": 1,
        "session_id": "s",
        "result": "x",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    d.update(kw)
    return d


async def _durch_run_completion(raw, monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    async def fake_query(prompt, options):
        for d in raw:
            yield resilient_parse_message(d, sdk.parse_message)

    monkeypatch.setattr(claude_cli, "query", fake_query)
    cli = object.__new__(claude_cli.ClaudeCodeCLI)
    cli.timeout = 30
    cli.cwd = tmp_path
    cli.claude_env_vars = {}
    cli.cache_dir = tmp_path
    cli.max_cache_size_mb = 10
    cli.file_discovery = MagicMock()
    return [c async for c in cli.run_completion(prompt="x", model="claude-sonnet-5")]


@pytest.mark.asyncio
async def test_abbruch_fortgesetzt_ueber_echten_parser(monkeypatch, tmp_path):
    raw = [
        _a("Hier:\n" + _BLOCK_START),
        _u(_ABBRUCH_TURN),
        _a(_BLOCK_REST),
        _r(stop_reason="end_turn"),
    ]
    chunks = await _durch_run_completion(raw, monkeypatch, tmp_path)
    assistants = [
        c for c in chunks if isinstance(c, dict) and isinstance(c.get("content"), list)
    ]
    assert len(assistants) == 2
    assert assistants[1][claude_cli.ABBRUCH_FORTSETZUNG_KEY] is True
    assert not assistants[1].get(claude_cli.FORTSETZUNG_KEY)
    assert antwort_ende(chunks) == "abbruch_fortgesetzt"
    assert teilantwort(chunks) == "Hier:\n" + _BLOCK_START + _BLOCK_REST


@pytest.mark.asyncio
async def test_abbruch_erschoepft_ueber_echten_parser(monkeypatch, tmp_path):
    """3 continuations, then the withheld Connection-lost error comes out."""
    raw = [_a("T1 " + _BLOCK_START)]
    for teil in ("T2", "T3", "T4"):
        raw += [_u(_ABBRUCH_TURN), _a(teil)]
    raw += [_a(_LOST_TEXT, error="server_error"), _r(is_error=True)]
    chunks = await _durch_run_completion(raw, monkeypatch, tmp_path)
    assert antwort_ende(chunks) == "abbruch_fortgesetzt"
    # The CLI's own error message is not part of the answer (BRTRUNC2N).
    assert teilantwort(chunks) == "T1 " + _BLOCK_START + "T2T3T4"


@pytest.mark.asyncio
async def test_parser_behaelt_issynthetic(monkeypatch, tmp_path):
    msg = resilient_parse_message(_u(_ABBRUCH_TURN), sdk.parse_message)
    assert msg._bridge_is_synthetic is True
    assert cli_fortsetzung(msg) == claude_cli.ABBRUCH_FORTSETZUNG_KEY


def test_nicht_synthetischer_turn_ist_keine_fortsetzung():
    """A real prompt quoting the sentence is not the CLI's turn."""
    msg = resilient_parse_message(_u(_ABBRUCH_TURN, synthetic=False), sdk.parse_message)
    assert cli_fortsetzung(msg) is None
    assert (
        cli_fortsetzung(resilient_parse_message(_tool_result_raw(), sdk.parse_message))
        is None
    )


def test_ausgabegrenze_bleibt_eigener_wert():
    msg = resilient_parse_message(_u(_GRENZE_TURN), sdk.parse_message)
    assert cli_fortsetzung(msg) == claude_cli.FORTSETZUNG_KEY


# --- BRTRUNCR mutant MC: the marker is reset after use ----------------------------


@pytest.mark.asyncio
async def test_fortsetzung_frueh_dann_werkzeug_dann_antwort(monkeypatch, tmp_path):
    """From BRTRUNCR (test_brtruncr_mc.py): continuation, tool call, complete
    answer. If the marker stayed set, the final answer would be marked too."""
    raw = [
        _a("Plan T1"),
        _u(_GRENZE_TURN),
        _a("Plan T2"),
        _tool_raw(),
        _tool_result_raw(),
        _a("Vollstaendige Antwort"),
        _r(stop_reason="end_turn"),
    ]
    chunks = await _durch_run_completion(raw, monkeypatch, tmp_path)
    assert antwort_ende(chunks) == "end_turn"


@pytest.mark.asyncio
async def test_abbruchmarke_wird_zurueckgesetzt(monkeypatch, tmp_path):
    raw = [
        _a("Plan T1"),
        _u(_ABBRUCH_TURN),
        _a("Plan T2"),
        _a("Zwischenstand"),
        _r(stop_reason="end_turn"),
    ]
    chunks = await _durch_run_completion(raw, monkeypatch, tmp_path)
    assistants = [
        c for c in chunks if isinstance(c, dict) and isinstance(c.get("content"), list)
    ]
    assert [bool(c.get(claude_cli.ABBRUCH_FORTSETZUNG_KEY)) for c in assistants] == [
        False,
        True,
        False,
    ]


# --- antwort_ende / teilantwort -----------------------------------------------------


def test_abbruch_vor_letztem_werkzeug_zaehlt_nicht():
    chunks = [
        _assistant("Plan …", **{claude_cli.ABBRUCH_FORTSETZUNG_KEY: True}),
        _tool(),
        _assistant("Antwort"),
        _result(stop_reason="end_turn"),
    ]
    assert antwort_ende(chunks) == "end_turn"


def test_teilantwort_nur_nach_letztem_werkzeug():
    chunks = [_assistant("Plan"), _tool(), _assistant("A"), _assistant("B"), _result()]
    assert teilantwort(chunks) == "AB"
    assert teilantwort([_tool(), _result()]) is None


# --- Handler ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_textvertrag_abbruch_fortgesetzt_scheitert_als_abgeschnitten():
    chunks = [
        _tool(),
        _assistant("Hier die Auswahl:\n" + _BLOCK_START),
        _assistant(_BLOCK_REST, **{claude_cli.ABBRUCH_FORTSETZUNG_KEY: True}),
        _result(stop_reason="end_turn"),
    ]
    res = await _run(chunks, _req())
    assert res.status == "error"
    assert res.fehler_code == "antwort_abgeschnitten"
    assert res.antwort_ende == "abbruch_fortgesetzt"
    assert res.retryable is False
    assert res.content is None
    assert res.error.startswith("Research answer cut off mid-stream")
    assert res.teilantwort == "Hier die Auswahl:\n" + _BLOCK_START + _BLOCK_REST


@pytest.mark.asyncio
async def test_textvertrag_ausgabegrenze_traegt_teilantwort():
    chunks = [
        _assistant("Hier die Auswahl:\n" + _BLOCK_START),
        _assistant(_BLOCK_REST, **{claude_cli.FORTSETZUNG_KEY: True}),
        _result(stop_reason="end_turn"),
    ]
    res = await _run(chunks, _req())
    assert res.fehler_code == "antwort_abgeschnitten"
    assert res.error.startswith("Research answer cut off at the output limit")
    assert res.teilantwort == "Hier die Auswahl:\n" + _BLOCK_START + _BLOCK_REST


@pytest.mark.asyncio
async def test_teilantwort_nur_bei_abgeschnitten():
    res = await _run(
        [_assistant("Ich rechne noch …"), _result("error_max_turns")], _req()
    )
    assert res.status == "error"
    assert res.teilantwort is None
    ok = await _run(
        [_assistant("Hier:\n```x\n{}\n```"), _result(stop_reason="end_turn")], _req()
    )
    assert ok.status == "success", ok.error
    assert ok.teilantwort is None


@pytest.mark.asyncio
async def test_dateivertrag_abbruch_nur_auskunft(tmp_path):
    from tests.test_research_antwort_ende import _metadata

    bericht = "# Research Report\n\n" + "x" * 800
    chunks = [
        _assistant("Zusammenfassung " + _BLOCK_START),
        _assistant(_BLOCK_REST, **{claude_cli.ABBRUCH_FORTSETZUNG_KEY: True}),
        _metadata(tmp_path, "output.md", bericht),
        _result(stop_reason="end_turn"),
    ]
    res = await _run(chunks, _req(antwort_im_text=False))
    assert res.status == "success", res.error
    assert res.content == bericht
    assert res.antwort_ende == "abbruch_fortgesetzt"
    assert res.teilantwort is None


# --- Pin: the sentences are verified against ONE worker CLI version ---------------


def test_cli_saetze_gegen_worker_pin_geprueft():
    """Fails when docker/Dockerfile.worker or .erkunder moves to another CLI version: then
    re-read both continuation sentences (strings on the npm package, search
    'Resume directly') and update CLI_CONTINUATION_VERIFIED_PIN."""
    pins = [
        pin
        for dockerfile in CLI_DOCKERFILES
        for pin in re.findall(
            r"@anthropic-ai/claude-code@([0-9][0-9A-Za-z.\-]*)", dockerfile.read_text()
        )
    ]
    assert len(pins) >= len(CLI_DOCKERFILES), f"claude-code pin missing in {CLI_DOCKERFILES}"
    assert set(pins) == {claude_cli.CLI_CONTINUATION_VERIFIED_PIN}, (
        f"worker CLI pin {pins} != {claude_cli.CLI_CONTINUATION_VERIFIED_PIN}: "
        f"re-verify CLI_CONTINUATION_SENTENCE / CLI_ABBRUCH_SENTENCE"
    )
    assert _ABBRUCH_TURN.startswith(claude_cli.CLI_ABBRUCH_SENTENCE)
    assert _GRENZE_TURN.startswith(claude_cli.CLI_CONTINUATION_SENTENCE)
