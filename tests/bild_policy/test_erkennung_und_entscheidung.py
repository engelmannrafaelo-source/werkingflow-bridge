"""Bild-Policy: Erkennung (alle Bauarten) x Entscheidung (Pfad, Provider) — am Koerper, ohne Client."""
from __future__ import annotations

import base64
import json

import pytest

from src.routing import bild_policy as bp

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PNG_B64 = base64.b64encode(PNG).decode()
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 120
GIF = b"GIF89a" + b"\x00" * 120
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 120
DATA_URL = "data:image/png;base64," + PNG_B64
ENERGY = "werking-energy"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("BRIDGE_VISION_DEFAULT_PROVIDER", "BRIDGE_IMAGE_POLICY_APPS", "BRIDGE_IMAGE_POLICY_MAX_BODY"):
        monkeypatch.delenv(k, raising=False)


def chat(content, **extra):
    return json.dumps({"model": "sonnet", "messages": [{"role": "user", "content": content}], **extra}).encode()


def ev(body, path="/v1/chat/completions", app=ENERGY, ctype="application/json", method="POST"):
    return bp.evaluate(app_id=app, method=method, path=path, content_type=ctype, body=body)


# Jede Bauart, die ein Bild in einen JSON-Koerper bringt.
BAUARTEN = {
    "image_url_block": [{"type": "image_url", "image_url": {"url": DATA_URL}}],
    "image_url_remote": [{"type": "image_url", "image_url": {"url": "https://x.test/a.png"}}],
    "image_url_ohne_type": [{"image_url": {"url": "https://x.test/a.png"}}],
    "image_url_ohne_type_string": [{"image_url": "https://x.test/a.png"}],
    "source_ohne_type_opak": [{"source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}],
    "image_url_string": [{"type": "image_url", "image_url": DATA_URL}],
    "block_ohne_nutzlast": [{"type": "image"}],
    "anthropic_source": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64}}],
    "anthropic_source_url": [{"type": "image", "source": {"type": "url", "url": "https://x.test/a.png"}}],
    "input_image": [{"type": "input_image", "image_url": DATA_URL}],
    "gemini_inline_data": [{"inline_data": {"mime_type": "image/png", "data": "QUJD"}}],
    "gemini_inlineData": [{"inlineData": {"mimeType": "image/jpeg", "data": "QUJD"}}],
    "inline_data_ohne_mime": [{"inline_data": {"data": "QUJD"}}],
    "mime_neben_daten": [{"mime_type": "image/webp", "data": "QUJD"}],
    "data_url_im_text": ["Bitte ansehen: " + DATA_URL],
    "data_url_im_text_block": [{"type": "text", "text": "x " + DATA_URL}],
    "png_base64_im_text": ["Analysiere (Base64): " + PNG_B64 * 2],
    "jpeg_base64_im_text": ["JPEG: " + base64.b64encode(JPG).decode()],
    "gif_base64_im_text": ["GIF: " + base64.b64encode(GIF).decode()],
    "webp_base64_im_text": ["WEBP: " + base64.b64encode(WEBP).decode()],
    "base64_mit_zeilenumbruechen": ["\n".join(PNG_B64[i:i + 32] for i in range(0, len(PNG_B64), 32))],
    "base64_urlsicher": [base64.urlsafe_b64encode(PNG + b"\xfb\xff" * 20).decode()],
    "svg_data_url": ["data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10'/>"],
    "verschachteltes_tool_result": [{"type": "tool_result", "tool_use_id": "t", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64}}]}],
    "file_data": [{"type": "file", "file": {"file_data": DATA_URL}}],
}


@pytest.mark.parametrize("name", sorted(BAUARTEN))
def test_jede_bauart_wird_erkannt(name):
    assert bp.find_images_in_body(chat(BAUARTEN[name]), "application/json"), name


@pytest.mark.parametrize("name", sorted(BAUARTEN))
def test_jede_bauart_ohne_gemini_wird_abgewiesen(name):
    v = ev(chat(BAUARTEN[name]))
    assert v is not None and v.status == 403 and v.code == "image_requires_gemini"


@pytest.mark.parametrize("name", sorted(BAUARTEN))
def test_jede_bauart_mit_gemini_geht_durch(name):
    assert ev(chat(BAUARTEN[name], provider_tier="gemini-vision")) is None


@pytest.mark.parametrize("extra", [
    {"provider_tier": "claude-direct-notools"}, {"provider_tier": "bedrock"}, {"backend": "bedrock"},
    {"provider_tier": "gemini-vision", "backend": "bedrock"}, {"provider_tier": ""}, {},
])
def test_andere_provider_werden_abgewiesen(extra):
    v = ev(chat(BAUARTEN["image_url_block"], **extra))
    assert v is not None and v.code == "image_requires_gemini"


