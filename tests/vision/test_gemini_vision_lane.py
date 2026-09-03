"""Gemini-Bildweg — Sperre, Abrechnung, Konvertierung.

Der Weg schickt Bilder an einen Anbieter, der in avv.md §5.4 NICHT als
Unterauftragsverarbeiter gelistet ist. Die Sperre davor ist deshalb kein
Komfort-Feature, sondern die Bedingung, unter der es ihn ueberhaupt geben darf —
und sie gehoert entsprechend festgenagelt, nicht bloss dokumentiert.
"""

import json

import pytest

from src.providers import gemini_vision as gv
from src.routing import gemini_vision_gate as gate


# ── Preis-Invariante ────────────────────────────────────────────────────────
# Ein Modell ohne Preiszeile buchte 0,00 EUR fuer echtes Geld — genau die
# Silent-Gratis-Klasse, die die Preis-SSoT verbietet.

def test_every_allowed_model_is_priced():
    from src.pricing import price_entry

    unpriced = [m for m in gv.GEMINI_VISION_MODELS if price_entry(m) is None]
    assert not unpriced, (
        f"Gemini-Bildmodelle ohne Preis in src/pricing.py: {unpriced}. "
        "Sie wuerden 0,00 EUR ins Ledger schreiben."
    )


def test_default_model_is_the_cheapest_of_the_family():
    """Regressionspin gegen den Reflex 'nimm die neueste Version'.

    Bei Flash-Lite ist neuer NICHT guenstiger (2.5: 0,10/0,40 · 3.1: 0,25/1,50 ·
    3.5: 0,30/2,50, geprueft 2026-09-03). Wer den Default auf eine neuere
    Variante hebt, soll das bewusst tun und diesen Test mit anfassen.
    """
    from src.pricing import price_entry

    def total(model):
        p = price_entry(model)
        return p["in"] + p["out"]

    cheapest = min(gv.GEMINI_VISION_MODELS, key=total)
    assert gv.DEFAULT_GEMINI_VISION_MODEL == cheapest


def test_unknown_model_env_fails_loud(monkeypatch):
    monkeypatch.setenv("GEMINI_VISION_MODEL", "gemini-3.5-pro")
    with pytest.raises(gv.GeminiVisionError):
        gv.resolve_gemini_vision_model()


# ── Die Sperre ──────────────────────────────────────────────────────────────

@pytest.fixture
def armed(monkeypatch):
    """Flag + Key gesetzt — der Zustand, in dem NUR noch Umgebung und
    Erklaerung ueber den Zugang entscheiden."""
    monkeypatch.setenv("BRIDGE_GEMINI_VISION_ENABLED", "true")
    monkeypatch.setenv("GEMINI_VISION_API_KEY", "test-key")


def _allow(**over):
    kwargs = {"app_env": "staging", "has_images": True, "declares_synthetic": True}
    kwargs.update(over)
    return gate.assert_gemini_vision_allowed(**kwargs)


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BRIDGE_GEMINI_VISION_ENABLED", raising=False)
    monkeypatch.setenv("GEMINI_VISION_API_KEY", "test-key")
    with pytest.raises(gate.GeminiVisionRefused):
        _allow()


def test_missing_key_refuses(monkeypatch):
    monkeypatch.setenv("BRIDGE_GEMINI_VISION_ENABLED", "true")
    monkeypatch.delenv("GEMINI_VISION_API_KEY", raising=False)
    with pytest.raises(gate.GeminiVisionRefused):
        _allow()


def test_empty_key_counts_as_missing(monkeypatch):
    """``GEMINI_VISION_API_KEY=${X:-}`` erzeugt eine leere, aber existierende
    Variable — derselbe Fehlmodus, der 2026-08-26 den Anthropic-Bildweg erst
    NACH der Docling-Arbeit sterben liess."""
    monkeypatch.setenv("BRIDGE_GEMINI_VISION_ENABLED", "true")
    monkeypatch.setenv("GEMINI_VISION_API_KEY", "   ")
    with pytest.raises(gate.GeminiVisionRefused):
        _allow()


