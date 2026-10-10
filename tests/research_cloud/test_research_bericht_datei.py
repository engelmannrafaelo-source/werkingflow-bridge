"""BR2 (10.10.2026): the result of a pool research run is its report FILE.

Energy-Nachtlauf z2b: with a large selection the model answered in chat with a
Python script that "writes" the report into claudedocs/output.md. Nobody runs
chat code, the file never existed, and the bridge handed the script out as
the research result (status=success). Guards:

  * the bridge-named file (OUTPUT_FILE_PATH) is the report, read from the
    run's own directory;
  * chat text without a report file is never the report: the run's model gets
    one round in the same session to write the file, otherwise the run fails
    loud;
  * GET /v1/research/{id}/content says why it cannot serve a session instead
    of "Session not found", and refuses ids that are not session uuids.
Neutral fixtures.
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
from fastapi import HTTPException  # noqa: E402

import src.main  # noqa: E402
from src.claude_cli import inject_output_path_for_file_discovery  # noqa: E402
from src.research_cloud.luecken import FLAG  # noqa: E402

BERICHT = "# Research Report\n\n## Summary\n" + "Befund mit Quelle. " * 40
SKRIPT = (
    "```python\nfrom pathlib import Path\n\nbericht = \"\"\"# Research Report\n"
    + "Befund. " * 80
    + "\"\"\"\nPath('claudedocs/output.md').write_text(bericht)\n```"
)
SESSION = "3aeda720-8443-4379-951d-b35b8246a7c3"


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


def _parse(chunks):
    return "\n".join(
        b["text"] for c in chunks if isinstance(c, dict) and isinstance(c.get("content"), list)
        for b in c["content"] if isinstance(b, dict) and b.get("text")
    ) or None


class FakeCli:
    """Research run that answers in chat (a script) and writes `files`; the
    repair round (resume_workdir set) writes the report if `repariert`."""

    def __init__(self, workdir: Path, *, files=None, chat=SKRIPT, repariert=True, session_id="cli-sess-1",
                 research_ende=None, nachhol_ende=None, nachhol_text=BERICHT):
        self.workdir = workdir
        # Result chunk a round ends with; None = success. M1: error_max_turns.
        self.research_ende = research_ende
        self.nachhol_ende = nachhol_ende
        self.nachhol_text = nachhol_text
        self.files = files or {}
        self.chat = chat
        self.repariert = repariert
        self.session_id = session_id
        self.calls = {"research": [], "nachhol": []}
        self.datei = workdir / "claudedocs" / "output.md"

    def __call__(self, **kw):
        if kw.get("resume_workdir") is not None:
            self.calls["nachhol"].append(kw)
            if self.repariert:
                self.datei.write_text(self.nachhol_text, encoding="utf-8")
            result = {"type": "result", "subtype": "success", "session_id": self.session_id,
                      "usage": {"input_tokens": 20, "output_tokens": 900}}
            result.update(self.nachhol_ende or {})
            return _stream(
                {"content": [{"type": "text", "text": "Bericht in die Datei geschrieben."}]},
                result,
            )
        self.calls["research"].append(kw)
        (self.workdir / "claudedocs").mkdir(parents=True, exist_ok=True)
        created = []
        for name, text in self.files.items():
            path = self.workdir / "claudedocs" / name
            path.write_text(text, encoding="utf-8")
            created.append({"path": str(path)})
        if created:
            meta = {"type": "x_claude_metadata", "files_created": created,
                    "session_tracking": {"cli_session_id": "bridge-1", "research_dir": str(self.workdir)}}
        else:
            meta = {"type": "x_claude_metadata", "files_created": [], "research_dir": str(self.workdir)}
        result = {"type": "result", "subtype": "success", "usage": {"input_tokens": 100, "output_tokens": 200}}
        if self.session_id:
            result["session_id"] = self.session_id
        result.update(self.research_ende or {})
        return _stream({"content": [{"type": "text", "text": self.chat}]}, result, meta)


@pytest.fixture
def persist():
    with patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()) as m:
        yield m


@pytest.fixture(autouse=True)
def ohne_zusatzwerkzeuge(monkeypatch):
    monkeypatch.delenv("RESEARCH_PERPLEXITY_ENABLED", raising=False)
    monkeypatch.delenv(FLAG, raising=False)


async def _run(fake, req=None):
    with patch.object(src.main.claude_cli, "run_completion", side_effect=fake), \
         patch.object(src.main.claude_cli, "parse_claude_message", side_effect=_parse):
        return await src.main._execute_research_impl(req or _make_req(), None, request=MagicMock())


# --- research run -----------------------------------------------------------


@pytest.mark.asyncio
async def test_skript_im_chat_wird_nie_der_bericht(tmp_path, persist):
    """z2b: chat answer is a script, no file. The repair round writes the
    report; the script is not handed out."""
    fake = FakeCli(tmp_path)
    result = await _run(fake)
    assert result.status == "success"
    assert result.content == BERICHT
    assert "write_text" not in result.content
    assert result.container_file == str(fake.datei)
    nachhol = fake.calls["nachhol"]
    assert len(nachhol) == 1
    assert nachhol[0]["session_id"] == "cli-sess-1" and nachhol[0]["resume_workdir"] == tmp_path
    # Descriptive: who reads the result and why the chat answer does not arrive.
    assert str(fake.datei) in nachhol[0]["prompt"]
    assert "receives exactly the contents of that file" in nachhol[0]["prompt"]
    assert "never run" in nachhol[0]["prompt"]
    booked = persist.await_args.kwargs
    assert booked["input_tokens"] == 120 and booked["output_tokens"] == 1100


@pytest.mark.asyncio
async def test_ohne_datei_auch_nach_nachholrunde_ist_fehler(tmp_path, persist):
    fake = FakeCli(tmp_path, repariert=False)
    result = await _run(fake)
    assert result.status == "error"
    assert result.content is None
    assert "no report file" in result.error and "not returned as the report" in result.error
    assert len(fake.calls["nachhol"]) == 1
    # The repair round's tokens still reach the error ledger row.
    booked = persist.await_args.kwargs
    assert booked["status"] == "error"
    assert booked["input_tokens"] == 120 and booked["output_tokens"] == 1100


@pytest.mark.asyncio
async def test_ohne_fortsetzbare_sitzung_ist_fehler(tmp_path, persist):
    fake = FakeCli(tmp_path, session_id=None)
    result = await _run(fake)
    assert result.status == "error" and "cannot be resumed" in result.error
    assert fake.calls["nachhol"] == []


@pytest.mark.asyncio
async def test_bestellte_datei_schlaegt_erste_geschriebene(tmp_path, persist):
    """A notes file written first is not the report; the bridge-named file is."""
    fake = FakeCli(tmp_path, files={"notizen.md": "Stichworte", "output.md": BERICHT}, chat="fertig")
    result = await _run(fake)
    assert result.status == "success" and result.content == BERICHT
    assert result.container_file == str(fake.datei)
    assert fake.calls["nachhol"] == []


@pytest.mark.asyncio
async def test_ohne_output_path_kein_gemeinsamer_tmp_pfad(tmp_path, persist):
    """No default copy to /tmp/<name> (shared by every run of the worker):
    the report is named and read where the run wrote it."""
    fake = FakeCli(tmp_path, files={"output.md": BERICHT}, chat="fertig")
    result = await _run(fake)
    assert result.output_file == str(fake.datei)
    assert not str(result.output_file).startswith("/tmp/")


@pytest.mark.asyncio
async def test_kopie_scheitert_bericht_bleibt(tmp_path, persist):
    fake = FakeCli(tmp_path / "w", files={"output.md": BERICHT}, chat="fertig")
    result = await _run(fake, _make_req(output_path=str(tmp_path / "fehlt" / "x.md")))
    assert result.status == "success" and result.content == BERICHT
    assert result.output_file == str(fake.datei)


@pytest.mark.asyncio
async def test_kopie_an_output_path(tmp_path, persist):
    ziel = tmp_path / "x.md"
    fake = FakeCli(tmp_path / "w", files={"output.md": BERICHT}, chat="fertig")
    result = await _run(fake, _make_req(output_path=str(ziel)))
    assert result.output_file == str(ziel)
    assert ziel.read_text(encoding="utf-8") == BERICHT


# --- M1: a round that does not end with success is no report ---------------

MAX_TURNS = {"subtype": "error_max_turns", "is_error": True}
TEILBERICHT = "# Research Report\n\n## Summary\n" + "Erster Teil, per Write geschrieben. " * 20


@pytest.mark.asyncio
async def test_nachholrunde_an_max_turns_gibt_keinen_teilbericht_aus(tmp_path, persist):
    """BR2R M1: the repair round writes the first part with Write, hits its
    turn limit during the Edits. output.md exists — and is half a report."""
    fake = FakeCli(tmp_path, nachhol_ende=MAX_TURNS, nachhol_text=TEILBERICHT)
    result = await _run(fake)
    assert fake.datei.read_text(encoding="utf-8") == TEILBERICHT
    assert result.status == "error"
    assert result.content is None
    assert "error_max_turns" in result.error and "not handed out" in result.error
    booked = persist.await_args.kwargs
    assert booked["status"] == "error"
    assert booked["input_tokens"] == 120 and booked["output_tokens"] == 1100


@pytest.mark.asyncio
async def test_hauptlauf_an_max_turns_gibt_keinen_teilbericht_aus(tmp_path, persist):
    """Same gap in the run itself: report file partly written, run stopped at
    max_turns. No repair round either — the run did not finish."""
    fake = FakeCli(tmp_path, files={"output.md": TEILBERICHT}, chat="weiter mit Abschnitt 3",
                   research_ende=MAX_TURNS)
    result = await _run(fake)
    assert result.status == "error"
    assert result.content is None
    assert "error_max_turns" in result.error and str(fake.datei) in result.error
    assert fake.calls["nachhol"] == []


@pytest.mark.asyncio
async def test_hauptlauf_an_max_turns_ohne_datei_keine_nachholrunde(tmp_path, persist):
    fake = FakeCli(tmp_path, research_ende=MAX_TURNS)
    result = await _run(fake)
    assert result.status == "error" and "error_max_turns" in result.error
    assert "write_text" not in (result.content or "")
    assert fake.calls["nachhol"] == []


@pytest.mark.asyncio
async def test_hauptlauf_sdk_resultmessage_an_max_turns(tmp_path, persist):
    """The converted SDK ResultMessage has no 'type' key — it counts too."""
    fake = FakeCli(tmp_path, files={"output.md": TEILBERICHT}, chat="fertig",
                   research_ende={"type": None, **MAX_TURNS})

    def ohne_type(**kw):
        async def strom():
            async for c in fake(**kw):
                if isinstance(c, dict) and "type" in c and c["type"] is None:
                    c = {k: v for k, v in c.items() if k != "type"}
                yield c
        return strom()

    result = await _run(ohne_type)
    assert result.status == "error" and "error_max_turns" in result.error


@pytest.mark.asyncio
async def test_success_mit_is_error_ist_kein_erfolg(tmp_path, persist):
    fake = FakeCli(tmp_path, files={"output.md": BERICHT}, chat="fertig", research_ende={"is_error": True})
    result = await _run(fake)
    assert result.status == "error"


def test_find_unfinished_result_formen():
    from src.claude_cli import find_unfinished_result
    ok = {"type": "result", "subtype": "success"}
    assert find_unfinished_result([{"content": []}, ok]) is None
    assert find_unfinished_result([{"subtype": "success", "usage": {}}]) is None
    assert find_unfinished_result([{"subtype": "error_max_turns"}])["subtype"] == "error_max_turns"
    assert find_unfinished_result([ok, {"type": "result", "subtype": "no_completion_marker", "is_error": True}])
    assert find_unfinished_result([{"content": []}])["subtype"] == "no_result"
    assert find_unfinished_result([{"type": "x_claude_metadata", "files_created": []}, ok]) is None


# --- prompt -----------------------------------------------------------------


def test_prompt_beschreibt_leser_und_form(tmp_path):
    datei = tmp_path / "claudedocs" / "output.md"
    out = inject_output_path_for_file_discovery("Recherchiere X.", datei, "s")
    assert "receives exactly the" in out and "contents of the file at OUTPUT_FILE_PATH" in out
    assert "code in a chat reply is never run" in out
    assert "not a program that produces it" in out
    assert out.count(f"OUTPUT_FILE_PATH: {datei}") == 2


# --- GET /v1/research/{id}/content -------------------------------------------


async def _content(session_id, root: Path, monkeypatch):
    monkeypatch.setenv("INSTANCES_DIR", str(root))
    with patch.object(src.main, "verify_api_key", new=AsyncMock()):
        return await src.main.get_research_content(session_id, MagicMock(), None)


def _session(root: Path, files: dict) -> Path:
    d = root / f"2026-10-10-0352_{SESSION}"
    (d / "claudedocs").mkdir(parents=True)
    for name, text in files.items():
        (d / "claudedocs" / name).write_text(text, encoding="utf-8")
    (d / "final_response.json").write_text('{"response": {"text": "chat"}}', encoding="utf-8")
    return d


@pytest.mark.asyncio
async def test_content_liefert_bestellte_datei(tmp_path, monkeypatch):
    _session(tmp_path, {"a-notizen.md": "Stichworte", "output.md": BERICHT})
    resp = await _content(SESSION, tmp_path, monkeypatch)
    assert resp.body.decode("utf-8") == BERICHT


@pytest.mark.asyncio
async def test_content_ohne_bericht_liefert_keinen_chattext(tmp_path, monkeypatch):
    _session(tmp_path, {})
    with pytest.raises(HTTPException) as e:
        await _content(SESSION, tmp_path, monkeypatch)
    assert e.value.status_code == 404
    assert e.value.detail["reason"] == "research_session_without_report"


@pytest.mark.asyncio
async def test_content_fremde_sitzung_sagt_warum(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "dev")
    monkeypatch.setenv("INSTANCE_NAME", "worker2")
    with pytest.raises(HTTPException) as e:
        await _content(SESSION, tmp_path, monkeypatch)
    assert e.value.status_code == 404
    d = e.value.detail
    assert d["reason"] == "research_session_not_on_this_host"
    assert d["bridge"] == "dev" and d["worker"] == "worker2"
    assert "GET /v1/jobs/" in d["message"]


@pytest.mark.asyncio
async def test_content_job_id_ist_keine_sitzung(tmp_path, monkeypatch):
    job = "job_prod_" + "c3e635a5bfff4c6faf55dd1dea126af9"
    with pytest.raises(HTTPException) as e:
        await _content(job, tmp_path, monkeypatch)
    assert e.value.status_code == 400
    assert e.value.detail["reason"] == "job_id_is_not_a_research_session"
    assert f"GET /v1/jobs/{job}" in e.value.detail["message"] and "prod" in e.value.detail["message"]


@pytest.mark.asyncio
async def test_content_glob_zeichen_werden_abgewiesen(tmp_path, monkeypatch):
    """'*' was spliced into glob() and served the first session of anyone."""
    _session(tmp_path, {"output.md": BERICHT})
    with pytest.raises(HTTPException) as e:
        await _content("*", tmp_path, monkeypatch)
    assert e.value.status_code == 400
    assert e.value.detail["reason"] == "research_session_id_malformed"
