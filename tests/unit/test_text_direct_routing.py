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


@pytest.fixture(autouse=True)
def _ohne_startstand(monkeypatch):
    # Jeder Test beginnt ohne beim Start gepruefte Fassung; Tests der
    # Startpruefung setzen sie selbst.
    import src.routing.text_direct_routing as td
    monkeypatch.setattr(td, "_start_stand", None)


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
        **({"X-App-ID": app_id} if app_id else {}),
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
               headers=None, cap=(False, 0.0, 100.0), sonst_antwort=False):
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
               AsyncMock(return_value=cap)), \
         patch.object(src.main, "prepaid_vision_over_cap",
                      AsyncMock(return_value=cap), create=True), \
         patch.object(src.main.claude_cli, "run_completion", MagicMock(side_effect=_sdk)), \
         patch("src.providers.anthropic_direct.call_anthropic_direct", _direct):
        try:
            antwort = await handler(
                ChatCompletionRequest(**body), _request(body, app_id, headers), None, None
            )
            if sonst_antwort:
                return "antwort", antwort, None
        except _DirectReached:
            rb, cfg = seen["direct"]
            return "direct", rb, cfg
        except _SdkReached:
            return "sdk", seen["sdk"], None
        except Exception as e:  # noqa: BLE001
            if sonst_antwort:
                return "fehler", e, None
            raise
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


def test_regel_kaputte_grenze_schaltet_nur_die_budget_bedingung_ab(monkeypatch):
    # Laut ist der Start (pruefe_beim_start); zur Laufzeit kein Wurf, der
    # jeden Aufruf mit max_tokens zu einem 500 machte.
    monkeypatch.setenv("BRIDGE_ABO_AUSGABEGRENZE", "viel")
    assert direct_route_reason(_req(max_tokens=64000), has_images=False) is None


# --- eingeengter Geltungsbereich (BRIDGE_TEXT_DIRECT_ROUTING_APPS) -----------

_BEREICH = "werking-energy:schema_analysis/beschreibung"


@pytest.fixture
def bereich(monkeypatch):
    # Globaler Schalter AUS — der Bereich muss allein wirken.
    monkeypatch.delenv("BRIDGE_TEXT_DIRECT_ROUTING_ENABLED", raising=False)
    monkeypatch.setenv("BRIDGE_TEXT_DIRECT_ROUTING_APPS", _BEREICH)
    monkeypatch.setenv("ANTHROPIC_VISION_API_KEY", "test-key")
    monkeypatch.delenv("BRIDGE_ABO_AUSGABEGRENZE", raising=False)


@pytest.mark.asyncio
async def test_bereich_energy_beschreibung_geht_direkt_ohne_globalen_schalter(bereich):
    weg, rb, cfg = await _run(_body(output_config={"effort": "low"}, max_tokens=64000))
    assert weg == "direct"
    assert rb.provider_tier == DIRECT_TIER
    assert rb.output_config == {"effort": "low"}
    assert rb.max_tokens == 64000
    assert cfg.provider_model == "claude-haiku-5-5"


@pytest.mark.asyncio
async def test_bereich_auch_kleines_budget_ohne_output_config(bereich):
    # Probe TEILE: max_tokens 64 → 4 759 Ausgabe auf dem SDK-Weg.
    weg, rb, _ = await _run(_body(max_tokens=64))
    assert weg == "direct"
    assert rb.max_tokens == 64


@pytest.mark.asyncio
async def test_bereich_mit_festem_app_tier_kommt_direkt_an(bereich):
    # Die App schickt heute provider_tier fest; die App-Regel raeumt ihn ab,
    # der Bereich setzt den direkten Weg wieder.
    weg, rb, _ = await _run(_body(provider_tier=DIRECT_TIER, max_tokens=64000,
                                  output_config={"effort": "low"}))
    assert weg == "direct"
    assert rb.output_config == {"effort": "low"}


