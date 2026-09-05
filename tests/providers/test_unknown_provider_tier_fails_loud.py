"""Ein unbekannter provider_tier darf nicht still den Default liefern (2026-09-05).

Die Zusicherungen pruefen VERHALTEN, nicht Objektidentitaet: unter pytest wird
src.providers.registry je nach Testreihenfolge mehrfach geladen, dann sind zwei
ProviderConfig-Instanzen gleich, aber nicht dasselbe Objekt (`is` schlaegt fehl),
und `UnknownProviderTierError` aus zwei Modulinstanzen ist nicht dieselbe Klasse.
"""
import pytest

from src.providers.registry import DEFAULT_TIER, PROVIDERS, get_provider


def test_no_tier_still_returns_the_default():
    """Kein Tier = keine Meinung des Aufrufers -> Default bleibt richtig."""
    assert get_provider(None).tier_id == DEFAULT_TIER
    assert get_provider("").tier_id == DEFAULT_TIER


def test_known_tier_is_returned_unchanged():
    tier = next(iter(PROVIDERS))
    assert get_provider(tier).tier_id == tier


def test_unknown_tier_raises_instead_of_serving_the_default():
    """Der gemessene Fall: werking-energy schickte 'gemini-vision', die Lane
    existiert auf dieser Bridge nicht — und jeder Aufruf bekam claude-sonnet-5
    mit HTTP 200 zurueck."""
    with pytest.raises(RuntimeError) as excinfo:
        get_provider("gemini-vision")
    msg = str(excinfo.value)
    assert "gemini-vision" in msg
    assert DEFAULT_TIER in msg  # nennt, was es NICHT stillschweigend getan hat
    assert type(excinfo.value).__name__ == "UnknownProviderTierError"


def test_error_is_a_runtimeerror_so_the_handlers_map_it_to_400():
    """main.py:2541 / :5302 fangen RuntimeError und antworten 400/503. Eine
    ValueError-Basis wuerde daran vorbeilaufen und 500 erzeugen."""
    from src.providers.registry import UnknownProviderTierError
    assert issubclass(UnknownProviderTierError, RuntimeError)
