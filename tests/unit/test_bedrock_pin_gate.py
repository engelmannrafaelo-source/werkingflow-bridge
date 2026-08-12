"""Unit Tests für das Bedrock-Pin-Gate (user_provider_override).

Rafaels Regel (2026-07-05): Bedrock gilt NUR für User, bei denen der Operator
es explizit gesetzt hat (users.provider_config) — sonst läuft alles über die
normale Bridge. Client-seitiges Opt-in (backend=bedrock oder ein
Bedrock-provider_tier wie 'claude-dsgvo') wird abgelehnt, weil jeder
Bedrock-Call pay-per-token gegen das AWS-Konto läuft und einem echten User
zuordenbar sein MUSS (Umlage + 1:1-Audit). Der Pin garantiert die
Attribution strukturell: er existiert nur auf einem echten User-Row.
"""

import os

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest

from types import SimpleNamespace

from src.models import BackendType
from src.routing.user_provider_override import (
    BedrockAttributionIncompleteError,
    BedrockNonProdRefusedError,
    BedrockPinRequiredError,
    UserProviderOverrideError,
    apply_user_provider_override,
    assert_bedrock_attribution_complete,
    assert_bedrock_is_pinned,
)


def _body():
    return SimpleNamespace(backend=None, bedrock_region=None, provider_tier="claude-premium")


BEDROCK_PIN = {"provider": "bedrock", "region": "eu-central-1"}


class TestBedrockPinGate:
    def test_anthropic_backend_passes_without_pin(self):
        """Normalfall: keine Pin, normaler Backend — kein Eingriff."""
        assert_bedrock_is_pinned(BackendType.ANTHROPIC, None)

    def test_bedrock_with_pin_in_prod_passes(self):
        """Der einzige legitime Bedrock-Weg: Operator-Pin AUS Production."""
        assert_bedrock_is_pinned(BackendType.BEDROCK, "bedrock", app_env="prod")

    def test_bedrock_without_pin_refused(self):
        """Client-Opt-in ohne Pin → 403-Klasse, kein silent-redirect."""
        with pytest.raises(BedrockPinRequiredError, match="operator-set per-user pin"):
            assert_bedrock_is_pinned(BackendType.BEDROCK, None, app_env="prod")

    def test_bedrock_with_anthropic_pin_refused(self):
        """Explizit auf anthropic gepinnter User darf nicht per Request-Feld
        nach Bedrock ausweichen — der Pin ist eine Operator-Entscheidung."""
        with pytest.raises(BedrockPinRequiredError):
            assert_bedrock_is_pinned(BackendType.BEDROCK, "anthropic", app_env="prod")

    def test_gate_is_not_the_503_class(self):
        """403 (Client darf das nicht) und 503 (Pin unservable) sind
        verschiedene Fehlerklassen — Handler mappen sie unterschiedlich."""
        assert not issubclass(BedrockPinRequiredError, UserProviderOverrideError)

    def test_openai_compatible_passes(self):
        assert_bedrock_is_pinned(BackendType.OPENAI_COMPATIBLE, None)


class TestBedrockIsProductionOnly:
    """Rafael 2026-08-11: der Pin sagt WER auf Bedrock darf, nicht WOMIT.
    Dieselben AWS-Credentials liegen auf beiden Bridges, und jede lokale,
    Staging-, Partner- oder CI-Instanz kann sich als derselbe User anmelden —
    genau so hat ein Konvertierungs-Loop einer Partner-Dev-Instanz sechs Tage
    lang echtes AWS-Geld gebucht. Bedrock daher NUR aus app_env='prod'."""

    @pytest.mark.parametrize("env", ["staging", "local"])
    def test_pinned_user_from_non_prod_refused(self, env):
        with pytest.raises(BedrockNonProdRefusedError, match="production"):
            assert_bedrock_is_pinned(BackendType.BEDROCK, "bedrock", app_env=env)

    def test_missing_app_env_is_fail_closed(self):
        """Kein nachweisbarer Prod-Call → kein Bedrock. Nicht 'im Zweifel ja'."""
        with pytest.raises(BedrockNonProdRefusedError):
            assert_bedrock_is_pinned(BackendType.BEDROCK, "bedrock", app_env=None)

    def test_default_without_app_env_argument_is_fail_closed(self):
        """Ein Aufrufer, der app_env vergisst, bekommt KEIN Bedrock —
        die unsichere Variante darf nicht die bequeme sein."""
        with pytest.raises(BedrockNonProdRefusedError):
            assert_bedrock_is_pinned(BackendType.BEDROCK, "bedrock")

    def test_non_prod_still_fine_on_anthropic(self):
        """Non-Prod laeuft weiter — nur eben ueber die internen Accounts."""
        assert_bedrock_is_pinned(BackendType.ANTHROPIC, None, app_env="local")

    def test_pin_and_env_are_distinct_error_classes(self):
        """403-Gruende muessen unterscheidbar bleiben: 'nicht gepinnt' ist ein
        Provisioning-Problem, 'nicht prod' ein Umgebungs-Problem."""
        assert not issubclass(BedrockNonProdRefusedError, BedrockPinRequiredError)
        assert not issubclass(BedrockPinRequiredError, BedrockNonProdRefusedError)


