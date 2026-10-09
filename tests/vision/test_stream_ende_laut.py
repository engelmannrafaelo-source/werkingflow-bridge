"""Ein Strom ohne Endesignal meldet keinen Erfolg (ZB3D, Rafael 2026-10-08:
"still abschneiden geht gar nicht").

Bricht die Verbindung zum Anbieter ab, bevor das Endesignal kommt, ist der
bis dahin gelieferte Text abgeschnitten. Der Aufrufer muss das als Fehler
sehen: ein `event: error`, kein Final-Chunk mit finish_reason "stop", kein
`[DONE]` — und in der Buchung Status "error".
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src import bedrock_service
from src.models import ChatCompletionRequest, Message


def _request(**kw):
    return ChatCompletionRequest(
        model="claude-sonnet-5", messages=[Message(role="user", content="hi")], **kw
    )


def _ev(d):
    return {"chunk": {"bytes": json.dumps(d).encode()}}


_START = _ev({"type": "message_start", "message": {"usage": {"input_tokens": 3}}})
_TEXT = _ev({"type": "content_block_delta", "delta": {"text": "teil"}})
_DELTA_END = _ev({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                  "usage": {"output_tokens": 5}})
_STOP = _ev({"type": "message_stop"})


def _fake_client(events):
    boto = SimpleNamespace(invoke_model_with_response_stream=lambda **kw: {
        "body": events, "ResponseMetadata": {"RequestId": "r"},
    })
    return SimpleNamespace(default_region="eu-central-1", get_client=lambda region: boto)


async def _run_stream(monkeypatch, events):
    monkeypatch.setattr(bedrock_service, "get_bedrock_client", lambda: _fake_client(events))
    sink: dict = {}
    chunks = [c async for c in bedrock_service.stream_bedrock(_request(stream=True), usage_sink=sink)]
    return chunks, sink


def _finish_reasons(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: {") and '"choices"' in c:
            fr = json.loads(c[6:])["choices"][0]["finish_reason"]
            if fr:
                out.append(fr)
    return out


def _assert_laut(chunks, sink):
    assert "data: [DONE]\n\n" not in chunks
    assert _finish_reasons(chunks) == []
    assert chunks[-1].startswith("event: error\n")
    assert sink["status"] == "error"
    assert sink.get("error_message")


async def test_vollstaendiger_strom_endet_mit_stop_und_done(monkeypatch):
    chunks, sink = await _run_stream(monkeypatch, [_START, _TEXT, _DELTA_END, _STOP])
    assert _finish_reasons(chunks) == ["stop"]
    assert chunks[-1] == "data: [DONE]\n\n"
    assert sink["status"] == "success"


@pytest.mark.parametrize("events", [
    pytest.param([_START, _TEXT], id="abriss-nach-text"),
    pytest.param([_START, _TEXT, _DELTA_END], id="abriss-vor-message_stop"),
    pytest.param([], id="leerer-strom"),
])
async def test_strom_ohne_message_stop_ist_laut(monkeypatch, events):
    chunks, sink = await _run_stream(monkeypatch, events)
    _assert_laut(chunks, sink)
    assert "message_stop" in sink["error_message"]


async def test_message_stop_ohne_stop_reason_ist_laut(monkeypatch):
    chunks, sink = await _run_stream(monkeypatch, [_START, _TEXT, _STOP])
    _assert_laut(chunks, sink)
    assert "stop_reason" in sink["error_message"]


async def test_fehlerereignis_im_strom_ist_laut(monkeypatch):
    events = [_START, _TEXT, {"modelStreamErrorException": {"message": "kaputt"}}]
    chunks, sink = await _run_stream(monkeypatch, events)
    _assert_laut(chunks, sink)
    assert "modelStreamErrorException" in sink["error_message"]


async def test_fehler_chunk_im_strom_ist_laut(monkeypatch):
    events = [_START, _TEXT, _ev({"type": "error", "error": {"type": "overloaded_error"}})]
    chunks, sink = await _run_stream(monkeypatch, events)
    _assert_laut(chunks, sink)
    assert "overloaded_error" in sink["error_message"]


async def test_direktweg_ohne_stop_reason_ist_fehlerantwort(monkeypatch):
    body = {"content": [{"type": "text", "text": "teil"}], "usage": {}}
    resp = {"body": SimpleNamespace(read=lambda: json.dumps(body).encode()),
            "ResponseMetadata": {"RequestId": "r"}}
    boto = SimpleNamespace(invoke_model=lambda **kw: resp)
    client = SimpleNamespace(default_region="eu-central-1", get_client=lambda region: boto)
    monkeypatch.setattr(bedrock_service, "get_bedrock_client", lambda: client)
    with pytest.raises(HTTPException) as exc:
        await bedrock_service.call_bedrock(_request())
    assert exc.value.status_code == 502
    assert "stop_reason" in str(exc.value.detail)
