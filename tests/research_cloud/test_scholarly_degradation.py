"""Tests für die OA-Schicht: Timeout-Budget und die EBENE der Degradierungs-Meldung.

Hintergrund (Messung 20.08.2026): `_core()` läuft einmal pro geplanter Suchquery (8-14 parallel
je Research-Lauf). Jede getimeoutete Teil-Query hat bisher eine WARNING mit Alarm-Marker
geloggt — 354 Zeilen in 3 Wochen, obwohl die Läufe fast immer trotzdem 12/12 Volltexte hatten.
Gemeldet werden darf deshalb nur die Lauf-Ebene ("davon 0 Volltext"), nicht die Teil-Query.

Zweite Hälfte: CORE-Antworten sind 1.2-2.3 MB und brauchen 7-12s — mit dem OpenAlex-Timeout von
15s lag der Abbruch im Normalbetrieb. CORE bekommt deshalb ein eigenes, größeres Budget.
"""
import logging

import pytest
import requests

from src import scholarly

# Marker, auf die orchestrator/bin/research-cloud-monitor.py die Inbox-Alarme baut.
RUN_LEVEL_MARKER = "davon 0 Volltext"
RETIRED_MARKER = "research-cloud: CORE request failed"


def _paper(kind: str, title: str) -> dict:
    return {"title": title, "year": 2024, "doi": f"10.1/{title}", "url": None,
            "text": f"text-{title}", "kind": kind, "source": "CORE" if kind == "fulltext" else "OpenAlex"}


def test_core_bekommt_groesseres_timeout_als_openalex(monkeypatch):
    """CORE-Volltext-Payloads brauchen mehr als das OpenAlex-Budget."""
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        seen["timeout"] = kw.get("timeout")
        raise requests.ReadTimeout("boom")

    monkeypatch.setattr(scholarly, "_get", fake_get)
    scholarly._core("query", 12)

    assert seen["timeout"] == scholarly._CORE_HTTP_TIMEOUT
    assert scholarly._CORE_HTTP_TIMEOUT > scholarly._HTTP_TIMEOUT


def test_teilquery_timeout_ist_kein_alarm(monkeypatch, caplog):
    """Eine ausgefallene Teil-Query darf keinen Inbox-Alarm auslösen (INFO, kein Marker)."""
    monkeypatch.setattr(scholarly, "_get",
                        lambda url, **kw: (_ for _ in ()).throw(requests.ReadTimeout("read timed out")))

    with caplog.at_level(logging.INFO, logger="scholarly"):
        assert scholarly._core("query", 12) == []

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert RETIRED_MARKER not in caplog.text
    assert "CORE-Teilquery ohne Ergebnis" in caplog.text  # sichtbar bleibt es trotzdem


def test_rate_limit_bleibt_alarm(monkeypatch, caplog):
    """429 heißt: der CORE_API_KEY greift nicht — das ist handlungsrelevant, bleibt WARNING."""
    class _R:
        status_code = 429

    monkeypatch.setattr(scholarly, "_get", lambda url, **kw: _R())

    with caplog.at_level(logging.INFO, logger="scholarly"):
        assert scholarly._core("query", 12) == []

    assert "research-cloud: CORE rate-limited" in caplog.text
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_lauf_ohne_volltext_meldet_alarm(caplog):
    """Nur wenn der ganze Lauf auf Abstracts fällt, gibt es eine WARNING mit Monitor-Marker."""
    papers = [_paper("abstract", f"a{i}") for i in range(3)]

    with caplog.at_level(logging.INFO, logger="scholarly"):
        block = _format(papers)

    assert RUN_LEVEL_MARKER in caplog.text
    assert any(r.levelno >= logging.WARNING for r in caplog.records)
    assert block  # Kontext-Block wird trotzdem geliefert (fail-soft, Abstracts sind besser als nichts)


def test_lauf_mit_volltext_meldet_nichts(caplog):
    papers = [_paper("fulltext", f"f{i}") for i in range(3)]

    with caplog.at_level(logging.INFO, logger="scholarly"):
        _format(papers)

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert "davon 3 Volltext" in caplog.text


def _format(papers):
    """_retrieve_and_format über einen gestubbten Fetch fahren (kein Netz)."""
    import src.scholarly as sc
    orig_openalex, orig_core = sc._openalex, sc._core
    sc._openalex = lambda q, n: papers
    sc._core = lambda q, n: []
    try:
        return sc._retrieve_and_format(["q1"], 12)
    finally:
        sc._openalex, sc._core = orig_openalex, orig_core
