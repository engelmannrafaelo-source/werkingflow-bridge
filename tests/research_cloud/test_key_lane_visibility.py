"""Der geteilte Schluessel darf nicht stumm bleiben.

Gemessen 04.09.2026: ANTHROPIC_VISION_API_KEY und RESEARCH_CLOUD_API_KEY waren
derselbe Schluessel unter zwei Namen (sha256 identisch, alle vier dev-Worker).
Folge: ein einzelner Recherche-Lauf kann die Bild-Lane leeren, waehrend die
Budget-Anzeige noch Luft zeigt. RESEARCH_CLOUD_ANTHROPIC_KEY trennt die
beiden; diese Zeile ist der Beleg, ob die Trennung im Worker angekommen ist —
gemessen am Vergleich der beiden Werte, nicht am Variablennamen.
"""
import logging

import pytest

from src.research_cloud import executor as ex


@pytest.fixture(autouse=True)
def _reset_once_flag():
    ex._key_lane_logged = False
    yield
    ex._key_lane_logged = False


def test_shared_key_is_reported_as_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_VISION_API_KEY", "sk-ant-same")
    with caplog.at_level(logging.WARNING, logger=ex.logger.name):
        ex._log_key_lane_once("sk-ant-same")

    assert any(
        "SHARED with the image lane" in r.getMessage() for r in caplog.records
    ), "geteilter Topf blieb unsichtbar"


def test_dedicated_key_is_reported_as_such(monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_VISION_API_KEY", "sk-ant-vision")
    with caplog.at_level(logging.INFO, logger=ex.logger.name):
        ex._log_key_lane_once("sk-ant-research")

    messages = [r.getMessage() for r in caplog.records]
    assert any("dedicated" in m for m in messages)
    assert not any("SHARED" in m for m in messages)


def test_the_key_itself_is_never_logged(monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_VISION_API_KEY", "sk-ant-secret-value")
    with caplog.at_level(logging.INFO, logger=ex.logger.name):
        ex._log_key_lane_once("sk-ant-secret-value")

    assert not any("sk-ant-secret-value" in r.getMessage() for r in caplog.records)


def test_it_says_it_once_per_process(monkeypatch, caplog):
    """Pro Lauf eine Zeile waere Rauschen — die Antwort aendert sich nur mit
    einem Container-Neustart."""
    monkeypatch.setenv("ANTHROPIC_VISION_API_KEY", "sk-ant-same")
    with caplog.at_level(logging.WARNING, logger=ex.logger.name):
        ex._log_key_lane_once("sk-ant-same")
        ex._log_key_lane_once("sk-ant-same")

    assert sum("SHARED" in r.getMessage() for r in caplog.records) == 1
