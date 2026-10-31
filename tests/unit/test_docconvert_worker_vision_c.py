"""Bau C: /v1/document/convert — der Bridge-Worker beschreibt Energy-Bilder selbst über den Pool.

Offline: Privacy-Dienst gemockt (liefert ``images`` ohne Beschreibung), Pool-Koordinator
gemockt, Anthropic per ``httpx.MockTransport``; kein ``CREDIT_POOL_TEST_DSN``, keine
echte Anfrage. Prüfplan docconvert-pool PLAN.md 1–8 sinngemäß für den Worker-Weg
plus Größe, Frist, Trennung des Aufrufers.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import time
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
NOT_FUNDED = "promotional_only: document vision not pool-funded; no paid fallback"
TRIAGE_PROMPT = "ENERGY-TRIAGE-KRITERIEN"


def _png() -> str:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 200, 10)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


class _Anthropic:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.systems: list[str] = []
        self.messages_status = 200
        self.count_status = 200
        self.delay = 0.0
        self.fail_names: set[int] = set()  # Nummer des Beschreibungsaufrufs mit 500

    async def handler(self, req: httpx.Request) -> httpx.Response:
        key = req.headers.get("x-api-key", "")
        self.calls.append((req.url.path, key))
        if req.url.path.endswith("/count_tokens"):
            return httpx.Response(self.count_status, json={"input_tokens": 50})
        if self.delay:
            await asyncio.sleep(self.delay)
        body = json.loads(req.content)
        self.systems.append(json.dumps(body.get("system", "")))
        if self.messages_status != 200:
            return httpx.Response(self.messages_status, text='{"error":{"message":"x"}}')
        n_img = sum(1 for b in body["messages"][0]["content"] if b.get("type") == "image")
        if n_img > 1 or "relevant" in json.dumps(body):
            text = json.dumps({str(i): {"relevant": True, "label": "x"} for i in range(1, n_img + 1)})
        else:
            if len(self.messages) in self.fail_names:
                return httpx.Response(500, text="upstream boom")
            text = "Beschreibung"
        return httpx.Response(200, json={
            "model": body["model"], "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 120, "output_tokens": 30},
        })

    @property
    def messages(self):
        return [c for c in self.calls if c[0] == "/v1/messages"]


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

    async def receive():
        await asyncio.sleep(3600)  # Aufrufer bleibt verbunden

    req = Request({"type": "http", "headers": [(b"x-app-id", app_id.encode())] if app_id else []}, receive)
    req.form = AsyncMock(return_value=form)
    return req


@pytest.fixture
def env(monkeypatch):
    import src.vision_provider as vp
    from src.privacy_service import image_triage
    from src.providers import credit_pool

    monkeypatch.setenv("ANTHROPIC_VISION_API_KEY", PAID_KEY)  # Worker hat ihn live (Wahrheitswert 1)
    monkeypatch.delenv("ANTHROPIC_CREDIT_POOL_URL", raising=False)
    monkeypatch.delenv("CREDIT_POOL_TEST_DSN", raising=False)
    for n in ("BRIDGE_DOCCONVERT_VISION_MAX_IMAGE_MB", "BRIDGE_DOCCONVERT_VISION_MAX_TOTAL_MB",
              "BRIDGE_DOCCONVERT_VISION_S_PER_CALL"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("BRIDGE_IMAGE_TRIAGE_THRESHOLD", "15")
    monkeypatch.setattr(vp, "_vision_provider", None)
    anth = _Anthropic()
    original = httpx.AsyncClient
    mock_client = lambda **kw: original(transport=httpx.MockTransport(anth.handler), **kw)  # noqa: E731
    monkeypatch.setattr(vp.httpx, "AsyncClient", mock_client)
    monkeypatch.setattr(credit_pool.httpx, "AsyncClient", mock_client)
    monkeypatch.setattr(image_triage, "_downscale_b64_png", lambda b64, max_edge=0: b64)
    coord: list[tuple[str, dict | None]] = []
    state: dict = {}

    async def coordinator(method, path, data=None):
        coord.append((path, data))
        if state.get("coord_error"):
            raise HTTPException(state["coord_error"], "Anthropic credit coordinator unavailable")
        if path == "/accounts":
            return {"accounts": [{"id": "acc1", "org_id": "org1", "grant_id": "g1", "api_key": POOL_KEY}]}
        return {}

    monkeypatch.setattr(credit_pool, "coordinator", coordinator)

    def pool(on: bool):
        if on:
            monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_URL", "http://platform-api:8000")
        else:
            monkeypatch.delenv("ANTHROPIC_CREDIT_POOL_URL", raising=False)

    async def proxy(app_id, fields, upstream, status=200):
        main = _import_main()
        resp = MagicMock()
        resp.status_code = status
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
        return out, json.loads(out.body), pc.post.call_args.kwargs["data"], persist

    return state, anth, coord, pool, proxy


ENERGY = "werking-energy"
FIELDS = [("describe_images", "true"), ("describe_prompt", "ENERGY-BESCHREIBUNG"),
          ("triage_prompt", TRIAGE_PROMPT)]


def _upstream(n: int, prefix: str = "fig") -> dict:
    imgs = {f"{prefix}{i:02d}.png": _png() for i in range(n)}
    return {"success": True, "format": "pdf", "markdown": "# Doc", "metadata": {"adapter": "fake"},
            "images": imgs or None, "image_count": n}


# 1 Pool im Worker aus, Bilder → 409, 0 Anthropic/Koordinator, bezahlter Schlüssel nie
async def test_c1_pool_off_images_409_no_call_paid_key_never(env):
    state, anth, coord, pool, proxy = env
    pool(False)
    out, body, data, persist = await proxy(ENERGY, FIELDS, _upstream(1))
    assert out.status_code == 409 and body["error"] == NOT_FUNDED
    assert data["describe_images"] == "false" and data["funding_policy"] == "promotional_only"
    assert anth.calls == [] and coord == []
    assert persist.await_args.kwargs["status"] == "error" and persist.await_args.kwargs["model"] == "docling"


# 2 Pool aus, 0 Bilder → 200 unverändert (Privacy-Antwort durchgereicht)
async def test_c2_no_images_200_unchanged(env):
    state, anth, coord, pool, proxy = env
    pool(False)
    up = _upstream(0)
    out, body, data, _ = await proxy(ENERGY, FIELDS, dict(up))
    assert out.status_code == 200 and body == up
    assert data["describe_images"] == "false" and anth.calls == []


# 3 Koordinator 503 → ganzer Aufruf 503, kein Marker, keine Reservierung
async def test_c3_coordinator_503_whole_call(env):
    state, anth, coord, pool, proxy = env
    pool(True)
    state["coord_error"] = 503
    out, body, _, _ = await proxy(ENERGY, FIELDS, _upstream(3))
    assert out.status_code == 503 and MARKER not in json.dumps(body)
    assert "markdown" not in body and anth.calls == []
    assert not any(p == "/reserve" for p, _ in coord)


# 4 alle Konten 429 bzw. „credit balance“ → 429 1:1, nur Pool-Schlüssel
@pytest.mark.parametrize("variante", ["429", "credit_balance"])
async def test_c4_exhausted_429(env, variante):
    state, anth, coord, pool, proxy = env
    pool(True)
    if variante == "429":
        anth.count_status = 429
    else:
        orig = anth.handler

        async def handler(req):
            if req.url.path == "/v1/messages":
                anth.calls.append((req.url.path, req.headers.get("x-api-key", "")))
                return httpx.Response(400, text='{"error":{"message":"Your credit balance is too low"}}')
            return await orig(req)
        anth.handler = handler
    out, body, _, _ = await proxy(ENERGY, FIELDS, _upstream(1))
    assert out.status_code == 429 and MARKER not in json.dumps(body) and "markdown" not in body
    assert anth.calls and all(k == POOL_KEY for _, k in anth.calls)


# 5 Erfolg → Markdown mit Beschreibungen, vision_funding, Buchung je Modell, nur Pool-Schlüssel
async def test_c5_success_markdown_attestation_booking(env):
    state, anth, coord, pool, proxy = env
    pool(True)
    out, body, data, persist = await proxy(ENERGY, FIELDS, _upstream(2))
    assert out.status_code == 200, body
    assert data["describe_images"] == "false"
    assert body["vision_funding"] == "credit_pool" and "vision_usage" not in body
    assert body["image_descriptions"] == {"fig00.png": "Beschreibung", "fig01.png": "Beschreibung"}
    assert body["markdown"].startswith("# Doc") and "## Bildbeschreibungen" in body["markdown"]
    assert "### fig01.png" in body["markdown"] and body["images"]  # Bilder bleiben wie heute
    assert all(k == POOL_KEY for _, k in anth.calls) and PAID_KEY not in {k for _, k in anth.calls}
    assert sum(1 for p, _ in coord if p == "/settle") == 2
    assert all("ENERGY-BESCHREIBUNG" in s for s in anth.systems)  # describe_prompt kommt an
    rows = [c.kwargs for c in persist.await_args_list]
    assert [r["model"] for r in rows] != ["docling"]
    (row,) = rows
    assert (row["input_tokens"], row["output_tokens"]) == (240, 60)
    assert row["provider_meta"]["vision_calls"] == 2
    assert len(row["provider_meta"]["credit_pool_reservations"]) == 2
    assert row["provider_meta"]["automatic_paid_fallback"] is False


# 6 Triage ≥ 16 Bilder bei Pool-Fehler → laut statt „keeping all“
async def test_c6_triage_pool_error_loud(env):
    state, anth, coord, pool, proxy = env
    pool(True)
    state["coord_error"] = 503
    out, body, _, _ = await proxy(ENERGY, FIELDS, _upstream(16))
    assert out.status_code == 503 and anth.calls == [] and "markdown" not in body


async def test_c6b_triage_success_books_and_honours_triage_prompt(env):
    state, anth, coord, pool, proxy = env
    pool(True)
    out, body, _, persist = await proxy(ENERGY, FIELDS, _upstream(16))
    assert out.status_code == 200, body
    assert body["described_count"] == 16 and body["triage"]["total_figures"] == 16
    assert any(TRIAGE_PROMPT in s for s in anth.systems)
    models = {c.kwargs["model"]: c.kwargs for c in persist.await_args_list}
    assert len(models) == 2  # Triage-Modell + Beschreibungsmodell, je eigene Zeile
    assert sum(m["provider_meta"]["vision_calls"] for m in models.values()) == 17


# 7 andere App: byte-gleich (describe_images=true geht an den Privacy-Dienst, keine Worker-Vision)
@pytest.mark.parametrize("app_id", ["werking-report", ""])
async def test_c7_other_app_unchanged(env, app_id):
    state, anth, coord, pool, proxy = env
    pool(True)
    up = _upstream(2)
    out, body, data, persist = await proxy(app_id, FIELDS, dict(up))
    assert out.status_code == 200 and body == up
    assert data == dict(FIELDS) and anth.calls == [] and coord == []
    assert persist.await_args.kwargs["model"] == "docling"


# 8 Energy ohne describe_images → nichts beschrieben; Aufruferwert funding_policy überschrieben
async def test_c8_energy_without_describe_untouched(env):
    state, anth, coord, pool, proxy = env
    pool(True)
    up = _upstream(2)
    out, body, data, _ = await proxy(ENERGY, [("funding_policy", "paid_ok")], dict(up))
    assert out.status_code == 200 and body == up
    assert data == {"funding_policy": "promotional_only"} and anth.calls == []


async def test_c8b_upstream_error_passes_1to1_no_vision(env):
    state, anth, coord, pool, proxy = env
    pool(True)
    out, body, _, _ = await proxy(ENERGY, FIELDS, {"detail": "zu gross"}, status=413)
    assert out.status_code == 413 and body == {"detail": "zu gross"} and anth.calls == []


# Größe: je Bild und gesamt → 413 vor jedem Aufruf
async def test_c9_image_too_large_413(env, monkeypatch):
    state, anth, coord, pool, proxy = env
    pool(True)
    monkeypatch.setenv("BRIDGE_DOCCONVERT_VISION_MAX_IMAGE_MB", "0.00001")
    out, body, _, _ = await proxy(ENERGY, FIELDS, _upstream(1))
    assert out.status_code == 413 and "je Bild" in body["error"] and anth.calls == [] and coord == []


async def test_c9b_total_too_large_413(env, monkeypatch):
    state, anth, coord, pool, proxy = env
    pool(True)
    one = len(_png())
    monkeypatch.setenv("BRIDGE_DOCCONVERT_VISION_MAX_TOTAL_MB", str(2.5 * one / 2**20))
    out, body, _, _ = await proxy(ENERGY, FIELDS, _upstream(3))
    assert out.status_code == 413 and "gesamt" in body["error"] and anth.calls == [] and coord == []


async def test_c9c_bad_limit_config_loud_500(env, monkeypatch):
    state, anth, coord, pool, proxy = env
    pool(True)
    monkeypatch.setenv("BRIDGE_DOCCONVERT_VISION_S_PER_CALL", "abc")
    out, body, _, _ = await proxy(ENERGY, FIELDS, _upstream(1))
    assert out.status_code == 500 and "BRIDGE_DOCCONVERT_VISION_S_PER_CALL" in body["error"] and anth.calls == []


# Frist: Schätzung passt nicht → 504 vor dem ersten Aufruf
async def test_c10_estimate_exceeds_budget_504_before_call(env, monkeypatch):
    state, anth, coord, pool, proxy = env
    pool(True)
    monkeypatch.setenv("BRIDGE_DOCCONVERT_VISION_S_PER_CALL", "601")
    out, body, _, _ = await proxy(ENERGY, FIELDS, _upstream(1))
    assert out.status_code == 504 and "keine Beschreibung gestartet" in body["error"]
    assert anth.calls == [] and coord == []


async def test_c10b_estimate_after_triage_exceeds_504(env, monkeypatch):
    """Triage (1 Aufruf) passt, 16 Beschreibungen (4 Runden) nicht."""
    from src.docconvert_worker_vision import describe_in_worker
    state, anth, coord, pool, _ = env
    pool(True)
    monkeypatch.setenv("BRIDGE_DOCCONVERT_VISION_S_PER_CALL", "100")
    status, body, usage = await describe_in_worker(_upstream(16), dict(FIELDS), "a.pdf",
                                                   deadline=time.monotonic() + 250)
    assert status == 504 and "Beschreibung nicht gestartet" in body["error"] and usage is None
    assert [p for p, _ in anth.messages] == ["/v1/messages"]  # nur die Triage


async def test_c10c_hard_deadline_cancels_504(env, monkeypatch):
    from src.docconvert_worker_vision import describe_in_worker
    state, anth, coord, pool, _ = env
    pool(True)
    monkeypatch.setenv("BRIDGE_DOCCONVERT_VISION_S_PER_CALL", "0.01")
    anth.delay = 5.0
    t0 = time.monotonic()
    status, body, _ = await describe_in_worker(_upstream(2), dict(FIELDS), "a.pdf",
                                               deadline=time.monotonic() + 0.3)
    assert status == 504 and "abgebrochen" in body["error"]
    assert time.monotonic() - t0 < 2.0
    assert not any(p == "/settle" for p, _ in coord)  # Reservierung bleibt (Obergrenze), nie still frei


# Aufrufer trennt → 499, Beschreibung abgebrochen
async def test_c11_client_disconnect_cancels(env, monkeypatch):
    from src.docconvert_worker_vision import describe_in_worker
    import src.docconvert_worker_vision as mod
    state, anth, coord, pool, _ = env
    pool(True)
    monkeypatch.setattr(mod, "_DISCONNECT_POLL_S", 0.05)
    anth.delay = 5.0
    req = MagicMock()
    req.is_disconnected = AsyncMock(side_effect=[False, True, True, True])
    t0 = time.monotonic()
    status, body, _ = await describe_in_worker(_upstream(2), dict(FIELDS), "a.pdf",
                                               deadline=time.monotonic() + 60, request=req)
    assert status == 499 and time.monotonic() - t0 < 2.0


# Provider-4xx für ein Bild → 1:1 (wie Bau B); Provider-5xx eines Bildes → Marker (wie B),
# alle 5xx → 502 statt „Dokument mit Beschreibungen“ ohne eine einzige
async def test_c12_provider_400_one_to_one(env):
    state, anth, coord, pool, proxy = env
    pool(True)
    anth.messages_status = 422
    out, body, _, _ = await proxy(ENERGY, FIELDS, _upstream(1))
    assert out.status_code == 422 and "markdown" not in body


async def test_c12b_provider_5xx_partial_marker_all_5xx_502(env):
    state, anth, coord, pool, proxy = env
    pool(True)
    anth.fail_names = {1}
    out, body, _, _ = await proxy(ENERGY, FIELDS, _upstream(2))
    assert out.status_code == 200 and body["vision_funding"] == "credit_pool"
    assert sum(MARKER in d for d in body["image_descriptions"].values()) == 1
    anth.calls.clear()
    anth.messages_status = 500
    out, body, _, _ = await proxy(ENERGY, FIELDS, _upstream(2))
    assert out.status_code == 502 and "keine der 2" in body["error"]


# Attestierung: Aufruf ohne Pool-Reservierung → 409 (nie still attestiert)
async def test_c13_unfunded_call_409(env, monkeypatch):
    from src.docconvert_worker_vision import describe_in_worker
    import src.privacy_service.image_describer as desc
    state, anth, coord, pool, _ = env
    pool(True)

    async def fake_describe(images, *, funding_log=None, **kw):
        funding_log.append({"kind": "describe", "model": "m", "credit_meta": None})
        return {n: "x" for n in images}
    monkeypatch.setattr(desc, "describe_images", fake_describe)
    status, body, _ = await describe_in_worker(_upstream(1), {}, "a.pdf", deadline=time.monotonic() + 60)
    assert status == 409 and body["error"] == NOT_FUNDED


# Import ohne Docling (Prüfpunkt 1): Modul + Abhängigkeiten laden kein docling
def test_c14_import_without_docling():
    import subprocess
    code = ("import sys; import src.docconvert_worker_vision, src.privacy_service.image_describer, "
            "src.privacy_service.image_triage; "
            "bad=[m for m in sys.modules if m.split('.')[0] in ('docling','docling_core','torch','pypdfium2')]; "
            "print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
