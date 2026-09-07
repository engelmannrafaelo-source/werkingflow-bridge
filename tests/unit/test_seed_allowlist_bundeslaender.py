"""
SEED_ALLOWLIST: Bundesländer und Staaten (AT/DE) für den "Rechtsraum:"-Prefix.

Kontext: report/energy stellen der KI-Recherche-Query künftig eine Zeile
"Rechtsraum: <Staat>, Bundesland <Bundesland>." voran (Guide-Karte
k17-bundesland-rechtsraum-recherche, 2026-09-07). Damit Presidio diese Werte
nicht als LOCATION maskiert, müssen sie exakt in SEED_ALLOWLIST stehen —
Presidio matcht dort nur exakte Spans (keine Flexionsformen).

Keine echten Presidio/Flair-Modelle nötig: anonymizer.py importiert Presidio
lazy (nur innerhalb von Funktionen), das Modul selbst ist ohne Presidio-Install
importierbar. Der zweite Test (Wiring) mockt `_get_analyzer` wie die übrigen
Tests in test_smart_anonymizer_consistency.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.privacy.anonymizer import SEED_ALLOWLIST, PresidioAnonymizer

REQUIRED_STATES_AND_COUNTRIES = [
    # Staaten
    "Österreich",
    "Deutschland",
    "Schweiz",
    # Österreichische Bundesländer
    "Wien",
    "Niederösterreich",
    "Oberösterreich",
    "Salzburg",
    "Tirol",
    "Vorarlberg",
    "Burgenland",
    "Steiermark",
    "Kärnten",
    # Deutsche Bundesländer
    "Baden-Württemberg",
    "Bayern",
    "Berlin",
    "Brandenburg",
    "Bremen",
    "Hamburg",
    "Hessen",
    "Mecklenburg-Vorpommern",
    "Niedersachsen",
    "Nordrhein-Westfalen",
    "Rheinland-Pfalz",
    "Saarland",
    "Sachsen",
    "Sachsen-Anhalt",
    "Schleswig-Holstein",
    "Thüringen",
]

# These names are simultaneously a Bundesland AND a city/Gemeinde. Allow-listing
# them for the Bundesland use case is a deliberate, accepted tradeoff (Rafael,
# 2026-09-07): it also un-masks the same string when it occurs as a city
# reference. A state/city name alone is not PII, unlike address/PLZ/street.
DUAL_CITY_AND_STATE_NAMES = {"Wien", "Salzburg", "Bremen", "Hamburg", "Berlin"}


def test_all_required_states_and_countries_present():
    missing = [name for name in REQUIRED_STATES_AND_COUNTRIES if name not in SEED_ALLOWLIST]
    assert not missing, f"Fehlende Bundesländer/Staaten in SEED_ALLOWLIST: {missing}"


def test_no_duplicates_in_allowlist():
    assert len(SEED_ALLOWLIST) == len(set(SEED_ALLOWLIST)), "SEED_ALLOWLIST enthält Duplikate"


def test_dual_city_state_names_are_the_documented_set():
    """Regression guard: if someone adds another dual-purpose name (or removes
    one of these), the tradeoff note in anonymizer.py needs to be revisited."""
    dual_in_list = DUAL_CITY_AND_STATE_NAMES & set(SEED_ALLOWLIST)
    assert dual_in_list == DUAL_CITY_AND_STATE_NAMES


def test_address_like_terms_are_not_in_allowlist():
    """Adresse/PLZ/Straße/Stadt bleiben maskiert — nur Bundesland/Staat sind neu erlaubt.
    Spot-check gegen ein paar Städte, die NICHT gleichzeitig Bundesland sind."""
    for city in ("Innsbruck", "Graz", "München", "Musterstraße", "6020"):
        assert city not in SEED_ALLOWLIST


def test_allow_list_passed_through_to_presidio_analyze():
    """Wiring check: PresidioAnonymizer.anonymize() must pass the (full, updated)
    SEED_ALLOWLIST into analyzer.analyze() unchanged — same pattern as the
    overlap-resolution tests in test_smart_anonymizer_consistency.py."""
    anon = PresidioAnonymizer(language="de")

    with patch.object(anon, "_get_analyzer") as mock_analyzer:
        engine = MagicMock()
        engine.analyze.return_value = []
        mock_analyzer.return_value = engine
        anon.anonymize("Rechtsraum: Österreich, Bundesland Tirol.", "de")

    _, kwargs = engine.analyze.call_args
    assert kwargs["allow_list"] == SEED_ALLOWLIST
    for name in ("Österreich", "Tirol"):
        assert name in kwargs["allow_list"]
