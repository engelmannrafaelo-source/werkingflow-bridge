"""Eingang (R5): mehrdeutige Requests -> 400, bevor irgendetwas geparst wird. Codex-R4-Proben gegen
einen echten FastAPI-Endpunkt mit dem echten ``ChatCompletionRequest`` (zaehlt nur Treffer)."""
from __future__ import annotations

import base64
import json
import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI

from src.middleware.bild_policy_middleware import ImagePolicyMiddleware, ambiguity
from src.models import ChatCompletionRequest

PNG_B64 = base64.b64encode(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")).decode()
HITS: list[str] = []


def _app():
    api = FastAPI()

    @api.post("/v1/chat/completions")
    async def chat(req: ChatCompletionRequest):
        HITS.append(req.model)
        return {"ok": True}

    api.add_middleware(ImagePolicyMiddleware)
    return api


@pytest.fixture(scope="module")
def port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    srv = uvicorn.Server(uvicorn.Config(_app(), host="127.0.0.1", port=p, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield p
    srv.should_exit = True
    t.join(5)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("BRIDGE_VISION_DEFAULT_PROVIDER", "BRIDGE_IMAGE_POLICY_APPS", "BRIDGE_PRINCIPALS_ENABLED", "BRIDGE_IMAGE_POLICY_MAX_BODY"):
        monkeypatch.delenv(k, raising=False)
    HITS.clear()


BODY = json.dumps({"model": "sonnet", "messages": [{"role": "user", "content": [
    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG_B64}}]}]}).encode()


def raw(port, headers: list[tuple[str, str]], body: bytes = BODY, chunked: bool = False) -> int:
    head = "".join(f"{k}: {v}\r\n" for k, v in headers)
    if chunked:
        payload = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
        req = f"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n{head}Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n".encode() + payload
    else:
        req = f"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n{head}Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
    with socket.create_connection(("127.0.0.1", port), timeout=30) as s:
        s.sendall(req)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    return int(data.split(b" ", 2)[1])


APP = ("X-App-ID", "werking-energy")
JSON = ("Content-Type", "application/json")


def test_kontrolle_json_mit_bild_ohne_gemini_wird_abgewiesen(port):
    assert raw(port, [APP, JSON]) == 403 and HITS == []


@pytest.mark.parametrize("zweiter", ["application/pdf", "multipart/form-data; boundary=missing", "audio/wav", "text/plain"])
def test_doppelter_content_type_erreicht_den_chat_parser_nie(port, zweiter):
    """Codex R4 #1: JSON zuerst, danach ein zweiter Typ -> vorher 200, jetzt 400."""
    assert raw(port, [APP, JSON, ("Content-Type", zweiter)]) == 400
    assert raw(port, [APP, ("Content-Type", zweiter), JSON]) == 400
    assert HITS == []


@pytest.mark.parametrize("dup", [("X-App-ID", "werking-report"), ("X-Client-ID", "x/y"), ("Authorization", "Bearer b")])
def test_doppelte_identitaetskopfzeilen_werden_abgewiesen(port, dup):
    """Nicht nur der Typ: wer X-App-ID doppelt schickt, liesse Policy (letzter) und Parser (erster) verschieden lesen."""
    assert raw(port, [JSON, ("X-App-ID", "werking-energy"), ("X-App-ID", "werking-report")] if dup[0] == "X-App-ID"
               else [APP, JSON, dup, dup]) == 400
    assert HITS == []


@pytest.mark.parametrize("enc", ["gzip", "deflate", "br", "zstd"])
def test_content_encoding_wird_abgewiesen(port, enc):
    assert raw(port, [APP, JSON, ("Content-Encoding", enc)]) == 400 and HITS == []


def test_identity_encoding_geht_durch(port):
    ok = json.dumps({"model": "sonnet", "messages": [{"role": "user", "content": "hallo"}]}).encode()
    assert raw(port, [APP, JSON, ("Content-Encoding", "identity")], body=ok) == 200 and HITS == ["sonnet"]


def test_content_length_zugleich_mit_transfer_encoding(port):
    head = [APP, JSON, ("Content-Length", str(len(BODY)))]
    assert raw(port, head, chunked=True) == 400 and HITS == []


def test_ungueltige_content_length_ist_mehrdeutig():
    assert ambiguity({"headers": [(b"content-length", b"12abc")]}) is not None
    assert ambiguity({"headers": [(b"content-length", b"-1")]}) is not None
    assert ambiguity({"headers": [(b"content-length", b"12"), (b"content-length", b"12")]}) is not None
    assert ambiguity({"headers": [(b"content-length", b"12"), (b"content-type", b"application/json")]}) is None


def test_gezielte_typ_taeuschung_mit_bild_im_json(port):
    for ctype in ("application/pdf", "audio/wav", "video/mp4"):
        assert raw(port, [APP, ("Content-Type", ctype)]) == 403
    assert HITS == []


def test_zu_grosser_koerper_wird_frueh_abgewiesen(port, monkeypatch):
    monkeypatch.setenv("BRIDGE_IMAGE_POLICY_MAX_BODY", "1000")
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nX-App-ID: werking-energy\r\nContent-Type: application/json\r\n"
              b"Transfer-Encoding: chunked\r\n\r\n" + (b"3e8\r\n" + b"a" * 1000 + b"\r\n") * 3)
    s.settimeout(5)
    data = s.recv(65536)  # Antwort kommt, OHNE dass der Sender den Koerper beendet hat
    s.close()
    assert data.split(b" ", 2)[1] == b"413"
