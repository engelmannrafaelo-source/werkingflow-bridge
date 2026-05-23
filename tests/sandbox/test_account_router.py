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

from src.sandbox.account_router import (
    NoCapacityError,
    PickedAccount,
    pick_account,
)


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
