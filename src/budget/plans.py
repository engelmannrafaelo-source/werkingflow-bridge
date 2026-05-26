"""
Plan configuration — DB-backed since migration 020_plans_table.sql.

The PLANS dict is a runtime cache populated at Bridge startup by
reload_plans() (called from platform_main.lifespan). Plan attributes
(price, description, name, ...) now live in the `plans` table and can
be iterated on without redeploying the Bridge. Plan IDs themselves stay
in the plan_id Postgres enum (typed FK from subscriptions/app_licenses).

Hot-swap: POST /v1/billing/plans/reload (operator-only) re-reads the
table and atomically replaces PLANS contents. Workers that hold a
module-level reference to PLANS see the new state without restart.
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


# Mutable runtime cache. Populated by reload_plans() at startup. Mutated
# in-place by reload — callers holding a reference see the new contents.
PLANS: dict[str, PlanConfig] = {}


async def reload_plans() -> int:
    """
    Re-read the plans table and atomically replace the PLANS dict contents.
    Returns the new plan count. Fails loud if the table is empty.

    Called from platform_main.lifespan on startup, and from the hot-reload
    admin endpoint after a price/description edit.
    """
    # Local import to avoid a startup-time circular dependency: plan_repo
    # imports PlanConfig from this module.
    from src.budget.plan_repo import load_plans_from_db
    new_plans = await load_plans_from_db()
    PLANS.clear()
    PLANS.update(new_plans)
    return len(PLANS)


def get_plan(plan_id: str) -> PlanConfig:
    plan = PLANS.get(plan_id)
    if not plan:
        # Empty PLANS would silently 404 every consumer — surface that as
        # a distinct error so it's obvious in logs that reload_plans()
        # never ran (or returned an empty set).
        if not PLANS:
            raise RuntimeError(
                f"[PlanManager] PLANS cache is empty — reload_plans() was "
                f"never called, or the plans table has no active rows. "
                f"Cannot resolve plan_id={plan_id!r}."
            )
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
