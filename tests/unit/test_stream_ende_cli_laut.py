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


# ── claude_code_sdk stub (not installed locally; runs inside Docker on bridge) ──
# Register a minimal stub so src.claude_cli can be imported without the real SDK.
if "claude_code_sdk" not in sys.modules:
    import types
    _sdk_stub = types.ModuleType("claude_code_sdk")

    class _ClaudeCodeOptions:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class _Message:
        pass

    async def _query_stub(*args, **kwargs):
        return
        yield  # make it an async generator

    _sdk_stub.query = _query_stub
    _sdk_stub.ClaudeCodeOptions = _ClaudeCodeOptions
    _sdk_stub.Message = _Message

    # Minimal type stubs used in claude_cli
    _types_stub = types.ModuleType("claude_code_sdk.types")

    class _TextBlock:
        def __init__(self, text=""):
            self.type = "text"
            self.text = text

    class _ToolUseBlock:
        def __init__(self, id="", name="", input=None):
            self.type = "tool_use"
            self.id = id
            self.name = name
            self.input = input or {}

    _types_stub.TextBlock = _TextBlock
    _types_stub.ToolUseBlock = _ToolUseBlock

    class _AssistantMessage:
        def __init__(self, content=None, model=""):
            self.content = content or []
            self.model = model

    class _SystemMessage:
        def __init__(self, subtype="", data=None):
            self.subtype = subtype
            self.data = data or {}

    _sdk_stub.AssistantMessage = _AssistantMessage
    _sdk_stub.SystemMessage = _SystemMessage

    _errors_stub = types.ModuleType("claude_code_sdk._errors")

    class _MessageParseError(Exception):
        pass

    _errors_stub.MessageParseError = _MessageParseError

    sys.modules["claude_code_sdk"] = _sdk_stub
    sys.modules["claude_code_sdk.types"] = _types_stub
    sys.modules["claude_code_sdk._errors"] = _errors_stub

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
