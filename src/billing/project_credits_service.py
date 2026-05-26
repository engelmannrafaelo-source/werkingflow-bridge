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
from typing import Any, Dict, List

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


async def consume_credit(user_id: uuid.UUID, plan_id: str) -> Dict[str, Any]:
    """
    Decrement available credit by 1 (FIFO: oldest row first).

    Uses SELECT ... FOR UPDATE in a transaction to prevent concurrent double-consume.
    Raises CreditsExhaustedError if no available credit exists.
    Returns the consumed credit row for audit logging.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, plan_id, quantity, used, granted_at, order_id
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

    return {
        "creditId": str(row["id"]),
        "planId": row["plan_id"],
        "quantityBefore": int(row["quantity"]),
        "usedBefore": int(row["used"]),
        "orderId": str(row["order_id"]),
        "grantedAt": row["granted_at"].isoformat(),
    }
