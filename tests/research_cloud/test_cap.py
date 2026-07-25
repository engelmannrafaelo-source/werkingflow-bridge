"""Tests for the research-cloud daily spend cap (src/research_cloud/cap.py).

Mirrors src/routing/prepaid_cap.py's contract: fail-open on disabled/DB-error,
(over_cap, spent, cap) tuple otherwise. Covers under-cap, at/over-cap, and the
fail-open error path.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.research_cloud.cap as cap


@pytest.fixture(autouse=True)
def _reset_cache():
    cap._cache["at"] = 0.0
    cap._cache["spent"] = 0.0
    yield
    cap._cache["at"] = 0.0
    cap._cache["spent"] = 0.0


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
    monkeypatch.delenv("RESEARCH_CLOUD_DAILY_CAP_ENABLED", raising=False)
    over, spent, cap_eur = await cap.research_cloud_over_cap()
    assert over is False
    assert spent == 0.0
    assert cap_eur == 50.0  # default


@pytest.mark.asyncio
async def test_under_cap_allows_cloud_routing(monkeypatch):
    monkeypatch.setenv("RESEARCH_CLOUD_DAILY_CAP_ENABLED", "true")
    monkeypatch.setenv("RESEARCH_CLOUD_DAILY_CAP_EUR", "50")
    pool = _mock_pool(10.0)
    with patch("src.db.client.get_pool", return_value=pool), patch(
        "src.db.client.is_db_enabled", return_value=True
    ):
        over, spent, cap_eur = await cap.research_cloud_over_cap()
    assert over is False
    assert spent == 10.0
    assert cap_eur == 50.0


@pytest.mark.asyncio
async def test_at_or_over_cap_blocks_cloud_routing(monkeypatch):
    monkeypatch.setenv("RESEARCH_CLOUD_DAILY_CAP_ENABLED", "true")
    monkeypatch.setenv("RESEARCH_CLOUD_DAILY_CAP_EUR", "50")
    pool = _mock_pool(50.0)  # exactly at the cap
    with patch("src.db.client.get_pool", return_value=pool), patch(
        "src.db.client.is_db_enabled", return_value=True
    ):
        over, spent, cap_eur = await cap.research_cloud_over_cap()
    assert over is True
    assert spent == 50.0


@pytest.mark.asyncio
async def test_db_disabled_fails_open(monkeypatch):
    monkeypatch.setenv("RESEARCH_CLOUD_DAILY_CAP_ENABLED", "true")
    with patch("src.db.client.is_db_enabled", return_value=False):
        over, spent, cap_eur = await cap.research_cloud_over_cap()
    assert over is False
    assert spent == 0.0


@pytest.mark.asyncio
async def test_db_error_fails_open(monkeypatch):
    monkeypatch.setenv("RESEARCH_CLOUD_DAILY_CAP_ENABLED", "true")
    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        raise RuntimeError("connection refused")
        yield  # pragma: no cover — unreachable, satisfies generator syntax

    pool.acquire = _acquire
    with patch("src.db.client.get_pool", return_value=pool), patch(
        "src.db.client.is_db_enabled", return_value=True
    ):
        over, spent, cap_eur = await cap.research_cloud_over_cap()
    assert over is False
    assert spent == 0.0


@pytest.mark.asyncio
async def test_cache_avoids_requerying_within_ttl(monkeypatch):
    monkeypatch.setenv("RESEARCH_CLOUD_DAILY_CAP_ENABLED", "true")
    pool = _mock_pool(5.0)
    with patch("src.db.client.get_pool", return_value=pool) as get_pool, patch(
        "src.db.client.is_db_enabled", return_value=True
    ):
        await cap.research_cloud_over_cap()
        await cap.research_cloud_over_cap()
    assert get_pool.call_count == 1
