"""Ein leerer Prepaid-Bildschluessel darf nicht wie eine Arbeitspause aussehen.

Befund DevOps 01.09.2026: Ein 402 vision_billing_exhausted erzeugt gar keine
usage_events-Zeile — der Erfolgspfad wird nie erreicht. Im Ledger ist
Erschoepfung damit von STILLE nicht zu unterscheiden. Genau deshalb schickt
vision-prepaid-budget-guard.py bis heute alle paar Stunden einen echten
Bild-Aufruf gegen die PROD-Bridge, nur um eine Stille zu BESTAETIGEN.

Hier steht der Vertrag, auf dem der Ersatz dafuer aufsetzt: die Erkennung des
Zustands ist EINE Liste, die zwei Leser teilen — der Fehler-Klassifizierer
(was der Aufrufer sieht: 402, nicht wiederholbar) und der Ledger-Schreibpfad
(dass es ueberhaupt eine Spur gibt).
"""
from __future__ import annotations

import json

import pytest

from src.middleware.bridge_error import (
    REASON_VISION_BILLING_EXHAUSTED,
    VISION_BILLING_EXHAUSTED_MARKERS,
    classify_exception,
    is_vision_billing_exhausted,
)


@pytest.mark.parametrize("text", [
    "Your credit balance is too low to access the Anthropic API.",
    "Please go to Plans & Billing to upgrade or purchase credits.",
    "insufficient_credit",
    "YOUR CREDIT BALANCE IS TOO LOW",  # Gross-/Kleinschreibung darf nichts aendern
])
def test_known_exhaustion_texts_are_recognised(text):
    assert is_vision_billing_exhausted(text) is True


@pytest.mark.parametrize("text", [
    "",
    "rate_limit_error: too many requests",
    "invalid_request_error: unknown model",
    "Internal server error",
])
def test_unrelated_errors_are_not_mistaken_for_exhaustion(text):
    assert is_vision_billing_exhausted(text) is False


def test_none_is_not_an_exhaustion():
    """Ein fehlender Fehlertext ist "weiss nicht", nicht "Guthaben leer" — ein
    Fehlalarm hier wuerde einen Alarm ueber leeres Guthaben ausloesen."""
    assert is_vision_billing_exhausted(None) is False


@pytest.mark.parametrize("marker", VISION_BILLING_EXHAUSTED_MARKERS)
def test_classifier_and_predicate_stay_on_the_same_list(marker):
    """Der Klassifizierer benutzt dieselbe Liste wie der Ledger-Schreibpfad.
    Driften die beiden auseinander, bekommt der Aufrufer ein 402 und das
    Ledger schweigt weiter — der Ausgangszustand."""
    response = classify_exception(Exception(f"Anthropic error: {marker}"))

    assert response.status_code == 402
    body = json.loads(response.body)
    assert body["error"]["reason"] == REASON_VISION_BILLING_EXHAUSTED
    assert body["error"]["retryable"] is False, (
        "Wiederholen hilft bei leerem Guthaben nie — es verbrennt nur Zeit"
    )
