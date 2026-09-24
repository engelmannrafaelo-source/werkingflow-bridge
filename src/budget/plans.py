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
    # Validate the cache we are about to serve from, not the one we replaced.
    # A catalog that cannot say which pot pays for a call must surface at boot
    # (or at hot-reload), not as a mis-billed call hours later.
    assert_catalog_is_unambiguous()
    return len(PLANS)



async def reload_plans_from_platform() -> int:
    """
    Load the plan catalog over platform-api — for a worker WITHOUT BRIDGE_DB_URL.

    Befund 24.09.2026 (prod): the four prod workers run without BRIDGE_DB_URL,
    main.lifespan loaded the catalog only in its DB branch, PLANS stayed empty,
    and _deduct_call_cost ended EVERY call with "app not in the plan catalog"
    — no POST /v1/budget/deduct ever left a worker, for any app.

    Source is GET /v1/billing/plans on platform-api (served from ITS PLANS,
    loaded from the plans table, is_active rows only). Same fail-loud contract
    as reload_plans(): an empty or malformed answer raises and leaves PLANS
    untouched — a worker that cannot meter must not start serving.
    """
    from src.platform_client import call_platform

    resp = await call_platform("GET", "/v1/billing/plans", retries=3, timeout_s=5.0)
    rows = resp.json.get("plans") if isinstance(resp.json, dict) else None
    if resp.status_code != 200 or not isinstance(rows, list):
        raise RuntimeError(
            f"[PlanManager] plan catalog via platform-api unusable: "
            f"status={resp.status_code} body={str(resp.json)[:200]!r}"
        )
    new_plans: dict[str, PlanConfig] = {}
    for row in rows:
        try:
            new_plans[row["id"]] = PlanConfig(
                id=row["id"],
                app_id=row["appId"],
                name=row["name"],
                price=float(row["priceEur"]),
                interval=row["interval"],
                api_budget_eur=float(row["apiBudgetEur"]),
                description=row.get("description") or "",
                trial=bool(row.get("trial", False)),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise RuntimeError(f"[PlanManager] malformed plan row from platform-api: {row!r}: {e}") from e
    if not new_plans:
        raise RuntimeError(
            "[PlanManager] platform-api returned an EMPTY plan catalog — refusing to "
            "serve un-metered traffic."
        )
    PLANS.clear()
    PLANS.update(new_plans)
    assert_catalog_is_unambiguous()
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


class AmbiguousPlanCatalog(RuntimeError):
    """An app declares more billable plans of one interval than can be told apart.

    A configuration error, never a runtime condition: it means the catalog
    cannot answer "which pot pays for this call?" without guessing.
    """


def find_monthly_plan_for_app(app_id: str) -> "PlanConfig | None":
    """
    Return the app's single billable monthly plan, or None if it has none.

    Raises AmbiguousPlanCatalog when an app declares more than one — two
    monthly pots for one app means every metering decision would be a coin
    flip, and a coin flip that mis-bills is exactly what this function
    exists to prevent.
    """
    monthly = [p for p in PLANS.values() if p.app_id == app_id and not p.trial and p.interval == "month"]
    if len(monthly) > 1:
        raise AmbiguousPlanCatalog(
            f"[PlanManager] app_id={app_id!r} declares {len(monthly)} billable monthly plans "
            f"({sorted(p.id for p in monthly)}). Exactly one monthly pot per app is required — "
            f"the metering path cannot choose between them."
        )
    return monthly[0] if monthly else None


def find_project_plans_for_app(app_id: str) -> "tuple[PlanConfig, ...]":
    """
    Every billable per-project plan of an app, in stable id order.

    An app may legitimately declare several (Report sells check credits as
    1/5/20 packs). Which of them pays for a given call is decided by the
    allocated project budget, not by this catalog — see
    src.budget.plan_resolution.resolve_billing_plan.
    """
    return tuple(
        sorted(
            (p for p in PLANS.values() if p.app_id == app_id and not p.trial and p.interval == "project"),
            key=lambda p: p.id,
        )
    )


def assert_catalog_is_unambiguous() -> None:
    """
    Boot-time invariant: every app can be metered without guessing.

    Called from reload_plans() so both the worker and platform-api validate
    the same cache they will serve from. Same doctrine as the surrounding
    fail-fast checks: a process that cannot bill correctly must refuse to
    start rather than serve traffic that looks healthy while silently
    charging the wrong pot.
    """
    for app_id in {p.app_id for p in PLANS.values()}:
        find_monthly_plan_for_app(app_id)  # raises on two monthly pots
        # Several per-project plans WITHOUT a monthly plan is a legal shape
        # since migration 049: werking-check sells only credit packs (1/5/20)
        # and has no monthly pot. The invariant "no call may be attributed by
        # guessing" moved from boot to the call site — resolve_billing_plan
        # raises PlanResolutionError for an UNALLOCATED call of such an app
        # instead of picking a tier. Boot keeps failing only on shapes no
        # resolution rule exists for (two monthly pots, via the line above).


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
