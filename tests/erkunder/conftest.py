"""Synthetic reports only; never copy customer data into tests."""

import pytest


@pytest.fixture
def auftrag():
    return {
        "schema": "erkunder-auftrag/1",
        "bericht_id": "bericht-test-123",
        "gegenstand": "Testanlage",
        "datenstand": {
            "von": "2026-01-01",
            "bis": "2026-01-02",
            "heute": "2026-01-03",
        },
        "auftrag": None,
        "zweck": "Betriebsprüfung",
        "vorwissen_md": "Testwissen",
        "vertiefung_md": None,
        "dateien": [],
        "korrekturkreis": 1,
    }
