"""BRREFR R2 (Codex, 12.10.2026): every output path goes past ONE acceptance
point (src/lauf_abschluss.annehmen) and a stored result carries its verdict.

Codex' eight R2 probes (local-storage/brrefr-r2-evidence/test_brrefr_r2_review.py)
unchanged in substance, plus: research-cloud return round, the job poll,
Gemini without a finish reason, and the download of a finished research.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# First: its sys.modules stand-ins for claude_code_sdk must be in place
# before src.main is imported through the research test module.
from tests.unit import test_chat_modell_abbruch as b  # isort: skip
from tests.unit.test_chat_modell_abbruch import frischer_zustand  # noqa: F401,E402  isort: skip (autouse)

import src.main as main  # noqa: E402
from src.lauf_abschluss import ABSCHLUSS_DATEI, vermerkt  # noqa: E402
from tests import test_research_antwort_im_text as r  # noqa: E402

SID = "3aeda720-8443-4379-951d-b35b8246a7c3"


def _refusal(text):
    return dict(r._assistant(text), stop_reason="refusal")


# ── R2-1 tool-leak retry without completion marker ─────────────────────────

@pytest.mark.parametrize("refusal", [False, True])
def test_retry_ohne_abschlussmarker_ist_503(monkeypatch, tmp_path, refusal):
    calls = 0

    def factory(ignored):
        async def query(prompt, options):
            nonlocal calls
            calls += 1
            first = b.normal_verlauf()
            first[1] = b._parsed("msg_intro", [{"type": "text", "text": "I will generate the file now."}],
                                 "end_turn", b.USAGE_ZUG_1)
            second = b.refusal_verlauf()[:2] if refusal else b.normal_verlauf()[:-1]
            for m in first if calls == 1 else second:
                yield m
        return query

    monkeypatch.setattr(b, "_query", factory)
    with patch("src.main.MessageAdapter.messages_to_prompt", return_value=("p" * 20000, None)):
        resp = b._sync_chat(monkeypatch, tmp_path, [])
    assert calls == 2
    assert resp.status_code == 503, (resp.status_code, resp.text[:300])


# ── R2-4 research-cloud ─────────────────────────────────────────────────────

async def test_cloud_executor_refusal_ist_fehler_mit_usage():
    from src.research_cloud.executor import (
        ResearchCloudModelAbbruch,
        run_research_cloud,
    )
    from tests.research_cloud.test_executor import _response, _usage

    client = MagicMock()
    client.post = AsyncMock(return_value=_response(200, {
        "model": "claude-sonnet-5", "stop_reason": "refusal", "usage": _usage(),
        "stop_details": {"type": "refusal", "category": "reasoning_extraction"},
        "content": [{"type": "text", "text": "partial report"}]}))
    with pytest.raises(ResearchCloudModelAbbruch) as ei:
        await run_research_cloud("query", "system", api_key="test-only", client=client)
    assert ei.value.usage.input_tokens == 100 and ei.value.usage.output_tokens == 50
    assert ei.value.abschluss.category == "reasoning_extraction"


async def test_cloud_route_refusal_ist_fehler():
    from src.research_cloud.models import ResearchCloudResult, ResearchCloudUsage

    obj = ResearchCloudResult(status="success", content="partial report", model="claude-sonnet-5",
                              usage=ResearchCloudUsage(input_tokens=10, output_tokens=20),
                              stop_reason="refusal")
    with (
        patch("src.research_cloud.executor.run_research_cloud", new=AsyncMock(return_value=obj)),
        patch("src.research_cloud.anonymize_gate.anonymize_query_for_cloud",
              new=AsyncMock(return_value="anonymous")),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()),
        patch("src.scholarly.scholarly_enabled", return_value=False),
    ):
        res = await main._execute_research_cloud_impl(MagicMock(), r._req(), attribution_ctx={})
    assert res.status == "error", res.model_dump()
    assert res.fehler_code == "modell_abbruch" and res.retryable is False
    assert res.content is None


async def test_cloud_rueckrunde_refusal_behaelt_bericht_von_vorher(monkeypatch):
    from src.research_cloud.luecken import Nachkontrolle
    from tests.research_cloud import test_luecken as lk

    monkeypatch.setenv("BRIDGE_ANONYMIZE_ENABLED", "true")  # as test_luecken's fixture
    nk = Nachkontrolle(pruefer=lk._pruefer([lk.LUECKE_NORM]))
    abbruch = lk._anthropic({"stop_reason": "refusal",
                             "content": [{"type": "text", "text": lk.NEU}]})
    result, _ = await lk._run([lk._end(lk.ALT), abbruch], nk)
    assert result.status == "success" and result.content == lk.ALT
    assert "ResearchCloudModelAbbruch" in nk.as_meta()["rueckrunde_fehler"]


# ── R2-2 / R2-3 research repair round and return round ──────────────────────

async def test_research_dateireparatur_refusal_ist_fehler(tmp_path):
    from tests.research_cloud import test_research_bericht_datei as f

    class RefusalRepair(f.FakeCli):
        def __call__(self, **kw):
            if kw.get("resume_workdir") is not None:
                self.datei.write_text("partial repaired report")
                self.calls["nachhol"].append(kw)
                return r._stream(_refusal("partial"), r._assistant("Sorry"), r._OK)
            return super().__call__(**kw)

    fake = RefusalRepair(tmp_path)
    with patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()):
        res = await f._run(fake)
    assert len(fake.calls["nachhol"]) == 1
    assert res.status == "error", res.model_dump()
    assert res.fehler_code == "modell_abbruch"


async def test_rueckrunde_refusal_behaelt_bericht_von_vorher(tmp_path):
    old = "previous complete report. " * 50
    partial = "new refused partial report. " * 50
    target = tmp_path / "output.md"
    target.write_text(old)

    async def stream(**kw):
        target.write_text(partial)
        for c in [_refusal("partial"), r._assistant("Sorry"), r._OK]:
            yield c

    check = SimpleNamespace(nachmessen=AsyncMock(), rueckrunde_fehler=None)
    with patch.object(main.claude_cli, "run_completion", side_effect=stream):
        result, _ = await main._pool_luecken_rueckrunde(
            nachkontrolle=check,
            offene=[SimpleNamespace(punkt="q", art="kennwert", gesucht=False, gelesen=False)],
            bericht=old, erste_chunks=[], cli_resume_id="fixture", workdir=tmp_path,
            report_file=str(target), output_file=None,
            pool_pplx=SimpleNamespace(max_uses=5, calls=0, gate_error=None), pool_doc=None,
            sdk_mcp_servers=None, append_system_prompt=None,
            request_body=r._req(antwort_im_text=False), backend_config=None)
    assert result == old
    assert target.read_text() == old
    assert "vom Modell abgebrochen" in check.rueckrunde_fehler
    check.nachmessen.assert_not_awaited()


# ── R2-5 doc-agent ──────────────────────────────────────────────────────────

async def test_doc_agent_refusal_ist_fehler():
    req = SimpleNamespace(files=[SimpleNamespace(name="test.md", content="neutral")], question="q",
                          model="claude-sonnet-5", max_turns=3)
    chunks = [_refusal("partial"), r._assistant("Sorry, cannot continue"), r._OK]
    with (
        patch.object(main.claude_cli, "run_completion", return_value=r._stream(*chunks)),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()),
    ):
        res = await main._execute_doc_agent_impl(req)
    assert res.status == "error", res.model_dump()
    assert res.answer is None and res.retryable is False


# ── R2-6 the download carries the verdict ───────────────────────────────────

async def _download(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTANCES_DIR", str(tmp_path))
    with patch.object(main, "verify_api_key", new=AsyncMock()):
        return await main.get_research_content(SID, MagicMock(), None)


async def test_abgelehnte_research_datei_wird_nicht_ausgeliefert(tmp_path, monkeypatch):
    from fastapi import HTTPException

    workdir = tmp_path / ("2026-10-12-0000_" + SID)
    workdir.mkdir()
    res, _ = await r._run([_refusal("partial"), r._metadata(workdir, ("output.md", "refused partial report")),
                           dict(r._OK, session_id=SID)], r._req(antwort_im_text=False))
    assert res.status == "error"
    assert vermerkt(workdir)["auslieferbar"] is False
    with pytest.raises(HTTPException) as ei:
        await _download(tmp_path, monkeypatch)
    assert ei.value.status_code == 409
    assert ei.value.detail["grund"] == "modell_abbruch"


async def test_fertige_research_wird_ausgeliefert(tmp_path, monkeypatch):
    workdir = tmp_path / ("2026-10-12-0000_" + SID)
    workdir.mkdir()
    bericht = "# Research Report\n\n" + "Befund mit Quelle. " * 40
    res, _ = await r._run([r._assistant("done"), r._metadata(workdir, ("output.md", bericht)),
                           dict(r._OK, session_id=SID)], r._req(antwort_im_text=False))
    assert res.status == "success", res.model_dump()
    assert vermerkt(workdir)["auslieferbar"] is True
    resp = await _download(tmp_path, monkeypatch)
    assert resp.status_code == 200 and resp.body.decode("utf-8") == bericht


async def test_research_ohne_vermerk_wird_nicht_ausgeliefert(tmp_path, monkeypatch):
    from fastapi import HTTPException

    docs = tmp_path / ("2026-10-12-0000_" + SID) / "claudedocs"
    docs.mkdir(parents=True)
    (docs / "output.md").write_text("report of a run that never finished")
    assert not (docs.parent / ABSCHLUSS_DATEI).exists()
    with pytest.raises(HTTPException) as ei:
        await _download(tmp_path, monkeypatch)
    assert ei.value.status_code == 409 and ei.value.detail["grund"] == "kein_vermerk"


# ── chat jobs: done = executed, the verdict is visible ──────────────────────

async def test_chat_job_poll_zeigt_abschluss(monkeypatch):
    from tests.jobs.test_error_verdict_br9 import _job, _poll

    ergebnis = {"choices": [{"index": 0, "finish_reason": "content_filter",
                             "message": {"role": "assistant", "content": "<teil"}}],
                "x_bridge_finish": {"stop_reason": "refusal", "vollstaendig": False,
                                    "category": "reasoning_extraction", "folgezuege": 1}}
    body = await _poll(_job(status="done", result=ergebnis), monkeypatch)
    assert body["status"] == "done"
    assert body["abschluss"]["vollstaendig"] is False
    assert body["abschluss"]["finish_reason"] == "content_filter"


async def test_altes_chat_ergebnis_ohne_vermerk_wird_am_finish_reason_gemessen(monkeypatch):
    from tests.jobs.test_error_verdict_br9 import _job, _poll

    ergebnis = {"choices": [{"index": 0, "finish_reason": "length", "message": {"content": "x"}}]}
    body = await _poll(_job(status="done", result=ergebnis), monkeypatch)
    assert body["abschluss"] == {"finish_reason": "length", "vollstaendig": False}


# ── Gemini: no reason reported is not "stop" ────────────────────────────────

async def test_gemini_ohne_abschlussgrund_ist_unknown(monkeypatch):
    import json

    import src.gemini_cli as gemini_cli

    class Proc:
        returncode = 0

        async def communicate(self, *a, **kw):
            return json.dumps({"response": "Antwort", "stats": {}}).encode(), b""

    monkeypatch.setattr(gemini_cli.asyncio, "create_subprocess_exec", AsyncMock(return_value=Proc()))
    out = await gemini_cli.call_gemini_cli(prompt="p", model="gemini-2.5-flash")
    assert out["choices"][0]["finish_reason"] == "unknown"
    assert out["x_bridge_finish"]["vollstaendig"] is False