def test_production_is_refused(armed):
    with pytest.raises(gate.GeminiVisionRefused) as exc:
        _allow(app_env="prod")
    assert "prod" in str(exc.value)


def test_unknown_environment_is_refused(armed):
    """Die tragende Entscheidung: ``app_env=None`` (kein X-App-Env) wird
    ABGEWIESEN, nicht durchgelassen. Eine Umgebung, die sich nicht ausweist,
    koennte Produktion sein — und dort duerfen keine Kundenplaene an Google."""
    with pytest.raises(gate.GeminiVisionRefused):
        _allow(app_env=None)


def test_missing_synthetic_declaration_is_refused(armed):
    with pytest.raises(gate.GeminiVisionRefused):
        _allow(declares_synthetic=False)


def test_without_image_is_refused(armed):
    with pytest.raises(gate.GeminiVisionRefused):
        _allow(has_images=False)


@pytest.mark.parametrize("env", ["staging", "local"])
def test_allowed_when_all_layers_hold(armed, env):
    _allow(app_env=env)  # wirft nicht


def test_anthropic_only_params_are_refused_not_dropped():
    """Stilles Weglassen waere der teuerste Silent-Fail dieses Wegs: 200
    zurueck, aber unter anderen Denk-Einstellungen erzeugt als angefordert —
    und genau die sind der Kostenposten, den die Messung erfassen soll."""
    with pytest.raises(gate.GeminiVisionRefused):
        gate.assert_no_anthropic_only_params(thinking={"type": "enabled"})
    with pytest.raises(gate.GeminiVisionRefused):
        gate.assert_no_anthropic_only_params(output_config={"effort": "low"})
    gate.assert_no_anthropic_only_params()  # nichts gesetzt -> still


class _Headers(dict):
    pass


class _Req:
    def __init__(self, headers):
        self.headers = _Headers(headers)


def test_declaration_is_read_from_the_header():
    assert gate.declares_synthetic_test_mode(_Req({"X-Vision-Test-Mode": "synthetic"}))
    assert not gate.declares_synthetic_test_mode(_Req({"X-Vision-Test-Mode": "echt"}))
    assert not gate.declares_synthetic_test_mode(_Req({}))
    # Fehlender HTTP-Kontext ist KEINE Erklaerung.
    assert not gate.declares_synthetic_test_mode(None)


# ── Weiche ──────────────────────────────────────────────────────────────────

def test_target_defaults_to_anthropic_for_existing_callers():
    """Regressionspin: ohne Gemini-Tier aendert dieser Bau nichts."""
    from src.models import BackendType
    from src.routing.vision_router import (
        VISION_TARGET_ANTHROPIC, VISION_TARGET_GEMINI, resolve_vision_target,
    )

    class _Cfg:
        def __init__(self, backend):
            self.backend = backend

    assert resolve_vision_target(None) == VISION_TARGET_ANTHROPIC
    assert resolve_vision_target(_Cfg(BackendType.ANTHROPIC)) == VISION_TARGET_ANTHROPIC
    assert resolve_vision_target(_Cfg(BackendType.BEDROCK)) == VISION_TARGET_ANTHROPIC
    assert resolve_vision_target(_Cfg(BackendType.GEMINI_API)) == VISION_TARGET_GEMINI


def test_lanes_are_separate():
    """Die Anthropic-Tageskappe summiert 'vision_prepaid'. Traegt der
    Gemini-Weg dieselbe Fahrspur, zaehlt er gegen ein Guthaben, das er gar
    nicht belastet — die Kappe waere ab dem ersten Testlauf falsch."""
    from src.routing.vision_router import LANE_ANTHROPIC_PREPAID, LANE_GEMINI_TEST

    assert LANE_ANTHROPIC_PREPAID != LANE_GEMINI_TEST


# ── Provider ────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeClient:
    """Faengt genau einen POST ab und legt den Request-Body offen."""

    captured = {}

    def __init__(self, payload=None, status_code=200, **_kw):
        self._payload = payload if payload is not None else _OK_PAYLOAD
        self._status = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        type(self).captured = {"url": url, "headers": headers, "body": json}
        return _FakeResponse(self._payload, self._status)


