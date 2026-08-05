"""
Which plan pays for one AI call?

Until 2026-08-04 both metering call sites (the pre-call gate and the post-call
deduction) asked `find_plan_for_app(app_id)` — "the" billable plan of an app.
That question has no answer once an app sells more than one kind of
entitlement. werking-report does: a monthly plan (`report-standard`) plus
per-project check credits (`report-check-credit*`). The old lookup returned
whichever the PLANS dict happened to yield first, and the whole month-vs-project
routing hung off that arbitrary pick.

Live consequence (Akt chk-aab8f249…, 2026-08-04): the monthly plan won, so the
`interval == "project"` branch was never taken. A paid check Akt got its 5 EUR
budget allocated at unlock (`project_budgets.used_eur` stayed 0.0000) while the
correction it paid for was billed to the customer's monthly trial pot
(`user_budgets` trial usedEur 2.561). With that pot exhausted — as it was for
the real customer on 2026-08-03, 4.709 of 5.00 — the paid deliverable is
refused although its own budget is untouched. Migration 046 and the app-side
attribution were both correct and both ineffective.

The fix is to resolve the plan per CALL, from the entitlement that actually
paid, and the evidence for that is the allocated project budget:

  * an allocation for (user, project_id) exists  → that project plan pays
  * otherwise                                    → the app's monthly plan
  * app with no monthly plan (Energy)            → its single project plan,
    preserving lazy allocation on a project's first call

The allocation — not the bare presence of a project_id — is the discriminator,
and it has to be: every ordinary werking-report call carries a project_id too,
and those must keep drawing from the monthly budget. The free pre-payment part
of a check therefore also stays on the monthly pot, which is exactly what
migration 046 describes.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from src.budget.plans import (
    PlanConfig,
    find_monthly_plan_for_app,
    find_project_plans_for_app,
    get_plan,
)

logger = logging.getLogger(__name__)


class PlanResolutionError(RuntimeError):
    """This call cannot be attributed to exactly one paying plan.

    The resolver's own error contract: callers gate on this without importing
    the budgets service (and its DB pool) just to name a failure mode.
    """


async def resolve_billing_plan(
    app_id: str,
    user_id: uuid.UUID,
    project_id: Optional[str],
) -> Optional[PlanConfig]:
    """
    Return the plan this call must be metered against, or None if the app is
    not in the plan catalog (not budget-tracked).

    Raises AmbiguousPlanCatalog (incoherent catalog) or PlanResolutionError
    (incoherent allocation). Callers apply their own policy for those; neither
    may be answered with a guess, because a wrong plan here silently charges
    the wrong pot.
    """
    monthly = find_monthly_plan_for_app(app_id)
    project_plans = find_project_plans_for_app(app_id)

    if monthly is None and not project_plans:
        return None  # app not in the plan catalog — not budget-tracked

    if project_id and project_plans:
        # Local import: project_budgets_service pulls the DB pool, which the
        # catalog layer must not depend on.
        from src.billing.project_budgets_service import (
            AmbiguousProjectBudget,
            find_allocated_plan_id,
        )

        try:
            allocated_id = await find_allocated_plan_id(user_id, project_id)
        except AmbiguousProjectBudget as e:
            raise PlanResolutionError(str(e)) from e
        if allocated_id:
            plan = get_plan(allocated_id)
            if plan.app_id != app_id or plan.interval != "project":
                # The allocation is the proof of payment; if it points at a
                # plan that cannot pay for this call, something wrote a row
                # nobody can bill. Guessing a different pot would hide it.
                raise PlanResolutionError(
                    f"project_id={project_id!r} is allocated under plan {allocated_id!r} "
                    f"(app={plan.app_id!r}, interval={plan.interval!r}), which cannot pay "
                    f"for a call of app={app_id!r}."
                )
            return plan

    if monthly is not None:
        return monthly

    # Project-only app with SEVERAL per-project plans (werking-check sells
    # 1/5/20 credit packs, migration 049): an unallocated call cannot be
    # attributed — the tiers differ only in price, guessing one would silently
    # charge the wrong pot. Such a call is also a caller bug by contract: the
    # free check funnel attributes itself as werking-report (migration 046
    # doctrine), only allocated (paid) work carries werking-check
    # (check-attribution.ts in werking-report). Refuse loudly so the
    # mis-attributed caller surfaces immediately.
    if len(project_plans) > 1:
        raise PlanResolutionError(
            f"app_id={app_id!r} declares several per-project plans "
            f"({[p.id for p in project_plans]}) and no monthly plan, and this call "
            f"(user_id={user_id}, project_id={project_id!r}) has no allocated project "
            f"budget. An unallocated call for such an app cannot be attributed to a "
            f"pot — the caller must either run on an allocation (paid work) or "
            f"attribute itself to the app that carries its free tier."
        )

    # Project-only app with exactly one plan (Energy). This is a resolution,
    # not a pick: the project's first call has no allocation yet and the
    # post-call deduction allocates it.
    return project_plans[0]
