"""ADR-0012 at the route: a poll for a job this bridge cannot hold says so.

Before this, every one of these cases answered the same 404 — "Async job not
found (unknown id, or expired)". That single answer covered three completely
different situations, and the one that mattered (the job is alive on the OTHER
bridge) read like the one that does not (it expired). Measured 2026-09-03:
20/20 polls 404 against a job that ran to completion on the peer bridge.

Tested through _reject_foreign_or_malformed, the guard the route runs before
it is allowed to reach for the store — the store must not even be consulted
for a job that provably is not ours.
"""
from __future__ import annotations

import os

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import json

import pytest

from src.jobs.routes import _reject_foreign_or_malformed

HEX32 = "0123456789abcdef" * 2


def _body(response) -> dict:
    return json.loads(bytes(response.body).decode())["error"]


def test_own_job_passes_through(monkeypatch):
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "prod")
    assert _reject_foreign_or_malformed(f"job_prod_{HEX32}") is None


def test_foreign_job_is_misdirected_not_missing(monkeypatch, caplog):
    """421, and the body names BOTH bridges — an operator reading it must not
    have to guess which store was asked and which one holds the row."""
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "prod")
    job_id = f"job_dev_{HEX32}"
    resp = _reject_foreign_or_malformed(job_id)

    assert resp is not None
    assert resp.status_code == 421, "a job on the peer bridge is misdirected, not gone"
    err = _body(resp)
    assert err["reason"] == "job_home_bridge_mismatch"
    assert err["job_home"] == "dev"
    assert err["this_bridge"] == "prod"
    assert err["retryable"] is False, "retrying the same poll here can only fail again"
    assert "expired" not in err["message"].lower()


def test_unknown_bridge_marker_is_also_loud(monkeypatch):
    """A marker naming no bridge we know is a routing fault, not a 404. nginx
    refuses to invent a destination for it and lets this answer."""
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "prod")
    resp = _reject_foreign_or_malformed(f"job_nowhere_{HEX32}")
    assert resp is not None and resp.status_code == 421
    assert _body(resp)["job_home"] == "nowhere"


@pytest.mark.parametrize("bad", ["job_xxxxx_deadbeef", "not-an-id", "job_dev_", ""])
def test_malformed_id_is_400_not_404(monkeypatch, bad):
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "prod")
    resp = _reject_foreign_or_malformed(bad)
    assert resp is not None
    assert resp.status_code == 400
    err = _body(resp)
    assert err["reason"] == "job_id_malformed"
    assert err["retryable"] is False


def test_legacy_id_is_served_locally_with_a_warning(monkeypatch, caplog):
    """The transition rule, and its bound. An unmarked id keeps the pre-ADR
    behaviour (answered here) so jobs in flight at cutover survive — but it
    must be VISIBLE, because after the store TTL it can only be a stale
    client."""
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "prod")
    with caplog.at_level("WARNING"):
        assert _reject_foreign_or_malformed(f"job_{HEX32}") is None
    assert any("LEGACY" in r.message for r in caplog.records), (
        "a legacy id must leave a trace — a silent permanent fallback is how "
        "this class of bug survives a rollout"
    )


def test_unconfigured_home_fails_closed(monkeypatch):
    """Without its own id this worker cannot tell own from foreign. It refuses
    rather than guessing 'probably mine' — guessing is what produced polls that
    silently asked the wrong store."""
    monkeypatch.delenv("BRIDGE_ORIGIN_ID", raising=False)
    resp = _reject_foreign_or_malformed(f"job_prod_{HEX32}")
    assert resp is not None
    assert resp.status_code == 503
    assert _body(resp)["reason"] == "job_home_unconfigured"


def test_unconfigured_home_still_serves_legacy_ids(monkeypatch):
    """Order matters: an unmarked id needs no identity comparison, so a bridge
    without BRIDGE_ORIGIN_ID must not turn old polls into 503s on top of
    everything else."""
    monkeypatch.delenv("BRIDGE_ORIGIN_ID", raising=False)
    assert _reject_foreign_or_malformed(f"job_{HEX32}") is None
