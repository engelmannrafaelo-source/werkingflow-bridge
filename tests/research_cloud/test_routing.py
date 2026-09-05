"""Tests for the pool-vs-cloud routing decision (src/research_cloud/routing.py).

Covers: feature flag off, unpinned+no-overflow, overflow-without-saturation,
overflow-with-saturation, explicit pin, the daily cap overriding both a pin
and an overflow-eligible request, and — since 05.09.2026 — the switched-off
lane refusing a pinned caller instead of serving them from the pool.
"""
from unittest.mock import AsyncMock, patch

import pytest

from src.research_cloud.routing import (
    ResearchCloudCapExceededError,
    ResearchCloudDisabledError,
    global_pin_defers_to_research_pin,
    resolve_research_cloud_routing,
)


def _patches(*, pinned=None, saturated=False, over_cap=False):
    return (
        patch(
            "src.routing.research_provider_override.get_user_research_pin",
            new=AsyncMock(return_value=pinned),
        ),
        patch(
            "src.research_cloud.pool_signal.is_worker_pool_saturated",
            return_value=saturated,
        ),
        patch(
            "src.research_cloud.cap.research_cloud_over_cap",
            new=AsyncMock(return_value=(over_cap, 0.0, 50.0)),
        ),
    )


@pytest.mark.asyncio
async def test_feature_disabled_never_routes_to_cloud(monkeypatch):
    """Lane aus, kein Pin: der Pool ist die Heimat des Aufrufers, kein Ersatz."""
    monkeypatch.delenv("RESEARCH_CLOUD_ENABLED", raising=False)
    p1, p2, p3 = _patches(pinned=None, saturated=True, over_cap=False)
    with p1, p2, p3:
        assert await resolve_research_cloud_routing("user-1", cloud_overflow=True) is False


@pytest.mark.asyncio
async def test_feature_disabled_refuses_a_pinned_caller(monkeypatch):
    """Rafael 05.09.2026: "Recherche deaktiviert" ist ein harter Fehler mit
    klarer Meldung — kein stilles Ausweichen auf die normalen Worker.

    Bis dahin gab der Flag-Check ein stummes False zurueck, BEVOR der Pin
    ueberhaupt gelesen wurde: ein Nutzer mit
    provider_config.research_provider='cloud' lief einfach auf dem
    Abo-Worker-Pool weiter — anderer Anbieter, anderes Kostenmodell, andere
    Datenschutzlage, kein Wort im Log. Genau die Ersetzung, die fuer den
    Cap-Pfad am 02.08.2026 schon abgeschafft wurde."""
    monkeypatch.delenv("RESEARCH_CLOUD_ENABLED", raising=False)
    p1, p2, p3 = _patches(pinned="cloud", saturated=True, over_cap=False)
    with p1, p2, p3:
        with pytest.raises(ResearchCloudDisabledError) as exc:
            await resolve_research_cloud_routing("user-1", cloud_overflow=True)

    text = str(exc.value)
    assert "RESEARCH_CLOUD_ENABLED" in text, "die Meldung nennt den Schalter nicht"
    assert "pinned" in text, "die Meldung nennt den Grund nicht"


@pytest.mark.asyncio
async def test_feature_disabled_still_serves_a_bedrock_pinned_user_from_the_pool(monkeypatch):
    """Gegenprobe: implicit_pin ist KEIN Cloud-Pin. Er heisst "Bedrock kann
    Recherche gar nicht, nimm die Cloud wenn es sie gibt" — der Pool war schon
    immer die dokumentierte Antwort, wenn es sie nicht gibt. Aus dieser Gruppe
    einen harten Fehler zu machen wuerde Produktions-Nutzern die Recherche
    abdrehen, sobald jemand den Schalter umlegt."""
    monkeypatch.delenv("RESEARCH_CLOUD_ENABLED", raising=False)
    p1, p2, p3 = _patches(pinned=None, saturated=True, over_cap=False)
    with p1, p2, p3:
        assert await resolve_research_cloud_routing(
            "user-1", cloud_overflow=True, implicit_pin=True
        ) is False


@pytest.mark.asyncio
async def test_unpinned_no_overflow_stays_on_pool(monkeypatch):
    monkeypatch.setenv("RESEARCH_CLOUD_ENABLED", "true")
    p1, p2, p3 = _patches(pinned=None, saturated=False, over_cap=False)
    with p1, p2, p3:
        assert await resolve_research_cloud_routing("user-1", cloud_overflow=False) is False