@pytest.mark.asyncio
async def test_bereich_andere_agent_id_derselben_app_bleibt_sdk(bereich):
    weg, _, _ = await _run(_body(output_config={"effort": "low"}, max_tokens=64000),
                           headers={"X-Agent-ID": "schema_analysis/block"})
    assert weg == "sdk"


@pytest.mark.asyncio
async def test_bereich_praefix_greift_segmentweise(bereich):
    weg, _, _ = await _run(_body(max_tokens=64000),
                           headers={"X-Agent-ID": "schema_analysis/beschreibung_alt"})
    assert weg == "sdk"
    weg, _, _ = await _run(_body(max_tokens=64000),
                           headers={"X-Agent-ID": "schema_analysis/beschreibung/lang"})
    assert weg == "direct"


@pytest.mark.asyncio
async def test_bereich_andere_app_gleicher_agent_bleibt_sdk(bereich):
    weg, _, _ = await _run(_body(output_config={"effort": "low"}), app_id="werking-report")
    assert weg == "sdk"


@pytest.mark.asyncio
async def test_bereich_werkzeuge_bleiben_sdk(bereich):
    weg, _, _ = await _run(_body(max_tokens=64000, enable_tools=True),
                           headers={"X-Claude-Allowed-Tools": "Read"})
    assert weg == "sdk"


@pytest.mark.asyncio
async def test_bereich_nutzer_pin_anthropic_gewinnt(bereich):
    weg, _, _ = await _run(_body(max_tokens=64000), pin_config={"provider": "anthropic"})
    assert weg == "sdk"


@pytest.mark.asyncio
async def test_bereich_nutzer_pin_bedrock_erreicht_nie_den_direkten_weg(bereich):
    weg, ergebnis, _ = await _run(
        _body(max_tokens=64000),
        pin_config={"provider": "bedrock", "region": "eu-central-1"},
        headers={"X-App-Env": "prod"},  # der Bedrock-Pin gilt nur in prod
        sonst_antwort=True,
    )
    assert weg != "direct", ergebnis


@pytest.mark.asyncio
async def test_bereich_tagesdeckel_ist_lautes_429(bereich):
    weg, antwort, _ = await _run(_body(max_tokens=64000), cap=(True, 50.0, 50.0),
                                 sonst_antwort=True)
    assert weg == "antwort"
    assert antwort.status_code == 429
    assert json.loads(antwort.body)["error"]["code"] == "prepaid_daily_cap"


@pytest.mark.parametrize("wert", [
    "werking-energy",                       # Agent fehlt
    "werking-energy:",                      # Agent leer
    ":schema_analysis/beschreibung",        # App fehlt
    "werking-energy;schema_analysis",       # falsches Trennzeichen
    "werking-energy:schema analysis",       # Leerzeichen
    "werking-energy:a,,x:b",                # leerer Eintrag
])
def test_bereich_tippfehler_ist_laut(monkeypatch, wert):
    from src.routing.text_direct_routing import geltungsbereich
    monkeypatch.setenv("BRIDGE_TEXT_DIRECT_ROUTING_APPS", wert)
    with pytest.raises(RuntimeError):
        geltungsbereich()


def test_bereich_mehrere_eintraege(monkeypatch):
    from src.routing.text_direct_routing import geltungsbereich, im_geltungsbereich
    monkeypatch.setenv("BRIDGE_TEXT_DIRECT_ROUTING_APPS",
                       " werking-energy:schema_analysis/beschreibung , werking-noise:essenz/ ")
    assert geltungsbereich() == [("werking-energy", "schema_analysis/beschreibung"),
                                 ("werking-noise", "essenz")]
    assert im_geltungsbereich("werking-noise", "essenz/x") == "werking-noise:essenz"
    assert im_geltungsbereich("werking-noise", None) is None


# --- BRIDGEFOLGER P3-a / P3-b -----------------------------------------------

@pytest.mark.asyncio
async def test_ohne_app_regel_global_an_geht_direkt(an):
    # P3-b: der Zweig user_pinned_provider is None (keine X-App-ID → keine
    # App-Provider-Regel) braucht einen eigenen Test, der direkt erwartet.
    weg, rb, cfg = await _run(_body(output_config={"effort": "low"}), app_id=None)
    assert weg == "direct"
    assert cfg.provider_model == "claude-haiku-5-5"


