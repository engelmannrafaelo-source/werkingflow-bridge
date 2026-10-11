"""Real CLI 2.1.285 streams as fixtures (BRTRUNC2N, from BRTRUNC2R).

The streams in tests/fixtures/cli_2_1_285_stroeme/ are what the real CLI wrote
against a local stand-in for the Messages API (no AI call): the stand-in cut the
stream after a finished text block (abbruch), did so on every continuation
(abbruch_immer), or stopped at max_tokens (grenze). Only assistant/user/result
lines are kept. They are the only evidence from the real wire; the hand-built
streams in test_research_abbruch_fortgesetzt.py follow them.

Also here: the precedence of the output limit over a stream break when both
continuations sit in one final answer (BRTRUNC2R mutant MX4).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.claude_cli as claude_cli
from src.claude_cli import antwort_ende, teilantwort
from tests.test_research_abbruch_fortgesetzt import (
    _ABBRUCH_TURN,
    _GRENZE_TURN,
    _a,
    _durch_run_completion,
    _r,
    _u,
)
from tests.test_research_antwort_ende import _assistant, _req, _result, _run

STROEME = Path(__file__).parent / "fixtures" / "cli_2_1_285_stroeme"
_ANFANG = 'Hier:\n```pruefwissen-auswahl\n{"dokument_ids": ["kw-1", "kw-2", '
_REST = '"kw-3"]}\n```'


def _strom(name):
    return [json.loads(z) for z in (STROEME / f"{name}.jsonl").read_text().splitlines() if z.strip()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,ende,teil",
    [
        ("grenze", "max_tokens_fortgesetzt", _ANFANG + _REST),
        ("abbruch", "abbruch_fortgesetzt", _ANFANG + _REST),
        ("abbruch_immer", "abbruch_fortgesetzt", _ANFANG + "T-weiter " * 3),
    ],
)
async def test_echter_cli_strom(name, ende, teil, monkeypatch, tmp_path):
    chunks = await _durch_run_completion(_strom(name), monkeypatch, tmp_path)
    assert antwort_ende(chunks) == ende
    assert teilantwort(chunks) == teil
    res = await _run(chunks, _req())
    assert res.status == "error"
    assert res.fehler_code == "antwort_abgeschnitten"
    assert res.retryable is False
    assert res.content is None
    assert res.antwort_ende == ende
    assert res.teilantwort == teil


@pytest.mark.asyncio
async def test_erschoepft_teilantwort_ohne_cli_fehlertext(monkeypatch, tmp_path):
    """The real exhausted run ends in the CLI's own message ("API Error:
    Connection lost mid-response...", error=server_error). It is the CLI's,
    not the model's, and stays out of teilantwort."""
    raw = _strom("abbruch_immer")
    letzte = [d for d in raw if d.get("type") == "assistant"][-1]
    assert letzte.get("error") == "server_error"
    assert "Connection lost mid-response" in json.dumps(letzte)
    chunks = await _durch_run_completion(raw, monkeypatch, tmp_path)
    assert "API Error" not in teilantwort(chunks)


# --- MX4: output limit takes precedence over a stream break ----------------------


@pytest.mark.parametrize("reihenfolge", ["grenze_zuerst", "abbruch_zuerst"])
def test_ausgabegrenze_vor_abbruch_im_mischfall(reihenfolge):
    """Both continuations in one final answer: the result is the output-limit
    value, whatever came first — max_tokens_fortgesetzt is the one Energy's
    text fallback knows ("cut off at the output limit")."""
    grenze = _assistant("B", **{claude_cli.FORTSETZUNG_KEY: True})
    abbruch = _assistant("C", **{claude_cli.ABBRUCH_FORTSETZUNG_KEY: True})
    mitte = [grenze, abbruch] if reihenfolge == "grenze_zuerst" else [abbruch, grenze]
    chunks = [_assistant("A"), *mitte, _result(stop_reason="end_turn")]
    assert antwort_ende(chunks) == "max_tokens_fortgesetzt"


@pytest.mark.asyncio
async def test_ausgabegrenze_vor_abbruch_ueber_echten_parser(monkeypatch, tmp_path):
    raw = [
        _a("T1"),
        _u(_ABBRUCH_TURN),
        _a("T2"),
        _u(_GRENZE_TURN),
        _a("T3"),
        _r(stop_reason="end_turn"),
    ]
    chunks = await _durch_run_completion(raw, monkeypatch, tmp_path)
    assert antwort_ende(chunks) == "max_tokens_fortgesetzt"
    res = await _run(chunks, _req())
    assert res.antwort_ende == "max_tokens_fortgesetzt"
    assert res.teilantwort == "T1T2T3"
