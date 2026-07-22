"""
Unit tests for sandbox.account_router.pick_account.

Architectural contract under test:
  - `available` from the account-pool-state is the single source of truth
    for hard locks. `pick_account` must not duplicate or contradict it.
  - `adaptive_cooldown_s` (rolled into cooldown_remaining_s) is a *pacing*
    signal from the limiter, NOT a lock. A SHRINK'd account with
    available=True and headroom>threshold MUST still be eligible.
  - `cooldown_remaining_s` is only used as a tiebreaker score.
  - Missing required state fields → RuntimeError (fail-fast).
  - No eligible account → NoCapacityError carrying per-account reasons.
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

import src.sandbox.account_router as account_router
from src.sandbox.account_router import (
    NoCapacityError,
    PickedAccount,
    pick_account,
)


@pytest.fixture(autouse=True)
def _reset_last_good_state():
    """Der Last-known-good-Cache ist modulweit — zwischen Tests zuruecksetzen,
    sonst faellt ein Fail-fast-Test still auf den Snapshot des Vortests zurueck."""
    account_router._last_good_state = None
    yield
    account_router._last_good_state = None


def _mk_account(
    *,
    available: bool,
    headroom_percent: float,
    cooldown_remaining_s: int,
    capacity_lock_remaining_s: int = 0,
    soft_penalty_remaining_s: int = 0,
    session_percent: float = 0.0,
    is_hard_limited: bool = False,
):
    return {
        "available": available,
        "headroom_percent": headroom_percent,
        "cooldown_remaining_s": cooldown_remaining_s,
        "capacity_lock_remaining_s": capacity_lock_remaining_s,
        "soft_penalty_remaining_s": soft_penalty_remaining_s,
        "session_percent": session_percent,
        "is_hard_limited": is_hard_limited,
    }


def _patch_pool_state(monkeypatch, accounts: dict):
    """Mock httpx.AsyncClient so pick_account sees the given pool state."""
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"accounts": accounts}

    client = AsyncMock()
    client.get = AsyncMock(return_value=response)

    cm = AsyncMock()
    cm.__aenter__.return_value = client
    cm.__aexit__.return_value = None

    monkeypatch.setattr(
        "src.sandbox.account_router.httpx.AsyncClient",
        lambda *args, **kwargs: cm,
    )


@pytest.mark.asyncio
async def test_picks_available_account_despite_shrink_cooldown(monkeypatch):
    """
    CORE FIX: an account with available=True and headroom>threshold MUST be
    pickable, even when cooldown_remaining_s > 0 (SHRINK pacing, not a lock).
    Before the fix, all four prod accounts in SHRINK simultaneously locked
    every new lease for 5+ minutes.
    """
    _patch_pool_state(monkeypatch, {
        "office":   _mk_account(available=True, headroom_percent=99.2, cooldown_remaining_s=275),
        "engelmann": _mk_account(available=True, headroom_percent=100.0, cooldown_remaining_s=253),
    })
    picked = await pick_account()
    assert isinstance(picked, PickedAccount)
    assert picked.account_id == "engelmann"  # highest headroom wins
    assert picked.headroom_percent == 100.0


@pytest.mark.asyncio
async def test_honours_preferred_account(monkeypatch):
    _patch_pool_state(monkeypatch, {
        "office":   _mk_account(available=True, headroom_percent=99.0, cooldown_remaining_s=0),
        "engelmann": _mk_account(available=True, headroom_percent=100.0, cooldown_remaining_s=0),
    })
    picked = await pick_account(preferred_account_id="office")
    assert picked.account_id == "office"


@pytest.mark.asyncio
async def test_preferred_not_eligible_falls_back_to_best(monkeypatch):
    _patch_pool_state(monkeypatch, {
        "office":   _mk_account(available=False, headroom_percent=0.0, cooldown_remaining_s=0,
                                capacity_lock_remaining_s=120),
        "engelmann": _mk_account(available=True, headroom_percent=100.0, cooldown_remaining_s=10),
    })
    picked = await pick_account(preferred_account_id="office")
    assert picked.account_id == "engelmann"


@pytest.mark.asyncio
async def test_no_capacity_carries_per_account_reasons(monkeypatch):
    _patch_pool_state(monkeypatch, {
        "office":   _mk_account(available=False, headroom_percent=50.0, cooldown_remaining_s=120,
                                capacity_lock_remaining_s=120),
        "gmail":    _mk_account(available=False, headroom_percent=0.0, cooldown_remaining_s=200,
                                is_hard_limited=True),
        "engelmann": _mk_account(available=True, headroom_percent=5.0, cooldown_remaining_s=0),
    })
    with pytest.raises(NoCapacityError) as exc_info:
        await pick_account()
    err = exc_info.value
    # retry_after_s should be smallest non-zero cooldown
    assert err.retry_after_s == 120
    # All three accounts should appear in reasons
    assert set(err.reasons.keys()) == {"office", "gmail", "engelmann"}
    assert "capacity_lock" in err.reasons["office"]
    assert "hard_limited" in err.reasons["gmail"]
    assert "headroom" in err.reasons["engelmann"]


@pytest.mark.asyncio
async def test_low_headroom_excluded_with_reason(monkeypatch):
    _patch_pool_state(monkeypatch, {
        "office": _mk_account(available=True, headroom_percent=5.0, cooldown_remaining_s=0),
    })
    with pytest.raises(NoCapacityError) as exc_info:
        await pick_account()
    assert "headroom" in exc_info.value.reasons["office"]


@pytest.mark.asyncio
async def test_empty_accounts_raises_runtime_error(monkeypatch):
    _patch_pool_state(monkeypatch, {})
    with pytest.raises(RuntimeError, match="empty accounts"):
        await pick_account()


@pytest.mark.asyncio
async def test_missing_required_field_fails_fast(monkeypatch):
    """Defensive: a broken state shape must crash loud, not silently default."""
    _patch_pool_state(monkeypatch, {
        "office": {"available": True, "headroom_percent": 99.0}  # missing cooldown_remaining_s
    })
    with pytest.raises(RuntimeError, match="missing required field"):
        await pick_account()


@pytest.mark.asyncio
async def test_metrics_reader_non_200_raises_runtime_error(monkeypatch):
    response = MagicMock()
    response.status_code = 500
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    cm = AsyncMock()
    cm.__aenter__.return_value = client
    cm.__aexit__.return_value = None
    monkeypatch.setattr(
        "src.sandbox.account_router.httpx.AsyncClient",
        lambda *args, **kwargs: cm,
    )
    with pytest.raises(RuntimeError, match="HTTP 500"):
        await pick_account()


@pytest.mark.asyncio
async def test_tiebreak_by_shorter_cooldown(monkeypatch):
    """Equal headroom — pick the account with shorter cooldown_remaining_s."""
    _patch_pool_state(monkeypatch, {
        "office":   _mk_account(available=True, headroom_percent=99.0, cooldown_remaining_s=200),
        "engelmann": _mk_account(available=True, headroom_percent=99.0, cooldown_remaining_s=50),
    })
    picked = await pick_account()
    assert picked.account_id == "engelmann"


# ---------------------------------------------------------------------------
# S7: Fair round-robin via lease_counts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_round_robin_picks_least_used_account(monkeypatch):
    """
    Same headroom on both accounts — the one with fewer recent leases wins.
    Without S7 the order was deterministic-but-unfair (dict-order + max-headroom);
    engelmann ended up with 87% of 7d leases in prod.
    """
    _patch_pool_state(monkeypatch, {
        "engelmann": _mk_account(available=True, headroom_percent=100.0, cooldown_remaining_s=0),
        "office":    _mk_account(available=True, headroom_percent=100.0, cooldown_remaining_s=0),
        "gmail":     _mk_account(available=True, headroom_percent=100.0, cooldown_remaining_s=0),
    })
    lease_counts = {"engelmann": 178, "office": 22, "gmail": 4}
    picked = await pick_account(lease_counts=lease_counts)
    assert picked.account_id == "gmail"  # least-used


@pytest.mark.asyncio
async def test_round_robin_missing_account_treated_as_zero(monkeypatch):
    """
    A never-used (or recently-idle) account is absent from lease_counts.
    It MUST be treated as 0 and naturally win the rotation against any account
    with leases in the window.
    """
    _patch_pool_state(monkeypatch, {
        "engelmann": _mk_account(available=True, headroom_percent=100.0, cooldown_remaining_s=0),
        "werking":   _mk_account(available=True, headroom_percent=100.0, cooldown_remaining_s=0),
    })
    # werking absent → defaults to 0 leases → wins over engelmann's 178
    lease_counts = {"engelmann": 178}
    picked = await pick_account(lease_counts=lease_counts)
    assert picked.account_id == "werking"


@pytest.mark.asyncio
async def test_round_robin_tiebreak_by_headroom(monkeypatch):
    """
    Equal lease_count — headroom is the next tiebreaker (highest wins).
    Guards against the round-robin overriding the meaningful capacity signal
    when accounts are otherwise equally used.
    """
    _patch_pool_state(monkeypatch, {
        "office":    _mk_account(available=True, headroom_percent=50.0, cooldown_remaining_s=0),
        "engelmann": _mk_account(available=True, headroom_percent=99.0, cooldown_remaining_s=0),
    })
    lease_counts = {"office": 10, "engelmann": 10}
    picked = await pick_account(lease_counts=lease_counts)
    assert picked.account_id == "engelmann"  # higher headroom on the tiebreak


@pytest.mark.asyncio
async def test_round_robin_preferred_account_still_wins(monkeypatch):
    """
    Even with round-robin active, an eligible preferred_account_id wins.
    Use-case: client resuming a session that ran on a specific account —
    fairness must not break session-affinity.
    """
    _patch_pool_state(monkeypatch, {
        "engelmann": _mk_account(available=True, headroom_percent=100.0, cooldown_remaining_s=0),
        "office":    _mk_account(available=True, headroom_percent=100.0, cooldown_remaining_s=0),
    })
    lease_counts = {"engelmann": 200, "office": 1}
    picked = await pick_account(preferred_account_id="engelmann", lease_counts=lease_counts)
    assert picked.account_id == "engelmann"


@pytest.mark.asyncio
async def test_empty_lease_counts_falls_back_to_headroom(monkeypatch):
    """
    Backward-compat: pick_account() called without lease_counts (or with {})
    must behave like the pre-S7 implementation — highest headroom wins,
    cooldown breaks ties.
    """
    _patch_pool_state(monkeypatch, {
        "office":    _mk_account(available=True, headroom_percent=80.0, cooldown_remaining_s=0),
        "engelmann": _mk_account(available=True, headroom_percent=100.0, cooldown_remaining_s=0),
    })
    picked = await pick_account(lease_counts={})
    assert picked.account_id == "engelmann"


# ---------------------------------------------------------------------------
# Last-known-good Fallback (Reader-Aussetzer, z.B. uvicorn-Child-Restart nach
# OOM): frischer Snapshot ueberbrueckt, alter/fehlender Snapshot bleibt fail-fast.
# ---------------------------------------------------------------------------

import httpx as _httpx
import time as _time


def _patch_pool_state_unreachable(monkeypatch):
    """Mock httpx.AsyncClient so every GET raises a RequestError."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=_httpx.RequestError("connection refused"))

    cm = AsyncMock()
    cm.__aenter__.return_value = client
    cm.__aexit__.return_value = None

    monkeypatch.setattr(
        "src.sandbox.account_router.httpx.AsyncClient",
        lambda *args, **kwargs: cm,
    )


