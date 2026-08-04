"""
Which pot pays for a call — the discriminator is the ALLOCATED budget.

Regression cover for the defect found on 2026-08-04 by walking a real Akt
(chk-aab8f249…) end to end: werking-report sells a monthly plan AND per-project
check credits, `find_plan_for_app` returned whichever came first, and the
month-vs-project routing hung off that arbitrary pick. The paid correction of a
9-EUR Akt was billed to the customer's monthly trial pot while the Akt's own
5-EUR budget sat untouched — so an exhausted monthly pot refuses a deliverable
the customer already paid for.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.budget.plan_resolution import PlanResolutionError, resolve_billing_plan
from src.budget.plans import (
    AmbiguousPlanCatalog,
    PLANS,
    PlanConfig,
    find_monthly_plan_for_app,
    find_project_plans_for_app,
)

_UID = uuid.UUID("7d67b1fb-7f96-4e88-98d2-5865b8bfbc5b")
_AKT = "chk-aab8f2494075f7da831877252800a378"


def _allocated(plan_id: str | None):
    """Patch the only DB touch in the resolver: the allocation lookup."""
    return patch(
        "src.billing.project_budgets_service.find_allocated_plan_id",
        AsyncMock(return_value=plan_id),
    )


@pytest.mark.asyncio
async def test_paid_akt_draws_from_its_own_project_budget():
    """An allocated check credit makes the check plan pay — not the monthly pot."""
    with _allocated("report-check-credit"):
        plan = await resolve_billing_plan("werking-report", _UID, _AKT)
    assert plan is not None
    assert plan.id == "report-check-credit"
    assert plan.interval == "project"


@pytest.mark.asyncio
async def test_ordinary_report_call_stays_on_the_monthly_plan():
    """Every ordinary report call carries a project_id too — with no allocation
    behind it, it must keep drawing from the monthly budget. This is why the
    allocation, not the bare project_id, is the discriminator."""
    with _allocated(None):
        plan = await resolve_billing_plan("werking-report", _UID, "proj-ordinary-report")
    assert plan is not None
    assert plan.id == "report-standard"
    assert plan.interval == "month"


@pytest.mark.asyncio
async def test_free_check_phase_stays_on_the_monthly_plan():
    """The free pre-payment part of a check runs before any credit is consumed,
    so nothing is allocated yet — it belongs on the monthly pot (migration 046)."""
    with _allocated(None):
        plan = await resolve_billing_plan("werking-report", _UID, _AKT)
    assert plan is not None
    assert plan.id == "report-standard"


@pytest.mark.asyncio
async def test_project_only_app_resolves_without_an_allocation():
    """Energy has no monthly plan. Its project's FIRST call has no allocation
    yet — lazy allocation by the post-call deduction must stay possible."""
    with _allocated(None):
        plan = await resolve_billing_plan("werking-energy", _UID, "Proj_1")
    assert plan is not None
    assert plan.id == "energy-project"


@pytest.mark.asyncio
async def test_app_outside_the_catalog_is_not_budget_tracked():
    with _allocated(None):
        assert await resolve_billing_plan("some-unlisted-app", _UID, "p1") is None


@pytest.mark.asyncio
async def test_allocation_pointing_at_a_foreign_app_fails_loud():
    """An allocation is proof of payment. If it names a plan that cannot pay for
    this call, guessing another pot would hide a row nobody can bill."""
    with _allocated("energy-project"):
        with pytest.raises(PlanResolutionError):
            await resolve_billing_plan("werking-report", _UID, _AKT)


@pytest.mark.asyncio
async def test_ambiguous_allocation_fails_loud():
    from src.billing.project_budgets_service import AmbiguousProjectBudget

    with patch(
        "src.billing.project_budgets_service.find_allocated_plan_id",
        AsyncMock(side_effect=AmbiguousProjectBudget("two plans for one project")),
    ):
        with pytest.raises(PlanResolutionError):
            await resolve_billing_plan("werking-report", _UID, _AKT)


def test_catalog_lookups_separate_the_two_intervals():
    monthly = find_monthly_plan_for_app("werking-report")
    projects = find_project_plans_for_app("werking-report")
    assert monthly is not None and monthly.id == "report-standard"
    assert [p.id for p in projects] == ["report-check-credit", "report-check-credit-5"]
    assert find_monthly_plan_for_app("werking-energy") is None


def test_two_monthly_plans_for_one_app_fail_loud():
    """Two monthly pots for one app make every metering decision a coin flip."""
    PLANS["report-second-monthly"] = PlanConfig(
        id="report-second-monthly", app_id="werking-report", name="Zweiter Monatsplan",
        price=1, interval="month", api_budget_eur=1, description="", trial=False,
    )
    try:
        with pytest.raises(AmbiguousPlanCatalog):
            find_monthly_plan_for_app("werking-report")
    finally:
        del PLANS["report-second-monthly"]


def test_boot_invariant_rejects_an_unattributable_catalog():
    """A project-only app with several project plans could not attribute a call
    that has no allocation yet — the boot invariant must refuse that catalog."""
    from src.budget.plans import assert_catalog_is_unambiguous

    assert_catalog_is_unambiguous()  # the seeded catalog is coherent
    PLANS["energy-project-xl"] = PlanConfig(
        id="energy-project-xl", app_id="werking-energy", name="Energy XL",
        price=2000, interval="project", api_budget_eur=200, description="", trial=False,
    )
    try:
        with pytest.raises(AmbiguousPlanCatalog):
            assert_catalog_is_unambiguous()
    finally:
        del PLANS["energy-project-xl"]
