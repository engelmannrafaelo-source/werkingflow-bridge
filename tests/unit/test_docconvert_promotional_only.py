"""/v1/document/convert unter promotional_only (Rafael R3, 31.10.2026).

Offline: Pool-Koordinator gemockt, Anthropic per ``httpx.MockTransport``;
kein ``CREDIT_POOL_TEST_DSN``, keine echte Anfrage. Prüfplan docconvert-pool
PLAN.md Punkte 1–8 (Punkt 9 = Deploy-Positivliste, unten).
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PAID_KEY = "sk-ant-platzhalter-bezahlt-nie-benutzen"
POOL_KEY = "pool-key-gratis"
MARKER = "_[Bildbeschreibung fehlgeschlagen"


def _png() -> str:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 10, 10)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


class _Anthropic:
    """MockTransport für api.anthropic.com/v1/messages und /count_tokens."""

    def __init__(self, messages_status: int = 200, count_status: int = 200):
        self.calls: list[tuple[str, str]] = []
        self.messages_status = messages_status
        self.count_status = count_status

    def handler(self, req: httpx.Request) -> httpx.Response:
        key = req.headers.get("x-api-key", "")
        self.calls.append((req.url.path, key))
        if req.url.path.endswith("/count_tokens"):
            return httpx.Response(self.count_status, json={"input_tokens": 50})
        if self.messages_status != 200:
            return httpx.Response(self.messages_status, json={"error": {"message": "rate"}})
        body = json.loads(req.content)
        if "Triage" in json.dumps(body.get("system", "")) or "relevant" in json.dumps(body):
            n = sum(1 for b in body["messages"][0]["content"] if b.get("type") == "image")
            text = json.dumps({str(i): {"relevant": True, "label": "x"} for i in range(1, n + 1)})
        else:
            text = "Beschreibung"
        return httpx.Response(200, json={
            "model": body["model"], "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 120, "output_tokens": 30},
        })

    @property
    def messages(self):
        return [c for c in self.calls if c[0] == "/v1/messages"]


@pytest.fixture
def env(monkeypatch):
    """Privacy-App mit Fake-Docling-Kette, echtem VisionProvider, gemocktem Netz."""
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient
    if "src.privacy_service.app" not in sys.modules and not os.environ.get("PRIVACY_SERVICE_TOKEN"):
        monkeypatch.setenv("PRIVACY_SERVICE_TOKEN", "test-privacy-token")
    import src.vision_provider as vp
    from src.privacy_service import app as app_mod
    from src.privacy_service import image_triage
    from src.privacy_service.document_converter import ConversionResult
    from src.providers import credit_pool

    monkeypatch.setenv("ANTHROPIC_VISION_API_KEY", PAID_KEY)
    monkeypatch.delenv("ANTHROPIC_CREDIT_POOL_URL", raising=False)
    monkeypatch.delenv("CREDIT_POOL_TEST_DSN", raising=False)
    monkeypatch.setattr(vp, "_vision_provider", None)

    state = {"images": {}, "docling_calls": 0}

    class _Chain:
        def convert(self, content, filename, mime):
            state["docling_calls"] += 1
            return ConversionResult(markdown="# Doc", fmt="pdf", metadata={"adapter": "fake"},
                                    images=dict(state["images"]) or None)

    monkeypatch.setattr(app_mod, "_get_chain", lambda: _Chain())
    anth = _Anthropic()
    original = httpx.AsyncClient
    mock_client = lambda **kw: original(transport=httpx.MockTransport(anth.handler), **kw)  # noqa: E731
    monkeypatch.setattr(vp.httpx, "AsyncClient", mock_client)
    monkeypatch.setattr(credit_pool.httpx, "AsyncClient", mock_client)

    coord_events: list[tuple[str, dict | None]] = []

    async def coordinator(method, path, data=None):
        coord_events.append((path, data))
        if state.get("coord_error"):
            raise HTTPException(state["coord_error"], "Anthropic credit coordinator unavailable")
        if path == "/accounts":
            return {"accounts": [{"id": "acc1", "org_id": "org1", "grant_id": "g1", "api_key": POOL_KEY}]}
        return {}

    monkeypatch.setattr(credit_pool, "coordinator", coordinator)
    monkeypatch.setattr(image_triage, "_downscale_b64_png", lambda b64, max_edge=0: b64)

    def pool(on: bool):
        if on:
            monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_URL", "http://pool.invalid")
        else:
            monkeypatch.delenv("ANTHROPIC_CREDIT_POOL_URL", raising=False)

    def post(data):
        return TestClient(app_mod.app).post(
            "/document/convert", files={"file": ("a.pdf", b"%PDF-1.4 fake", "application/pdf")}, data=data,
            headers={"Authorization": f"Bearer {app_mod._SERVICE_TOKEN}"})

    return state, anth, coord_events, pool, post


PROMO = {"describe_images": "true", "funding_policy": "promotional_only"}


def test_1_pool_off_with_image_409_before_vision(env):
    state, anth, coord, pool, post = env
    state["images"] = {"fig1.png": _png()}
    pool(False)
    r = post(PROMO)
    assert r.status_code == 409
    assert "promotional_only: document vision not pool-funded; no paid fallback" in r.json()["detail"]
    assert state["docling_calls"] == 1 and anth.calls == [] and coord == []


def test_2_pool_off_no_images_200_unchanged(env):
    state, anth, coord, pool, post = env
    pool(False)
    r = post(PROMO)
    assert r.status_code == 200
    body = r.json()
    assert "vision_funding" not in body and "image_count" not in body and anth.calls == []


def test_3_coordinator_503_whole_call_503(env):
    state, anth, coord, pool, post = env
    state["images"] = {f"fig{i}.png": _png() for i in range(3)}
    state["coord_error"] = 503
    pool(True)
    r = post(PROMO)
    assert r.status_code == 503
    assert MARKER not in r.text
    assert anth.calls == [] and not any(p == "/reserve" for p, _ in coord)


@pytest.mark.parametrize("variante", ["429", "credit_balance"])
def test_4_all_accounts_exhausted_429(env, variante):
    state, anth, coord, pool, post = env
    state["images"] = {"fig1.png": _png()}
    pool(True)
    if variante == "429":
        anth.count_status = 429
    else:
        anth.messages_status = 400
        orig = anth.handler

        def handler(req):
            if req.url.path == "/v1/messages":
                anth.calls.append((req.url.path, req.headers.get("x-api-key", "")))
                return httpx.Response(400, text='{"error":{"message":"Your credit balance is too low"}}')
            return orig(req)
        anth.handler = handler
    r = post(PROMO)
    assert r.status_code == 429
    assert MARKER not in r.text
    assert all(k == POOL_KEY for _, k in anth.calls)


def test_5_pool_success_attests_and_uses_only_pool_key(env):
    state, anth, coord, pool, post = env
    state["images"] = {"fig1.png": _png(), "fig2.png": _png()}
    pool(True)
    r = post(PROMO)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vision_funding"] == "credit_pool"
    assert len(body["vision_usage"]) == 2
    for e in body["vision_usage"]:
        assert e["credit_meta"]["funding_policy"] == "promotional_only"
        assert e["credit_meta"]["credit_pool_reservation"]
        assert (e["input_tokens"], e["output_tokens"]) == (120, 30)
    assert anth.messages and all(k == POOL_KEY for _, k in anth.calls)
    assert PAID_KEY not in {k for _, k in anth.calls}
    assert sum(1 for p, _ in coord if p == "/settle") == 2


def test_6_triage_pool_error_loud_not_keep_all(env, monkeypatch):
    state, anth, coord, pool, post = env
    monkeypatch.setenv("BRIDGE_IMAGE_TRIAGE_THRESHOLD", "15")
    state["images"] = {f"fig{i:02d}.png": _png() for i in range(16)}
    state["coord_error"] = 503
    pool(True)
    r = post(PROMO)
    assert r.status_code == 503
    assert MARKER not in r.text and anth.calls == []


def test_6b_triage_success_books_triage_and_describe(env, monkeypatch):
    state, anth, coord, pool, post = env
    monkeypatch.setenv("BRIDGE_IMAGE_TRIAGE_THRESHOLD", "15")
    state["images"] = {f"fig{i:02d}.png": _png() for i in range(16)}
    pool(True)
    r = post(PROMO)
    assert r.status_code == 200, r.text
    kinds = [e["kind"] for e in r.json()["vision_usage"]]
    assert "triage" in kinds and kinds.count("describe") == 16
    assert all(k == POOL_KEY for _, k in anth.calls)


def test_7_without_funding_policy_old_behaviour(env):
    """Andere Apps: Pool aus → bezahlter Schlüssel wie heute; Pool-Fehler → Marker/200."""
    state, anth, coord, pool, post = env
    state["images"] = {"fig1.png": _png()}
    pool(False)
    r = post({"describe_images": "true"})
    assert r.status_code == 200
    body = r.json()
    assert "vision_funding" not in body and "vision_usage" not in body
    assert body["image_descriptions"] == {"fig1.png": "Beschreibung"}
    assert {k for _, k in anth.calls} == {PAID_KEY}

    state["coord_error"] = 503
    pool(True)
    r = post({"describe_images": "true"})
    assert r.status_code == 200 and MARKER in r.text and "vision_funding" not in r.json()


# --- Punkt 8: Bridge-Proxy (src/main.py) -----------------------------------

def _import_main():
    for mod in ["claude_code_sdk", "claude_code_sdk._errors", "claude_code_sdk._internal",
                "claude_code_sdk._internal.client", "src.identity.routes", "src.db.client"]:
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()
    import src.main
    return src.main


class _FakeTrackCall:
    async def __aenter__(self):
        return 0

    async def __aexit__(self, *exc):
        return False


def _request(app_id: str, fields):
    from starlette.requests import Request
    upload = MagicMock()
    upload.filename, upload.content_type = "a.pdf", "application/pdf"
    upload.read = AsyncMock(return_value=b"%PDF-1.4 fake")
    form = MagicMock()
    form.get = MagicMock(side_effect=lambda k, d=None: upload if k == "file" else d)
    form.multi_items = MagicMock(return_value=[("file", upload), *fields])
    req = Request({"type": "http", "headers": [(b"x-app-id", app_id.encode())] if app_id else []})
    req.form = AsyncMock(return_value=form)
    return req


async def _proxy(app_id, fields, upstream):
    main = _import_main()
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value=upstream)
    pc = MagicMock()
    pc.post = AsyncMock(return_value=resp)
    pc.track_call = MagicMock(return_value=_FakeTrackCall())
    req = _request(app_id, fields)
    persist = AsyncMock()
    with (patch("src.main.get_privacy_client", return_value=pc),
          patch("src.main._record_document_call_metrics"),
          patch("src.main._maybe_repair_pdf_actualtext", AsyncMock(side_effect=lambda c, *a: c)),
          patch("src.activity.ai_call_writer.persist_ai_call_activity", persist),
          patch("src.main.verify_api_key", AsyncMock())):
        out = await main.convert_document_endpoint(req, None)
    return out, pc.post.call_args.kwargs["data"], persist


USAGE = [{"kind": "describe", "model": "claude-sonnet-5-5", "input_tokens": 120, "output_tokens": 30,
          "credit_meta": {"credit_pool_reservation": "r1", "funding_policy": "promotional_only"}},
         {"kind": "triage", "model": "claude-haiku-4-5-20251001", "input_tokens": 10, "output_tokens": 5,
          "credit_meta": {"credit_pool_reservation": "r2", "funding_policy": "promotional_only"}}]


async def test_8a_energy_field_set_serverside_caller_value_overridden():
    out, data, _ = await _proxy("werking-energy",
                                [("describe_images", "true"), ("funding_policy", "paid_ok")],
                                {"success": True, "markdown": "x"})
    assert data["funding_policy"] == "promotional_only" and out.status_code == 200


async def test_8b_images_without_attestation_409():
    out, _, persist = await _proxy("werking-energy", [("describe_images", "true")],
                                   {"success": True, "markdown": "x", "image_count": 2, "images": {"a": "b"}})
    assert out.status_code == 409
    assert "not pool-funded" in json.loads(out.body)["error"]
    assert persist.await_args.kwargs["status"] == "error"


async def test_8c_attested_books_honest_usage_not_docling():
    out, _, persist = await _proxy("werking-energy", [("describe_images", "true")],
                                   {"success": True, "markdown": "x", "image_count": 1,
                                    "vision_funding": "credit_pool", "vision_usage": USAGE})
    assert out.status_code == 200
    body = json.loads(out.body)
    assert "vision_usage" not in body and body["vision_funding"] == "credit_pool"
    rows = {c.kwargs["model"]: c.kwargs for c in persist.await_args_list}
    assert "docling" not in rows
    assert rows["claude-sonnet-5-5"]["input_tokens"] == 120 and rows["claude-sonnet-5-5"]["output_tokens"] == 30
    assert rows["claude-sonnet-5-5"]["provider_meta"]["credit_pool_reservations"] == ["r1"]
    assert rows["claude-haiku-4-5-20251001"]["input_tokens"] == 10


async def test_8d_other_app_unchanged():
    upstream = {"success": True, "markdown": "x", "image_count": 1, "images": {"a": "b"}}
    out, data, persist = await _proxy("werking-report", [("describe_images", "true")], dict(upstream))
    assert "funding_policy" not in data and out.status_code == 200
    assert json.loads(out.body) == upstream
    assert persist.await_args.kwargs["model"] == "docling" and persist.await_args.kwargs["input_tokens"] == 0


# --- Punkt 9: Deploy-Positivliste ------------------------------------------

def test_9_gpu_deploy_allowlist_contains_vision_files():
    script = ROOT / "scripts" / "gpu-privacy-deploy.sh"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    line = next(z for z in script.read_text().splitlines() if z.startswith("unexpected="))
    for f in ("src/privacy_service/app.py", "src/privacy_service/image_describer.py",
              "src/privacy_service/image_triage.py", "src/vision_provider.py",
              "src/providers/credit_pool.py"):
        assert f in line
