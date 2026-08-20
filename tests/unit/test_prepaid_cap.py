"""Tests for src/routing/prepaid_cap.py (ADR-0009 Schritt 2a, C6).

Covers the pre-existing fail-open cap contract PLUS the new resolution path:
platform-api first, falling back to the direct DB query in the same call on
PlatformUnavailable — mirrors src/principals.py's C2 pattern. The outer
fail-open behaviour (prepaid_vision_over_cap catching any exception) is
untouched and still the last line of defense.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.routing.prepaid_cap as prepaid_cap
from src.platform_client import PlatformResponse, PlatformUnavailable


@pytest.fixture(autouse=True)
def _reset_cache():
    prepaid_cap._cache["at"] = 0.0
    prepaid_cap._cache["spent"] = 0.0
    yield
    prepaid_cap._cache["at"] = 0.0
    prepaid_cap._cache["spent"] = 0.0


def _mock_pool(spent_value):
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=spent_value)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


@pytest.mark.asyncio
async def test_disabled_by_default_is_fail_open(monkeypatch):
    monkeypatch.delenv("PREPAID_VISION_DAILY_CAP_ENABLED", raising=False)
    over, spent, cap_eur = await prepaid_cap.prepaid_vision_over_cap()
    assert over is False
    assert spent == 0.0
    assert cap_eur == 30.0  # default


@pytest.mark.asyncio
async def test_reads_via_platform_api_when_reachable(monkeypatch):
    monkeypatch.setenv("PREPAID_VISION_DAILY_CAP_ENABLED", "true")
    monkeypatch.setenv("PREPAID_VISION_DAILY_CAP_EUR", "30")

    mock_call = AsyncMock(return_value=PlatformResponse(status_code=200, json={"spent_eur": 12.5}))
    with patch("src.platform_client.call_platform", mock_call):
        over, spent, cap_eur = await prepaid_cap.prepaid_vision_over_cap()

    mock_call.assert_awaited_once_with("GET", "/v1/internal/prepaid-vision/spent-24h")
    assert over is False
    assert spent == 12.5
    assert cap_eur == 30.0


@pytest.mark.asyncio
async def test_over_cap_via_platform_api_blocks(monkeypatch):
    monkeypatch.setenv("PREPAID_VISION_DAILY_CAP_ENABLED", "true")
    monkeypatch.setenv("PREPAID_VISION_DAILY_CAP_EUR", "30")

    mock_call = AsyncMock(return_value=PlatformResponse(status_code=200, json={"spent_eur": 35.0}))
    with patch("src.platform_client.call_platform", mock_call):
        over, spent, cap_eur = await prepaid_cap.prepaid_vision_over_cap()

    assert over is True
    assert spent == 35.0


@pytest.mark.asyncio
async def test_platform_unavailable_falls_back_to_direct_db(monkeypatch):
    monkeypatch.setenv("PREPAID_VISION_DAILY_CAP_ENABLED", "true")
    monkeypatch.setenv("PREPAID_VISION_DAILY_CAP_EUR", "30")

    mock_call = AsyncMock(side_effect=PlatformUnavailable("platform-api down"))
    pool = _mock_pool(7.0)
    with patch("src.platform_client.call_platform", mock_call), patch(
        "src.db.client.get_pool", return_value=pool
    ), patch("src.db.client.is_db_enabled", return_value=True):
        over, spent, cap_eur = await prepaid_cap.prepaid_vision_over_cap()

    assert over is False
    assert spent == 7.0


@pytest.mark.asyncio
async def test_unexpected_platform_response_falls_back_to_direct_db(monkeypatch):
    monkeypatch.setenv("PREPAID_VISION_DAILY_CAP_ENABLED", "true")
    monkeypatch.setenv("PREPAID_VISION_DAILY_CAP_EUR", "30")

    mock_call = AsyncMock(return_value=PlatformResponse(status_code=500, json=None))
    pool = _mock_pool(3.0)
    with patch("src.platform_client.call_platform", mock_call), patch(
        "src.db.client.get_pool", return_value=pool
    ), patch("src.db.client.is_db_enabled", return_value=True):
        over, spent, cap_eur = await prepaid_cap.prepaid_vision_over_cap()

    assert spent == 3.0


@pytest.mark.asyncio
async def test_both_channels_failing_is_still_fail_open(monkeypatch):
    monkeypatch.setenv("PREPAID_VISION_DAILY_CAP_ENABLED", "true")
    monkeypatch.setenv("PREPAID_VISION_DAILY_CAP_EUR", "30")

    mock_call = AsyncMock(side_effect=PlatformUnavailable("platform-api down"))
    with patch("src.platform_client.call_platform", mock_call), patch(
        "src.db.client.is_db_enabled", return_value=False
    ):
        over, spent, cap_eur = await prepaid_cap.prepaid_vision_over_cap()

    # query_spent_last_24h_from_db raises RuntimeError when DB disabled too —
    # prepaid_vision_over_cap's own except-Exception is what fails this open.
    assert over is False
    assert spent == 0.0


@pytest.mark.asyncio
async def test_cache_avoids_second_platform_call(monkeypatch):
    monkeypatch.setenv("PREPAID_VISION_DAILY_CAP_ENABLED", "true")

    mock_call = AsyncMock(return_value=PlatformResponse(status_code=200, json={"spent_eur": 5.0}))
    with patch("src.platform_client.call_platform", mock_call):
        await prepaid_cap.prepaid_vision_over_cap()
        await prepaid_cap.prepaid_vision_over_cap()

    mock_call.assert_awaited_once()
