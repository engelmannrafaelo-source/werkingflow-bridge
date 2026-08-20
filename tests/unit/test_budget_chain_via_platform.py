"""Tests for the budget-gate chain's move to platform-api (ADR-0009 Schritt 2b,
C2/C3/C4 + D2).

The risky properties, and what each guards against:

  * AmbiguousProjectBudget survives the hop as an EXCEPTION. If it degraded to
    an outage, the gate's fail-open catch-all would wave through a call whose
    paying pot is ambiguous — the exact mis-billing this guard exists to stop.
  * LegacyTopUpBalanceError is reconstructed identically (same type, same
    numbers), so unmigrated customer money still surfaces loud.
  * A malformed answer falls back to the DB instead of being interpreted. An
    empty budget reads as "unlicensed" and would block a paying customer.
  * The read leaves opt into the bounded retry; the trial write opts in too,
    which is only safe because provisioning is idempotent.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

import src.billing.project_budgets_service as pbs
import src.budget.routes as broutes
from src.budget.calculator import MonthlyBudgetEntry, TopUpLot, UserBudget
from src.budget.topup_store import LegacyTopUpBalanceError
from src.platform_client import PlatformResponse, PlatformUnavailable


def _resp(status, body):
    return PlatformResponse(status_code=status, json=body)


def _sample_budget() -> UserBudget:
    return UserBudget(
        user_id=str(uuid.uuid4()),
        monthly_budgets={
            "engelmann": MonthlyBudgetEntry(limit_eur=25.0, used_eur=4.25,
                                            reset_at="2026-09-01T00:00:00+00:00")
        },
        top_up_lots=[TopUpLot(id="lot-1", amount_eur=10.0,
                              purchased_at="2026-01-01T00:00:00+00:00",
                              expires_at="2027-01-01T00:00:00+00:00")],
    )


# ── C2: allocated plan id ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_c2_returns_plan_id_from_platform():
    with patch("src.platform_client.call_platform",
               new=AsyncMock(return_value=_resp(200, {"planId": "report-project"}))):
        got = await pbs.resolve_allocated_plan_id(uuid.uuid4(), "p-1")
    assert got == "report-project"


@pytest.mark.asyncio
async def test_c2_null_plan_id_is_a_normal_answer():
    with patch("src.platform_client.call_platform",
               new=AsyncMock(return_value=_resp(200, {"planId": None}))):
        got = await pbs.resolve_allocated_plan_id(uuid.uuid4(), "p-1")
    assert got is None


@pytest.mark.asyncio
async def test_c2_409_raises_ambiguous_and_does_not_fall_back():
    """THE mis-billing guard: must stay an exception, must not retry locally."""
    detail = {"detail": {"error": "ambiguous_project_budget", "message": "two plans"}}
    with patch("src.platform_client.call_platform",
               new=AsyncMock(return_value=_resp(409, detail))), \
         patch.object(pbs, "find_allocated_plan_id", new=AsyncMock()) as direct:
        with pytest.raises(pbs.AmbiguousProjectBudget):
            await pbs.resolve_allocated_plan_id(uuid.uuid4(), "p-1")
    direct.assert_not_awaited()


@pytest.mark.asyncio
async def test_c2_unavailable_falls_back_to_direct_db():
    with patch("src.platform_client.call_platform",
               new=AsyncMock(side_effect=PlatformUnavailable("down"))), \
         patch.object(pbs, "find_allocated_plan_id",
                      new=AsyncMock(return_value="report-project")) as direct:
        got = await pbs.resolve_allocated_plan_id(uuid.uuid4(), "p-1")
    assert got == "report-project"
    direct.assert_awaited_once()


@pytest.mark.asyncio
async def test_c2_opts_into_retry():
    with patch("src.platform_client.call_platform",
               new=AsyncMock(return_value=_resp(200, {"planId": None}))) as called:
        await pbs.resolve_allocated_plan_id(uuid.uuid4(), "p-1")
    assert called.await_args.kwargs["retries"] == 1


# ── C3: project pot state ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_c3_returns_state_from_platform():
    state = {"exists": True, "allowed": True, "remainingEur": 3.0, "topUpRemainingEur": 0.0}
    with patch("src.platform_client.call_platform",
               new=AsyncMock(return_value=_resp(200, state))):
        got = await pbs.evaluate_via_platform(uuid.uuid4(), "plan", "p-1", 0.5)
    assert got == state


@pytest.mark.asyncio
async def test_c3_malformed_answer_falls_back_instead_of_being_read_as_no_budget():
    """A missing `exists` key must not silently reroute a paid project call
    onto the monthly pot."""
    fallback = {"exists": True, "allowed": True, "remainingEur": 9.0, "topUpRemainingEur": 0.0}
    with patch("src.platform_client.call_platform",
               new=AsyncMock(return_value=_resp(200, {"unexpected": "shape"}))), \
         patch.object(pbs, "evaluate", new=AsyncMock(return_value=fallback)) as direct:
        got = await pbs.evaluate_via_platform(uuid.uuid4(), "plan", "p-1", 0.5)
    assert got == fallback
    direct.assert_awaited_once()


# ── C4: monthly pot state ──────────────────────────────────────────────────

def test_c4_serialize_deserialize_round_trip_is_lossless():
    """The wire shape must survive both directions — a dropped field here would
    silently shrink somebody's budget."""
    original = _sample_budget()
    wire = {
        "userId": original.user_id,
        "monthlyBudgets": broutes._serialize_monthly_budgets(original.monthly_budgets),
        "topUpLots": broutes._serialize_topup_lots(original.top_up_lots),
    }
    back = broutes._deserialize_user_budget(wire)
    assert back.user_id == original.user_id
    assert back.monthly_budgets == original.monthly_budgets
    assert back.top_up_lots == original.top_up_lots


