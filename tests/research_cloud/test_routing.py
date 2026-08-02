"""Tests for the pool-vs-cloud routing decision (src/research_cloud/routing.py).

Covers: feature flag off, unpinned+no-overflow, overflow-without-saturation,
overflow-with-saturation, explicit pin, and the daily cap overriding both a
pin and an overflow-eligible request.
"""
from unittest.mock import AsyncMock, patch

import pytest

from src.research_cloud.routing import (
    ResearchCloudCapExceededError,
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
    monkeypatch.delenv("RESEARCH_CLOUD_ENABLED", raising=False)
    p1, p2, p3 = _patches(pinned="cloud", saturated=True, over_cap=False)
    with p1, p2, p3:
        assert await resolve_research_cloud_routing("user-1", cloud_overflow=True) is False


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
