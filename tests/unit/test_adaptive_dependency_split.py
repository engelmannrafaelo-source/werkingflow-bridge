"""adaptive_limit_dependency is split into body-caching and pool admission.

Endpoints whose execution path is not known at dependency time (/v1/research:
subscription pool vs. research-cloud) must cache the body WITHOUT admitting,
then admit explicitly on their pool-bound branch. Combining the two is what
gated cloud-bound research on capacity it never consumes.
"""
import sys
from unittest.mock import MagicMock as _MagicMock

for _mod_name in ["claude_code_sdk", "src.db.client"]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

import json  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

from src.middleware.adaptive_limiter import (  # noqa: E402
    cache_request_body_dependency,
    enforce_pool_admission,
)


def _request(body: dict = None, *, raises: bool = False):
    req = MagicMock()
    req.state = MagicMock(spec=[])  # bare namespace: getattr(...) misses are real
    if raises:
        req.body = AsyncMock(side_effect=RuntimeError("stream already consumed"))
    else:
        req.body = AsyncMock(return_value=json.dumps(body or {}).encode())
    return req


@pytest.mark.asyncio
async def test_cache_dependency_estimates_but_never_admits():
    req = _request({"messages": [{"role": "user", "content": "hello"}]})
    limiter = MagicMock()
    with patch("src.middleware.adaptive_limiter.get_adaptive_limiter", return_value=limiter):
        await cache_request_body_dependency(req)

    assert isinstance(req.state.adaptive_est_tokens, int)
    assert req.state.cached_body_dict["messages"][0]["content"] == "hello"
    limiter.acquire_with_wait.assert_not_called()  # admission is NOT this function's job


@pytest.mark.asyncio
async def test_cache_dependency_leaves_no_estimate_when_body_unreadable():
    req = _request(raises=True)
    await cache_request_body_dependency(req)
    assert not hasattr(req.state, "adaptive_est_tokens")


@pytest.mark.asyncio
async def test_admission_without_an_estimate_passes_through_loudly(caplog):
    """Pre-existing behaviour for an unreadable body — but it must be visible,
    not silent: an unmeasured request skipping admission is worth seeing."""
    req = _request()
    limiter = MagicMock()
    with patch("src.middleware.adaptive_limiter.get_adaptive_limiter", return_value=limiter):
        await enforce_pool_admission(req)   # must not raise

    limiter.acquire_with_wait.assert_not_called()
    assert any("no token estimate" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_admission_admits_when_the_limiter_accepts():
    req = _request()
    req.state.adaptive_est_tokens = 1234
    limiter = MagicMock()
    limiter.acquire_with_wait = AsyncMock(return_value=(True, None, {}, 0.0))
    with patch("src.middleware.adaptive_limiter.get_adaptive_limiter", return_value=limiter):
        await enforce_pool_admission(req)   # must not raise

    limiter.acquire_with_wait.assert_awaited_once_with(1234)


@pytest.mark.asyncio
async def test_admission_raises_bridge_error_when_the_limiter_rejects():
    from src.middleware.bridge_error import BridgeError

    req = _request()
    req.state.adaptive_est_tokens = 999
    limiter = MagicMock()
    limiter.acquire_with_wait = AsyncMock(
        return_value=(False, "cap full", {"cap_tokens": 10, "inflight_tokens": 10}, 0.0)
    )
    with patch("src.middleware.adaptive_limiter.get_adaptive_limiter", return_value=limiter):
        with pytest.raises(BridgeError):
            await enforce_pool_admission(req)