@pytest.mark.asyncio
async def test_unreachable_uses_fresh_last_known_good(monkeypatch):
    """Reader weg, aber Snapshot < _STALE_MAX_S alt → Lease klappt weiter."""
    _patch_pool_state(monkeypatch, {
        "office": _mk_account(available=True, headroom_percent=80.0, cooldown_remaining_s=0),
    })
    first = await pick_account()
    assert first.account_id == "office"

    _patch_pool_state_unreachable(monkeypatch)
    picked = await pick_account()
    assert picked.account_id == "office"


@pytest.mark.asyncio
async def test_unreachable_without_cache_raises(monkeypatch):
    """Kein Snapshot vorhanden → RuntimeError wie bisher (fail-fast)."""
    _patch_pool_state_unreachable(monkeypatch)
    with pytest.raises(RuntimeError, match="unreachable"):
        await pick_account()


@pytest.mark.asyncio
async def test_unreachable_with_stale_cache_raises(monkeypatch):
    """Snapshot aelter als _STALE_MAX_S → RuntimeError mit Alter im Text."""
    _patch_pool_state(monkeypatch, {
        "office": _mk_account(available=True, headroom_percent=80.0, cooldown_remaining_s=0),
    })
    await pick_account()
    # Snapshot kuenstlich altern lassen (weit ueber die Deckelung hinaus).
    ts, accounts = account_router._last_good_state
    account_router._last_good_state = (ts - account_router._STALE_MAX_S - 10, accounts)

    _patch_pool_state_unreachable(monkeypatch)
    with pytest.raises(RuntimeError, match="last-known-good"):
        await pick_account()


@pytest.mark.asyncio
async def test_stale_fallback_still_honours_no_capacity(monkeypatch):
    """Fallback-Snapshot ohne eligible Accounts → weiterhin NoCapacityError,
    der Cache darf Sperren nicht aufweichen."""
    _patch_pool_state(monkeypatch, {
        "office": _mk_account(available=False, headroom_percent=0.0, cooldown_remaining_s=120),
    })
    with pytest.raises(NoCapacityError):
        await pick_account()

    _patch_pool_state_unreachable(monkeypatch)
    with pytest.raises(NoCapacityError):
        await pick_account()
