"""Bild-Policy am laufenden Server: die sieben Transportproben aus dem Codex-Urteil (R3) + Kontrolle.

Ein lokaler uvicorn-Server (dynamischer Port) mit der echten ``ImagePolicyMiddleware`` und einem
zaehlenden Endpunkt. Jeder Client sendet PNG-Base64 OHNE Gemini-Felder als werking-energy — egal ob
curl, urllib, Socket, requests, httpx (Thread, ``content=``), Node-fetch oder Node-child_process:
die Pruefung sitzt am Server, also sieht der Endpunkt nie etwas.
"""
from __future__ import annotations

import base64
import json
import socket
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module

import pytest
import uvicorn

from src.middleware.bild_policy_middleware import ImagePolicyMiddleware

PNG_B64 = base64.b64encode(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")).decode()
HITS: list[int] = []


async def _stub(scope, receive, send):
    assert scope["type"] == "http"
    body = b""
    more = True
    while more:
        m = await receive()
        body += m.get("body", b"")
        more = m.get("more_body", False)
    HITS.append(len(body))
    out = json.dumps({"ok": True, "bytes": len(body)}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": out})


@pytest.fixture(scope="module")
def server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(ImagePolicyMiddleware(_stub), host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started
    yield port
    srv.should_exit = True
    t.join(5)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("BRIDGE_VISION_DEFAULT_PROVIDER", "BRIDGE_IMAGE_POLICY_APPS", "BRIDGE_PRINCIPALS_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    HITS.clear()


def _payload(gemini: bool) -> bytes:
    body = {"model": "sonnet", "messages": [{"role": "user", "content": "Analysiere (Base64): " + PNG_B64}]}
    if gemini:
        body["provider_tier"] = "gemini-vision"
    return json.dumps(body).encode()


def _hdr(extra=None):
    return {"Content-Type": "application/json", "X-App-ID": "werking-energy", **(extra or {})}


def _curl(port, body, headers):
    args = ["curl", "-s", "-o", "-", "-w", "\n%{http_code}", "-X", "POST", f"http://127.0.0.1:{port}/v1/chat/completions", "--data-binary", "@-"]
    for k, v in headers.items():
        args += ["-H", f"{k}: {v}"]
    out = subprocess.run(args, input=body, capture_output=True, timeout=30).stdout.decode()
    return int(out.rsplit("\n", 1)[1])


def _urllib(port, body, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=body, headers=headers, method="POST")
    try:
        return urllib.request.urlopen(req, timeout=30).status
    except urllib.error.HTTPError as e:
        return e.code


def _socket(port, body, headers):
    head = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    raw = f"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n{head}Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
    with socket.create_connection(("127.0.0.1", port), timeout=30) as s:
        s.sendall(raw)
        data = b""
        while chunk := s.recv(65536):
            data += chunk
    return int(data.split(b" ", 2)[1])


def _requests(port, body, headers):
    return import_module("requests").post(f"http://127.0.0.1:{port}/v1/chat/completions", data=body, headers=headers, timeout=30).status_code


def _httpx_thread(port, body, headers):
    httpx = import_module("httpx")
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(lambda: httpx.post(f"http://127.0.0.1:{port}/v1/chat/completions", content=body, headers=headers, timeout=30).status_code).result()


def _httpx_content_stream(port, body, headers):
    httpx = import_module("httpx")
    return httpx.post(f"http://127.0.0.1:{port}/v1/chat/completions", content=(body[i:i + 16] for i in range(0, len(body), 16)), headers=headers, timeout=30).status_code


def _node(script):
    def run(port, body, headers):
        out = subprocess.run(["node", "-e", script], input=body, capture_output=True, timeout=60,
                             env={"PATH": "/usr/bin:/bin", "PORT": str(port), "HDRS": json.dumps(headers)}).stdout.decode().strip()
        return int(out)
    return run


_node_fetch = _node("""
let b=[];process.stdin.on('data',d=>b.push(d)).on('end',async()=>{
 const r=await fetch(`http://127.0.0.1:${process.env.PORT}/v1/chat/completions`,{method:'POST',headers:JSON.parse(process.env.HDRS),body:Buffer.concat(b)});
 console.log(r.status)})""")
_node_child = _node("""
let b=[];process.stdin.on('data',d=>b.push(d)).on('end',()=>{
 const h=Object.entries(JSON.parse(process.env.HDRS)).flatMap(([k,v])=>['-H',k+': '+v]);
 const r=require('child_process').spawnSync('curl',['-s','-o','/dev/null','-w','%{http_code}','-X','POST',`http://127.0.0.1:${process.env.PORT}/v1/chat/completions`,...h,'--data-binary','@-'],{input:Buffer.concat(b)});
 console.log(r.stdout.toString())})""")

CLIENTS = {
    "curl_subprocess": _curl, "urllib": _urllib, "socket": _socket, "requests_indirekt": _requests,
    "httpx_thread_content": _httpx_thread, "httpx_content_stream": _httpx_content_stream,
    "node_fetch": _node_fetch, "node_child_process_curl": _node_child,
}


@pytest.mark.parametrize("client", sorted(CLIENTS))
def test_png_ohne_gemini_wird_von_jedem_client_abgewiesen(server, client):
    assert CLIENTS[client](server, _payload(gemini=False), _hdr()) == 403
    assert HITS == [], f"{client}: der Endpunkt hat die Anfrage gesehen"


@pytest.mark.parametrize("client", sorted(CLIENTS))
def test_kontrolle_mit_gemini_kommt_vollstaendig_an(server, client):
    body = _payload(gemini=True)
    assert CLIENTS[client](server, body, _hdr()) == 200
    assert HITS == [len(body)], f"{client}: Koerper kam verfaelscht an"


def test_anfrage_ohne_bild_und_andere_app_bleiben_unberuehrt(server):
    text = json.dumps({"messages": [{"role": "user", "content": "Explain data:image/ URLs"}]}).encode()
    assert _requests(server, text, _hdr()) == 200
    assert _requests(server, _payload(False), _hdr({"X-App-ID": "werking-report"})) == 200
    assert _requests(server, _payload(False), {"Content-Type": "application/json", "X-Client-ID": "workflow/werking-energy/vision-handler/s1"}) == 403
    assert len(HITS) == 2


def test_roh_png_und_multipart_ueber_den_server(server):
    raw = base64.b64decode(PNG_B64)
    r = import_module("requests")
    assert r.post(f"http://127.0.0.1:{server}/v1/research", data=raw, headers={"X-App-ID": "werking-energy", "Content-Type": "application/octet-stream"}).status_code == 403
    assert r.post(f"http://127.0.0.1:{server}/v1/document/convert", files={"f": ("a.bin", raw)}, headers={"X-App-ID": "werking-energy"}).status_code == 403
    assert HITS == []


def test_principal_gewinnt_vor_der_selbsterklaerten_app(server, monkeypatch):
    import types
    import src.principals as principals

    async def resolve(token):
        return types.SimpleNamespace(allowed_apps=["werking-energy"], is_legacy=False)

    monkeypatch.setenv("BRIDGE_PRINCIPALS_ENABLED", "true")
    monkeypatch.setattr(principals, "resolve_principal_by_token", resolve)
    h = {"Content-Type": "application/json", "X-App-ID": "werking-report", "Authorization": "Bearer t"}
    assert _requests(server, _payload(False), h) == 403
    assert HITS == []


def test_ausfall_der_principal_aufloesung_faellt_auf_die_kopfzeile_zurueck(server, monkeypatch):
    import src.principals as principals

    async def boom(token):
        raise RuntimeError("platform-api weg")

    monkeypatch.setenv("BRIDGE_PRINCIPALS_ENABLED", "true")
    monkeypatch.setattr(principals, "resolve_principal_by_token", boom)
    assert _requests(server, _payload(False), {**_hdr(), "Authorization": "Bearer t"}) == 403


def test_middleware_ist_in_main_eingehaengt():
    import ast
    import pathlib

    tree = ast.parse((pathlib.Path(__file__).parents[2] / "src/main.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_middleware"
             and n.args and getattr(n.args[0], "id", "") == "ImagePolicyMiddleware"]
    assert len(calls) == 1
