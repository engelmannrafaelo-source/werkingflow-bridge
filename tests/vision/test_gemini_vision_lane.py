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


def test_default_is_25_flash_and_the_cheapest_of_its_generation():
    """Zwei Festlegungen in einem Test.

    (1) Der Default ist 2.5 FLASH — Rafaels Entscheidung vom 2026-09-03. Nicht
        Flash-Lite (waere 3x guenstiger, aber es geht um die Qualitaet der
        Analyseberichte) und nicht eine neuere Generation.
    (2) Innerhalb der Flash-Familie ist 2.5 die guenstigste — neuer ist hier
        NICHT billiger (3.5 Flash: 1,50/9,00 gegen 0,30/2,50). Wer den Default
        hebt, soll das bewusst tun und diesen Test mit anfassen.
    """
    from src.pricing import price_entry

    assert gv.DEFAULT_GEMINI_VISION_MODEL == "gemini-2.5-flash"

    def total(m):
        p = price_entry(m)
        return p["in"] + p["out"]

    flash = [m for m in gv.GEMINI_VISION_MODELS if "flash-lite" not in m]
    assert gv.DEFAULT_GEMINI_VISION_MODEL == min(flash, key=total)


def test_gemini_is_much_cheaper_than_the_sonnet_it_replaces():
    """Der Grund fuer den ganzen Bau, als Zahl festgehalten."""
    from src.pricing import price_entry

    g = price_entry("gemini-2.5-flash")
    s = price_entry("claude-sonnet-5")
    assert g["in"] < s["in"] / 5      # 0,30 gegen 2,00
    assert g["out"] < s["out"] / 3    # 2,50 gegen 10,00


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
    kwargs = {"app_env": "staging", "has_images": True}
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


def test_a_gemini_model_name_in_the_model_field_is_rejected():
    """E3 wollte model='gemini-flash-lite' als Marker senden. Das geht NICHT:
    resolve_model laeuft in main.py lange VOR der Vision-Weiche und kennt nur
    Claude-Modelle — der Aufruf staerbe mit 400 model_not_found, bevor der Tier
    ueberhaupt betrachtet wird. Der Marker gehoert in provider_tier; welches
    Modell tatsaechlich bedient hat, schreibt die Bridge selbst ins Ledger.
    Dieser Test haelt die Absage fest, damit sie nicht als Bug missverstanden
    und 'behoben' wird, indem jemand Gemini in die Claude-Modellregistry haengt.
    """
    from src.model_registry import resolve_model

    for name in ("gemini-flash-lite", "gemini-2.5-flash-lite"):
        resolved, msg = resolve_model(name)
        assert resolved is None, f"{name} sollte nicht aufloesen"
    assert resolve_model("claude-sonnet-5")[0] == "claude-sonnet-5"


# ── Kollision mit der App-Provider-Regel ────────────────────────────────────
# Ohne diese Ausnahme haette werking-energy — genau die App, die messen will —
# den Tier verloren und still Sonnet gemessen. Auf dem Anthropic-Prepaid-Key.

def _energy_request(tier="gemini-vision"):
    from src.models import BackendType

    class _Body:
        def __init__(self):
            self.backend = BackendType.ANTHROPIC
            self.provider_tier = tier
    return _Body()


def test_app_rule_does_not_silently_strip_the_gemini_tier():
    from src.routing.app_provider_policy import (
        APP_PROVIDER_RULES, apply_app_provider_policy, client_requests_gemini_vision,
    )

    body = _energy_request()
    assert client_requests_gemini_vision(body)
    applied = apply_app_provider_policy(body, APP_PROVIDER_RULES["werking-energy"])

    # Die Regel laesst den Aufruf unangetastet (wie beim ausdruecklich
    # angefragten Bedrock), statt den Tier abzuraeumen.
    assert applied is None
    assert body.provider_tier == "gemini-vision", (
        "Der Gemini-Testtier wurde von der App-Regel abgeraeumt — der Aufruf "
        "waere still von Anthropic beantwortet worden, waehrend der Aufrufer "
        "glaubt, er misst Gemini."
    )


