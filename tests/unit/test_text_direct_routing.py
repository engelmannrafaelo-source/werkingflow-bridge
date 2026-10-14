"""Text-Wegwahl: Textaufrufe, die output_config oder ein Ausgabebudget ueber der
Abo-Grenze verlangen, laufen ueber den direkten Weg (claude-direct-notools).

Anlass (14.10.2026): der SDK-Weg reicht weder output_config noch max_tokens
weiter; Muehls Langbeschreibung kam mit 71 197 Ausgabe-Token bei max_tokens
64 000. werking-energy schickte deshalb provider_tier fest mit — die Regel
gehoert in die Bridge (src/routing/text_direct_routing.py).

Die Endpunkt-Tests laufen durch den ECHTEN Handler bis zum wirksamen Aufruf:
geprueft wird, was call_anthropic_direct bzw. der SDK-Weg (run_completion)
tatsaechlich bekommt — nicht nur ein gesetztes Feld.
"""
import sys
from unittest.mock import MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
    "src.identity.routes",
    "src.db.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

import inspect  # noqa: E402
import json  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

import src.main  # noqa: E402
from src.models import ChatCompletionRequest  # noqa: E402
from src.routing.text_direct_routing import (  # noqa: E402
    DIRECT_TIER,
    direct_route_reason,
)

_PIN_CONFIG = "src.routing.user_provider_override.get_user_provider_config"
_TIER_POLICY = "src.routing.app_tier_policy.resolve_app_tier_policy"


class _DirectReached(BaseException):
    pass


class _SdkReached(BaseException):
    pass


def _body(**extra):
    body = {
        "model": "claude-haiku-5-5",
        "messages": [{"role": "user", "content": "Langbeschreibung bitte"}],
    }
    body.update(extra)
    return body


def _request(body: dict, app_id: str, extra_headers=None):
    req = MagicMock()
    req.state = MagicMock(spec=[])
    req.headers = {
        "X-User-ID": "user-wegwahl",
        "X-App-ID": app_id,
        "X-App-Env": "staging",
        "X-Agent-ID": "schema_analysis/beschreibung",
        **(extra_headers or {}),
    }
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    req.state.cached_body_dict = body
    req.state.adaptive_est_tokens = 10
    return req


def _tracker():
    t = MagicMock()
    t.should_reject_new_request.return_value = False
    t.is_hard_limited.return_value = False
    return t


async def _run(body: dict, *, pin_config=None, tier_policy=None, app_id="werking-energy",
               headers=None):
    """Fuehrt den echten Handler bis zum ersten Backend-Aufruf.

    Rueckgabe ("direct", request, backend_config) oder ("sdk", kwargs).
    """
    seen = {}

    async def _direct(request_body, backend_config, *a, **k):
        seen["direct"] = (request_body, backend_config)
        raise _DirectReached

    def _sdk(*a, **k):
        seen["sdk"] = k
        raise _SdkReached

    handler = inspect.unwrap(src.main.chat_completions)
    with patch.object(src.main, "verify_api_key", AsyncMock()), \
         patch.object(src.main, "enforce_pool_admission", AsyncMock()), \
         patch("src.budget.gate.enforce_budget", AsyncMock()), \
         patch("src.claude_cli.rate_limit_tracker", _tracker()), \
         patch.object(src.main, "_org_disabled_precheck", MagicMock(return_value=None)), \
         patch(_PIN_CONFIG, AsyncMock(return_value=pin_config)), \
         patch(_TIER_POLICY, AsyncMock(return_value=tier_policy)), \
         patch("src.routing.prepaid_cap.prepaid_vision_over_cap",
               AsyncMock(return_value=(False, 0.0, 100.0))), \
         patch.object(src.main, "prepaid_vision_over_cap",
                      AsyncMock(return_value=(False, 0.0, 100.0)), create=True), \
         patch.object(src.main.claude_cli, "run_completion", MagicMock(side_effect=_sdk)), \
         patch("src.providers.anthropic_direct.call_anthropic_direct", _direct):
        try:
            await handler(ChatCompletionRequest(**body), _request(body, app_id, headers), None, None)
        except _DirectReached:
            rb, cfg = seen["direct"]
            return "direct", rb, cfg
        except _SdkReached:
            return "sdk", seen["sdk"], None
    raise AssertionError("weder direkter Weg noch SDK-Weg erreicht")


@pytest.fixture
def an(monkeypatch):
    monkeypatch.setenv("BRIDGE_TEXT_DIRECT_ROUTING_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_VISION_API_KEY", "test-key")
    monkeypatch.delenv("BRIDGE_ABO_AUSGABEGRENZE", raising=False)


# --- am wirksamen Aufruf -----------------------------------------------------

@pytest.mark.asyncio
async def test_output_config_geht_direkt_mit_budget_und_angefragtem_modell(an):
    weg, rb, cfg = await _run(_body(output_config={"effort": "low"}, max_tokens=64000))
    assert weg == "direct"
    assert rb.provider_tier == DIRECT_TIER
    assert rb.output_config == {"effort": "low"}
    assert rb.max_tokens == 64000
    # Der Tier traegt Sonnet als Vorgabe — die Wegwahl darf das Modell nicht tauschen.
    assert cfg.provider_model == "claude-haiku-5-5"


@pytest.mark.asyncio
async def test_budget_ueber_abo_grenze_geht_direkt(an):
    weg, rb, cfg = await _run(_body(max_tokens=64000))
    assert weg == "direct"
    assert rb.max_tokens == 64000


