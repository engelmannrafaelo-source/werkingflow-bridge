"""OpenAI-kompatibler Strom ohne Endesignal meldet keinen Erfolg (ZB3D).

Bis ZB3D reichte die Bridge die Zeilen des Anbieters nur durch: endete dessen
Strom ohne [DONE] oder ohne finish_reason, schloss die Bridge die Antwort
sauber — der Aufrufer hielt abgeschnittenen Text fuer fertig.
"""
from __future__ import annotations

import json

import httpx
import pytest

from src.models import ChatCompletionRequest, Message
from src.providers import openai_compatible


def _request():
    return ChatCompletionRequest(
        model="m", messages=[Message(role="user", content="hi")], stream=True
    )


def _chunk(content=None, finish=None):
    delta = {"content": content} if content is not None else {}
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})


class _BrokenStream(httpx.AsyncByteStream):
    def __init__(self, first: bytes):
        self.first = first

    async def __aiter__(self):
        yield self.first
        raise httpx.ReadError("connection reset")


async def _run(monkeypatch, body=None, stream=None):
    def handler(request):
        if stream is not None:
            return httpx.Response(200, stream=stream)
        return httpx.Response(200, content=body.encode())

    real = httpx.AsyncClient
    monkeypatch.setattr(
        openai_compatible.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    return [c async for c in openai_compatible.stream_openai_compatible(_request(), "http://x", "k")]


def _error(out):
    assert out[-1].startswith("event: error\n")
    assert "data: [DONE]\n\n" not in out
    return json.loads(out[-1].split("data: ", 1)[1])["error"]


async def test_vollstaendiger_strom_endet_mit_done(monkeypatch):
    body = "\n".join([_chunk("a"), _chunk(finish="stop"), "data: [DONE]", ""])
    out = await _run(monkeypatch, body)
    assert out[-1] == "data: [DONE]\n\n"
    assert not any(c.startswith("event: error") for c in out)


async def test_strom_ohne_done_ist_laut(monkeypatch):
    out = await _run(monkeypatch, "\n".join([_chunk("abgeschn"), ""]))
    err = _error(out)
    assert err["code"] == "stream_incomplete"
    assert "without [DONE]" in err["message"]


async def test_done_ohne_finish_reason_ist_laut(monkeypatch):
    out = await _run(monkeypatch, "\n".join([_chunk("abgeschn"), "data: [DONE]", ""]))
    assert "without finish_reason" in _error(out)["message"]


async def test_abriss_mitten_im_strom_ist_laut_und_ohne_neustart(monkeypatch):
    calls = []
    stream = _BrokenStream((_chunk("abgeschn") + "\n").encode())

    def counting(request):
        calls.append(1)
        return httpx.Response(200, stream=stream)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        openai_compatible.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(counting), **kw),
    )
    out = [c async for c in openai_compatible.stream_openai_compatible(_request(), "http://x", "k")]
    assert "ReadError" in _error(out)["message"]
    assert len(calls) == 1
