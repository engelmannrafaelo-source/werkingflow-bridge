"""
Plan configuration (Python port of PlanManager.ts).
SSoT: werkingflow-business/shared/operations/PRICING-STRATEGY.md
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PlanConfig:
    id: str
    app_id: str
    name: str
    price: float        # EUR
    interval: str       # month | project | once
    api_budget_eur: float
    description: str
    trial: bool = False


PLANS: dict[str, PlanConfig] = {
    "trial": PlanConfig(
        id="trial",
        app_id="werking-report",
        name="7-Tage-Test",
        price=0,
        interval="month",
        api_budget_eur=5,
        trial=True,
        description="7 Tage kostenlos, voller Zugang. Danach EUR 250 oder weg.",
    ),
    "report-standard": PlanConfig(
        id="report-standard",
        app_id="werking-report",
        name="Standard",
        price=250,
        interval="month",
        api_budget_eur=50,
        description="Voller Funktionsumfang. KI-Budget inklusive, weitere Sitze zum gleichen Preis.",
    ),
    "energy-project": PlanConfig(
        id="energy-project",
        app_id="werking-energy",
        name="Energy-Projekt",
        price=1000,
        interval="project",
        api_budget_eur=100,
        description="KI-Budget inklusive. Beliebig viele Neuberechnungen, solange das Budget reicht.",
    ),
    # safety-project removed 2026-05-26 — WerkING Safety ist pausiert (nicht in Entwicklung).
    # Reaktivieren: Eintrag wiederherstellen aus git log, Bridge restart.
    "noise-tbd": PlanConfig(
        id="noise-tbd",
        app_id="werking-noise",
        name="WerkING Noise",
        price=0,
        interval="month",
        api_budget_eur=0,
        description="Akustik-Gutachten mit KI-Unterstützung. Aktuell in Beta-Tests.",
    ),
    "engelmann-custom": PlanConfig(
        id="engelmann-custom",
        app_id="engelmann",
        name="Engelmann Custom",
        price=0,
        interval="month",
        api_budget_eur=0,
        description="Custom-Projekt, kein WerkING-Produkt. Separate Konditionen.",
    ),
}


def get_plan(plan_id: str) -> PlanConfig:
    plan = PLANS.get(plan_id)
    if not plan:
        raise ValueError(f"[PlanManager] Unknown plan: {plan_id}")
    return plan


def find_plan_for_app(app_id: str) -> "PlanConfig | None":
    """
    Return the billable (non-trial) plan for an app, or None if the app
    has no plan in the catalog. Used by the chat/completions budget gate
    to resolve which plan a call is metered against.
    """
    for p in PLANS.values():
        if p.app_id == app_id and not p.trial:
            return p
    return None


def find_trial_plan_for(plan_id: str) -> "PlanConfig | None":
    """Find a trial-plan with same app_id as the given plan. Returns None
    if no trial-sibling exists for this app (most apps have none)."""
    plan = PLANS.get(plan_id)
    if plan is None:
        raise ValueError(f"[PlanManager] Unknown plan: {plan_id}")
    if plan.trial:
        # This plan IS the trial; no separate sibling exists.
        return None
    for p in PLANS.values():
        if p.app_id == plan.app_id and p.trial:
            return p
    return None
