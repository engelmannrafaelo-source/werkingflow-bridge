"""Tests for src.main._execute_research_cloud_with_pool_fallback and the
implicit-pin routing.

Behaviour since Rafael 2026-08-02 ("kein stiller Rückfall auf eine andere
Quelle" — production über Bedrock, Recherche direkt über Anthropic):

  - a successful cloud run never touches the pool (unchanged)
  - a failed cloud run is retried on the SAME path (cloud), never on the
    subscription pool — a different provider, cost model and privacy posture
  - if every same-path attempt fails, the cloud error is surfaced as-is
  - a globally Bedrock-pinned user routes to the cloud via implicit_pin
    without reading the research-scoped pin (research cannot run on Bedrock)

Until 2026-08-02 a cloud failure silently retried on the subscription pool's
default backend (Rafael 2026-07-27) — that fallback is gone; see
tests/research_cloud/test_pool_admission_ordering.py for the pool-admission
gates this file does NOT re-test.
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
    cloud.assert_awaited_once()
    pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_error_retries_on_the_same_path_not_the_pool():
    cloud = AsyncMock(side_effect=[
        _resp("error", error="executor exploded"),
        _resp("success", content="# Cloud Report (retry)"),
    ])
    pool = AsyncMock()
    body = MagicMock()
    with patch.object(src.main, "_execute_research_cloud_impl", cloud), \
         patch.object(src.main, "_execute_research_impl", pool):
        result = await src.main._execute_research_cloud_with_pool_fallback(
            MagicMock(), body, attribution_ctx={"user_id": "u1"}
        )
    assert result.status == "success"
    assert result.content == "# Cloud Report (retry)"
    assert cloud.await_count == 2
    pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_error_exhausting_retries_surfaces_the_cloud_error():
    # No pool left to hide behind — the last cloud attempt's error is returned.
    cloud = AsyncMock(return_value=_resp("error", error="cloud down"))
    pool = AsyncMock(return_value=_resp("success", content="should never be used"))
    with patch.object(src.main, "_execute_research_cloud_impl", cloud), \
         patch.object(src.main, "_execute_research_impl", pool):
        result = await src.main._execute_research_cloud_with_pool_fallback(
            MagicMock(), MagicMock(), attribution_ctx=None
        )
    assert result.status == "error"
    assert "cloud down" in (result.error or "")
    assert cloud.await_count == src.main._RESEARCH_CLOUD_MAX_ATTEMPTS
    pool.assert_not_awaited()


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
async def test_implicit_pin_over_cap_raises_instead_of_falling_back(monkeypatch):
    """Cloud was already the committed answer for an implicit (Bedrock)
    pin — over cap must defer (raise), not quietly execute on the pool."""
    from src.research_cloud.routing import ResearchCloudCapExceededError

    monkeypatch.setenv("RESEARCH_CLOUD_ENABLED", "true")
    p1, p2 = _routing_patches(over_cap=True)
    with p1, p2:
        with pytest.raises(ResearchCloudCapExceededError):
            await resolve_research_cloud_routing(
                "bedrock-user", cloud_overflow=True, implicit_pin=True
            )
