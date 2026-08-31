"""Minimal plan-catalog seed for the gate-polarity tests.

PLANS is DB-backed (migration 020) and empty in offline tests; the two gate
tests here only need ONE catalogued app (werking-report, monthly) so the
pre-flight actually runs — the full canonical seed lives in
tests/budget/conftest.py and is not duplicated beyond that.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _seed_minimal_plans():
    from src.budget.plans import PLANS, PlanConfig

    seeded = {
        "report-standard": PlanConfig(
            id="report-standard", app_id="werking-report", name="Standard",
            price=250, interval="month", api_budget_eur=100, description="",
            trial=False),
    }
    saved = dict(PLANS)
    PLANS.clear()
    PLANS.update(seeded)
    try:
        yield
    finally:
        PLANS.clear()
        PLANS.update(saved)
