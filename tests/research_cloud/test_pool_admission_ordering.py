"""Pool-capacity admission must run AFTER the pool-vs-cloud decision, and only
on the pool-bound branch.

The defect this locks down (found 2026-07-29, fixed 2026-07-30): two capacity
gates sat in FRONT of the routing decision on /v1/research —
adaptive_limit_dependency and the worker rate-limit pre-check. Both govern the
subscription pool, but the research-cloud path consumes zero pool capacity AND
its overflow trigger IS pool saturation (src/research_cloud/routing.py). So the
escape hatch was unreachable in exactly the state it exists for, and
cloud-PINNED users were blocked outright.

nginx is a third gate — but only since the rewrite-phase `if ($target_worker =
"unavailable")` was replaced by an access-phase ngx.exec (it could never fire
before, so every request reached a worker even on a fully exhausted pool).
Making that gate real is precisely why /v1/research must be carved out of it
($pool_overflow_capable) and handed X-Pool-Exhausted instead: a working gate
would otherwise re-break the overflow it just unblocked.

These tests pin the invariant from both sides: the pool branch still gets every
gate, and the cloud branch gets none of them.
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

import json  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

import src.main  # noqa: E402
from src.models import ResearchResponse  # noqa: E402


def _request(headers: dict = None):
    """Minimal stand-in for a Starlette Request (headers + state only)."""
    req = MagicMock()
    req.headers = headers if headers is not None else {}
    req.state = MagicMock()
    return req


def _resp(status: str, content: str = "", error: str = None) -> ResearchResponse:
    return ResearchResponse(
        status=status, query="q", model="claude-sonnet-5", content=content, error=error
    )


def _body(response) -> dict:
    return json.loads(bytes(response.body).decode())


# ---------------------------------------------------------------------------
# The nginx marker
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    ("1", True),
    (" 1 ", True),
    ("", False),
    ("0", False),
    ("true", False),   # strictly "1" — nginx writes exactly that or nothing
])
def test_pool_exhausted_marker_parsing(value, expected):
    assert src.main._pool_exhausted_marker(_request({"X-Pool-Exhausted": value})) is expected


def test_pool_exhausted_marker_absent_header_is_false():
    assert src.main._pool_exhausted_marker(_request({})) is False


def test_pool_exhausted_marker_no_request_is_false():
    """Internal self-calls (durable research job) bypass nginx entirely — no
    marker means no claim either way, the worker-local signals still apply."""
    assert src.main._pool_exhausted_marker(None) is False


# ---------------------------------------------------------------------------
# The pool-capacity signal — ONE definition, shared with the overflow trigger
# ---------------------------------------------------------------------------
def test_pool_availability_uses_the_same_signal_as_the_overflow_trigger():
    """_pool_can_serve_research must agree with is_worker_pool_saturated: if the
    two ever disagreed, research could pick the cloud path (pool saturated)
    while still arming the pool fallback (pool 'available')."""
    with patch("src.research_cloud.pool_signal.is_worker_pool_saturated", return_value=True):
        assert src.main._pool_can_serve_research(_request({})) is False
    with patch("src.research_cloud.pool_signal.is_worker_pool_saturated", return_value=False):
        assert src.main._pool_can_serve_research(_request({})) is True


def test_pool_availability_honours_the_nginx_marker_without_probing():
    """The marker is authoritative for the WHOLE pool; this worker's own
    saturation cannot override it."""
    with patch("src.research_cloud.pool_signal.is_worker_pool_saturated",
               return_value=False) as probe:
        assert src.main._pool_can_serve_research(_request({"X-Pool-Exhausted": "1"})) is False
        probe.assert_not_called()


def test_pool_availability_probe_failure_assumes_usable():
    """Unknown capacity must not silently disable the reliability fallback."""
    with patch("src.research_cloud.pool_signal.is_worker_pool_saturated",
               side_effect=RuntimeError("boom")):
        assert src.main._pool_can_serve_research(_request({})) is True


# ---------------------------------------------------------------------------
# _admit_research_to_pool — the marker is the cheapest gate and short-circuits
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pool_bound_request_rejected_when_nginx_marks_pool_exhausted():
    tracker = MagicMock()
    enforce = AsyncMock()
    with patch("src.claude_cli.rate_limit_tracker", tracker), \
         patch.object(src.main, "enforce_pool_admission", enforce):
        out = await src.main._admit_research_to_pool(
            _request({"X-Pool-Exhausted": "1"}), "worker1"
        )

    assert out is not None and out.status_code == 429
    err = _body(out)["error"]
    assert err["bridge_type"] == "account_exhausted"
    assert err["retryable"] is True
    # Fail FAST: the cheapest gate short-circuits the expensive ones.
    tracker.should_reject_new_request.assert_not_called()
    enforce.assert_not_awaited()


# ---------------------------------------------------------------------------
# _admit_research_to_pool — the pool branch keeps EVERY gate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pool_bound_request_rejected_when_worker_rate_limited():
    tracker = MagicMock()
    tracker.should_reject_new_request.return_value = True
    tracker.get_retry_after.return_value = 45
    tracker.is_hard_limited.return_value = True
    enforce = AsyncMock()
    with patch("src.claude_cli.rate_limit_tracker", tracker), \
         patch.object(src.main, "enforce_pool_admission", enforce):
        out = await src.main._admit_research_to_pool(_request({}), "worker2")

    assert out is not None and out.status_code == 429
    err = _body(out)["error"]
    assert err["bridge_type"] == "worker_unavailable"
    assert err["reason"] == "worker_account_rate_limited"
    assert out.headers["Retry-After"] == "45"
    enforce.assert_not_awaited()


@pytest.mark.asyncio
async def test_pool_bound_request_admitted_runs_the_adaptive_budget():
    tracker = MagicMock()
    tracker.should_reject_new_request.return_value = False
    enforce = AsyncMock()
    with patch("src.claude_cli.rate_limit_tracker", tracker), \
         patch.object(src.main, "enforce_pool_admission", enforce):
        out = await src.main._admit_research_to_pool(_request({}), "worker3")

    assert out is None            # admitted
    enforce.assert_awaited_once()  # the third gate is NOT skipped


# ---------------------------------------------------------------------------
# The cloud branch: no pool fallback when the pool provably cannot serve it
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cloud_failure_does_not_retry_on_an_exhausted_pool():
    cloud = AsyncMock(return_value=_resp("error", error="cloud boom"))
    pool = AsyncMock()
    with patch.object(src.main, "_execute_research_cloud_impl", cloud), \
         patch.object(src.main, "_execute_research_impl", pool):
        out = await src.main._execute_research_cloud_with_pool_fallback(
            _request({}), MagicMock(), pool_available=False
        )

    # The real cloud error is surfaced, not buried behind a doomed second call.
    assert out.status == "error" and "cloud boom" in out.error
    pool.assert_not_awaited()


@pytest.mark.asyncio
async def test_cloud_failure_still_retries_on_a_healthy_pool():
    """Regression guard for the reliability fallback (Rafael 2026-07-27)."""
    cloud = AsyncMock(return_value=_resp("error", error="cloud boom"))
    pool = AsyncMock(return_value=_resp("success", content="# Report"))
    with patch.object(src.main, "_execute_research_cloud_impl", cloud), \
         patch.object(src.main, "_execute_research_impl", pool):
        out = await src.main._execute_research_cloud_with_pool_fallback(
            _request({}), MagicMock(), pool_available=True
        )

    assert out.status == "success"
    pool.assert_awaited_once()


# ---------------------------------------------------------------------------
# Structural: the route must NOT use the combined (admitting) dependency
# ---------------------------------------------------------------------------
def test_research_route_uses_body_cache_dependency_not_the_admitting_one():
    """Swapping cache_request_body_dependency back to adaptive_limit_dependency
    would silently reinstate the gate in front of the routing decision — the
    exact regression, invisible in any behavioural test that mocks the handler.
    """
    from src.middleware.adaptive_limiter import (
        adaptive_limit_dependency,
        cache_request_body_dependency,
    )

    route = next(r for r in src.main.app.routes
                 if getattr(r, "path", None) == "/v1/research"
                 and "POST" in getattr(r, "methods", set()))
    deps = [d.call for d in route.dependant.dependencies]
    assert cache_request_body_dependency in deps
    assert adaptive_limit_dependency not in deps
