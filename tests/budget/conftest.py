"""
Break the circular import chain that exists in production (and only surfaces in test):
  src.budget.__init__ → routes → api_auth → api_auth.deps → identity.jwt_utils
  → identity.__init__ → identity.routes → api_auth  (partial, circular)

Stubbing out identity.routes before it is imported prevents identity.__init__ from
triggering the re-import of a partially-initialized api_auth.
"""
import sys
from unittest.mock import MagicMock

import pytest

if "src.identity.routes" not in sys.modules:
    _stub = MagicMock()
    _stub.router = MagicMock()
    sys.modules["src.identity.routes"] = _stub


@pytest.fixture(autouse=True)
def _seed_plans():
    """Populate the runtime PLANS cache for offline tests.

    PLANS is DB-backed (migration 020) and normally filled by reload_plans() at
    Bridge startup. Tests have no DB, so we seed the canonical plan set from
    migration 020_plans_table.sql as amended by later plan migrations (045:
    report-standard api_budget_eur 50 -> 100; 046: check credits get a per-Akt
    budget). Autouse so every budget test can call get_plan() /
    find_trial_plan_for() / the plan lookups without a live DB.

    werking-report deliberately carries BOTH a monthly plan and per-project
    check credits here — that is the real catalog since 046, and the shape in
    which "the billable plan of an app" stopped having a single answer.
    """
    from src.budget.plans import PLANS, PlanConfig

    seeded = {
        "trial": PlanConfig(
            id="trial", app_id="werking-report", name="7-Tage-Test", price=0,
            interval="month", api_budget_eur=5, description="", trial=True),
        "report-standard": PlanConfig(
            id="report-standard", app_id="werking-report", name="Standard", price=250,
            interval="month", api_budget_eur=100, description="", trial=False),
        "report-check-credit": PlanConfig(
            id="report-check-credit", app_id="werking-report", name="Check-Guthaben", price=9,
            interval="project", api_budget_eur=5, description="", trial=False),
        "report-check-credit-5": PlanConfig(
            id="report-check-credit-5", app_id="werking-report", name="Check-Guthaben 5", price=35,
            interval="project", api_budget_eur=5, description="", trial=False),
        "energy-project": PlanConfig(
            id="energy-project", app_id="werking-energy", name="Energy-Projekt", price=1000,
            interval="project", api_budget_eur=100, description="", trial=False),
        "noise-tbd": PlanConfig(
            id="noise-tbd", app_id="werking-noise", name="WerkING Noise", price=0,
            interval="month", api_budget_eur=0, description="", trial=False),
        "engelmann-custom": PlanConfig(
            id="engelmann-custom", app_id="engelmann", name="Engelmann Custom", price=0,
            interval="month", api_budget_eur=0, description="", trial=False),
    }
    saved = dict(PLANS)
    PLANS.clear()
    PLANS.update(seeded)
    try:
        yield
    finally:
        PLANS.clear()
        PLANS.update(saved)
