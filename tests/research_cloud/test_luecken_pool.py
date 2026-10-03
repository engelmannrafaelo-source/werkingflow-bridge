"""Lücken-Nachkontrolle on the pool path: _execute_research_impl resumes the
same CLI session in its own directory, exactly once. Neutral fixtures.
"""
from __future__ import annotations

import json
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

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

import src.main  # noqa: E402
import src.research_pool_perplexity as rpp  # noqa: E402
from src.research_cloud.luecken import FLAG, PRUEF_MODELL  # noqa: E402

ALT = "# Bericht\n" + "Befund. " * 60 + "\nTyp X-100, Schallleistung: nicht bestätigt."
NEU = "# Bericht\n" + "Befund. " * 60 + "\nTyp X-100, Schallleistung 52 dB(A) (Datenblatt https://h.example/x.pdf, S. 3)."
GAP = {"punkt": "Schallleistung Typ X-100", "art": "kennwert", "gesucht": False, "gelesen": False}


def _make_req(output_path=None):
    ns = MagicMock()
    for k, v in dict(
        query="Welche Kennwerte hat Gerät X-100?", model="claude-sonnet-4-5", depth="quick",
        strategy="planning", max_turns=10, max_hops=None, confidence_threshold=0.7,
        parallel_searches=5, source_filter=None, output_path=output_path, async_mode=False,
        backend=None, privacy=None, bedrock_region=None, research_mode=None,
    ).items():
        setattr(ns, k, v)
    return ns


async def _stream(*chunks):
    for c in chunks:
        yield c


class FakeCli:
    """Dispatches run_completion by role: research run, checker, return round."""

    def __init__(self, workdir, *, mit_datei=False, gaps=([GAP], []), rueckrunde_text=NEU, session_id="cli-sess-1"):
        self.workdir = workdir
        self.mit_datei = mit_datei
        self.gaps = list(gaps)
        self.rueckrunde_text = rueckrunde_text
        self.session_id = session_id
        self.calls = {"research": [], "pruefer": [], "rueckrunde": []}
        self.datei = workdir / "claudedocs" / "output.md"

    def __call__(self, **kw):
        if kw.get("resume_workdir") is not None:
            self.calls["rueckrunde"].append(kw)
            if self.mit_datei:
                self.datei.write_text(self.rueckrunde_text, encoding="utf-8")
                text = "Datei aktualisiert."
            else:
                text = self.rueckrunde_text
            return _stream(
                {"content": [SimpleNamespace(name=rpp.MCP_TOOL_NAME, input={"frage": "Datenblatt X-100"})]},
                {"content": [{"type": "text", "text": text}]},
                {"type": "result", "subtype": "success", "session_id": self.session_id,
                 "usage": {"input_tokens": 30, "output_tokens": 40}},
            )
        if kw.get("model") == PRUEF_MODELL:
            self.calls["pruefer"].append(kw)
            return _stream(
                {"content": [{"type": "text", "text": json.dumps({"luecken": self.gaps.pop(0)})}]},
                {"type": "result", "subtype": "success", "usage": {"input_tokens": 7, "output_tokens": 3}},
            )
        self.calls["research"].append(kw)
        chunks = []
        if self.mit_datei:
            self.datei.parent.mkdir(parents=True, exist_ok=True)
            self.datei.write_text(ALT, encoding="utf-8")
            meta = {"type": "x_claude_metadata", "files_created": [{"path": str(self.datei)}],
                    "session_tracking": {"cli_session_id": "bridge-1", "research_dir": str(self.workdir)}}
            text = "Bericht geschrieben."
        else:
            meta = {"type": "x_claude_metadata", "files_created": [], "research_dir": str(self.workdir)}
            text = ALT
        chunks.append({"content": [{"type": "text", "text": text}]})
        result = {"type": "result", "subtype": "success", "usage": {"input_tokens": 100, "output_tokens": 200}}
        if self.session_id:
            result["session_id"] = self.session_id
        chunks.append(result)
        chunks.append(meta)
        return _stream(*chunks)


@pytest.fixture
def persist():
    with patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()) as m:
        yield m


@pytest.fixture
def pplx_on(monkeypatch):
    monkeypatch.setenv("BRIDGE_ANONYMIZE_ENABLED", "true")
    monkeypatch.setenv("RESEARCH_PERPLEXITY_ENABLED", "true")
    monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-test")
    monkeypatch.delenv(FLAG, raising=False)


def _parse(chunks):
    return "\n".join(
        b["text"] for c in chunks if isinstance(c, dict) and isinstance(c.get("content"), list)
        for b in c["content"] if isinstance(b, dict) and b.get("text")
    ) or None


async def _run(fake, req=None):
    with patch.object(src.main.claude_cli, "run_completion", side_effect=fake), \
         patch.object(src.main.claude_cli, "parse_claude_message", side_effect=_parse):
        return await src.main._execute_research_impl(req or _make_req(), None, request=MagicMock())


