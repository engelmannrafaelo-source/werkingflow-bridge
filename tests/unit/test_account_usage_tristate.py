"""'Never measured' must never look like 'measured 0%'.

The production bridge reported weekly_percent=0 / headroom_percent=100 for all
four of its workers for months. Nothing was idle — no usage snapshot had ever
reached that host, and the limiter's initial 0.0 is indistinguishable from a
real zero once it leaves the process. 0% is the most attractive value in every
downstream ranking, so the pool preferred exactly the accounts nobody could see.

These tests pin the three properties that make that impossible to repeat:
  1. the usage store can find THIS account's newest sample even when another
     producer wrote the last line,
  2. every no-data path yields known=False WITH a reason, never a number,
  3. a stale sample expires into unknown instead of ageing into a fake zero.
"""
import json
import os
import sys
import time
from unittest.mock import MagicMock as _MagicMock

for _mod_name in ["claude_code_sdk", "src.db.client"]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

import pytest  # noqa: E402


def _snapshot(ts: float, accounts: list[tuple[str, float, float]]) -> str:
    return json.dumps({
        "ts": ts,
        "accounts": [
            {
                "account": name,
                "currentSession": {"percent": session, "resetIn": ""},
                "weeklyAllModels": {"percent": weekly, "resetDate": ""},
            }
            for name, weekly, session in accounts
        ],
    })


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from src.middleware import bridge_metrics_store as bms
    s = bms.CCUsageStore()
    monkeypatch.setattr(s, "JSONL_PATH", str(tmp_path / "cc_usage_snapshots.jsonl"))
    return s


# --------------------------------------------------------------------------
# 1. store lookup
# --------------------------------------------------------------------------

def test_missing_file_reports_reason_not_zero(store):
    acc, ts, reason = store.latest_for_account("engelmann", 3600)
    assert acc is None and ts is None
    assert "missing" in reason


def test_finds_own_account_behind_a_foreign_producers_snapshot(store):
    """Two producers write into one file. The last line is not necessarily mine.

    The dev-server scraper posts the four dev accounts; the partner-server
    scraper posts the partner accounts. 'Take the newest snapshot and look for
    myself in it' blanks every worker for one cadence whenever the other
    producer wrote last — which is how a working delivery still produces gaps.
    """
    now = time.time()
    with open(store.JSONL_PATH, "w") as f:
        f.write(_snapshot(now - 300, [("engelmann", 87.0, 23.0)]) + "\n")
        f.write(_snapshot(now - 10, [("coach", 3.0, 24.0)]) + "\n")  # foreign, newest

    acc, ts, reason = store.latest_for_account("engelmann", 3600)
    assert reason == ""
    assert acc["weeklyAllModels"]["percent"] == 87.0
    assert ts == pytest.approx(now - 300)

    acc, ts, reason = store.latest_for_account("coach", 3600)
    assert acc["weeklyAllModels"]["percent"] == 3.0


def test_sample_older_than_window_is_not_a_measurement(store):
    now = time.time()
    with open(store.JSONL_PATH, "w") as f:
        f.write(_snapshot(now - 7200, [("engelmann", 87.0, 23.0)]) + "\n")

    acc, ts, reason = store.latest_for_account("engelmann", 3600)
    assert acc is None
    assert "engelmann" in reason and "old" in reason


def test_unknown_account_names_its_own_absence(store):
    now = time.time()
    with open(store.JSONL_PATH, "w") as f:
        f.write(_snapshot(now - 10, [("engelmann", 87.0, 23.0)]) + "\n")

    acc, _, reason = store.latest_for_account("sahori", 3600)
    assert acc is None
    assert "sahori" in reason


def test_corrupt_line_does_not_hide_a_good_one(store):
    now = time.time()
    with open(store.JSONL_PATH, "w") as f:
        f.write(_snapshot(now - 60, [("engelmann", 87.0, 23.0)]) + "\n")
        f.write("{not json at all\n")

    acc, _, reason = store.latest_for_account("engelmann", 3600)
    assert acc is not None and reason == ""


