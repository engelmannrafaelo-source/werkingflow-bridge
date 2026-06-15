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
    """SUM(quantity - used) for user+plan, only rows where used < quantity."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(quantity - used), 0) AS available
              FROM manual_project_credits
             WHERE user_id = $1
               AND plan_id = $2
               AND used < quantity
            """,
            user_id,
            plan_id,
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


async def consume_credit(
    user_id: uuid.UUID, plan_id: str, project_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Decrement available credit by 1 (FIFO: oldest row first).

    Uses SELECT ... FOR UPDATE in a transaction to prevent concurrent double-consume.
    Raises CreditsExhaustedError if no available credit exists.
    Returns the consumed credit row for audit logging.

    When `project_id` is given, the consumed slot's per-project API budget is
    allocated in the SAME transaction (atomic: a project gets exactly its
    plan.api_budget_eur the moment its slot is consumed). Allocation is
    idempotent, so a retried job-create never double-allocates. Omitting
    project_id keeps the legacy behaviour (slot only; the budget then falls
    back to the monthly tenant budget downstream).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
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
                plan_id,
            )
            if not row:
                raise CreditsExhaustedError(plan_id)

            await conn.execute(
                "UPDATE manual_project_credits SET used = used + 1 WHERE id = $1",
                row["id"],
            )

            if project_id:
                # Allocate this project's API budget atomically with the slot.
                from src.budget.plans import get_plan
                from src.billing.project_budgets_service import allocate_budget

                plan = get_plan(plan_id)
                await allocate_budget(
                    conn,
                    user_id=user_id,
                    tenant_id=row["tenant_id"],
                    plan_id=plan_id,
                    project_id=project_id,
                    limit_eur=float(plan.api_budget_eur),
                    credit_id=row["id"],
                )

    return {
        "creditId": str(row["id"]),
        "planId": row["plan_id"],
        "quantityBefore": int(row["quantity"]),
        "usedBefore": int(row["used"]),
        "orderId": str(row["order_id"]),
        "grantedAt": row["granted_at"].isoformat(),
    }