@pytest.mark.asyncio
async def test_budget_unter_abo_grenze_bleibt_auf_sdk(an):
    weg, _, _ = await _run(_body(max_tokens=8000))
    assert weg == "sdk"


@pytest.mark.asyncio
async def test_feld_default_ist_kein_verlangtes_budget(an):
    # max_tokens nicht geschickt -> Default 64000 zaehlt nicht
    weg, _, _ = await _run(_body())
    assert weg == "sdk"


@pytest.mark.asyncio
async def test_werkzeuge_bleiben_auf_sdk(an):
    # enable_tools gilt nur mit X-Claude-Allowed-Tools (enforce_tools_policy).
    weg, _, _ = await _run(_body(output_config={"effort": "low"}, enable_tools=True),
                           headers={"X-Claude-Allowed-Tools": "Read"})
    assert weg == "sdk"


@pytest.mark.asyncio
async def test_aufrufer_tier_gewinnt(an):
    # Nur bei NON_CUSTOMER_APP_IDS bleibt ein Aufrufer-Tier stehen; bei jeder
    # Kunden-App (auch ohne eigene Regel, globale Vorgabe) raeumt die
    # App-Provider-Regel ihn ab.
    weg, rb, cfg = await _run(_body(output_config={"effort": "low"},
                                    provider_tier="claude-premium"),
                              app_id="cui")
    assert weg == "sdk"


@pytest.mark.asyncio
async def test_schalter_aus_bleibt_auf_sdk(an, monkeypatch):
    monkeypatch.setenv("BRIDGE_TEXT_DIRECT_ROUTING_ENABLED", "false")
    weg, _, _ = await _run(_body(output_config={"effort": "low"}, max_tokens=64000))
    assert weg == "sdk"


@pytest.mark.asyncio
async def test_direkter_weg_nicht_eingerichtet_bleibt_auf_sdk(an, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_VISION_API_KEY", raising=False)
    weg, _, _ = await _run(_body(output_config={"effort": "low"}))
    assert weg == "sdk"


@pytest.mark.asyncio
async def test_abo_grenze_einstellbar(an, monkeypatch):
    monkeypatch.setenv("BRIDGE_ABO_AUSGABEGRENZE", "4000")
    weg, _, _ = await _run(_body(max_tokens=8000))
    assert weg == "direct"


@pytest.mark.asyncio
async def test_app_ohne_provider_regel_geht_ebenso_direkt(an):
    weg, rb, cfg = await _run(_body(output_config={"effort": "low"}), app_id="br-wegwahl-test")
    assert weg == "direct"
    assert cfg.provider_model == "claude-haiku-5-5"


@pytest.mark.asyncio
async def test_nutzer_pin_gewinnt(an):
    # Operator-Pin auf den Abo-Pool: die Regel darf ihn nicht umgehen.
    weg, _, _ = await _run(_body(output_config={"effort": "low"}),
                           pin_config={"provider": "anthropic"})
    assert weg == "sdk"


@pytest.mark.asyncio
async def test_energy_festes_tier_wird_von_app_regel_abgeraeumt_ohne_schalter(an, monkeypatch):
    # Befund 14.10.2026: apply_app_provider_policy setzt fuer werking-energy
    # provider_tier=None. Das fest mitgeschickte claude-direct-notools der App
    # (a34e288719) kommt so nie an — ohne Bridge-Regel landet der Aufruf auf
    # dem SDK-Weg, output_config und max_tokens gelten dort nicht.
    monkeypatch.setenv("BRIDGE_TEXT_DIRECT_ROUTING_ENABLED", "false")
    weg, _, _ = await _run(_body(output_config={"effort": "low"},
                                 provider_tier=DIRECT_TIER, max_tokens=64000))
    assert weg == "sdk"


@pytest.mark.asyncio
async def test_energy_festes_tier_mit_schalter_kommt_direkt_an(an):
    weg, rb, cfg = await _run(_body(output_config={"effort": "low"},
                                    provider_tier=DIRECT_TIER, max_tokens=64000))
    assert weg == "direct"
    assert rb.output_config == {"effort": "low"}
    assert rb.max_tokens == 64000
    assert cfg.provider_model == "claude-haiku-5-5"


# --- Regel als reine Funktion -------------------------------------------------

def _req(**extra):
    return ChatCompletionRequest(**_body(**extra))


def test_regel_bild_bleibt_draussen(monkeypatch):
    monkeypatch.delenv("BRIDGE_ABO_AUSGABEGRENZE", raising=False)
    assert direct_route_reason(_req(output_config={"effort": "low"}), has_images=True) is None
    assert direct_route_reason(_req(output_config={"effort": "low"}), has_images=False) == "output_config"


def test_regel_sitzung_bleibt_auf_sdk():
    assert direct_route_reason(_req(output_config={"effort": "low"}, session_id="s1"),
                               has_images=False) is None


def test_regel_bedrock_bleibt_unberuehrt():
    assert direct_route_reason(_req(output_config={"effort": "low"}, backend="bedrock"),
                               has_images=False) is None


def test_regel_kaputte_grenze_ist_laut(monkeypatch):
    monkeypatch.setenv("BRIDGE_ABO_AUSGABEGRENZE", "viel")
    with pytest.raises(RuntimeError):
        direct_route_reason(_req(max_tokens=64000), has_images=False)
