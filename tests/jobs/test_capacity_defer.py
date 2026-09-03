"""A job refused for capacity waits; it does not die.

The job's LLM work is a self-call pinned to the worker that accepted the POST
(executors.SELF_BASE_URL = localhost, deliberately: that worker's account does
the work). The pin means the inner call has no nginx failover. When the
account's limit window closes AFTER admission, the self-call 429s and there is
no next worker to try — so the job used to be marked terminally 'error'
(UPSTREAM_HTTP_429) while seven other accounts sat idle. Observed 2026-09-03:
three video takes killed by "[Bridge worker-kurt] Worker rate-limited (soft)",
surfacing at the app as an unhandled error.

A 429 is not a failed job. It is a job whose turn has not come — so it is
parked and re-claimed by the watchdog, which is the failover the self-call
cannot have (any worker on the bridge may claim a stale row, so the retry
lands on a different account).
"""
from __future__ import annotations

import os

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

from unittest.mock import AsyncMock, patch

import pytest

from src.jobs import registry, store_client
from src.jobs.executors import ExecutorHTTPError


def _executor_raising(exc: Exception):
    async def _run(payload, attribution, report_progress):
        raise exc
    return _run


class _Seam:
    """The store calls the runner made — captured while the patch is active,
    so assertions read them after the run instead of the restored originals."""

    def __init__(self):
        self.defer_job = AsyncMock()
        self.mark_error = AsyncMock()


async def _run_with(exc: Exception, defer_count: int = 0) -> _Seam:
    seam = _Seam()
    registry.register_executor("capacity-test", _executor_raising(exc))
    with patch.multiple(
        store_client,
        mark_running=AsyncMock(),
        heartbeat=AsyncMock(),
        update_progress=AsyncMock(),
        mark_done=AsyncMock(),
        mark_error=seam.mark_error,
        defer_job=seam.defer_job,
        get_job=AsyncMock(return_value={"defer_count": defer_count}),
    ):
        await registry.run_generic_job("job_dev_x", "capacity-test", {}, None)
    return seam


async def test_429_parks_the_job_instead_of_killing_it():
    sc = await _run_with(ExecutorHTTPError(429, "worker rate-limited (soft)"))
    sc.defer_job.assert_awaited_once()
    sc.mark_error.assert_not_awaited()


async def test_retry_after_from_upstream_is_used():
    """The bridge that refused knows when its window resets. Throwing that
    number away and picking our own would be worse than what it told us."""
    sc = await _run_with(
        ExecutorHTTPError(429, "rate-limited", retry_after_s=240)
    )
    assert sc.defer_job.await_args.args[1] == 240


@pytest.mark.parametrize(
    "hint,expected",
    [
        (0, registry.CAPACITY_RETRY_MIN_DELAY_S),        # no hot loop
        (99999, registry.CAPACITY_RETRY_MAX_DELAY_S),    # no indefinite parking
        (None, registry.CAPACITY_RETRY_DEFAULT_DELAY_S),  # nothing said → our default
        ("nonsense", registry.CAPACITY_RETRY_DEFAULT_DELAY_S),
    ],
)
async def test_retry_delay_is_clamped(hint, expected):
    sc = await _run_with(ExecutorHTTPError(429, "rate-limited", retry_after_s=hint))
    assert sc.defer_job.await_args.args[1] == expected


async def test_patience_is_bounded_then_fails_loud():
    """A worker pool that never frees up must still end in a terminal, visible
    error — waiting forever is its own silent failure."""
    sc = await _run_with(
        ExecutorHTTPError(429, "rate-limited"),
        defer_count=registry.DEPENDENCY_MAX_DEFERS,
    )
    sc.defer_job.assert_not_awaited()
    sc.mark_error.assert_awaited_once()
    assert sc.mark_error.await_args.kwargs["code"] == "UPSTREAM_HTTP_429"


async def test_non_capacity_errors_still_fail_immediately():
    """Only 429 means 'not your turn'. A 400 is deterministic — parking it
    would recreate the doomed-retry loop that cost 4.5h on 2026-07-20."""
    sc = await _run_with(ExecutorHTTPError(400, "invalid request"))
    sc.defer_job.assert_not_awaited()
    sc.mark_error.assert_awaited_once()


async def test_dependency_deferral_still_works():
    """The 424 path shares the new helper — it must be unchanged."""
    sc = await _run_with(
        ExecutorHTTPError(registry.DEPENDENCY_UNAVAILABLE_STATUS, "privacy down")
    )
    sc.defer_job.assert_awaited_once()
    assert sc.defer_job.await_args.args[1] == registry.DEPENDENCY_RETRY_DELAY_S
    sc.mark_error.assert_not_awaited()