class TestBedrockAttributionComplete:
    """A real-money (Bedrock) call must be FULLY attributed — app_id AND app_env
    both present — else the spend is un-attributable in the cost dashboard (the
    €1.30 blind spot, 2026-07-09). WHO pays is already guaranteed by the pin."""

    def test_bedrock_fully_attributed_passes(self):
        for env in ("prod", "staging", "local"):
            assert_bedrock_attribution_complete(
                BackendType.BEDROCK, app_env=env, app_id="werking-energy"
            )

    def test_bedrock_without_app_env_refused(self):
        """Real money + NULL app_env → fail loud, not invisible booking."""
        with pytest.raises(BedrockAttributionIncompleteError, match="X-App-Env"):
            assert_bedrock_attribution_complete(
                BackendType.BEDROCK, app_env=None, app_id="werking-energy"
            )

    def test_bedrock_without_app_id_refused(self):
        """Real money + NULL app_id → fail loud (which app is paying?)."""
        with pytest.raises(BedrockAttributionIncompleteError, match="X-App-ID"):
            assert_bedrock_attribution_complete(
                BackendType.BEDROCK, app_env="prod", app_id=None
            )

    def test_bedrock_missing_both_lists_both(self):
        with pytest.raises(BedrockAttributionIncompleteError) as exc:
            assert_bedrock_attribution_complete(
                BackendType.BEDROCK, app_env="", app_id=""
            )
        assert "X-App-Env" in str(exc.value) and "X-App-ID" in str(exc.value)

    def test_anthropic_incomplete_passes(self):
        """The flat-rate pool is €0 marginal cost — absent dimensions there are a
        non-fatal diagnostic, not a hard reject (only Bedrock is real money)."""
        assert_bedrock_attribution_complete(
            BackendType.ANTHROPIC, app_env=None, app_id=None
        )

    def test_openai_compatible_incomplete_passes(self):
        assert_bedrock_attribution_complete(
            BackendType.OPENAI_COMPATIBLE, app_env=None, app_id=None
        )

    def test_incomplete_is_distinct_error_class(self):
        """400 (fix your request) is a different class from 403 (pin) and 503."""
        assert not issubclass(BedrockAttributionIncompleteError, BedrockPinRequiredError)
        assert not issubclass(BedrockAttributionIncompleteError, UserProviderOverrideError)


class TestBedrockPinIsIgnoredOutsideProd:
    """Rafael 2026-08-12: „Bedrock-Pin wird ausserhalb prod ignoriert."

    Der Pin traegt die EU-Datenresidenz ECHTER Kundendaten. Auf Staging und
    lokal gibt es die nicht — dort war er nur noch eine Sperre gegen das eigene
    Testen: die Dev-Bridge hat keine AWS-Zugangsdaten, also starb jeder Aufruf
    eines gepinnten Users mit 503 ``user_provider_override_unavailable``, das
    nginx als ``capacity_busy`` weiterreichte. Der gesamte Check-Trichter war
    damit auf Staging nicht pruefbar.
    """

    @pytest.mark.parametrize("env", ["staging", "local"])
    def test_bedrock_pin_downgrades_to_anthropic(self, env):
        body = _body()
        assert apply_user_provider_override(body, BEDROCK_PIN, app_env=env) == "anthropic"
        assert body.backend == BackendType.ANTHROPIC
        # Region gehoert zum Bedrock-Weg und darf nicht stehenbleiben.
        assert body.bedrock_region is None
        # Der Pin schlaegt weiterhin einen client-gewaehlten Tier — nur eben
        # auf den Anthropic-Weg.
        assert body.provider_tier is None

    def test_bedrock_pin_applies_in_prod(self):
        body = _body()
        assert apply_user_provider_override(body, BEDROCK_PIN, app_env="prod") == "bedrock"
        assert body.backend == BackendType.BEDROCK
        assert body.bedrock_region == "eu-central-1"

    def test_unknown_app_env_is_NOT_downgraded(self):
        """Der Unterschied, auf den es ankommt.

        Ein Produktions-Deployment, das X-App-Env vergisst, darf NICHT still
        auf die Anthropic-Konten wechseln — das waere eine unbemerkte Aenderung
        der Datenresidenz. Der Pin wird angewendet; abgewiesen wird er dann
        laut im Gate (assert_bedrock_is_pinned), nicht heimlich hier.
        """
        body = _body()
        assert apply_user_provider_override(body, BEDROCK_PIN, app_env=None) == "bedrock"
        assert body.backend == BackendType.BEDROCK
        with pytest.raises(BedrockNonProdRefusedError):
            assert_bedrock_is_pinned(body.backend, "bedrock", app_env=None)

    def test_downgraded_call_passes_the_gate(self):
        """Die Herunterstufung muss das Gate auch wirklich passieren —
        sonst haetten wir 503 nur gegen 403 getauscht."""
        body = _body()
        pinned = apply_user_provider_override(body, BEDROCK_PIN, app_env="staging")
        assert_bedrock_is_pinned(body.backend, pinned, app_env="staging")

    @pytest.mark.parametrize("env", ["staging", "local", "prod", None])
    def test_anthropic_pin_is_unaffected(self, env):
        """Nur der Bedrock-Zweig kennt die Umgebung — ein anthropic-Pin
        verhaelt sich ueberall gleich."""
        body = _body()
        assert apply_user_provider_override(body, {"provider": "anthropic"}, app_env=env) == "anthropic"
        assert body.backend == BackendType.ANTHROPIC