_OK_PAYLOAD = {
    "candidates": [{
        "content": {"parts": [{"text": "Ein Grundriss."}]},
        "finishReason": "STOP",
    }],
    "usageMetadata": {
        "promptTokenCount": 1200,
        "candidatesTokenCount": 300,
        "thoughtsTokenCount": 900,
    },
    "modelVersion": "gemini-2.5-flash-lite",
}

_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _image_messages():
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": "Beschreibe den Plan."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_PNG}"}},
        ],
    }]


@pytest.mark.asyncio
async def test_missing_key_never_calls_upstream(monkeypatch):
    monkeypatch.delenv("GEMINI_VISION_API_KEY", raising=False)

    def _boom(*a, **k):
        raise AssertionError("Ohne Key darf kein Upstream-Call passieren")

    monkeypatch.setattr(gv.httpx, "AsyncClient", _boom)
    with pytest.raises(gv.GeminiVisionError):
        await gv.GeminiVisionProvider().analyze(messages=_image_messages())


@pytest.mark.asyncio
async def test_image_is_sent_as_inline_data(monkeypatch):
    monkeypatch.setenv("GEMINI_VISION_API_KEY", "test-key")
    monkeypatch.setattr(gv.httpx, "AsyncClient", _FakeClient)

    await gv.GeminiVisionProvider().analyze(messages=_image_messages())

    body = _FakeClient.captured["body"]
    parts = body["contents"][0]["parts"]
    inline = [p for p in parts if "inline_data" in p]
    assert len(inline) == 1
    assert inline[0]["inline_data"]["mime_type"] == "image/png"
    assert inline[0]["inline_data"]["data"] == _PNG
    assert any("text" in p for p in parts)
    # Key gehoert in den Header, nicht in die URL (Query-Strings landen in Logs).
    assert _FakeClient.captured["headers"]["x-goog-api-key"] == "test-key"
    assert "key=" not in _FakeClient.captured["url"]


@pytest.mark.asyncio
async def test_no_output_cap_is_invented(monkeypatch):
    """Rafael, 2026-09-03: kein Ausgabendeckel. Ohne ausdruecklichen Wunsch des
    Aufrufers darf die Bridge kein maxOutputTokens setzen."""
    monkeypatch.setenv("GEMINI_VISION_API_KEY", "test-key")
    monkeypatch.setattr(gv.httpx, "AsyncClient", _FakeClient)

    await gv.GeminiVisionProvider().analyze(messages=_image_messages())
    assert "maxOutputTokens" not in _FakeClient.captured["body"].get("generationConfig", {})

    await gv.GeminiVisionProvider().analyze(messages=_image_messages(), max_tokens=1234)
    assert _FakeClient.captured["body"]["generationConfig"]["maxOutputTokens"] == 1234


@pytest.mark.asyncio
async def test_thinking_tokens_count_as_output(monkeypatch):
    """Gemini weist Denk-Tokens getrennt aus, verrechnet sie aber zum
    Output-Preis. Wer nur candidatesTokenCount bucht, unterschaetzt genau den
    Posten, der bei der Bildanalyse dominiert — und liesse Gemini in der
    Messung guenstiger aussehen, als es ist."""
    monkeypatch.setenv("GEMINI_VISION_API_KEY", "test-key")
    monkeypatch.setattr(gv.httpx, "AsyncClient", _FakeClient)

    res = await gv.GeminiVisionProvider().analyze(messages=_image_messages())
    assert res.usage["prompt_tokens"] == 1200
    assert res.usage["completion_tokens"] == 300 + 900
    assert res.usage["total_tokens"] == 1200 + 1200


