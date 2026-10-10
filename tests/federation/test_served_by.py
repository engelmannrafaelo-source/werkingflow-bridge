"""X-Bridge-Served-By — the answering worker names its bridge (BR6 E1).

The deploy smoke decides "did the deployed bridge answer?" from this header.
These tests pin the contract it relies on: present on every response
(streaming included), set rather than appended, and an unconfigured identity
visible as `unset` instead of guessed.
"""
from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from src.federation import SERVED_BY_HEADER, ServedByMiddleware, served_by_value


def _app():
    async def plain(_request):
        return JSONResponse({"ok": True})

    async def relayed(_request):
        # A response relayed from another worker already carries its stamp.
        return JSONResponse({"ok": True}, headers={SERVED_BY_HEADER: "prod/worker-erk"})

    async def stream(_request):
        async def gen():
            yield b"data: 1\n\n"
            yield b"data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    app = Starlette(routes=[Route("/plain", plain), Route("/relayed", relayed),
                            Route("/stream", stream)])
    app.add_middleware(ServedByMiddleware)
    return app


async def _get(path):
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/plain", "/stream"])
async def test_every_response_names_bridge_and_worker(monkeypatch, path):
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "Dev")
    monkeypatch.setenv("INSTANCE_NAME", "worker2")
    r = await _get(path)
    assert r.status_code == 200
    assert r.headers.get_list(SERVED_BY_HEADER) == ["dev/worker2"]


@pytest.mark.asyncio
async def test_own_identity_replaces_a_relayed_stamp(monkeypatch):
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "dev")
    monkeypatch.setenv("INSTANCE_NAME", "worker1")
    r = await _get("/relayed")
    assert r.headers.get_list(SERVED_BY_HEADER) == ["dev/worker1"]


def test_missing_identity_is_visible_not_guessed(monkeypatch):
    monkeypatch.delenv("BRIDGE_ORIGIN_ID", raising=False)
    monkeypatch.delenv("INSTANCE_NAME", raising=False)
    assert served_by_value() == "unset/unset"
