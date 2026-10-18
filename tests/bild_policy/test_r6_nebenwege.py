"""R6: Laufzeitproben und Inventar-Lint.

Das Inventar findet bekannte Schreibweisen, keine vollstaendige Ausgangsabdeckung.
Dynamische Imports/URLs und weitere Calls in bekannten Modulen koennen unentdeckt
bleiben. Die tragende Sperre ist der Laufzeit-Ausgang, nicht dieser Lint.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import pathlib
import re

import pytest

from src.routing import bild_policy as bp

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
PNG_B64 = base64.b64encode(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")).decode()
ANTHROPIC_IMG = {"model": "m", "messages": [{"role": "user", "content": [
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG_B64}}]}]}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("BRIDGE_IMAGE_ANTHROPIC_ALLOWED_APPS", raising=False)
    tok = bp.CURRENT_APP.set(None)
    yield
    bp.CURRENT_APP.reset(tok)


# --- 1 Umkehr: ohne Identitaet = eingeschraenkt (Codex-Probe: Privacy-Dienst, App=null) -------------

@pytest.mark.parametrize("ctx", [None, "", "unbekannt", "werking-energy"])
def test_ohne_oder_mit_falscher_identitaet_sperrt_der_provider_guard(ctx):
    bp.CURRENT_APP.set(ctx)
    with pytest.raises(bp.ImageEgressRefused):
        bp.guard_provider_payload("anthropic", ANTHROPIC_IMG)


def test_nur_erlaubte_apps_duerfen_an_anthropic():
    for app in sorted(bp.IMAGE_ANTHROPIC_ALLOWED_APPS):
        bp.CURRENT_APP.set(app)
        bp.guard_provider_payload("anthropic", ANTHROPIC_IMG)


def test_privacy_dienst_uebernimmt_die_verifizierte_app_aus_dem_header(monkeypatch):
    """Codex-Gegenprobe am ECHTEN Privacy-FastAPI-App-Objekt: Header setzt den Kontext, fehlender sperrt."""
    from fastapi.testclient import TestClient

    import src.privacy_service.app as papp
    seen: list[str | None] = []

    @papp.app.post("/__probe")
    async def probe():
        seen.append(bp.CURRENT_APP.get())
        try:
            bp.guard_provider_payload("anthropic", ANTHROPIC_IMG)
            return {"gesperrt": False}
        except bp.ImageEgressRefused:
            return {"gesperrt": True}

    c = TestClient(papp.app)
    assert c.post("/__probe", headers={"X-Bridge-Verified-App": "werking-report"}).json() == {"gesperrt": False}
    assert c.post("/__probe", headers={"X-Bridge-Verified-App": "werking-energy"}).json() == {"gesperrt": True}
    assert c.post("/__probe").json() == {"gesperrt": True}
    assert seen == ["werking-report", "werking-energy", ""]


def test_describe_images_wird_fuer_eingeschraenkte_apps_laut_abgewiesen():
    for app in (None, "", "werking-energy", "x"):
        for val in ("true", "1", "True", "on"):
            v = bp.describe_images_refusal(app, val)
            assert v is not None and v.status == 403 and v.code == "image_requires_gemini"
        assert bp.describe_images_refusal(app, "false") is None
        assert bp.describe_images_refusal(app, None) is None
    assert bp.describe_images_refusal("werking-report", "true") is None


def test_proxy_reicht_die_verifizierte_app_an_den_privacy_dienst():
    main = (SRC / "main.py").read_text(encoding="utf-8")
    i = main.index("async def _proxy_document_endpoint")
    body = main[i:i + 6000]
    assert 'describe_images_refusal(' in body and '"X-Bridge-Verified-App"' in body
    assert body.index("describe_images_refusal(") < body.index("privacy_client.post(")


# --- 3 OpenAI-Guard: Pydantic-ContentPart ------------------------------------------------------

def test_pydantic_content_part_wird_im_anbieter_body_erkannt():
    from src.models import ChatCompletionRequest
    req = ChatCompletionRequest(model="sonnet", messages=[{"role": "user", "content": [
        {"type": "text", "text": "x"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG_B64}}]}])
    teile = req.messages[0].content
    assert not isinstance(teile, (str, dict)) and not isinstance(teile[1], dict), "Test braucht echte Objekte"
    bp.CURRENT_APP.set("")
    with pytest.raises(bp.ImageEgressRefused):
        bp.guard_provider_payload("openai-compatible", {"model": "m", "messages": [{"role": "user", "content": teile}]})
    bp.CURRENT_APP.set("werking-report")
    bp.guard_provider_payload("openai-compatible", {"model": "m", "messages": [{"role": "user", "content": teile}]})


def test_openai_adapter_serialisiert_vor_dem_guard():
    text = (SRC / "providers/openai_compatible.py").read_text(encoding="utf-8")
    assert text.count('guard_provider_payload("openai-compatible"') == 2


# --- Agenten-/Textwege: Prompt mit Bildnutzlast --------------------------------------------------

@pytest.mark.parametrize("provider,prompt", [
    ("claude-cli", "Analysiere dieses Bild: data:image/png;base64," + PNG_B64),
    ("erkunder", "Bericht\n" + PNG_B64 * 2),
    ("perplexity", "Suche zu " + PNG_B64 * 2),
])
def test_agentenwege_sperren_bildnutzlast_im_prompt(provider, prompt):
    bp.CURRENT_APP.set("werking-energy")
    with pytest.raises(bp.ImageEgressRefused):
        bp.guard_prompt_text(provider, prompt)
    bp.CURRENT_APP.set("werking-report")
    bp.guard_prompt_text(provider, prompt)
    bp.CURRENT_APP.set("werking-energy")
    bp.guard_prompt_text(provider, "Normaler Prompt ueber data:image/ und image_url in Prosa.")


def test_run_completion_und_erkunder_rufen_den_text_guard():
    assert 'guard_prompt_text("claude-cli", [prompt, system_prompt, append_system_prompt])' in (SRC / "claude_cli.py").read_text(encoding="utf-8")
    assert 'guard_prompt_text("erkunder"' in (SRC / "erkunder/kind.py").read_text(encoding="utf-8")


# --- 2 Inventar-Lint: bekannte Anbieter-Schreibweisen, keine Vollstaendigkeitsgarantie ----------------------------------

#: Module, die einen Modellanbieter erreichen, und wie sie den Ausgang durchlaufen.
#: GUARD  = ruft guard_provider_payload / guard_prompt_text im selben Modul.
#: CENTRAL = laeuft durch enforce_image_egress in chat_completions (Chat-Dispatch).
#: KEIN_BILD = Anbieter ist Gemini selbst, Abrechnung/Metadaten/Registry oder reiner Text ohne Bildweg (Grund).
AUSGANG = {
    "bedrock_service.py": "GUARD",
    "vision_provider.py": "GUARD",
    "providers/openai_compatible.py": "GUARD",
    "research_cloud/executor.py": "GUARD",
    "research_cloud/luecken.py": "GUARD",
    "research_cloud/perplexity.py": "GUARD",
    "claude_cli.py": "GUARD",
    "erkunder/kind.py": "GUARD",
    "providers/gemini_vision.py": "KEIN_BILD: Gemini ist das erlaubte Ziel",
    "gemini_cli.py": "KEIN_BILD: Gemini ist das erlaubte Ziel",
    "providers/anthropic_direct.py": "CENTRAL: ruft VisionProvider (GUARD) auf",
    "providers/fallback.py": "KEIN_BILD: waehlt nur den Adapter (openai_compatible/anthropic_direct, beide GUARD)",
    "providers/gemini_oauth.py": "KEIN_BILD: Gemini-OAuth, Anbieter ist Gemini",
    "main.py": "CENTRAL: enforce_image_egress in chat_completions; Agenten ueber claude_cli (GUARD)",
    "research_pool_perplexity.py": "KEIN_BILD: Werkzeug ruft ask_perplexity (GUARD)",
    "privacy_service/app.py": "CENTRAL: Header -> CURRENT_APP, Vision ueber VisionProvider (GUARD)",
    "privacy_service/image_describer.py": "CENTRAL: VisionProvider (GUARD)",
    "privacy_service/image_triage.py": "CENTRAL: VisionProvider (GUARD)",
}
#: Fundstellen im Baum, die keinen Modellanbieter erreichen (Konfiguration, Abrechnung, eigene Dienste).
KEIN_MODELLZUGANG = {
    "federation.py", "hetzner_service.py", "metrics_reader/main.py", "middleware/bridge_error.py", "model_registry.py",
    "platform_client.py", "privacy_client.py", "providers/registry.py", "reconciliation/bedrock_reconciliation.py",
    "research_cloud/library.py", "routing/backend_router.py", "sandbox/account_router.py", "sdk_parser.py",
    "tenant/budget_checker.py", "tenant/client.py", "routing/bild_policy.py", "middleware/bild_policy_middleware.py",
}
_MARKER = re.compile(
    r"api\.anthropic\.com|ANTHROPIC_API_URL|api\.perplexity|PERPLEXITY_API_URL|generativelanguage|api\.openai|"
    r"invoke_model|from claude_code_sdk import|import claude_code_sdk|import anthropic|from anthropic|"
    r"import openai|from openai|rstrip\('/'\)\}/chat/completions|boto3|google\.genai|google\.generativeai|\.messages\.create\(|chat\.completions\.create\("
)


def _module_mit_anbieterzugriff() -> set[str]:
    gefunden = set()
    for p in SRC.rglob("*.py"):
        rel = str(p.relative_to(SRC))
        if "__pycache__" in rel:
            continue
        text = p.read_text(encoding="utf-8")
        ast.parse(text)
        if _MARKER.search(text):
            gefunden.add(rel)
    return gefunden


def test_inventar_lint_erkennt_bekannte_anbietermarker():
    unbekannt = sorted(_module_mit_anbieterzugriff() - set(AUSGANG) - KEIN_MODELLZUGANG)
    assert not unbekannt, (
        f"Neues Modul mit Anbieterzugriff: {unbekannt}. Es muss durch den gemeinsamen Ausgang laufen "
        "(guard_provider_payload / guard_prompt_text aus src.routing.bild_policy) und hier in AUSGANG eingetragen "
        "werden — oder mit Begruendung in KEIN_MODELLZUGANG."
    )


def test_inventar_hat_keine_toten_eintraege():
    gefunden = _module_mit_anbieterzugriff()
    tot = sorted(m for m in (set(AUSGANG) | KEIN_MODELLZUGANG) if not (SRC / m).exists())
    assert tot == [], tot
    ohne_marker = sorted(m for m in AUSGANG if m not in gefunden and AUSGANG[m] == "GUARD")
    assert ohne_marker == [], ohne_marker


def test_inventar_lint_findet_guard_aufruf_im_modul():
    fehlt = []
    for m, art in AUSGANG.items():
        if art != "GUARD":
            continue
        text = (SRC / m).read_text(encoding="utf-8")
        if not re.search(r"guard_(provider_payload|prompt_text)\(", text):
            fehlt.append(m)
    assert fehlt == [], fehlt


def test_zentraler_ausgang_steht_in_chat_completions():
    main = (SRC / "main.py").read_text(encoding="utf-8")
    assert "enforce_image_egress(" in main and "image_route(" in main


def test_kontext_des_eingangs_gewinnt_vor_dem_selbst_erklaerten_app_id():
    """Principal sagt Energy, Kopfzeile sagt Report: Energy gilt (Eingang setzt CURRENT_APP)."""
    bp.CURRENT_APP.set("werking-energy")
    assert bp.app_is_gemini_only("werking-report") is True
    bp.CURRENT_APP.set("werking-report")
    assert bp.app_is_gemini_only("werking-energy") is False
    bp.CURRENT_APP.set(None)  # kein Request-Kontext: die uebergebene App gilt
    assert bp.app_is_gemini_only("werking-report") is False and bp.app_is_gemini_only(None) is True


def test_eingang_ohne_identitaet_setzt_kontext_eingeschraenkt_und_sperrt_am_provider():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.middleware.bild_policy_middleware import ImagePolicyMiddleware

    api = FastAPI()
    seen = []

    @api.post("/v1/anything")
    async def anything():
        seen.append(bp.CURRENT_APP.get())
        try:
            bp.guard_provider_payload("anthropic", ANTHROPIC_IMG)
            return {"gesperrt": False}
        except bp.ImageEgressRefused:
            return {"gesperrt": True}

    api.add_middleware(ImagePolicyMiddleware)
    c = TestClient(api)
    harmlos = {"messages": [{"role": "user", "content": "hallo"}]}
    assert c.post("/v1/anything", json=harmlos).json() == {"gesperrt": True}       # keine Identitaet
    assert c.post("/v1/anything", json=harmlos, headers={"X-App-ID": "werking-report"}).json() == {"gesperrt": False}
    assert seen == ["", "werking-report"]
    # Bild ohne Identitaet und ohne Gemini: schon am Eingang abgewiesen
    assert c.post("/v1/anything", json=ANTHROPIC_IMG).status_code == 403
    assert c.post("/v1/anything", json=ANTHROPIC_IMG, headers={"X-App-ID": "werking-report"}).status_code == 200