@pytest.mark.asyncio
async def test_finish_reason_is_translated(monkeypatch):
    """MAX_TOKENS muss als abgeschnitten erkennbar bleiben — sonst ist eine
    gekappte Antwort vom Erfolg nicht zu unterscheiden (der Befund vom
    31.08. auf dem Anthropic-Weg)."""
    monkeypatch.setenv("GEMINI_VISION_API_KEY", "test-key")
    payload = dict(_OK_PAYLOAD)
    payload["candidates"] = [{
        "content": {"parts": [{"text": "abgeschnitten"}]},
        "finishReason": "MAX_TOKENS",
    }]
    monkeypatch.setattr(
        gv.httpx, "AsyncClient", lambda **kw: _FakeClient(payload=payload)
    )

    res = await gv.GeminiVisionProvider().analyze(messages=_image_messages())
    assert res.stop_reason == "max_tokens"

    from src.vision_provider import finish_reason_for
    assert finish_reason_for(res.stop_reason) == "length"


@pytest.mark.asyncio
async def test_blocked_prompt_is_a_clear_error_not_an_indexerror(monkeypatch):
    monkeypatch.setenv("GEMINI_VISION_API_KEY", "test-key")
    payload = {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
    monkeypatch.setattr(
        gv.httpx, "AsyncClient", lambda **kw: _FakeClient(payload=payload)
    )

    with pytest.raises(gv.GeminiVisionError) as exc:
        await gv.GeminiVisionProvider().analyze(messages=_image_messages())
    assert "SAFETY" in str(exc.value)


@pytest.mark.asyncio
async def test_thought_parts_are_not_returned_as_content(monkeypatch):
    monkeypatch.setenv("GEMINI_VISION_API_KEY", "test-key")
    payload = dict(_OK_PAYLOAD)
    payload["candidates"] = [{
        "content": {"parts": [
            {"text": "internes Denken", "thought": True},
            {"text": "Die Antwort."},
        ]},
        "finishReason": "STOP",
    }]
    monkeypatch.setattr(
        gv.httpx, "AsyncClient", lambda **kw: _FakeClient(payload=payload)
    )

    res = await gv.GeminiVisionProvider().analyze(messages=_image_messages())
    assert res.content == "Die Antwort."


@pytest.mark.asyncio
async def test_route_to_vision_labels_the_gemini_lane(monkeypatch):
    """Die Naht: das Ledger muss Google als Empfaenger und die eigene Fahrspur
    sehen — nicht das Etikett des Anthropic-Wegs."""
    monkeypatch.setenv("GEMINI_VISION_API_KEY", "test-key")
    monkeypatch.setattr(gv.httpx, "AsyncClient", _FakeClient)

    from src.routing.vision_router import (
        LANE_GEMINI_TEST, VISION_TARGET_GEMINI, route_to_vision,
    )
    from src.activity.providers import PROVIDER_GEMINI

    res = await route_to_vision(
        messages=_image_messages(),
        model="claude-sonnet-5",
        target=VISION_TARGET_GEMINI,
    )
    assert res.ledger_provider == PROVIDER_GEMINI
    assert res.api_key_lane == LANE_GEMINI_TEST
    assert res.model in gv.GEMINI_VISION_MODELS


# ── Regressionspin fuer die Wiederverwendung ────────────────────────────────

def test_anthropic_converter_still_works_as_a_static_helper():
    """Der Gemini-Weg nutzt VisionProvider._convert_to_anthropic_messages ohne
    Instanz weiter (die Bildextraktion aus vier Eingabeformaten soll es nur
    EINMAL geben). Beim Umbau auf @staticmethod blieb zunaechst ein
    ``self.``-Aufruf im Rumpf stehen — das haette NICHT nur den neuen Weg,
    sondern den bestehenden ANTHROPIC-Bildweg mit einem NameError zerlegt.
    Dieser Test haelt beide Aufrufarten fest.
    """
    from src.vision_provider import VisionProvider

    msgs, system = VisionProvider._convert_to_anthropic_messages(_image_messages())
    blocks = msgs[0]["content"]
    assert any(b.get("type") == "image" for b in blocks)
    assert any(b.get("type") == "text" for b in blocks)

    # Und ueber eine Instanz (so ruft VisionProvider.analyze selbst auf):
    msgs2, _ = VisionProvider.__new__(VisionProvider)._convert_to_anthropic_messages(
        _image_messages()
    )
    assert msgs2 == msgs