# --------------------------------------------------------------------------
# 2. limiter state
# --------------------------------------------------------------------------

@pytest.fixture()
def limiter(tmp_path, monkeypatch):
    # Patch the module constant, not the env var: METRICS_DIR is resolved at
    # import time, so setenv only works if this test happens to import the
    # module first — which depends on test collection order.
    from src.middleware import adaptive_limiter as al
    monkeypatch.setattr(al, "METRICS_DIR", str(tmp_path))
    return al.AdaptiveLoadLimiter()


def _force_refresh(lim):
    """Bypass the TTL so each call re-reads."""
    lim._account_usage["ts"] = 0.0
    lim._refresh_account_usage()


def test_no_snapshot_yields_unknown_with_reason_not_zero(limiter, monkeypatch, store):
    from src.middleware import adaptive_limiter as al
    monkeypatch.setattr(al, "WORKER_ACCOUNT", "engelmann")
    monkeypatch.setattr(
        "src.middleware.bridge_metrics_store.get_cc_usage_store", lambda: store
    )
    _force_refresh(limiter)

    u = limiter._account_usage
    assert u["known"] is False
    assert u["weekly_pct"] is None, "unknown must not be representable as a number"
    assert u["session_pct"] is None
    assert u["reason"]
    # Local admission stays permissive — a blind spot must not become an outage.
    assert u["budget_multiplier"] == 1.0


def test_missing_worker_account_is_unknown_not_zero(limiter, monkeypatch):
    """A worker whose account name was never configured — the production case.

    _WORKER_ACCOUNT_MAP only ever knew worker1..4, so every prod worker
    resolved to None, matched nothing, and kept its initial 0.0 forever.
    """
    from src.middleware import adaptive_limiter as al
    monkeypatch.setattr(al, "WORKER_ACCOUNT", None)
    _force_refresh(limiter)

    u = limiter._account_usage
    assert u["known"] is False
    assert u["weekly_pct"] is None
    assert "WORKER_ACCOUNT" in u["reason"]


def test_fresh_snapshot_is_measured(limiter, monkeypatch, store):
    from src.middleware import adaptive_limiter as al
    monkeypatch.setattr(al, "WORKER_ACCOUNT", "engelmann")
    monkeypatch.setattr(
        "src.middleware.bridge_metrics_store.get_cc_usage_store", lambda: store
    )
    now = time.time()
    with open(store.JSONL_PATH, "w") as f:
        f.write(_snapshot(now - 30, [("engelmann", 87.0, 23.0)]) + "\n")

    _force_refresh(limiter)
    u = limiter._account_usage
    assert u["known"] is True
    assert u["weekly_pct"] == 87.0 and u["session_pct"] == 23.0
    assert u["source_ts"] == pytest.approx(now - 30)


def test_measured_zero_stays_zero_and_stays_known(limiter, monkeypatch, store):
    """The other half of the contract: a real 0% must survive as a real 0%."""
    from src.middleware import adaptive_limiter as al
    monkeypatch.setattr(al, "WORKER_ACCOUNT", "erk")
    monkeypatch.setattr(
        "src.middleware.bridge_metrics_store.get_cc_usage_store", lambda: store
    )
    now = time.time()
    with open(store.JSONL_PATH, "w") as f:
        f.write(_snapshot(now - 30, [("erk", 0.0, 0.0)]) + "\n")

    _force_refresh(limiter)
    u = limiter._account_usage
    assert u["known"] is True
    assert u["weekly_pct"] == 0.0