@pytest.mark.asyncio
async def test_c4_loads_budget_from_platform():
    original = _sample_budget()
    wire = {
        "userId": original.user_id,
        "monthlyBudgets": broutes._serialize_monthly_budgets(original.monthly_budgets),
        "topUpLots": broutes._serialize_topup_lots(original.top_up_lots),
    }
    with patch("src.platform_client.call_platform",
               new=AsyncMock(return_value=_resp(200, wire))):
        got = await broutes._load_budget_via_platform(uuid.UUID(original.user_id))
    assert got.monthly_budgets == original.monthly_budgets


@pytest.mark.asyncio
async def test_c4_409_reconstructs_the_identical_legacy_error():
    uid = uuid.uuid4()
    detail = {"detail": {"error": "legacy_topup_balance", "message": "x",
                         "userId": str(uid), "balanceEur": 4.2}}
    with patch("src.platform_client.call_platform",
               new=AsyncMock(return_value=_resp(409, detail))):
        with pytest.raises(LegacyTopUpBalanceError) as exc:
            await broutes._load_budget_via_platform(uid)
    assert exc.value.user_id == uid
    assert exc.value.balance_eur == pytest.approx(4.2)


@pytest.mark.asyncio
async def test_c4_unavailable_falls_back_to_direct_db():
    original = _sample_budget()
    with patch("src.platform_client.call_platform",
               new=AsyncMock(side_effect=PlatformUnavailable("down"))), \
         patch.object(broutes, "_load_user_budget_direct",
                      new=AsyncMock(return_value=original)) as direct:
        got = await broutes._load_budget_via_platform(uuid.uuid4())
    assert got is original
    direct.assert_awaited_once()


# ── D2: trial provisioning ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_d2_returns_refreshed_budget_and_opts_into_retry():
    original = _sample_budget()
    payload = {
        "provisioned": True,
        "trialPlanId": "engelmann-trial",
        "state": {
            "userId": original.user_id,
            "monthlyBudgets": broutes._serialize_monthly_budgets(original.monthly_budgets),
            "topUpLots": broutes._serialize_topup_lots(original.top_up_lots),
        },
    }
    trial = object()
    with patch("src.platform_client.call_platform",
               new=AsyncMock(return_value=_resp(200, payload))) as called:
        got = await broutes._ensure_trial_via_platform(
            uuid.UUID(original.user_id), "engelmann", trial
        )
    assert got.monthly_budgets == original.monthly_budgets
    assert called.await_args.kwargs["retries"] == 1
