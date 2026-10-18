"""Bild-Ausgang (R5): die tragende Pruefung sitzt nach allen Pins, im Anbieter-Body — nicht am Eingang."""
from __future__ import annotations

import base64
import pathlib
import re

import pytest

from src.routing import bild_policy as bp

PNG_B64 = base64.b64encode(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")).decode()
SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
ENERGY = "werking-energy"

BILD_MSG = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG_B64}}]}]
TEXT_MSG = [{"role": "user", "content": "Analysiere (Base64): " + PNG_B64 * 2}]
ASSISTANT_BILD = [{"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG_B64}}]},
                  {"role": "user", "content": "weiter"}]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("BRIDGE_IMAGE_POLICY_APPS", raising=False)
    tok = bp.CURRENT_APP.set(None)
    yield
    bp.CURRENT_APP.reset(tok)


def egress(msgs, *, app=ENERGY, has_vision=True, gemini_backend=False, router_gemini=False):
    bp.enforce_image_egress(app_id=app, messages=msgs, has_vision=has_vision,
                            backend_is_gemini=gemini_backend, router_sends_to_gemini=router_gemini)


# --- zentrale Pruefung: Router und Sperre benutzen dieselbe Weiche ----------------------

def test_gemini_backend_oder_router_nach_gemini_geht_durch():
    egress(BILD_MSG, gemini_backend=True)
    egress(BILD_MSG, router_gemini=True)


def _cfg(backend, tier=None):
    from src.routing.backend_router import BackendConfig
    return BackendConfig(backend=backend, region=None, model_id="m", bedrock_model_id=None, privacy_enabled=False,
                         env_vars={}, provider_tier=tier)


@pytest.mark.parametrize("default", [None, "gemini"])
@pytest.mark.parametrize("name,backend,tier,erlaubt", [
    ("gemini_api", "GEMINI_API", "gemini-vision", True),
    ("anthropic_standard", "ANTHROPIC", None, None),   # entscheidet der Deployment-Standard
    ("bedrock_pin", "BEDROCK", None, False),
    ("anthropic_direct", "ANTHROPIC_DIRECT", "claude-direct-notools", False),
    ("openai_kompatibel", "OPENAI_COMPATIBLE", "some-tier", False),
])
def test_finale_provider_entscheidung_bestimmt_den_ausgang(monkeypatch, default, name, backend, tier, erlaubt):
    """Codex R4 #2 mit den ECHTEN Funktionen: Bedrock-Pin/Tier bleiben Anthropic, auch bei Gemini-Standard."""
    from src.models import BackendType
    if default:
        monkeypatch.setenv("BRIDGE_VISION_DEFAULT_PROVIDER", default)
    else:
        monkeypatch.delenv("BRIDGE_VISION_DEFAULT_PROVIDER", raising=False)
    gem, router = bp.image_route(_cfg(getattr(BackendType, backend), tier))
    if erlaubt is None:
        erlaubt = default == "gemini"
    if erlaubt:
        egress(BILD_MSG, gemini_backend=gem, router_gemini=router)
    else:
        with pytest.raises(bp.ImageEgressRefused) as e:
            egress(BILD_MSG, gemini_backend=gem, router_gemini=router)
        assert e.value.status == 403 and e.value.code == "image_requires_gemini"


def test_bild_das_die_weiche_nicht_routet_wird_abgewiesen():
    """assistant-/tool-Bilder: has_vision_content sieht sie nicht -> kein Gemini-Weg -> kein Ausgang."""
    with pytest.raises(bp.ImageEgressRefused) as e:
        egress(ASSISTANT_BILD, has_vision=False, router_gemini=True)
    assert e.value.status == 400 and e.value.code == "image_not_routable"


def test_base64_als_text_ist_kein_bildtransport():
    """Befund 3: im fertigen Anbieter-Payload ist das ein Textstring (Token, kein Pixel)."""
    egress(TEXT_MSG, has_vision=False)  # geht durch: kein Bild
    bp.CURRENT_APP.set(ENERGY)
    bp.guard_provider_payload("bedrock", {"messages": [{"role": "user", "content": [{"type": "text", "text": TEXT_MSG[0]["content"]}]}]})


def test_andere_apps_sind_unberuehrt():
    egress(BILD_MSG, app="werking-report")
    bp.guard_provider_payload("bedrock", {"messages": [{"content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}]}]})


# --- Letztpruefung am fertigen Anbieter-Body -------------------------------------------------

ANBIETER_BODIES = {
    "anthropic_image": {"messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}]}]},
    "bedrock_image": {"anthropic_version": "bedrock-2023-05-31", "messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}}]}]},
    "openai_image_url": {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG_B64}}]}]},
    "document_source_bild": {"messages": [{"role": "user", "content": [{"type": "document", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}]}]},
}


@pytest.mark.parametrize("name", sorted(ANBIETER_BODIES))
@pytest.mark.parametrize("provider", ["bedrock", "anthropic", "openai-compatible"])
def test_anbieter_body_mit_bild_wird_fuer_energy_nicht_gesendet(name, provider):
    bp.CURRENT_APP.set(ENERGY)
    with pytest.raises(bp.ImageEgressRefused):
        bp.guard_provider_payload(provider, ANBIETER_BODIES[name])


def test_anbieter_body_ohne_bild_geht_durch():
    bp.CURRENT_APP.set(ENERGY)
    bp.guard_provider_payload("bedrock", {"messages": [{"role": "user", "content": [{"type": "text", "text": "hallo"}]}]})


# --- Verdrahtung: die Pruefung steht wirklich an den Ausgaengen -----------------------------

def test_zentrale_pruefung_steht_vor_dem_ersten_backend_zweig():
    main = (SRC / "main.py").read_text(encoding="utf-8")
    i = main.index("enforce_image_egress(")
    assert i < main.index("# BEDROCK ROUTING: Direct boto3 call")
    assert i < main.index("# GEMINI CLI ROUTING")
    assert i < main.index("if backend_config and backend_config.backend == BackendType.ANTHROPIC_DIRECT:", i)


@pytest.mark.parametrize("datei,anzahl", [("bedrock_service.py", 2), ("vision_provider.py", 1), ("providers/openai_compatible.py", 2)])
def test_jeder_serialisierer_ruft_die_letztpruefung(datei, anzahl):
    text = (SRC / datei).read_text(encoding="utf-8")
    assert len(re.findall(r'guard_provider_payload\("', text)) == anzahl, datei
