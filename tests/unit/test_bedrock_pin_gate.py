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
    BedrockPinRequiredError,
    UserProviderOverrideError,
    assert_bedrock_is_pinned,
)


class TestBedrockPinGate:
    def test_anthropic_backend_passes_without_pin(self):
        """Normalfall: keine Pin, normaler Backend — kein Eingriff."""
        assert_bedrock_is_pinned(BackendType.ANTHROPIC, None)

    def test_bedrock_with_pin_passes(self):
        """Der einzige legitime Bedrock-Weg: Operator-Pin."""
        assert_bedrock_is_pinned(BackendType.BEDROCK, "bedrock")

    def test_bedrock_without_pin_refused(self):
        """Client-Opt-in ohne Pin → 403-Klasse, kein silent-redirect."""
        with pytest.raises(BedrockPinRequiredError, match="operator-set per-user pin"):
            assert_bedrock_is_pinned(BackendType.BEDROCK, None)

    def test_bedrock_with_anthropic_pin_refused(self):
        """Explizit auf anthropic gepinnter User darf nicht per Request-Feld
        nach Bedrock ausweichen — der Pin ist eine Operator-Entscheidung."""
        with pytest.raises(BedrockPinRequiredError):
            assert_bedrock_is_pinned(BackendType.BEDROCK, "anthropic")

    def test_gate_is_not_the_503_class(self):
        """403 (Client darf das nicht) und 503 (Pin unservable) sind
        verschiedene Fehlerklassen — Handler mappen sie unterschiedlich."""
        assert not issubclass(BedrockPinRequiredError, UserProviderOverrideError)

    def test_openai_compatible_passes(self):
        assert_bedrock_is_pinned(BackendType.OPENAI_COMPATIBLE, None)
