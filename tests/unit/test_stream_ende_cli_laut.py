"""CLI/SDK-Streaming ohne Endesignal meldet keinen Erfolg (ZB3D).

run_completion haengt einen Abschneide-Marker (no_completion_marker) an, wenn
der SDK-Strom ohne Result-Nachricht der CLI endet. Bis ZB3D wurde der Marker
auf dem Streaming-Weg uebergangen: der Aufrufer bekam abgeschnittenen Text mit
finish_reason "stop" und [DONE]. Ein Lauf ohne jeden Text bekam sogar einen
erfundenen Ersatzsatz. Beides endet jetzt mit `event: error` und ohne [DONE].
"""
from __future__ import annotations

import json
import sys


# claude_code_sdk lebt nur im Worker-Image. Gleicher MagicMock-Ersatz wie in
# tests/research_cloud — ein schmalerer Stub auf Modulebene braeche dort
# Tests, die nach dieser Datei laufen (create_sdk_mcp_server fehlte).
from unittest.mock import MagicMock as _MagicMock  # noqa: E402

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

import src.main as main  # noqa: E402
from src.models import ChatCompletionRequest, Message


def _request():
    return ChatCompletionRequest(
        model="claude-sonnet-5", messages=[Message(role="user", content="hi")], stream=True
    )


def _text(t):
    return {"content": [{"type": "text", "text": t}]}


_RESULT_OK = {"subtype": "success", "is_error": False, "usage": {"input_tokens": 1, "output_tokens": 1}}
_MARKER = {"type": "result", "subtype": "no_completion_marker", "is_error": True,
           "error_message": "Response may be incomplete - no completion marker received"}
_TOOL = {"content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}


async def _stream(monkeypatch, chunks):
    async def fake_run_completion(**kw):
        for c in chunks:
            yield c
    monkeypatch.setattr(main.claude_cli, "run_completion", fake_run_completion)
    return [c async for c in main.generate_streaming_response(_request(), "req-1")]


def _finish_reasons(out):
    res = []
    for c in out:
        if c.startswith("data: {") and '"choices"' in c:
            fr = json.loads(c[6:])["choices"][0]["finish_reason"]
            if fr:
                res.append(fr)
    return res


def _content(out):
    return "".join(
        json.loads(c[6:])["choices"][0]["delta"].get("content") or ""
        for c in out if c.startswith("data: {") and '"choices"' in c
    )


def _error_event(out):
    errs = [c for c in out if c.startswith("event: error\n")]
    assert len(errs) == 1
    return json.loads(errs[0].split("data: ", 1)[1])["error"]


async def test_vollstaendiger_strom_endet_mit_stop_und_done(monkeypatch):
    out = await _stream(monkeypatch, [_text("Antwort"), _RESULT_OK])
    assert _finish_reasons(out) == ["stop"]
    assert out[-1] == "data: [DONE]\n\n"
    assert not any(c.startswith("event: error") for c in out)


async def test_abschneide_marker_endet_laut(monkeypatch):
    out = await _stream(monkeypatch, [_text("abgeschnitt"), _MARKER])
    assert "data: [DONE]\n\n" not in out
    assert _finish_reasons(out) == []
    err = _error_event(out)
    assert err["code"] == "stream_incomplete"
    assert "no_completion_marker" in err["message"]
    assert _content(out) == "abgeschnitt"


async def test_strom_ohne_inhalt_endet_laut_statt_ersatzsatz(monkeypatch):
    out = await _stream(monkeypatch, [_RESULT_OK])
    assert "data: [DONE]\n\n" not in out
    assert _finish_reasons(out) == []
    assert "unable to provide" not in _content(out)
    assert _error_event(out)["code"] == "stream_incomplete"


async def test_nur_werkzeugaufruf_ist_kein_abbruch(monkeypatch):
    out = await _stream(monkeypatch, [_TOOL, _RESULT_OK])
    assert _finish_reasons(out) == ["stop"]
    assert out[-1] == "data: [DONE]\n\n"
    assert "unable to provide" not in _content(out)


async def _stream_mit_buchung(monkeypatch, chunks):
    """Wie _stream, aber mit Anfrageobjekt, damit die Ledger-Buchung laeuft."""
    booked = []

    async def fake_persist(**kw):
        booked.append(kw)

    import src.activity.ai_call_writer as writer
    import src.middleware.prompt_metrics as pm
    monkeypatch.setattr(writer, "persist_ai_call_activity", fake_persist)
    monkeypatch.setattr(pm, "get_prompt_metrics", lambda: type("M", (), {"record": lambda self, **kw: None})())
    monkeypatch.setattr(main, "extract_attribution_context", lambda req: {})
    monkeypatch.setattr(main, "get_tenant_from_request", lambda req: None)

    async def fake_run_completion(**kw):
        for c in chunks:
            yield c
    monkeypatch.setattr(main.claude_cli, "run_completion", fake_run_completion)

    from types import SimpleNamespace

    class _Req:
        state = SimpleNamespace()
        headers: dict = {}

        async def is_disconnected(self):
            return False

    out = [c async for c in main.generate_streaming_response(_request(), "req-1", fastapi_request=_Req())]
    return out, booked


async def test_abgebrochener_strom_wird_mit_grund_als_fehler_gebucht(monkeypatch):
    out, booked = await _stream_mit_buchung(monkeypatch, [_text("abgeschnitt"), _MARKER])
    assert len(booked) == 1
    assert booked[0]["status"] == "error"
    assert booked[0]["error_code"] == "stream_incomplete"
    assert "no_completion_marker" in booked[0]["error_message"]


async def test_vollstaendiger_strom_wird_als_erfolg_gebucht(monkeypatch):
    out, booked = await _stream_mit_buchung(monkeypatch, [_text("Antwort"), _RESULT_OK])
    assert len(booked) == 1
    assert booked[0]["status"] == "success"
    assert booked[0]["error_code"] is None


# ── /v1/doc-agent sammelt denselben CLI-Strom intern ────────────────────────

async def _doc_agent(monkeypatch, chunks):
    from src.models import DocAgentFile, DocAgentRequest

    async def fake_run_completion(**kw):
        for c in chunks:
            yield c

    async def fake_persist(**kw):
        return None

    import src.activity.ai_call_writer as writer
    monkeypatch.setattr(writer, "persist_ai_call_activity", fake_persist)
    monkeypatch.setattr(main.claude_cli, "run_completion", fake_run_completion)
    req = DocAgentRequest(question="Was steht drin?", files=[DocAgentFile(name="a.txt", content="x")])
    return await main._execute_doc_agent_impl(req)


async def test_doc_agent_abgeschnitten_ist_fehler(monkeypatch):
    res = await _doc_agent(monkeypatch, [_text("halbe Antw"), _MARKER])
    assert res.status == "error"
    assert "no_completion_marker" in res.error
    assert not res.answer


async def test_doc_agent_vollstaendig_ist_erfolg(monkeypatch):
    res = await _doc_agent(monkeypatch, [_text("Antwort"), _RESULT_OK])
    assert res.status == "success"
    assert res.answer == "Antwort"
