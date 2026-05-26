"""
Plan repository — load PlanConfig rows from the Postgres `plans` table.

Replaces the hardcoded PLANS dict in plans.py. The module-level PLANS dict
in plans.py is now a mutable cache; it is populated by reload_plans() at
Bridge startup (platform_main.lifespan) and can be re-populated on demand
via the /v1/billing/plans/reload admin endpoint.

Design:
  - reload_plans() reads only is_active=TRUE rows from `plans`. Deactivated
    rows stay in the DB (FK-safe for historical subscriptions) but are
    invisible to the runtime catalog.
  - On reload, the existing PLANS dict is mutated in-place (clear + update),
    so callers holding a reference to PLANS see the new state without
    re-importing. Fail-fast at every step: missing DB / empty result / row
    parsing errors all raise — Bridge would rather not start than serve a
    silently empty catalog.
"""
from typing import Dict

from src.budget.plans import PlanConfig
from src.db.client import get_pool


async def load_plans_from_db() -> Dict[str, PlanConfig]:
    """
    Fetch active plans from the database and return them as a dict keyed by
    plan id. Raises RuntimeError if the catalog is empty — never silently
    serve zero plans, which would 404 every customer in the portal.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, app_id, name, price_eur, interval, api_budget_eur,
                   description, is_trial
            FROM plans
            WHERE is_active = TRUE
            ORDER BY sort_order, id
            """
        )

    if not rows:
        raise RuntimeError(
            "[plan_repo] plans table has no active rows — refusing to start "
            "with an empty catalog. Check migration 020 was applied and that "
            "no manual UPDATE flipped all rows to is_active=FALSE."
        )

    result: Dict[str, PlanConfig] = {}
    for row in rows:
        plan_id = row["id"]
        result[plan_id] = PlanConfig(
            id=plan_id,
            app_id=row["app_id"],
            name=row["name"],
            price=float(row["price_eur"]),
            interval=row["interval"],
            api_budget_eur=float(row["api_budget_eur"]),
            description=row["description"],
            trial=row["is_trial"],
        )
    return result
