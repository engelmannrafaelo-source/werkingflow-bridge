"""Verdrahtung der Bibliothek im Abo-Pool-Weg (_execute_research_impl).

Geprueft wird die Naht, nicht die Bibliothek selbst (die hat
test_library_pool.py): Bekommt die CLI Katalog und Dateien? Bricht ein Lauf
laut ab, wenn die eingeschaltete Bibliothek nicht steht? Und bleibt ein Lauf
ohne Bibliothek genau so, wie er vorher war?
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
    "src.identity.routes",
    "src.db.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

from pathlib import Path  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

import src.main  # noqa: E402
from src.research_cloud.library import LibraryConfig, LibraryUnavailableError  # noqa: E402
from src.research_library_pool import LIBRARY_WORKDIR_NAME, LibraryMirror  # noqa: E402

_REPORT = "Executive Summary. " * 40  # ueber RESEARCH_MIN_INLINE_REPORT_CHARS

_INDEX = {
    "documents": [
        {"id": "oib-rl-5", "title": "OIB-Richtlinie 5", "jurisdiction": "AT"},
    ]
}


def _make_req(**kwargs):
    defaults = dict(
        query="Welche Ausgabe der OIB-Richtlinie 5 gilt?",
        model="claude-sonnet-4-5",
        depth="quick",
        strategy="planning",
        max_turns=10,
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
    )
    defaults.update(kwargs)
    ns = MagicMock()
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


async def _stream(*chunks):
    for c in chunks:
        yield c


def _assistant_chunks(*extra_blocks):
    return [
        {"type": "assistant", "content": [{"type": "text", "text": _REPORT}, *extra_blocks]},
        {"type": "result", "subtype": "success",
         "usage": {"input_tokens": 100, "output_tokens": 200}},
    ]


class _ReadBlock:
    def __init__(self, path):
        self.name = "Read"
        self.input = {"file_path": path}


@pytest.fixture
def spy_cli():
    """run_completion durch einen Spion ersetzen, der die Argumente festhaelt."""
    captured = {}

    def _run(**kwargs):
        captured.update(kwargs)
        chunks = captured.pop("_chunks", None) or _assistant_chunks()
        return _stream(*chunks)

    with patch.object(src.main.claude_cli, "run_completion", side_effect=_run) as spy:
        yield spy, captured


@pytest.fixture
def no_persist():
    with patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()) as m:
        yield m


@pytest.mark.asyncio
async def test_ohne_bibliothek_bleibt_der_lauf_unveraendert(spy_cli, no_persist):
    spy, captured = spy_cli
    with patch("src.research_cloud.library.load_library_for_run", new=AsyncMock(return_value=None)):
        result = await src.main._execute_research_impl(_make_req(), None)

    assert result.status == "success"
    assert captured["append_system_prompt"] is None
    assert captured["seed_links"] is None
    # None heisst "war nicht eingeschaltet" — nicht "wurde nicht benutzt".
    assert result.library_calls is None


@pytest.mark.asyncio
async def test_katalog_und_dateien_erreichen_die_cli(spy_cli, no_persist, tmp_path):
    spy, captured = spy_cli
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "oib-rl-5.md").write_text("OIB Text", encoding="utf-8")
    mirror = LibraryMirror(docs_dir=docs, available_ids=["oib-rl-5"])

    with patch("src.research_cloud.library.load_library_for_run", new=AsyncMock(return_value=_INDEX)), \
         patch("src.research_library_pool.sync_library_mirror", new=AsyncMock(return_value=mirror)):
        result = await src.main._execute_research_impl(_make_req(), None)

    assert result.status == "success"
    block = captured["append_system_prompt"]
    assert block and "`oib-rl-5`" in block
    assert f"{LIBRARY_WORKDIR_NAME}/<id>.md" in block
    assert captured["seed_links"] == {
        LIBRARY_WORKDIR_NAME: {"oib-rl-5.md": str(docs / "oib-rl-5.md")}
    }


@pytest.mark.asyncio
async def test_bibliotheksaufrufe_werden_gezaehlt(no_persist, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "oib-rl-5.md").write_text("OIB Text", encoding="utf-8")
    mirror = LibraryMirror(docs_dir=docs, available_ids=["oib-rl-5"])
    chunks = _assistant_chunks(_ReadBlock(f"/app/instances/s/{LIBRARY_WORKDIR_NAME}/oib-rl-5.md"))

    def _run(**kwargs):
        return _stream(*chunks)

    with patch.object(src.main.claude_cli, "run_completion", side_effect=_run), \
         patch("src.research_cloud.library.load_library_for_run", new=AsyncMock(return_value=_INDEX)), \
         patch("src.research_library_pool.sync_library_mirror", new=AsyncMock(return_value=mirror)):
        result = await src.main._execute_research_impl(_make_req(), None)

    assert result.library_calls == 1
    meta = no_persist.await_args.kwargs["provider_meta"]
    assert meta["library_calls"] == 1
    assert meta["library_docs"] == ["oib-rl-5"]


@pytest.mark.asyncio
async def test_unbrauchbare_bibliothek_startet_den_lauf_gar_nicht(spy_cli, no_persist):
    """Der teure Fehlermodus vom 04.09.: Lauf meldet Erfolg, hat aber ohne
    Bibliothek aus dem offenen Netz geantwortet. Hier muss er laut abbrechen —
    und zwar bevor ein Token fliesst."""
    spy, _ = spy_cli
    with patch("src.research_cloud.library.load_library_for_run", new=AsyncMock(return_value=_INDEX)), \
         patch("src.research_library_pool.sync_library_mirror",
               new=AsyncMock(side_effect=LibraryUnavailableError("Spiegel kaputt"))):
        result = await src.main._execute_research_impl(_make_req(), None)

    assert result.status == "error"
    assert "Bibliothek" in result.error and "Spiegel kaputt" in result.error
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_kaputter_index_startet_den_lauf_gar_nicht(spy_cli, no_persist):
    spy, _ = spy_cli
    with patch("src.research_cloud.library.load_library_for_run",
               new=AsyncMock(side_effect=LibraryUnavailableError("index laedt nicht"))):
        result = await src.main._execute_research_impl(_make_req(), None)

    assert result.status == "error"
    spy.assert_not_called()
