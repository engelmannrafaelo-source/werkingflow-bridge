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

from src.models import BackendType
from src.routing.user_provider_override import (
    BedrockAttributionIncompleteError,
    BedrockNonProdRefusedError,
    BedrockPinRequiredError,
    UserProviderOverrideError,
    assert_bedrock_attribution_complete,
    assert_bedrock_is_pinned,
)


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