def test_delivery_breaking_falls_back_to_unknown_not_to_the_last_good_value(
    limiter, monkeypatch, store
):
    """Keeping the last value forever is how a dead scraper stays invisible."""
    from src.middleware import adaptive_limiter as al
    monkeypatch.setattr(al, "WORKER_ACCOUNT", "engelmann")
    monkeypatch.setattr(
        "src.middleware.bridge_metrics_store.get_cc_usage_store", lambda: store
    )
    now = time.time()
    with open(store.JSONL_PATH, "w") as f:
        f.write(_snapshot(now - 30, [("engelmann", 87.0, 23.0)]) + "\n")
    _force_refresh(limiter)
    assert limiter._account_usage["known"] is True

    # Producer dies: the newest sample ages out of the window.
    with open(store.JSONL_PATH, "w") as f:
        f.write(_snapshot(now - 99999, [("engelmann", 87.0, 23.0)]) + "\n")
    _force_refresh(limiter)

    u = limiter._account_usage
    assert u["known"] is False
    assert u["weekly_pct"] is None
    assert u["reason"]


def test_store_error_is_unknown_not_zero(limiter, monkeypatch):
    from src.middleware import adaptive_limiter as al
    monkeypatch.setattr(al, "WORKER_ACCOUNT", "engelmann")

    def boom():
        raise OSError("volume gone")

    monkeypatch.setattr(
        "src.middleware.bridge_metrics_store.get_cc_usage_store", boom
    )
    _force_refresh(limiter)

    u = limiter._account_usage
    assert u["known"] is False
    assert "usage store read failed" in u["reason"]


def test_snapshot_exposes_the_tri_state(limiter, monkeypatch, store):
    from src.middleware import adaptive_limiter as al
    monkeypatch.setattr(al, "WORKER_ACCOUNT", "engelmann")
    monkeypatch.setattr(
        "src.middleware.bridge_metrics_store.get_cc_usage_store", lambda: store
    )
    _force_refresh(limiter)
    snap = limiter.snapshot()
    assert snap["account_usage_known"] is False
    assert snap["account_weekly_pct"] is None
    assert snap["account_usage_reason"]


# --------------------------------------------------------------------------
# 3. ranking: unmeasured accounts are held back, not preferred
# --------------------------------------------------------------------------

def _row(headroom, *, known, in_flight=0):
    return {
        "worker": "w",
        "available": True,
        "headroom_percent": headroom,
        "cooldown_remaining_s": 0,
        "usage_known": known,
        "current_in_flight_tokens": in_flight,
        "effective_cap_tokens": 400000,
    }


@pytest.mark.asyncio
async def test_sandbox_picker_prefers_a_measured_account_over_an_unseen_one(monkeypatch):
    """An unmeasured account advertises the best headroom there is.

    Its headroom is the in-flight ceiling alone, undiminished by any weekly or
    session consumption — so a headroom-ranked picker chooses it over a
    measured account every single time. That is the phantom-capacity bug.
    """
    from src.sandbox import account_router as ar

    async def fake_state():
        return {
            "gmail": _row(50.0, known=True),
            "sahori": _row(100.0, known=False),
        }

    monkeypatch.setattr(ar, "_fetch_pool_state", fake_state)
    picked = await ar.pick_account()
    assert picked.account_id == "gmail"


@pytest.mark.asyncio
async def test_sandbox_picker_still_serves_when_nothing_is_measured(monkeypatch):
    """Held back, not excluded — a blind spot must not become an outage.

    On the production bridge today NO account is measured. Excluding unmeasured
    accounts outright would reject 100% of sandbox leases there.
    """
    from src.sandbox import account_router as ar

    async def fake_state():
        return {"sahori": _row(90.0, known=False), "kurt": _row(80.0, known=False)}

    monkeypatch.setattr(ar, "_fetch_pool_state", fake_state)
    picked = await ar.pick_account()
    assert picked.account_id == "sahori"


@pytest.mark.asyncio
async def test_sandbox_picker_treats_a_missing_flag_as_unmeasured(monkeypatch):
    """A worker on a pre-tri-state image must not read as measured."""
    from src.sandbox import account_router as ar

    old = _row(100.0, known=True)
    del old["usage_known"]

    async def fake_state():
        return {"legacy": old, "gmail": _row(20.0, known=True)}

    monkeypatch.setattr(ar, "_fetch_pool_state", fake_state)
    picked = await ar.pick_account()
    assert picked.account_id == "gmail"
