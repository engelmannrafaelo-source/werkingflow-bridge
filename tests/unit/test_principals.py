"""Tests for src/principals.py's resolution path (ADR-0009 Schritt 2a, C2).

Covers: cache hit skips the network entirely, platform-api 200/404 answers
(incl. caching a "not found" result), the PlatformUnavailable fallback to
direct DB, an unexpected platform-api response also falling back, and the
fail-loud raise when NEITHER channel can answer. Admin CRUD (create/rotate/
list) is untouched by 2a and not covered here.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.principals as principals
from src.platform_client import PlatformResponse, PlatformUnavailable

TOKEN = "sk-test-token-abc"
TOKEN_HASH = principals.hash_token(TOKEN)

PRINCIPAL_JSON = {
    "id": "11111111-1111-1111-1111-111111111111",
    "name": "engelmann",
    "allowed_apps": ["engelmann"],
    "allowed_paths": ["*"],
    "monthly_cap_eur": 50.0,
}


@pytest.fixture(autouse=True)
def _reset_cache():
    principals.invalidate_cache()
    yield
    principals.invalidate_cache()


def _mock_pool(row):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


class _FakeRow(dict):
    """asyncpg Record supports __getitem__ by column name — a dict is enough."""


@pytest.mark.asyncio
async def test_resolves_via_platform_api_on_200():
    mock_call = AsyncMock(return_value=PlatformResponse(status_code=200, json={"principal": PRINCIPAL_JSON}))
    with patch.object(principals, "call_platform", mock_call):
        result = await principals.resolve_principal_by_token(TOKEN)

    mock_call.assert_awaited_once_with("GET", f"/v1/internal/principals/{TOKEN_HASH}")
    assert result is not None
    assert result.name == "engelmann"
    assert result.allowed_apps == ["engelmann"]
    assert result.monthly_cap_eur == 50.0


@pytest.mark.asyncio
async def test_null_principal_resolves_to_none_and_is_cached():
    mock_call = AsyncMock(return_value=PlatformResponse(status_code=200, json={"principal": None}))
    with patch.object(principals, "call_platform", mock_call):
        first = await principals.resolve_principal_by_token(TOKEN)
        second = await principals.resolve_principal_by_token(TOKEN)

    assert first is None
    assert second is None
    mock_call.assert_awaited_once()  # second call served from cache


@pytest.mark.asyncio
async def test_cache_hit_never_calls_platform_or_db():
    principals._cache_put(TOKEN_HASH, principals.LEGACY_PRINCIPAL)
    mock_call = AsyncMock(side_effect=AssertionError("must not call platform-api on a cache hit"))
    with patch.object(principals, "call_platform", mock_call):
        result = await principals.resolve_principal_by_token(TOKEN)

    assert result is principals.LEGACY_PRINCIPAL
    mock_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_platform_unavailable_falls_back_to_direct_db():
    mock_call = AsyncMock(side_effect=PlatformUnavailable("platform-api down"))
    row = _FakeRow(
        id="11111111-1111-1111-1111-111111111111",
        name="engelmann",
        allowed_apps=["engelmann"],
        allowed_paths=["*"],
        monthly_cap_eur=50.0,
    )
    pool = _mock_pool(row)
    with patch.object(principals, "call_platform", mock_call), patch.object(
        principals, "get_pool", return_value=pool
    ), patch.object(principals, "is_db_enabled", return_value=True):
        result = await principals.resolve_principal_by_token(TOKEN)

    assert result is not None
    assert result.name == "engelmann"


@pytest.mark.asyncio
async def test_unexpected_platform_response_falls_back_to_direct_db():
    mock_call = AsyncMock(return_value=PlatformResponse(status_code=500, json=None))
    pool = _mock_pool(None)
    with patch.object(principals, "call_platform", mock_call), patch.object(
        principals, "get_pool", return_value=pool
    ), patch.object(principals, "is_db_enabled", return_value=True):
        result = await principals.resolve_principal_by_token(TOKEN)

    assert result is None  # DB fallback found no row either — a real "no principal"


@pytest.mark.asyncio
async def test_both_channels_unavailable_raises_not_none():
    mock_call = AsyncMock(side_effect=PlatformUnavailable("platform-api down"))
    with patch.object(principals, "call_platform", mock_call), patch.object(
        principals, "is_db_enabled", return_value=False
    ):
        with pytest.raises(RuntimeError):
            await principals.resolve_principal_by_token(TOKEN)


@pytest.mark.asyncio
async def test_get_principal_row_by_hash_is_pure_db_read():
    row = _FakeRow(
        id="22222222-2222-2222-2222-222222222222",
        name="werking-report",
        allowed_apps=["werking-report"],
        allowed_paths=["*"],
        monthly_cap_eur=None,
    )
    pool = _mock_pool(row)
    with patch.object(principals, "get_pool", return_value=pool):
        result = await principals.get_principal_row_by_hash("some-hash")

    assert result == {
        "id": "22222222-2222-2222-2222-222222222222",
        "name": "werking-report",
        "allowed_apps": ["werking-report"],
        "allowed_paths": ["*"],
        "monthly_cap_eur": None,
    }


@pytest.mark.asyncio
async def test_get_principal_row_by_hash_returns_none_on_no_row():
    pool = _mock_pool(None)
    with patch.object(principals, "get_pool", return_value=pool):
        result = await principals.get_principal_row_by_hash("unknown-hash")

    assert result is None


@pytest.mark.asyncio
async def test_404_is_a_missing_route_and_falls_back_to_the_db():
    """REGRESSION (2026-08-20, auf der dev-Bridge gemessen): ein noch nicht
    deploytes platform-api antwortet auf /v1/internal/* mit 404. Da 404 eine
    gewoehnliche Antwort ist und KEIN PlatformUnavailable, wuerde "kein
    Principal" daraus jeden Aufrufer abweisen — und die Abweisung auch noch
    cachen — statt auf die DB zurueckzufallen."""
    mock_call = AsyncMock(return_value=PlatformResponse(status_code=404, json={"detail": "Not Found"}))
    with patch.object(principals, "call_platform", mock_call), \
         patch.object(principals, "_resolve_via_direct_db",
                      new=AsyncMock(return_value="SENTINEL")) as direct:
        result = await principals.resolve_principal_by_token(TOKEN)
    assert result == "SENTINEL"
    direct.assert_awaited_once()
