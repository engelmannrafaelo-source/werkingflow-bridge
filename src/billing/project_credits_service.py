"""
Project Credits Service — Slot-Zähler für manuelle Projekt-Bestellungen.

Workflow:
  release_order (interval='project') → INSERT in manual_project_credits (quantity Slots, used=0)
  Energy-App Job-Creation → consume_credit (SELECT FOR UPDATE + UPDATE used+1)

Fail-fast:
  - CreditsExhaustedError wenn keine verfügbaren Slots
  - Transaction + SELECT FOR UPDATE verhindert concurrent Double-Consume
  - DB-CHECK (used <= quantity) als letzte Absicherung
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from src.db.client import get_pool


class CreditsExhaustedError(Exception):
    """No available project credits remain for a user+plan combination."""
    def __init__(self, plan_id: str) -> None:
        super().__init__(f"No available project credits for plan '{plan_id}'")
        self.plan_id = plan_id


async def get_available_credits(user_id: uuid.UUID, plan_id: str) -> int:
    """
    Slots usable for `plan_id`: SUM(quantity - used) over every credit row
    consume_credit would accept for it (own kind + pot plan's legacy credits,
    see src.budget.plans.accepted_credit_plan_ids).
    """
    from src.budget.plans import accepted_credit_plan_ids

    accepted = list(accepted_credit_plan_ids(plan_id))
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(quantity - used), 0) AS available
              FROM manual_project_credits
             WHERE user_id = $1
               AND plan_id = ANY($2::varchar[])
               AND used < quantity
            """,
            user_id,
            accepted,
        )
    return int(row["available"]) if row else 0


async def list_user_credits(user_id: uuid.UUID) -> List[Dict[str, Any]]:
    """List all credit rows for user, grouped by plan_id with totals."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT plan_id,
                   SUM(quantity) AS total,
                   SUM(used)     AS used,
                   SUM(quantity - used) AS available
              FROM manual_project_credits
             WHERE user_id = $1
             GROUP BY plan_id
            """,
            user_id,
        )
    return [
        {
            "planId": r["plan_id"],
            "total": int(r["total"]),
            "used": int(r["used"]),
            "available": int(r["available"]),
        }
        for r in rows
    ]


def usable_credits_by_plan(credits: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Per catalog project plan: how many slots consume_credit would accept for
    it, derived from the per-kind rows of list_user_credits.

    The per-kind list alone cannot answer "can this user start an
    Objektbericht?" once legacy 'energy-project' credits count for every
    Energy credit kind — the app would have to know the migration rule. It
    reads this map instead. Only plans with at least one usable slot appear.
    """
    from src.budget.plans import PLANS, accepted_credit_plan_ids

    available = {c["planId"]: c["available"] for c in credits}
    usable: Dict[str, int] = {}
    for plan in PLANS.values():
        if plan.interval != "project" or plan.trial:
            continue
        n = sum(available.get(pid, 0) for pid in accepted_credit_plan_ids(plan.id))
        if n > 0:
            usable[plan.id] = n
    return usable


async def consume_credit(
    user_id: uuid.UUID, plan_id: str, project_id: Optional[str] = None,
    hard_cap: bool = False,
) -> Dict[str, Any]:
    """
    Decrement available credit by 1.

    Which rows qualify is decided by src.budget.plans.accepted_credit_plan_ids:
    the requested kind's own credits first (FIFO), then — for a credit kind
    with a pot plan (migration 066/067) — the pot plan's credits (FIFO). That
    second step is the migration path for credits bought as the former single
    Energy kind 'energy-project': they stay valid for every Berichtsart. The
    response names the kind that actually paid (`planId`) next to the
    requested one (`requestedPlanId`), so the substitution is never silent.

    Uses SELECT ... FOR UPDATE in a transaction to prevent concurrent double-consume.
    Raises CreditsExhaustedError if no available credit exists.
    Returns the consumed credit row for audit logging.

    When `project_id` is given, the consumed slot's per-project API budget is
    allocated in the SAME transaction (atomic: a project gets exactly its
    plan.api_budget_eur the moment its slot is consumed). The allocation is
    keyed by the POT plan (budget_plan_for), never by a credit kind — the
    metering path only knows pots. Allocation is idempotent, so a retried
    job-create never double-allocates. `hard_cap=True` marks that
    budget as a run ceiling (no TopUp fallback, see project_budgets_service). Omitting project_id keeps the legacy
    behaviour (slot only; the budget then self-provisions lazily downstream).
    """
    from src.budget.plans import accepted_credit_plan_ids, budget_plan_for, get_plan

    requested = get_plan(plan_id)  # unknown plan → ValueError, before any DB work
    accepted = accepted_credit_plan_ids(plan_id)

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = None
            for credit_plan_id in accepted:
                row = await conn.fetchrow(
                    """
                    SELECT id, plan_id, tenant_id, quantity, used, granted_at, order_id
                      FROM manual_project_credits
                     WHERE user_id = $1
                       AND plan_id = $2
                       AND used < quantity
                     ORDER BY granted_at ASC
                     LIMIT 1
                       FOR UPDATE
                    """,
                    user_id,
                    credit_plan_id,
                )
                if row:
                    break
            if not row:
                raise CreditsExhaustedError(plan_id)

            await conn.execute(
                "UPDATE manual_project_credits SET used = used + 1 WHERE id = $1",
                row["id"],
            )

            if project_id:
                # Allocate this project's API budget atomically with the slot,
                # under the pot plan; the limit is the paying kind's budget.
                from src.billing.project_budgets_service import allocate_budget

                paying = get_plan(row["plan_id"])
                await allocate_budget(
                    conn,
                    user_id=user_id,
                    tenant_id=row["tenant_id"],
                    plan_id=budget_plan_for(requested).id,
                    project_id=project_id,
                    limit_eur=float(paying.api_budget_eur),
                    credit_id=row["id"],
                    hard_cap=hard_cap,
                )

    return {
        "creditId": str(row["id"]),
        "planId": row["plan_id"],
        "requestedPlanId": requested.id,
        "legacyCredit": row["plan_id"] != requested.id,
        "quantityBefore": int(row["quantity"]),
        "usedBefore": int(row["used"]),
        "orderId": str(row["order_id"]),
        "grantedAt": row["granted_at"].isoformat(),
    }