def test_deployment_standard_bindet_am_eingang_nicht(monkeypatch):
    """Codex R4: ein Deploymentdefault belegt weder Erkennung noch endgueltigen Anbieter."""
    monkeypatch.setenv("BRIDGE_VISION_DEFAULT_PROVIDER", "gemini")
    assert ev(chat(BAUARTEN["image_url_block"])) is not None
    assert ev(chat(BAUARTEN["image_url_block"], provider_tier="gemini-vision")) is None


# --- Text-Fehlalarme duerfen durch -------------------------------------------------

@pytest.mark.parametrize("text", [
    "Explain the data:image/ URL prefix and the data:image/png;base64, syntax",
    "Das Wort image, image_url und type image kommen in Prosa vor.",
    "Ein Hash " + "A" * 200,
    "Lade https://example.invalid/image.png und beschreibe es",
    "iVBORw0KGgo ist der Anfang eines PNG in Base64",
    "Normaler Text. " * 500,
])
def test_text_ohne_bildnutzlast_geht_durch(text):
    assert ev(chat(text)) is None


def test_json_schema_mit_image_url_eigenschaft_ist_kein_bild():
    body = chat("Gib JSON zurueck", response_format={"type": "json_schema", "json_schema": {"schema": {
        "type": "object", "properties": {"image_url": {"type": "string"}, "image": {"type": "string"}}}}})
    assert ev(body) is None


def test_pdf_mime_in_inline_data_ist_kein_bild():
    assert ev(chat([{"inline_data": {"mime_type": "application/pdf", "data": "QUJD"}}])) is None


# --- Pfade und Jobs ----------------------------------------------------------------

@pytest.mark.parametrize("path", ["/v1/research", "/v1/doc-agent", "/v1/convert-html-to-pdf", "/v1/document/convert",
                                  "/v1/privacy/smart-anonymize", "/v1/messages", "/v1/anything"])
def test_andere_pfade_mit_bild_werden_abgewiesen_auch_mit_gemini(path):
    v = ev(chat(BAUARTEN["image_url_block"], provider_tier="gemini-vision"), path=path)
    assert v is not None and v.code == "image_requires_gemini"


def test_jobs_chat_mit_gemini_geht_durch_ohne_nicht():
    pay = json.loads(chat(BAUARTEN["image_url_block"], provider_tier="gemini-vision"))
    assert ev(json.dumps({"kind": "chat", "payload": pay}).encode(), path="/v1/jobs") is None
    pay.pop("provider_tier")
    assert ev(json.dumps({"kind": "chat", "payload": pay}).encode(), path="/v1/jobs") is not None
    pay["provider_tier"] = "gemini-vision"
    assert ev(json.dumps({"kind": "research", "payload": pay}).encode(), path="/v1/jobs") is not None


def test_pfad_mit_schraegstrich_am_ende_zaehlt_wie_ohne():
    assert ev(chat(BAUARTEN["image_url_block"], provider_tier="gemini-vision"), path="/v1/chat/completions/") is None


# --- rohe Koerper ------------------------------------------------------------------

@pytest.mark.parametrize("raw,ctype", [
    (PNG, "image/png"), (PNG, "application/octet-stream"), (JPG, "application/octet-stream"),
    (GIF, "application/octet-stream"), (WEBP, "application/octet-stream"), (WEBP, "text/plain"),
])
def test_roh_bilder_werden_abgewiesen(raw, ctype):
    assert ev(raw, path="/v1/research", ctype=ctype) is not None


def _multipart(parts):
    b = "BOUNDARY123"
    out = b""
    for headers, payload in parts:
        out += f"--{b}\r\n".encode() + "\r\n".join(headers).encode() + b"\r\n\r\n" + payload + b"\r\n"
    return out + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


@pytest.mark.parametrize("headers,payload", [
    (['Content-Disposition: form-data; name="f"; filename="a.png"', "Content-Type: image/png"], PNG),
    (['Content-Disposition: form-data; name="f"; filename="a.bin"', "Content-Type: application/octet-stream"], PNG),
    (['Content-Disposition: form-data; name="f"; filename="scan.JPG"', "Content-Type: application/octet-stream"], b"x" * 40),
    (['Content-Disposition: form-data; name="f"'], WEBP),
    (['Content-Disposition: form-data; name="m"', "Content-Type: application/json"], chat(BAUARTEN["image_url_block"])),
    (['Content-Disposition: form-data; name="t"'], DATA_URL.encode()),
])
def test_multipart_mit_bild_wird_abgewiesen(headers, payload):
    body, ctype = _multipart([(['Content-Disposition: form-data; name="x"'], b"hallo"), (headers, payload)])
    assert ev(body, path="/v1/document/convert", ctype=ctype) is not None


