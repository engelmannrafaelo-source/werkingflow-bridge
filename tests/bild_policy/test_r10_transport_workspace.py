"""Offline regressions use real local tools and final provider transport seams."""

import asyncio
import base64
import hashlib
import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from src.erkunder.kind import sdk_options
from src.erkunder.leitstand import Coordinator, StepFailed
from src.providers import text_egress
from src.routing import bild_policy as bp

PDF = (
    b"%PDF-1.4\n1 0 obj\n<< /Typ"
    b"e /Catalog /Pages 2 0 R "
    b">>\nendobj\n2 0 obj\n<< /Ty"
    b"pe /Pages /Kids [3 0 R] "
    b"/Count 1 >>\nendobj\n3 0 o"
    b"bj\n<< /Type /Page /Paren"
    b"t 2 0 R /MediaBox [0 0 1"
    b"0 10] /Resources << /XOb"
    b"ject << /Im0 4 0 R >> >>"
    b" /Contents 5 0 R >>\nendo"
    b"bj\n4 0 obj\n<< /Type /XOb"
    b"ject /Subtype /Image /Wi"
    b"dth 1 /Height 1 /ColorSp"
    b"ace /DeviceRGB /BitsPerC"
    b"omponent 8 /Filter /ASCI"
    b"IHexDecode /Length 7 >>\n"
    b"stream\nff0000>\nendstream"
    b"\nendobj\n5 0 obj\n<< /Leng"
    b"th 29 >>\nstream\nq 10 0 0"
    b" 10 0 0 cm /Im0 Do Q\nend"
    b"stream\nendobj\nxref\n0 6\n0"
    b"000000000 65535 f \n00000"
    b"00009 00000 n \n000000005"
    b"8 00000 n \n0000000115 00"
    b"000 n \n0000000243 00000 "
    b"n \n0000000416 00000 n \nt"
    b"railer\n<< /Size 6 /Root "
    b"1 0 R >>\nstartxref\n494\n%"
    b"%EOF\n"
)
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00"
    b"\x03\x03\x02\x00\xef\xa3\x0f[\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture(autouse=True)
def energy():
    token = bp.CURRENT_APP.set("werking-energy")
    yield
    bp.CURRENT_APP.reset(token)


@pytest.mark.parametrize(
    "command,data", [(["cat"], PDF), (["base64"], PDF), (["xxd"], PDF), (["xxd"], PNG)]
)
def test_real_bash_encoding_is_blocked(command, data, tmp_path):
    source = tmp_path / "original.txt"
    source.write_bytes(data)
    stdout = subprocess.check_output([*command, str(source)]).decode()
    (tmp_path / "work").mkdir()
    options = sdk_options(
        {
            "ordner": str(tmp_path / "work"),
            "max_turns": 1,
            "claude_token": "erkunder-proxy-offline",
            "model_proxy_url": "http://proxy.invalid/model",
        }
    )
    decision = asyncio.run(
        options.hooks["PostToolUse"][-1].hooks[0](
            {"tool_name": "Bash", "tool_response": {"stdout": stdout, "stderr": ""}},
            None,
            None,
        )
    )
    assert decision["continue"] is False
    assert options._image_policy_failures[0].code == "image_tool_access_refused"


@pytest.mark.parametrize("quote", ["'", '"', ""])
def test_privacy_conversion_never_sends_data_url(monkeypatch, quote):
    import src.privacy_service.app as app

    encoded = base64.b64encode(PNG).decode()
    html = "<div style='position:absolute'>synthetic</div>" * 20
    html += f"<img src={quote}data:image/png;base64,{encoded}{quote}>"
    sent = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            sent.append(kwargs["json"])
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "content": [
                        {
                            "type": "text",
                            "text": "<style>p{color:black}</style>"
                            + "<p>Converted</p>" * 40,
                        }
                    ],
                    "stop_reason": "end_turn",
                },
            )

    monkeypatch.setenv("PRIVACY_MODEL_PROXY_URL", "http://proxy.invalid/messages")
    monkeypatch.setenv("PRIVACY_MODEL_PROXY_TOKEN", "offline")
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    response = TestClient(app.app).post(
        "/convert-semantic-html",
        json={"html": html},
        headers={"X-Bridge-Verified-App": "werking-energy"},
    )
    assert response.status_code in {200, 403}
    assert all(encoded not in str(body) for body in sent)
    if quote != '"':
        assert response.status_code == 403
        assert not sent