def test_app_rule_still_strips_every_other_tier():
    """Die Ausnahme ist eine Einzelnennung, keine Kategorie: jeder andere Tier
    wird weiterhin abgeraeumt. Sonst waere aus der Ausnahme ein Loch geworden,
    durch das eine App sich ihr Backend selbst aussuchen kann."""
    from src.models import BackendType
    from src.routing.app_provider_policy import APP_PROVIDER_RULES, apply_app_provider_policy

    body = _energy_request(tier="openrouter-claude")
    applied = apply_app_provider_policy(body, APP_PROVIDER_RULES["werking-energy"])
    assert applied == "anthropic"
    assert body.provider_tier is None
    assert body.backend == BackendType.ANTHROPIC


# ── Bridge-weiter Standard-Anbieter (Rafael 2026-09-03) ─────────────────────

class _Cfg:
    def __init__(self, backend=None, provider_tier=None):
        from src.models import BackendType
        self.backend = backend if backend is not None else BackendType.ANTHROPIC
        self.provider_tier = provider_tier


def test_default_provider_is_anthropic_unless_switched(monkeypatch):
    """Ohne Schalter aendert sich nichts — der Bau ist auf jeder Bridge inert,
    die ihn nicht ausdruecklich einschaltet."""
    from src.routing.vision_router import (
        VISION_TARGET_ANTHROPIC, default_vision_target, resolve_vision_target,
    )

    monkeypatch.delenv("BRIDGE_VISION_DEFAULT_PROVIDER", raising=False)
    assert default_vision_target() == VISION_TARGET_ANTHROPIC
    assert resolve_vision_target(None) == VISION_TARGET_ANTHROPIC
    assert resolve_vision_target(_Cfg()) == VISION_TARGET_ANTHROPIC


def test_switch_makes_gemini_the_default_without_any_app_change(monkeypatch):
    """Der Sinn des Schalters: der Anbieterwechsel ist eine Eigenschaft des
    Deployments, nicht eine Aenderung an vier Aufrufstellen je App."""
    from src.routing.vision_router import VISION_TARGET_GEMINI, resolve_vision_target

    monkeypatch.setenv("BRIDGE_VISION_DEFAULT_PROVIDER", "gemini")
    assert resolve_vision_target(None) == VISION_TARGET_GEMINI
    assert resolve_vision_target(_Cfg()) == VISION_TARGET_GEMINI


def test_switch_never_overrides_a_bedrock_pin(monkeypatch):
    """Die wichtigste Nicht-Wirkung: ein Bedrock-Pin traegt die vertragliche
    EU-Datenresidenz eines echten Kunden. Ein Bridge-weiter Standard darf ihn
    unter keinen Umstaenden nach Google umbiegen."""
    from src.models import BackendType
    from src.routing.vision_router import VISION_TARGET_ANTHROPIC, resolve_vision_target

    monkeypatch.setenv("BRIDGE_VISION_DEFAULT_PROVIDER", "gemini")
    assert resolve_vision_target(_Cfg(backend=BackendType.BEDROCK)) == VISION_TARGET_ANTHROPIC


def test_switch_never_overrides_an_explicitly_chosen_tier(monkeypatch):
    """Wer ausdruecklich einen anderen Tier gewaehlt hat (z.B.
    claude-direct-notools), bekommt ihn — der Standard fuellt nur eine Luecke."""
    from src.routing.vision_router import VISION_TARGET_ANTHROPIC, resolve_vision_target

    monkeypatch.setenv("BRIDGE_VISION_DEFAULT_PROVIDER", "gemini")
    assert resolve_vision_target(
        _Cfg(provider_tier="claude-direct-notools")
    ) == VISION_TARGET_ANTHROPIC


