"""Tests for src.main._execute_research_cloud_with_pool_fallback and the
implicit-pin routing (Rafael decisions 2026-07-27):

  - a production user never sees a cloud-path failure — a failed cloud run is
    automatically retried on the subscription pool (default backend, NOT the
    caller's original/Bedrock backend)
  - a successful cloud run never touches the pool
  - a globally Bedrock-pinned user routes to the cloud via implicit_pin
    without reading the research-scoped pin (research cannot run on Bedrock)
"""
import sys
from unittest.mock import AsyncMock, MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
    "src.identity.routes",
    "src.db.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

import src.main  # noqa: E402
from src.models import ResearchResponse  # noqa: E402
from src.research_cloud.routing import resolve_research_cloud_routing  # noqa: E402


def _resp(status: str, content: str = "", error: str = None) -> ResearchResponse:
    return ResearchResponse(
        status=status, query="q", model="claude-sonnet-5", content=content, error=error
    )


@pytest.mark.asyncio
async def test_cloud_success_never_touches_pool():
    cloud = AsyncMock(return_value=_resp("success", content="# Report"))
    pool = AsyncMock()
    with patch.object(src.main, "_execute_research_cloud_impl", cloud), \
         patch.object(src.main, "_execute_research_impl", pool):
        result = await src.main._execute_research_cloud_with_pool_fallback(
            MagicMock(), MagicMock(), attribution_ctx={"user_id": "u1"}
        )
    assert result.status == "success"
    assert result.content == "# Report"
    pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_error_retries_on_pool_with_default_backend():
    cloud = AsyncMock(return_value=_resp("error", error="executor exploded"))
    pool = AsyncMock(return_value=_resp("success", content="# Pool Report"))
    body = MagicMock()
    with patch.object(src.main, "_execute_research_cloud_impl", cloud), \
         patch.object(src.main, "_execute_research_impl", pool):
        result = await src.main._execute_research_cloud_with_pool_fallback(
            MagicMock(), body, attribution_ctx={"user_id": "u1"}
        )
    assert result.status == "success"
    assert result.content == "# Pool Report"
    # backend_config must be None — default pool env, never the caller's
    # (possibly Bedrock) backend, where research has no web search.
    pool.assert_awaited_once()
    args, kwargs = pool.await_args
    assert args[0] is body
    assert args[1] is None


@pytest.mark.asyncio
async def test_pool_fallback_error_still_propagates():
    # If BOTH paths fail there is nothing left to hide behind — the error
    # response of the pool attempt is returned (no infinite retry).
    cloud = AsyncMock(return_value=_resp("error", error="cloud down"))
    pool = AsyncMock(return_value=_resp("error", error="pool down too"))
    with patch.object(src.main, "_execute_research_cloud_impl", cloud), \
         patch.object(src.main, "_execute_research_impl", pool):
        result = await src.main._execute_research_cloud_with_pool_fallback(
            MagicMock(), MagicMock(), attribution_ctx=None
        )
    assert result.status == "error"
    assert "pool down too" in (result.error or "")


def _routing_patches(*, saturated=False, over_cap=False):
    return (
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
async def test_implicit_pin_routes_to_cloud_without_reading_research_pin(monkeypatch):
    monkeypatch.setenv("RESEARCH_CLOUD_ENABLED", "true")
    pin_read = AsyncMock(return_value=None)
    p1, p2 = _routing_patches()
    with patch(
        "src.routing.research_provider_override.get_user_research_pin", new=pin_read
    ), p1, p2:
        assert await resolve_research_cloud_routing(
            "bedrock-user", cloud_overflow=False, implicit_pin=True
        ) is True
    pin_read.assert_not_awaited()


@pytest.mark.asyncio
async def test_implicit_pin_respects_feature_flag_off(monkeypatch):
    monkeypatch.delenv("RESEARCH_CLOUD_ENABLED", raising=False)
    p1, p2 = _routing_patches()
    with p1, p2:
        assert await resolve_research_cloud_routing(
            "bedrock-user", cloud_overflow=True, implicit_pin=True
        ) is False


@pytest.mark.asyncio
async def test_implicit_pin_respects_daily_cap(monkeypatch):
    monkeypatch.setenv("RESEARCH_CLOUD_ENABLED", "true")
    p1, p2 = _routing_patches(over_cap=True)
    with p1, p2:
        assert await resolve_research_cloud_routing(
            "bedrock-user", cloud_overflow=True, implicit_pin=True
        ) is False