@pytest.mark.asyncio
async def test_inline_report_gets_one_round_in_the_same_session(tmp_path, persist, pplx_on):
    fake = FakeCli(tmp_path)
    result = await _run(fake)
    assert result.status == "success" and result.content == NEU
    assert len(fake.calls["rueckrunde"]) == 1 and len(fake.calls["pruefer"]) == 2
    rr = fake.calls["rueckrunde"][0]
    assert rr["session_id"] == "cli-sess-1" and rr["resume_workdir"] == tmp_path
    # Same tool instances: the budgets of the first round continue.
    assert rr["sdk_mcp_servers"] is fake.calls["research"][0]["sdk_mcp_servers"]
    assert GAP["punkt"] in rr["prompt"] and rpp.MCP_TOOL_NAME in rr["prompt"]
    # The checker runs without tools.
    assert "mcp__*" in fake.calls["pruefer"][0]["disallowed_tools"]
    booked = persist.await_args.kwargs
    meta = booked["provider_meta"]
    assert meta["rueckrunde"] is True and meta["luecken_ohne_suche"] == 1
    assert meta["luecken_nach_rueckrunde"] == 0
    assert meta["rueckrunde_usage"]["input_tokens"] == 30
    # Ledger: the round's tokens are part of the run.
    assert booked["input_tokens"] == 130 and booked["output_tokens"] == 240
    assert meta["luecken_pruef_input_tokens"] == 14


@pytest.mark.asyncio
async def test_file_report_is_rewritten_in_place_and_copied(tmp_path, persist, pplx_on):
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "out.md"
    fake = FakeCli(work, mit_datei=True)
    result = await _run(fake, _make_req(output_path=str(out)))
    assert result.content == NEU
    assert out.read_text(encoding="utf-8") == NEU
    assert str(fake.datei) in fake.calls["rueckrunde"][0]["prompt"]
    assert result.file_size_bytes == len(NEU.encode("utf-8"))


@pytest.mark.asyncio
async def test_fragment_overwrite_is_undone(tmp_path, persist, pplx_on):
    work = tmp_path / "work"
    work.mkdir()
    out = tmp_path / "out.md"
    fake = FakeCli(work, mit_datei=True, rueckrunde_text="nur ergänzt")
    result = await _run(fake, _make_req(output_path=str(out)))
    assert result.content == ALT
    assert fake.datei.read_text(encoding="utf-8") == ALT
    meta = persist.await_args.kwargs["provider_meta"]
    assert "keinen vollständigen Bericht" in meta["rueckrunde_fehler"]


@pytest.mark.asyncio
async def test_no_resumable_session_keeps_report_and_names_it(tmp_path, persist, pplx_on):
    fake = FakeCli(tmp_path, session_id=None)
    result = await _run(fake)
    assert result.content == ALT and fake.calls["rueckrunde"] == []
    assert "keine fortsetzbare Sitzung" in persist.await_args.kwargs["provider_meta"]["rueckrunde_fehler"]


@pytest.mark.asyncio
async def test_no_gaps_no_round(tmp_path, persist, pplx_on):
    fake = FakeCli(tmp_path, gaps=([],))
    result = await _run(fake)
    assert result.content == ALT and fake.calls["rueckrunde"] == []
    assert persist.await_args.kwargs["provider_meta"]["rueckrunde"] is False


@pytest.mark.asyncio
async def test_flag_off_no_checker(tmp_path, persist, pplx_on, monkeypatch):
    monkeypatch.setenv(FLAG, "off")
    fake = FakeCli(tmp_path)
    result = await _run(fake)
    assert result.content == ALT and fake.calls["pruefer"] == []
    assert "rueckrunde" not in persist.await_args.kwargs["provider_meta"]


@pytest.mark.asyncio
async def test_flag_on_without_perplexity_refuses_before_the_cli(tmp_path, persist, monkeypatch):
    monkeypatch.delenv("RESEARCH_PERPLEXITY_ENABLED", raising=False)
    monkeypatch.setenv(FLAG, "on")
    fake = FakeCli(tmp_path)
    result = await _run(fake)
    assert result.status == "error" and FLAG in result.error
    assert fake.calls["research"] == []


@pytest.mark.asyncio
async def test_perplexity_off_no_checker_at_all(tmp_path, persist, monkeypatch):
    monkeypatch.delenv("RESEARCH_PERPLEXITY_ENABLED", raising=False)
    monkeypatch.delenv(FLAG, raising=False)
    fake = FakeCli(tmp_path)
    result = await _run(fake)
    assert result.content == ALT and fake.calls["pruefer"] == []


@pytest.mark.asyncio
async def test_resume_workdir_needs_session_id_and_existing_dir(tmp_path):
    with pytest.raises(ValueError, match="session_id"):
        await src.main.claude_cli.run_completion(prompt="x", resume_workdir=tmp_path).__anext__()
    with pytest.raises(RuntimeError, match="does not exist"):
        await src.main.claude_cli.run_completion(
            prompt="x", session_id="s", resume_workdir=tmp_path / "fehlt"
        ).__anext__()
    with pytest.raises(ValueError, match="seed"):
        await src.main.claude_cli.run_completion(
            prompt="x", session_id="s", resume_workdir=tmp_path, seed_files={"a.md": "x"}
        ).__anext__()