def test_typo_in_the_switch_fails_loud(monkeypatch):
    """Ein Tippfehler darf nicht monatelang so aussehen, als liefe alles ueber
    Gemini."""
    from src.routing.vision_router import default_vision_target

    monkeypatch.setenv("BRIDGE_VISION_DEFAULT_PROVIDER", "gemeni")
    with pytest.raises(ValueError):
        default_vision_target()


def test_gate_still_blocks_production_even_when_gemini_is_the_default(monkeypatch):
    """Der Schalter ist eine Standardwahl, KEINE Erlaubnis. Auf prod bleibt der
    Weg zu — dort liegt ohnehin kein Key, aber auch mit Key waere Schluss."""
    monkeypatch.setenv("BRIDGE_VISION_DEFAULT_PROVIDER", "gemini")
    monkeypatch.setenv("BRIDGE_GEMINI_VISION_ENABLED", "true")
    monkeypatch.setenv("GEMINI_VISION_API_KEY", "test-key")
    with pytest.raises(gate.GeminiVisionRefused):
        gate.assert_gemini_vision_allowed(app_env="prod", has_images=True)


# ── Denk-Budget: der groesste Kostenhebel des Wegs ──────────────────────────
# Gemessen 2026-09-03 am selben Bild: Flash mit Denken 3,1x guenstiger als
# Sonnet, ohne Denken 23x. Der Unterschied steckt allein in diesem Schalter.

def test_thinking_budget_defaults_to_googles_behaviour(monkeypatch):
    monkeypatch.delenv("GEMINI_VISION_THINKING_BUDGET", raising=False)
    assert gv.gemini_vision_thinking_budget() is None


@pytest.mark.parametrize("raw,expected", [("0", 0), ("512", 512)])
def test_thinking_budget_is_read_from_env(monkeypatch, raw, expected):
    monkeypatch.setenv("GEMINI_VISION_THINKING_BUDGET", raw)
    assert gv.gemini_vision_thinking_budget() == expected


@pytest.mark.parametrize("raw", ["aus", "-1", "1.5"])
def test_broken_thinking_budget_fails_loud(monkeypatch, raw):
    """Ein Tippfehler darf nicht still zu Googles Default werden — sonst denkt
    das Modell weiter und niemand versteht, warum die Rechnung nicht faellt."""
    monkeypatch.setenv("GEMINI_VISION_THINKING_BUDGET", raw)
    with pytest.raises(gv.GeminiVisionError):
        gv.gemini_vision_thinking_budget()


@pytest.mark.asyncio
async def test_thinking_config_is_only_sent_when_configured(monkeypatch):
    monkeypatch.setenv("GEMINI_VISION_API_KEY", "test-key")
    monkeypatch.setattr(gv.httpx, "AsyncClient", _FakeClient)

    monkeypatch.delenv("GEMINI_VISION_THINKING_BUDGET", raising=False)
    await gv.GeminiVisionProvider().analyze(messages=_image_messages())
    assert "thinkingConfig" not in _FakeClient.captured["body"].get("generationConfig", {})

    monkeypatch.setenv("GEMINI_VISION_THINKING_BUDGET", "0")
    await gv.GeminiVisionProvider().analyze(messages=_image_messages())
    assert _FakeClient.captured["body"]["generationConfig"]["thinkingConfig"] == {
        "thinkingBudget": 0
    }


def test_anthropic_thinking_params_are_still_refused():
    """Der Env-Schalter ist eine Gemini-EIGENE Einstellung. Er macht die
    Anthropic-Parameter nicht plötzlich uebersetzbar — die bleiben abgewiesen,
    sonst waere die Grenze zwischen 'eingestellt' und 'geraten' weg."""
    with pytest.raises(gate.GeminiVisionRefused):
        gate.assert_no_anthropic_only_params(output_config={"effort": "low"})