@pytest.mark.parametrize(
    "payload",
    [
        {
            "messages": [
                {"role": "user", "content": "data:application/pdf;base64,JVBERi0xLjQK"}
            ]
        },
        {
            "messages": [
                {"role": "user", "content": {"type": "input_file", "file_id": "x"}}
            ]
        },
        {"messages": [{"role": "user", "content": base64.b64encode(PDF).decode()}]},
    ],
)
def test_transport_validation_precedes_any_send(payload):
    client = SimpleNamespace(
        post=AsyncMock(),
        invoke_model=lambda **kw: pytest.fail("sent"),
        invoke_model_with_response_stream=lambda **kw: pytest.fail("sent"),
    )
    with pytest.raises(bp.ImageEgressRefused):
        asyncio.run(text_egress.post(client, "https://provider.invalid", json=payload))
    client.post.assert_not_called()
    import json

    for stream in (False, True):
        with pytest.raises(bp.ImageEgressRefused):
            text_egress.invoke(client, streaming=stream, body=json.dumps(payload))


def test_text_transport_still_sends():
    client = SimpleNamespace(post=AsyncMock(return_value="ok"))
    assert (
        asyncio.run(
            text_egress.post(
                client,
                "https://provider.invalid",
                json={
                    "messages": [
                        {"role": "user", "content": "Measurements: 13 + 17 = 30"}
                    ]
                },
            )
        )
        == "ok"
    )
    client.post.assert_awaited_once()


@pytest.mark.parametrize(
    "data,name",
    [
        (PDF, "plan/a.pdf"),
        (PDF, "unterlagen/a.txt"),
        (PNG, "unterlagen/a.txt"),
        (base64.b64encode(PDF), "unterlagen/a.md"),
    ],
)
def test_original_never_written_to_workspace(tmp_path, data, name):
    class Response:
        def raise_for_status(self):
            pass

        async def aiter_bytes(self):
            yield data

    class Client:
        @asynccontextmanager
        async def stream(self, *args):
            yield Response()

    ident = "offline-report"
    directory = tmp_path / ident
    directory.mkdir()
    coordinator = Coordinator.__new__(Coordinator)
    coordinator.root = tmp_path
    coordinator.client = Client()
    coordinator.states = {ident: {}}
    file = SimpleNamespace(
        ziel=name,
        url="https://storage.invalid/file",
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    order = SimpleNamespace(vorwissen_md="Text", vertiefung_md=None, dateien=[file])
    with pytest.raises(StepFailed, match="image_tool_access_refused"):
        asyncio.run(coordinator.download(ident, order))
    assert not (directory / "eingang" / name).exists()
    assert all(
        p.read_bytes() not in {PDF, PNG} for p in directory.rglob("*") if p.is_file()
    )


def test_resume_refuses_old_original(tmp_path):
    entry = tmp_path / "eingang"
    entry.mkdir()
    (entry / "old.txt").write_bytes(PDF)
    with pytest.raises(bp.ImageEgressRefused):
        sdk_options(
            {
                "ordner": str(tmp_path / "work"),
                "max_turns": 1,
                "claude_token": "erkunder-proxy-offline",
                "model_proxy_url": "http://proxy.invalid/model",
            }
        )


def test_stream_checks_before_entering_transport():
    class Client:
        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            pytest.fail("unguarded stream entered")
            yield

    async def probe():
        async with text_egress.stream(
            Client(),
            "POST",
            "https://provider.invalid",
            json={
                "messages": [
                    {"role": "user", "content": base64.b64encode(PDF).decode()}
                ]
            },
        ):
            pytest.fail("unguarded stream yielded")

    with pytest.raises(bp.ImageEgressRefused):
        asyncio.run(probe())


@pytest.mark.parametrize(
    "extra",
    [{"files": {"file": b"binary"}}, {"content": b"binary"}, {"data": {"image": "x"}}],
)
def test_alternate_body_cannot_override_validated_json(extra):
    client = SimpleNamespace(post=AsyncMock())
    with pytest.raises(bp.ImageEgressRefused):
        asyncio.run(
            text_egress.post(
                client,
                "https://provider.invalid",
                json={"messages": [{"role": "user", "content": "Text"}]},
                **extra,
            )
        )
    client.post.assert_not_called()
