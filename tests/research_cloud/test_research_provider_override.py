"""Tests for the research-scoped provider pin (src/routing/research_provider_override.py).

Reuses user_provider_override.get_user_provider_config's DB read (same
users.provider_config column, different key) — mocked here rather than
duplicating a DB-mock story that test_ledger_real_cost.py etc. already cover.
"""
from unittest.mock import AsyncMock, patch

import pytest

from src.routing.research_provider_override import (
    ResearchProviderOverrideError,
    get_user_research_pin,
)


@pytest.mark.asyncio
async def test_no_config_returns_none():
    with patch(
        "src.routing.user_provider_override.get_user_provider_config",
        new=AsyncMock(return_value=None),
    ):
        assert await get_user_research_pin("user-1") is None


@pytest.mark.asyncio
async def test_config_without_research_provider_key_returns_none():
    with patch(
        "src.routing.user_provider_override.get_user_provider_config",
        new=AsyncMock(return_value={"provider": "bedrock"}),
    ):
        assert await get_user_research_pin("user-1") is None


@pytest.mark.asyncio
async def test_cloud_pin_returns_cloud():
    with patch(
        "src.routing.user_provider_override.get_user_provider_config",
        new=AsyncMock(return_value={"research_provider": "cloud"}),
    ):
        assert await get_user_research_pin("user-1") == "cloud"


@pytest.mark.asyncio
async def test_unsupported_value_raises_loud():
    with patch(
        "src.routing.user_provider_override.get_user_provider_config",
        new=AsyncMock(return_value={"research_provider": "bogus-typo"}),
    ):
        with pytest.raises(ResearchProviderOverrideError, match="bogus-typo"):
            await get_user_research_pin("user-1")
