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


#: Ein Tier, den es hier absichtlich NICHT gibt. Frueher stand hier
#: "gemini-vision" — der gemessene Fall vom 05.09.2026, als werking-energy
#: genau diesen Tier schickte und still claude-sonnet-5 bekam. Seit dem Merge
#: des Gemini-Bildwegs EXISTIERT dieser Tier, und der Test haette ab da das
#: Gegenteil dessen geprueft, was er behauptet (er wurde gruen, weil der Tier
#: bekannt ist — nicht, weil die Absage funktioniert). Der Beispielname muss
#: deshalb einer bleiben, den diese Bridge nie ausliefert; die Zusicherung
#: unten haelt das nach.
NONEXISTENT_TIER = "kein-solcher-tier-2026-09"


def test_unknown_tier_raises_instead_of_serving_the_default():
    """Der gemessene Fall (05.09.2026): werking-energy schickte einen Tier, den
    die ausgerollte Bridge nicht kannte — und jeder Aufruf bekam claude-sonnet-5
    mit HTTP 200 zurueck."""
    assert NONEXISTENT_TIER not in PROVIDERS, (
        "Der Beispiel-Tier dieses Tests ist real geworden. Dann prueft der Test "
        "nichts mehr — neuen, nicht existierenden Namen waehlen."
    )
    with pytest.raises(RuntimeError) as excinfo:
        get_provider(NONEXISTENT_TIER)
    msg = str(excinfo.value)
    assert NONEXISTENT_TIER in msg
    assert DEFAULT_TIER in msg  # nennt, was es NICHT stillschweigend getan hat
    assert type(excinfo.value).__name__ == "UnknownProviderTierError"


def test_error_is_a_runtimeerror_so_the_handlers_map_it_to_400():
    """main.py:2541 / :5302 fangen RuntimeError und antworten 400/503. Eine
    ValueError-Basis wuerde daran vorbeilaufen und 500 erzeugen."""
    from src.providers.registry import UnknownProviderTierError
    assert issubclass(UnknownProviderTierError, RuntimeError)