def test_multipart_pdf_geht_durch():
    body, ctype = _multipart([(['Content-Disposition: form-data; name="f"; filename="a.pdf"', "Content-Type: application/pdf"], b"%PDF-1.7 " + b"\x00" * 50)])
    assert ev(body, path="/v1/convert-pdf", ctype=ctype) is None


def test_multipart_ohne_erkennbare_teile_ist_nicht_pruefbar():
    v = ev(b"das ist kein multipart-koerper", ctype="multipart/form-data; boundary=missing")
    assert v is not None and v.code == "image_policy_unverifiable"


@pytest.mark.parametrize("ctype", ["application/pdf", "audio/wav", "video/mp4"])
def test_behaupteter_pdf_audio_video_typ_ist_kein_freibrief(ctype):
    assert ev(chat(BAUARTEN["image_url_block"]), ctype=ctype) is not None
    body, _ = _multipart([(['Content-Disposition: form-data; name="m"', f"Content-Type: {ctype}"], chat(BAUARTEN["image_url_block"]))])
    assert ev(body, path="/v1/document/convert", ctype="multipart/form-data; boundary=BOUNDARY123") is not None


def test_multipart_ohne_boundary_ist_nicht_pruefbar():
    v = ev(b"abc", ctype="multipart/form-data")
    assert v is not None and v.code == "image_policy_unverifiable"


# --- Geltungsbereich ----------------------------------------------------------------

def test_erlaubte_apps_und_lesende_methoden_sind_unberuehrt():
    body = chat(BAUARTEN["image_url_block"])
    for app in ("werking-report", "engelmann", "cui", "platform", "werking-noise", "werking-safety", "acro-community"):
        assert ev(body, app=app) is None, app
    assert ev(body, method="GET") is None


@pytest.mark.parametrize("app", [None, "", "unbekannt", "werking-energy", "Werking-Energy", "werking-report ", "anonymous:kein-header",
                                 "werking-report2", "werking", "energy"])
def test_fehlende_oder_unbekannte_identitaet_gilt_wie_energy(app):
    """R6 Umkehr: nur ausdruecklich erlaubte Apps duerfen Bilder an Nicht-Gemini schicken."""
    v = ev(chat(BAUARTEN["image_url_block"]), app=app)
    assert v is not None and v.code == "image_requires_gemini"
    assert ev(chat(BAUARTEN["image_url_block"], provider_tier="gemini-vision"), app=app) is None


def test_positivliste_ist_ueberschreibbar_energy_nie(monkeypatch):
    monkeypatch.setenv("BRIDGE_IMAGE_ANTHROPIC_ALLOWED_APPS", "meine-neue-app,werking-energy")
    assert ev(chat(BAUARTEN["image_url_block"]), app="meine-neue-app") is None
    assert ev(chat(BAUARTEN["image_url_block"]), app="werking-report") is not None
    assert ev(chat(BAUARTEN["image_url_block"]), app="werking-energy") is not None
    assert "werking-energy" not in bp.allowed_apps()


def test_positivliste_entspricht_known_app_ids_ohne_energy():
    import ast
    import pathlib
    src = (pathlib.Path(__file__).parents[2] / "src/main.py").read_text()
    node = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "") == "KNOWN_APP_IDS")
    known = {e.value for e in node.value.elts}
    assert bp.IMAGE_ANTHROPIC_ALLOWED_APPS == (known - {"werking-energy"}) | {"partner-platform", "dev-tooling"}
    assert bp.IMAGE_ANTHROPIC_ALLOWED_EXTRA_APPS == {"partner-platform", "dev-tooling"}


def test_zu_grosser_koerper_wird_nicht_still_durchgelassen(monkeypatch):
    monkeypatch.setenv("BRIDGE_IMAGE_POLICY_MAX_BODY", "100")
    v = ev(chat("a" * 500))
    assert v is not None and v.status == 413


def test_uebertiefe_verschachtelung_ist_eine_absage_kein_absturz():
    deep: object = "x"
    for _ in range(200):
        deep = [deep]
    v = ev(json.dumps({"messages": deep}).encode())
    assert v is not None and v.code == "image_policy_unverifiable"


def test_defekter_json_koerper_wird_als_text_gescannt():
    assert ev(b'{"messages": [ ' + DATA_URL.encode()) is not None
    assert ev(b'{"messages": [ "hallo"') is None
