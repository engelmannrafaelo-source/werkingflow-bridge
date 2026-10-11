"""Output contract antwort_im_text for /v1/research (BRRES, 11.10.2026).

Befund Energy DS2, Phase 4, Runde 3: the caller asks for a ```pruefwissen-auswahl```
block in the reply text. The bridge ordered a report FILE (protocol +
OUTPUT_FILE_PATH injection) and handed out the file the run touched first.
Three answers came back without the block: 4.355 and 40.562 chars of Python
(helper scripts the run had written; one ends in open(".../output.md","w")),
and a 926-char "Research Report" whose text says the block is "in der
Schlussantwort" — the final message, which the file contract discards.

Guards:
  * protocol with antwort_im_text names no file, names the final message and
    lets a format prescribed by the query win (with and without library);
  * the CLI injects no OUTPUT_FILE_PATH under the contract;
  * the handler returns the LAST assistant text, ignores written files, keeps
    Write/Edit/Bash away from the run, and says antwort_quelle="text";
  * a cut-off run (error_max_turns) and an empty final message fail loud;
  * file contract (default): a script the run wrote is never the report.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.main as main_module
from src.research_library_pool import LIBRARY_WORKDIR_NAME
from src.research_protocol import build_research_execution_prompt

_BLOCK = '```pruefwissen-auswahl\n{"dokument_ids": ["kw-stoerung-1"]}\n```'
_RAW = '/sc:research "Wähle Prüfwissen aus. Antworte mit einem pruefwissen-auswahl-Block." --depth deep'


# --- Protocol -------------------------------------------------------------------


@pytest.mark.parametrize("library_dir", [None, LIBRARY_WORKDIR_NAME])
def test_protokoll_textvertrag_nennt_keine_datei(library_dir):
    prompt, _, _ = build_research_execution_prompt(
        _RAW, 45, library_dir=library_dir, antwort_im_text=True
    )
    assert "OUTPUT_FILE_PATH" not in prompt
    assert "report file" not in prompt
    assert "FINAL message" in prompt
    assert "DO NOT write files" in prompt and "scripts" in prompt
    assert "prescribes an output format" in prompt
    assert "Wähle Prüfwissen aus" in prompt


@pytest.mark.parametrize("library_dir", [None, LIBRARY_WORKDIR_NAME])
def test_protokoll_dateivertrag_unveraendert(library_dir):
    ohne = build_research_execution_prompt(_RAW, 45, library_dir=library_dir)
    mit_false = build_research_execution_prompt(_RAW, 45, library_dir=library_dir, antwort_im_text=False)
    assert ohne == mit_false
    assert "OUTPUT_FILE_PATH" in ohne[0]


# --- CLI: no OUTPUT_FILE_PATH injection under the contract ------------------------


class _Stop(Exception):
    pass


async def _injected(monkeypatch, tmp_path, antwort_im_text):
    import src.claude_cli as cc

    calls = []

    def spy_inject(**kw):
        calls.append(kw)
        return kw["prompt"]

    monkeypatch.setattr(cc, "inject_output_path_for_file_discovery", spy_inject)

    stopped = []

    class _Options:
        def __init__(self, *a, **k):
            stopped.append(True)
            raise _Stop

    monkeypatch.setattr(cc, "ClaudeCodeOptions", _Options)
    cli = object.__new__(cc.ClaudeCodeCLI)
    cli.timeout = 30
    cli.cwd = tmp_path
    cli.claude_env_vars = {}
    cli.cache_dir = tmp_path
    cli.max_cache_size_mb = 10
    cli.file_discovery = MagicMock()
    # run_completion turns the stop into an error chunk or raises it — either
    # way the injection decision was taken before the SDK options are built.
    try:
        async for _ in cli.run_completion(
            prompt=_RAW, model="claude-sonnet-4-5",
            enable_file_discovery=True, antwort_im_text=antwort_im_text,
        ):
            pass
    except Exception:
        pass
    assert stopped, "run_completion never reached the SDK options — test proves nothing"
    return calls


@pytest.mark.asyncio
async def test_cli_textvertrag_injiziert_keinen_dateipfad(monkeypatch, tmp_path):
    assert await _injected(monkeypatch, tmp_path, True) == []


@pytest.mark.asyncio
async def test_cli_dateivertrag_injiziert_dateipfad(monkeypatch, tmp_path):
    assert len(await _injected(monkeypatch, tmp_path, False)) == 1


# --- Handler ----------------------------------------------------------------------


def _req(**kw):
    d = dict(
        query="Wähle Prüfwissen aus.", model="claude-sonnet-4-5", depth="deep",
        strategy="planning", max_turns=45, max_hops=None, confidence_threshold=0.7,
        parallel_searches=5, source_filter=None, output_path=None, async_mode=False,
        backend=None, privacy=None, bedrock_region=None, research_mode=None,
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


def _assistant(text):
    return {"type": "assistant", "content": [{"type": "text", "text": text}]}


_OK = {"type": "result", "subtype": "success", "usage": {"input_tokens": 10, "output_tokens": 20}}
_MAX_TURNS = {"type": "result", "subtype": "error_max_turns", "is_error": False}


def _metadata(tmp_path, *files):
    claudedocs = tmp_path / "claudedocs"
    claudedocs.mkdir(exist_ok=True)
    created = []
    for name, text in files:
        f = claudedocs / name
        f.write_text(text, encoding="utf-8")
        created.append({"path": str(f)})
    return {
        "type": "x_claude_metadata",
        "files_created": created,
        "session_tracking": {"research_dir": str(tmp_path)},
    }


def _first_text(chunks):
    for c in chunks:
        if isinstance(c, dict) and isinstance(c.get("content"), list):
            t = "\n".join(b["text"] for b in c["content"] if b.get("type") == "text")
            if t:
                return t
    return None
  # parse_claude_message semantics: FIRST text


async def _run(chunks, req):
    cli = MagicMock()
    cli.run_completion = MagicMock(return_value=_stream(*chunks))
    cli.parse_claude_message = MagicMock(side_effect=_first_text)
    cli.cwd = "/nirgends"
    with (
        patch("src.main.claude_cli", cli),
        patch("src.claude_cli.detect_quota_exhaustion", return_value=False),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock()),
    ):
        res = await main_module._execute_research_impl(req, None, attribution_ctx=None)
    return res, cli




@pytest.mark.asyncio
async def test_textvertrag_liefert_letzte_nachricht_nicht_datei(tmp_path):
    zusammenfassung = "# Research Report\n\n## Summary\nDer Datensatz steht in der Schlussantwort." * 3
    chunks = [
        _assistant("Ich lese zuerst die Bibliothek."),
        _assistant("Hier die Auswahl:\n" + _BLOCK),
        _metadata(tmp_path, ("output.md", zusammenfassung), ("bau.py", "import json\nopen('x','w')")),
        _OK,
    ]
    res, cli = await _run(chunks, _req())
    assert res.status == "success", res.error
    assert res.content == "Hier die Auswahl:\n" + _BLOCK
    assert res.antwort_quelle == "text"
    assert res.container_file is None
    kw = cli.run_completion.call_args.kwargs
    assert kw["antwort_im_text"] is True
    assert {"Write", "Edit", "Bash"} <= set(kw["disallowed_tools"])


@pytest.mark.asyncio
async def test_textvertrag_abgeschnittener_lauf_scheitert_laut(tmp_path):
    res, _ = await _run([_assistant("Ich rechne noch …" * 100), _MAX_TURNS], _req())
    assert res.status == "error"
    assert "antwort_im_text" in res.error


@pytest.mark.asyncio
async def test_textvertrag_ohne_text_scheitert_laut(tmp_path):
    chunks = [{"type": "assistant", "content": [{"type": "tool_use", "name": "Read"}]}, _OK]
    res, _ = await _run(chunks, _req())
    assert res.status == "error"


@pytest.mark.asyncio
async def test_dateivertrag_unveraendert_liefert_berichtsdatei(tmp_path):
    bericht = "# Research Report\n\n" + _BLOCK + "\n" + "x" * 600
    chunks = [_assistant("Erste Nachricht."), _metadata(tmp_path, ("output.md", bericht)), _OK]
    res, cli = await _run(chunks, _req(antwort_im_text=False))
    assert res.status == "success", res.error
    assert res.content == bericht
    assert res.antwort_quelle == "datei"
    kw = cli.run_completion.call_args.kwargs
    assert kw["antwort_im_text"] is False and kw["disallowed_tools"] is None


@pytest.mark.asyncio
async def test_dateivertrag_skript_ist_nie_der_bericht(tmp_path):
    """DS2 Runde 3, Antwort 2: the run wrote bau.py (which would write output.md)
    but output.md never existed — the script was handed out as the result."""
    skript = 'import json\nopen("/app/instances/x/claudedocs/output.md","w").write(txt)\n' + "#" * 600
    chunks = [_assistant("Ich baue die Auswahl per Skript." + " " * 600),
              _metadata(tmp_path, ("bau.py", skript)), _OK]
    with patch(
        "src.main._pool_berichtsdatei_nachholen",
        AsyncMock(side_effect=main_module.ResearchReportMissing("keine Berichtsdatei")),
    ):
        res, _ = await _run(chunks, _req(antwort_im_text=False))
    assert res.status == "error"
    assert res.content is None or "import json" not in res.content


def test_berichtsdatei_auswahl_ueberspringt_skripte(tmp_path):
    py = tmp_path / "bau.py"; py.write_text("x")
    md = tmp_path / "notizen.md"; md.write_text("y")
    assert main_module._research_report_file([py, md], None) == md
    assert main_module._research_report_file([py], None) is None
    assert main_module._research_report_file([py], tmp_path / "fehlt.md") is None
