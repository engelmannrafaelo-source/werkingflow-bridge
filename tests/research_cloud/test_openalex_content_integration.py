"""Echter Live-Call gegen content.openalex.org — nur mit gesetztem OPENALEX_API_KEY.

    OPENALEX_API_KEY=... pytest tests/research_cloud/test_openalex_content_integration.py

Kostet echte Credits (100/Abruf, geteiltes Tagesbudget — siehe Modul-Docstring in
src/openalex_content.py). Deshalb bewusst nur 2 Werke, keine Schleife über viele IDs.
"""
import os

import pytest

from src import openalex_content as oc

API_KEY = os.environ.get("OPENALEX_API_KEY")

pytestmark = pytest.mark.skipif(
    not API_KEY,
    reason="Live-Test nur mit OPENALEX_API_KEY ausführen (kostet echte Credits)",
)

# Gemessen 03.09.2026: sauberer englischer Volltext (Physics-Review-Artikel).
WORK_ENGLISH = "W3038067977"
# Gemessen 03.09.2026: koreanisches Paper mit englischem OpenAlex-Abstract — Regressionsanker
# für die Sprach-Degradierung gegen die echte API (nicht nur gegen selbstgebautes TEI-Fixture).
WORK_NON_ENGLISH = "W2032996814"
# Existiert nicht im Content-Index -> muss 404/None liefern, kein Crash.
WORK_MISSING = "W9999999999999"


def test_live_englisches_werk_liefert_volltext():
    r = oc.fetch_fulltext(WORK_ENGLISH, API_KEY)

    assert r is not None
    assert r["kind"] == "fulltext"
    assert r["language"] == "en"
    assert len(r["text"]) > 500


def test_live_nicht_englisches_werk_degradiert_auf_abstract():
    r = oc.fetch_fulltext(WORK_NON_ENGLISH, API_KEY)

    assert r is not None
    assert r["kind"] == "abstract"
    assert r["degraded_to_abstract"] is True
    assert r["text"] == r["abstract"]


def test_live_fehlendes_werk_gibt_none_ohne_crash():
    assert oc.fetch_fulltext(WORK_MISSING, API_KEY) is None


def test_live_fetch_fulltexts_haelt_den_credit_deckel_ein():
    out = oc.fetch_fulltexts([WORK_ENGLISH, WORK_NON_ENGLISH, WORK_MISSING], API_KEY, max_fetches=1)

    assert list(out.keys()) == [WORK_ENGLISH]
