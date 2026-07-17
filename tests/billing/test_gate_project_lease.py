"""
Regression: a project-interval plan (e.g. Energy 'energy-project') must NOT be
blocked by the budget gate when no project_id is supplied — that is the
sandbox-editor OAuth lease path. Before the fix the call fell into the monthly
evaluate_budget path, which returns reason='unlicensed' for any plan with no
monthly budget entry, producing a 402 that broke the Energy report editor for
every user.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.budget.gate import enforce_budget
from src.budget.plans import PlanConfig

_UID = uuid.UUID("11111111-2222-3333-4444-555555555555")

_PROJECT_PLAN = PlanConfig(
    id="energy-project", app_id="werking-energy", name="Energy Projekt",
    price=100.0, interval="project", api_budget_eur=100.0, description="",
)


@pytest.mark.asyncio
async def test_project_plan_without_project_id_is_let_through():
    """Sandbox lease (no project_id) on a project plan must NOT raise."""
    with patch("src.budget.gate.find_plan_for_app", return_value=_PROJECT_PLAN), \
         patch("src.budget.gate.resolve_user_id", AsyncMock(return_value=_UID)), \
         patch("src.budget.gate.evaluate_budget", AsyncMock(
             return_value={"allowed": False, "reason": "unlicensed",
                           "effectivePlanId": "energy-project",
                           "totalRemainingEur": 0.0})) as mock_monthly:
        # Must not raise — the lease is a cost-0 token acquire.
        await enforce_budget(str(_UID), "werking-energy", 0.0)
        # And it must NOT have consulted the monthly path at all.
        mock_monthly.assert_not_called()


@pytest.mark.asyncio
async def test_project_plan_with_exhausted_project_budget_still_blocks():
    """With a project_id and an exhausted per-project budget, the gate blocks."""
    with patch("src.budget.gate.find_plan_for_app", return_value=_PROJECT_PLAN), \
         patch("src.budget.gate.resolve_user_id", AsyncMock(return_value=_UID)), \
         patch("src.billing.project_budgets_service.evaluate", AsyncMock(
             return_value={"exists": True, "allowed": False, "remainingEur": 0.0,
                           "topUpRemainingEur": 0.0})):
        with pytest.raises(HTTPException) as ei:
            await enforce_budget(str(_UID), "werking-energy", 0.01, project_id="Proj_1")
        assert ei.value.status_code == 402
        assert ei.value.detail["reason"] == "project_budget_exhausted"
        assert ei.value.detail["totalRemainingEur"] == 0.0


@pytest.mark.asyncio
async def test_project_plan_lets_call_through_when_topup_covers_exhausted_project():
    """Regression for the KFR bug: project pot exhausted but the user's TopUp
    balance covers the call — project_budgets_service.evaluate() already
    folds the TopUp fallback into `allowed`; the gate must trust it and NOT
    raise 402 while TopUp money sits unused."""
    with patch("src.budget.gate.find_plan_for_app", return_value=_PROJECT_PLAN), \
         patch("src.budget.gate.resolve_user_id", AsyncMock(return_value=_UID)), \
         patch("src.billing.project_budgets_service.evaluate", AsyncMock(
             return_value={"exists": True, "allowed": True, "remainingEur": 0.0,
                           "topUpRemainingEur": 15500.0})):
        # Must not raise — TopUp covers the exhausted project budget.
        await enforce_budget(str(_UID), "werking-energy", 5.0, project_id="Proj_1")
