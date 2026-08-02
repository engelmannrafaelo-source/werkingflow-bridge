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

import ast  # noqa: E402
import inspect  # noqa: E402
import json  # noqa: E402
import textwrap  # noqa: E402
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
# The cloud branch: NEVER falls back to the pool, regardless of pool health
# (Rafael 2026-08-02 — see tests/research_cloud/test_pool_fallback.py for the
# same-path-retry behaviour this invariant now has instead).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cloud_failure_never_touches_the_pool_even_when_it_is_healthy():
    cloud = AsyncMock(return_value=_resp("error", error="cloud boom"))
    pool = AsyncMock(return_value=_resp("success", content="# Report"))
    with patch.object(src.main, "_execute_research_cloud_impl", cloud), \
         patch.object(src.main, "_execute_research_impl", pool):
        out = await src.main._execute_research_cloud_with_pool_fallback(
            _request({}), MagicMock()
        )

    # The real cloud error is surfaced, not buried behind a silent pool run.
    assert out.status == "error" and "cloud boom" in out.error
    pool.assert_not_awaited()


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


# ---------------------------------------------------------------------------
# Structural: pool admission must stay INSIDE the pool-bound branch
# ---------------------------------------------------------------------------
def _admit_calls_within(node) -> list:
    """Every _admit_research_to_pool() call reachable from `node`."""
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_admit_research_to_pool"
    ]


def _guards_the_pool_branch(test) -> bool:
    """True for exactly `not _use_research_cloud`."""
    return (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and isinstance(test.operand, ast.Name)
        and test.operand.id == "_use_research_cloud"
    )


def test_pool_admission_is_only_reachable_on_the_pool_bound_branch():
    """The cloud path must stay admissible while the pool is at its wall.

    That is the entire point of the ordering: the research-cloud path runs on
    the Anthropic 1P API, consumes zero pool capacity, and its overflow trigger
    IS pool saturation. Admitting it against pool capacity makes the overflow
    unreachable in exactly the state it exists for, and locks out cloud-PINNED
    users outright.

    The sibling cases above all pin the POOL branch; the route test above pins
    the dependency. Neither notices if someone lifts the admission call out of
    `if not _use_research_cloud:` inside the handler — the pool cases still
    pass, and the damage shows up only as cloud users being refused during a
    capacity window, which is precisely when nobody is looking for a routing
    bug. Asserted structurally because a behavioural test would have to stand up
    the whole 200-line handler and would mock away the very branch in question.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(src.main.research)))

    all_calls = _admit_calls_within(tree)
    assert all_calls, (
        "the research handler no longer calls _admit_research_to_pool — if the "
        "gate moved, move this invariant with it rather than deleting it"
    )

    guarded = [
        call
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and _guards_the_pool_branch(node.test)
        for stmt in node.body
        for call in _admit_calls_within(stmt)
    ]

    assert len(guarded) == len(all_calls), (
        f"{len(all_calls) - len(guarded)} of {len(all_calls)} "
        "_admit_research_to_pool() call(s) sit outside `if not "
        "_use_research_cloud:` — pool capacity would gate the cloud branch too"
    )
