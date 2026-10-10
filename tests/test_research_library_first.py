"""Library first, web second — on both research paths (Befund c76ab7fb, 29.09.2026).

The CLI protocol (research_protocol.py, the path Energy uses) named only the
web in step 1; the library sat in an appended block that ranked it only
conditionally ("Deckt ein Eintrag … ab"). Measured: 17 WebSearch / 7 WebFetch
against 2 library reads with 204 documents in the folder.

Guards:
  * with a library: library step first, web only for gaps/currency, source
    list split "Bibliothek: <id>" / "Web: <URL>", origin per number;
  * without a library: output byte-identical to develop before this change
    (snapshots in tests/fixtures/research_prompt_snapshots/, captured from
    origin/develop 95ea470; the two output-file lines changed deliberately in
    BR2, 10.10.2026 — one output file, named under OUTPUT_FILE_PATH).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.research_cloud.prompt import (
    build_library_catalogue,
    build_pool_library_catalogue,
    build_system_prompt,
)
from src.research_library_pool import LIBRARY_WORKDIR_NAME
from src.research_protocol import build_research_execution_prompt

_SNAP = Path(__file__).parent / "fixtures" / "research_prompt_snapshots"
_DEPTHS = ("quick", "standard", "deep", "exhaustive")
_QUERY = "Welche U-Werte fordert OIB-RL 6?"

_INDEX = {
    "documents": [
        {"id": "oib-rl-6", "title": "OIB-Richtlinie 6", "jurisdiction": "AT",
         "file": "oib-rl-6.md"},
    ]
}

_STEP1 = (
    f"1. Durchsuche {LIBRARY_WORKDIR_NAME}/ (Grep nach Begriffen, Normnummern, Werten) "
    "und lies alle passenden Dokumente."
)
_STEP2 = "2. WebSearch/WebFetch nur für das, was dort fehlt, und für Aktualität/Fassungsstand"


def _raw(depth: str) -> str:
    return f'/sc:research "{_QUERY}" --depth {depth} --strategy planning'


# --- CLI path: without library ----------------------------------------------


@pytest.mark.parametrize("depth", _DEPTHS)
@pytest.mark.parametrize("library_dir", [None, ""])
def test_cli_ohne_bibliothek_byte_identisch(depth, library_dir):
    out, _, _ = build_research_execution_prompt(_raw(depth), 100, library_dir=library_dir)
    assert out == (_SNAP / f"cli_protocol_{depth}.txt").read_text(encoding="utf-8")


def test_cli_ohne_bibliothek_default_argument_byte_identisch():
    out, _, _ = build_research_execution_prompt(_raw("standard"), 100)
    assert out == (_SNAP / "cli_protocol_standard.txt").read_text(encoding="utf-8")


# --- CLI path: with library -------------------------------------------------


def _cli_lib(depth: str = "standard") -> str:
    out, _, _ = build_research_execution_prompt(
        _raw(depth), 100, library_dir=LIBRARY_WORKDIR_NAME
    )
    return out


def test_cli_mit_bibliothek_schritt1_ist_die_bibliothek():
    out = _cli_lib()
    assert _STEP1 in out
    assert _STEP2 in out
    assert out.index(_STEP1) < out.index(_STEP2)
    # Keine Zeile nennt die Websuche vor der Bibliothek.
    assert "1. Use WebSearch" not in out


def test_cli_mit_bibliothek_quellen_getrennt_und_herkunft_je_zahl():
    out = _cli_lib()
    sources = out.split("## Sources", 1)[1]
    assert "Bibliothek: <id>" in sources
    assert "Web: <URL>" in sources
    assert "[List URLs]" not in out
    assert "Every number states its origin: (Bibliothek: <id>) or (Web: <URL>)" in out


@pytest.mark.parametrize("depth,searches,fetches", [("standard", 10, 6), ("deep", 15, 10)])
def test_cli_mit_bibliothek_budget_bleibt(depth, searches, fetches):
    out = _cli_lib(depth)
    assert f"up to {searches} searches" in out
    assert f"up to {fetches} page fetches" in out


def test_cli_mit_bibliothek_turns_und_depth_unveraendert():
    with_lib = build_research_execution_prompt(_raw("deep"), 100, library_dir=LIBRARY_WORKDIR_NAME)
    without = build_research_execution_prompt(_raw("deep"), 100)
    assert with_lib[1:] == without[1:]


def test_cli_mit_bibliothek_behaelt_querytext_und_pflichtdatei():
    out = _cli_lib()
    assert f"QUERY: \"{_QUERY}\"" in out
    assert "--depth" not in out
    assert "The report file is the result of this research" in out
    assert "Offene Lücken" in out


# --- CLI wiring: claude_cli passes the library only when documents are seeded


class _Stop(Exception):
    pass


async def _captured_library_dir(monkeypatch, tmp_path, seed_links):
    import src.research_protocol as rp
    from src.claude_cli import ClaudeCodeCLI

    seen = {}

    def spy(prompt, max_turns, library_dir=None):
        seen["library_dir"] = library_dir
        raise _Stop

    monkeypatch.setattr(rp, "build_research_execution_prompt", spy)
    cli = object.__new__(ClaudeCodeCLI)
    cli.timeout = 30
    cli.cwd = tmp_path
    cli.claude_env_vars = {}
    cli.cache_dir = tmp_path
    cli.max_cache_size_mb = 10
    cli.file_discovery = MagicMock()
    with pytest.raises(_Stop):
        async for _ in cli.run_completion(
            prompt=_raw("quick"), model="claude-sonnet-4-5",
            enable_file_discovery=True, seed_links=seed_links,
        ):
            pass
    return seen["library_dir"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seed_links,expected",
    [
        (None, None),
        ({LIBRARY_WORKDIR_NAME: {}}, None),
        ({"andere": {"x.md": "/nirgends/x.md"}}, None),
        ({LIBRARY_WORKDIR_NAME: {"oib-rl-6.md": "/spiegel/oib-rl-6.md"}}, LIBRARY_WORKDIR_NAME),
    ],
)
async def test_cli_verdrahtung_bibliothek_nur_mit_dokumenten(monkeypatch, tmp_path, seed_links, expected):
    assert await _captured_library_dir(monkeypatch, tmp_path, seed_links) == expected


# --- Cloud path ---------------------------------------------------------------


@pytest.mark.parametrize("depth", _DEPTHS)
def test_cloud_ohne_bibliothek_byte_identisch(depth):
    expected = (_SNAP / f"cloud_system_{depth}.txt").read_text(encoding="utf-8")
    assert build_system_prompt(depth) == expected
    assert build_system_prompt(depth, library_index=None) == expected
    assert build_system_prompt(depth, library_index={"documents": []}) == expected


def _assert_library_first(block: str, step1_marker: str) -> None:
    assert "Deckt ein Eintrag" not in block  # keine bedingte Rangfolge mehr
    assert "immer in dieser Reihenfolge" in block
    step2 = "nur für das, was dort fehlt, und für Aktualität/Fassungsstand"
    assert step1_marker in block and step2 in block
    assert block.index(step1_marker) < block.index(step2)
    assert "„Bibliothek: <id>“" in block and "„Web: <URL>“" in block
    assert "Gib bei jeder Zahl an, woher sie stammt" in block


def test_cloud_mit_bibliothek_rangfolge_unbedingt():
    prompt = build_system_prompt("standard", library_index=_INDEX)
    _assert_library_first(prompt, "1. Zuerst die Bibliothek")
    assert "lade alle passenden Einträge mit `library_get`" in prompt
    # Cloud-Weg hat keine Dateien — kein Grep, kein Ordnerpfad.
    assert f"{LIBRARY_WORKDIR_NAME}/" not in build_library_catalogue(_INDEX)


def test_pool_block_mit_bibliothek_rangfolge_unbedingt():
    block = build_pool_library_catalogue(_INDEX, dir_name=LIBRARY_WORKDIR_NAME)
    _assert_library_first(block, f"1. Zuerst die Bibliothek: Durchsuche `{LIBRARY_WORKDIR_NAME}/`")
    assert "(Grep nach Begriffen, Normnummern, Werten)" in block
    assert "WebSearch/WebFetch nur für das, was dort fehlt" in block