@pytest.mark.asyncio
async def test_bereich_fuer_nicht_kunden_app_ohne_app_regel_geht_direkt(bereich, monkeypatch):
    # P3-b ueber den Bereich: cui hat keine App-Provider-Regel
    # (NON_CUSTOMER_APP_IDS) → user_pinned_provider bleibt None.
    monkeypatch.setenv("BRIDGE_TEXT_DIRECT_ROUTING_APPS", "cui:werkzeug")
    weg, rb, _ = await _run(_body(max_tokens=64), app_id="cui",
                            headers={"X-Agent-ID": "werkzeug/zusammenfassung"})
    assert weg == "direct"
    assert rb.max_tokens == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("app_id", [None, "cui", "platform", "dev-tooling",
                                    "partner-platform", "engelmann"])
async def test_nur_bereich_an_laesst_alle_anderen_unberuehrt(bereich, app_id):
    # P3-a: mit nur dem eingeengten Bereich geht weder ein Aufruf ohne
    # App-ID noch einer der Nicht-Kunden-Apps oder Engelmann direkt.
    weg, _, _ = await _run(_body(output_config={"effort": "low"}, max_tokens=64000),
                           app_id=app_id)
    assert weg == "sdk"


@pytest.mark.asyncio
async def test_engelmann_mit_ueberstimmtem_pin_bleibt_auch_global_auf_sdk(an):
    # P3-a: Engelmanns App-Regel ueberstimmt einen Nutzer-Pin
    # (app_rule_outranks_user_pin). Der Pin sperrt die Text-Regel trotzdem.
    weg, ergebnis, _ = await _run(
        _body(output_config={"effort": "low"}, max_tokens=64000),
        app_id="engelmann",
        pin_config={"provider": "bedrock", "region": "eu-central-1"},
        headers={"X-App-Env": "prod"},
        sonst_antwort=True,
    )
    assert weg == "sdk", ergebnis


# --- Kanzler 12:3xZ: kein Kollateralschaden, kein stiller Rueckfall ----------

def _code(antwort):
    return antwort.status_code, json.loads(antwort.body)["error"]["code"]


@pytest.mark.asyncio
async def test_tippfehler_zur_laufzeit_trifft_nur_die_genannte_app(bereich, monkeypatch):
    monkeypatch.setenv(
        "BRIDGE_TEXT_DIRECT_ROUTING_APPS",
        "werking-energy:schema_analysis/beschreibung,werking-noise;essenz",
    )
    # gueltiger Eintrag wirkt weiter
    weg, rb, _ = await _run(_body(max_tokens=64))
    assert weg == "direct"
    # die App des kaputten Eintrags: laut, nur dieser Aufruf
    weg, antwort, _ = await _run(_body(max_tokens=64), app_id="werking-noise",
                                 headers={"X-Agent-ID": "essenz"}, sonst_antwort=True)
    assert weg == "antwort"
    assert _code(antwort) == (503, "text_direct_config_invalid")
    # alle anderen unveraendert
    for app_id in ("werking-report", "cui", None):
        weg, _, _ = await _run(_body(output_config={"effort": "low"}, max_tokens=64000),
                               app_id=app_id)
        assert weg == "sdk", app_id


@pytest.mark.asyncio
async def test_tippfehler_ohne_gueltigen_eintrag_laesst_fremde_apps_unberuehrt(bereich, monkeypatch):
    monkeypatch.setenv("BRIDGE_TEXT_DIRECT_ROUTING_APPS", "werking-energy")
    weg, antwort, _ = await _run(_body(max_tokens=64), sonst_antwort=True)
    assert _code(antwort) == (503, "text_direct_config_invalid")
    weg, _, _ = await _run(_body(max_tokens=64000), app_id="werking-report")
    assert weg == "sdk"