@pytest.mark.asyncio
async def test_overflow_requested_but_pool_not_saturated_stays_on_pool(monkeypatch):
    monkeypatch.setenv("RESEARCH_CLOUD_ENABLED", "true")
    p1, p2, p3 = _patches(pinned=None, saturated=False, over_cap=False)
    with p1, p2, p3:
        assert await resolve_research_cloud_routing("user-1", cloud_overflow=True) is False


@pytest.mark.asyncio
async def test_overflow_requested_and_pool_saturated_routes_to_cloud(monkeypatch):
    monkeypatch.setenv("RESEARCH_CLOUD_ENABLED", "true")
    p1, p2, p3 = _patches(pinned=None, saturated=True, over_cap=False)
    with p1, p2, p3:
        assert await resolve_research_cloud_routing("user-1", cloud_overflow=True) is True


@pytest.mark.asyncio
async def test_explicit_pin_routes_to_cloud_without_overflow_flag(monkeypatch):
    monkeypatch.setenv("RESEARCH_CLOUD_ENABLED", "true")
    p1, p2, p3 = _patches(pinned="cloud", saturated=False, over_cap=False)
    with p1, p2, p3:
        assert await resolve_research_cloud_routing("user-1", cloud_overflow=False) is True


@pytest.mark.asyncio
async def test_daily_cap_defers_explicit_pin_instead_of_falling_back(monkeypatch):
    """Over cap while pinned to cloud MUST raise, not silently return to the
    pool (Rafael 2026-08-02: no silent provider swap) — the pin is a
    compliance/preference commitment the pool cannot legitimately substitute."""
    monkeypatch.setenv("RESEARCH_CLOUD_ENABLED", "true")
    p1, p2, p3 = _patches(pinned="cloud", saturated=False, over_cap=True)
    with p1, p2, p3:
        with pytest.raises(ResearchCloudCapExceededError) as ei:
            await resolve_research_cloud_routing("user-1", cloud_overflow=False)
    assert ei.value.spent_eur == 0.0
    assert ei.value.cap_eur == 50.0


@pytest.mark.asyncio
async def test_daily_cap_defers_overflow_eligibility_instead_of_falling_back(monkeypatch):
    monkeypatch.setenv("RESEARCH_CLOUD_ENABLED", "true")
    p1, p2, p3 = _patches(pinned=None, saturated=True, over_cap=True)
    with p1, p2, p3:
        with pytest.raises(ResearchCloudCapExceededError):
            await resolve_research_cloud_routing("user-1", cloud_overflow=True)


# ---------------------------------------------------------------------------
# Erreichbarkeit: bei welchem globalen Pin wird ueber die Cloud ueberhaupt
# entschieden (Befund 05.09.2026)
# ---------------------------------------------------------------------------


def test_no_global_pin_defers_to_the_research_pin():
    assert global_pin_defers_to_research_pin(None) is True


def test_anthropic_pin_defers_to_the_research_pin():
    """Der Kern des Befunds: der Handler fragte die Cloud-Entscheidung nur bei
    global_pin IS NONE. "anthropic" entsteht aber regulaer — ein Bedrock-Pin
    ausserhalb prod wird darauf heruntergestuft, und eine App-Regel kann ihn
    direkt setzen. Diese Aufrufer liefen stumm am research_provider='cloud'-Pin
    vorbei. "anthropic" sagt ueber Pool-vs-Cloud auch gar nichts: die
    Recherche-Cloud IST Anthropic."""
    assert global_pin_defers_to_research_pin("anthropic") is True


def test_bedrock_pin_does_not_defer():
    """Bedrock hat seinen eigenen Zweig im Handler (Recherche kann dort gar
    nicht laufen — kein WebSearch). Wuerde er hier mitlaufen, gaebe es zwei
    Stellen, die dieselbe Entscheidung treffen."""
    assert global_pin_defers_to_research_pin("bedrock") is False


def test_an_unknown_pin_does_not_silently_defer():
    """Ein dritter Provider darf nicht durch Zufall in den Cloud-Pfad
    rutschen — der Handler meldet ihn stattdessen laut."""
    assert global_pin_defers_to_research_pin("some-future-provider") is False