@pytest.mark.asyncio
async def test_kaputte_abo_grenze_zur_laufzeit_kein_500(an, monkeypatch):
    monkeypatch.setenv("BRIDGE_ABO_AUSGABEGRENZE", "viel")
    weg, _, _ = await _run(_body(max_tokens=64000), app_id="werking-report")
    assert weg == "sdk"


@pytest.mark.asyncio
async def test_bereich_ohne_schluessel_ist_503_kein_stiller_rueckfall(bereich, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_VISION_API_KEY", raising=False)
    weg, antwort, _ = await _run(_body(output_config={"effort": "low"}, max_tokens=64000),
                                 sonst_antwort=True)
    assert weg == "antwort"
    assert _code(antwort) == (503, "text_direct_key_missing")
    # ausserhalb des Bereichs unveraendert
    weg, _, _ = await _run(_body(max_tokens=64000),
                           headers={"X-Agent-ID": "schema_analysis/block"})
    assert weg == "sdk"
    weg, _, _ = await _run(_body(max_tokens=64000), app_id="werking-report")
    assert weg == "sdk"


# Startpruefung

def test_start_ohne_bereich_ok(monkeypatch):
    from src.routing.text_direct_routing import pruefe_beim_start
    monkeypatch.delenv("BRIDGE_TEXT_DIRECT_ROUTING_APPS", raising=False)
    monkeypatch.delenv("ANTHROPIC_VISION_API_KEY", raising=False)
    monkeypatch.delenv("BRIDGE_ABO_AUSGABEGRENZE", raising=False)
    assert pruefe_beim_start() == []


def test_start_bricht_bei_tippfehler_ab(bereich, monkeypatch):
    from src.routing.text_direct_routing import pruefe_beim_start
    monkeypatch.setenv("BRIDGE_TEXT_DIRECT_ROUTING_APPS", "werking-energy;schema_analysis")
    with pytest.raises(RuntimeError, match="app-id:agent-praefix"):
        pruefe_beim_start()


def test_start_bricht_bei_bereich_ohne_schluessel_ab(bereich, monkeypatch):
    from src.routing.text_direct_routing import pruefe_beim_start
    monkeypatch.delenv("ANTHROPIC_VISION_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_VISION_API_KEY"):
        pruefe_beim_start()


def test_start_bricht_bei_kaputter_abo_grenze_ab(monkeypatch):
    from src.routing.text_direct_routing import pruefe_beim_start
    monkeypatch.setenv("BRIDGE_ABO_AUSGABEGRENZE", "viel")
    with pytest.raises(RuntimeError):
        pruefe_beim_start()


def test_start_mit_bereich_und_schluessel_ok(bereich):
    from src.routing.text_direct_routing import pruefe_beim_start
    assert pruefe_beim_start() == [("werking-energy", "schema_analysis/beschreibung")]


@pytest.mark.asyncio
async def test_lifespan_ruft_die_startpruefung_und_bricht_ab(bereich, monkeypatch):
    # Am echten lifespan: Bereich ohne Schluessel -> Start bricht ab, bevor
    # der Worker Aufrufe annimmt.
    monkeypatch.delenv("ANTHROPIC_VISION_API_KEY", raising=False)
    with patch("src.pricing.validate_billing_integrity", MagicMock()):
        with pytest.raises(RuntimeError, match="ANTHROPIC_VISION_API_KEY"):
            async with src.main.lifespan(src.main.app):
                pass


# --- BRIDGEFOLGER Delta-Runde: P2 / P3-d / P3-e ------------------------------

@pytest.mark.asyncio
async def test_laufzeit_gilt_der_beim_start_gepruefte_wert(bereich, monkeypatch):
    # P2: Start mit gueltigem Wert, danach kaputte Umgebung — es gilt weiter
    # der gepruefte Bereich; keine App bekommt einen Fehler.
    from src.routing.text_direct_routing import pruefe_beim_start
    pruefe_beim_start(src.main.KNOWN_APP_IDS)
    monkeypatch.setenv("BRIDGE_TEXT_DIRECT_ROUTING_APPS", "werking-energy;kaputt")
    weg, rb, _ = await _run(_body(max_tokens=64))
    assert weg == "direct"
    for app_id in ("werking-report", "werking-noise", "cui", None):
        weg, _, _ = await _run(_body(output_config={"effort": "low"}, max_tokens=64000),
                               app_id=app_id)
        assert weg == "sdk", app_id


@pytest.mark.asyncio
async def test_start_ohne_bereich_bleibt_ohne_bereich(bereich, monkeypatch):
    from src.routing.text_direct_routing import pruefe_beim_start
    monkeypatch.delenv("BRIDGE_TEXT_DIRECT_ROUTING_APPS")
    pruefe_beim_start(src.main.KNOWN_APP_IDS)
    monkeypatch.setenv("BRIDGE_TEXT_DIRECT_ROUTING_APPS", _BEREICH)
    weg, _, _ = await _run(_body(max_tokens=64))
    assert weg == "sdk"


def test_start_unbekannte_app_id_bricht_ab(bereich, monkeypatch):
    # P3-d: gueltiges Format, falscher App-Name wirkte sonst still gar nicht.
    from src.routing.text_direct_routing import pruefe_beim_start
    monkeypatch.setenv("BRIDGE_TEXT_DIRECT_ROUTING_APPS", "werking-enrgy:schema_analysis/beschreibung")
    with pytest.raises(RuntimeError, match="unbekannte App-ID"):
        pruefe_beim_start(src.main.KNOWN_APP_IDS)


async def _lifespan_start():
    with patch("src.pricing.validate_billing_integrity", MagicMock()):
        async with src.main.lifespan(src.main.app):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("wert, meldung", [
    ("werking-energy", "app-id:agent-praefix"),
    ("werking-energy:schema_analysis/beschreibung,", "app-id:agent-praefix"),
    ("werking-enrgy:schema_analysis/beschreibung", "unbekannte App-ID"),
])
async def test_lifespan_bricht_bei_kaputtem_wert_ab(bereich, monkeypatch, wert, meldung):
    # P3-e: der echte lifespan faehrt die Startpruefung und bricht ab, bevor
    # der Worker Aufrufe annimmt (vor der SDK-Pruefung).
    monkeypatch.setenv("BRIDGE_TEXT_DIRECT_ROUTING_APPS", wert)
    with pytest.raises(RuntimeError, match=meldung):
        await _lifespan_start()


@pytest.mark.asyncio
async def test_zweite_linie_vergleicht_den_app_teil_exakt(bereich, monkeypatch):
    # P3-g: ein kaputter Eintrag fuer "werking-noise-beta" darf werking-noise
    # nicht treffen (vorher: Teilzeichenkette → 503 fuer werking-noise).
    monkeypatch.setenv(
        "BRIDGE_TEXT_DIRECT_ROUTING_APPS",
        "werking-energy:schema_analysis/beschreibung,werking-noise-beta;essenz",
    )
    weg, _, _ = await _run(_body(max_tokens=64000), app_id="werking-noise",
                           headers={"X-Agent-ID": "essenz"})
    assert weg == "sdk"
    weg, antwort, _ = await _run(_body(max_tokens=64), app_id="werking-noise-beta",
                                 headers={"X-Agent-ID": "essenz"}, sonst_antwort=True)
    assert _code(antwort) == (503, "text_direct_config_invalid")
    weg, _, _ = await _run(_body(max_tokens=64))
    assert weg == "direct"


@pytest.mark.parametrize("eintrag, app", [
    ("werking-energy", "werking-energy"),
    ("werking-energy:", "werking-energy"),
    ("werking-energy;schema_analysis", "werking-energy"),
    ("werking-energy:schema analysis", "werking-energy"),
    ("werking-energy/x", "werking-energy"),
    (":schema_analysis", ""),
])
def test_app_teil(eintrag, app):
    from src.routing.text_direct_routing import _app_teil
    assert _app_teil(eintrag) == app
