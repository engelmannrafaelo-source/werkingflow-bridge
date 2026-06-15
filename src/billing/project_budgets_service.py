"""
Project Budgets Service — per-project API budget for project-interval plans.

Project plans (Energy: interval='project') include a fixed API budget per
project (EUR 100). Unlike monthly subscription budgets, this budget belongs to
ONE project: it is allocated when the project's slot is consumed (job creation)
and drawn down only by that project's LLM calls. No monthly reset, no sharing
across projects.

The project_id equals the app's project_id, which is also the Bridge
attribution workflow_id (X-Workflow-ID) — so the pre-call gate and the
post-call deduction can resolve the right budget from the attribution they
already carry.

Lifecycle:
  slot consume (project_credits_service.consume_credit)
      → allocate_budget(... project_id, limit=plan.api_budget_eur)   [same txn]
  pre-call gate (src/budget/gate.py)
      → evaluate_budget(... project_id, estimated_cost)
  post-call deduction (src/activity/ai_call_writer.py)
      → deduct(... project_id, cost)

Design:
- allocate_budget is idempotent on (user_id, plan_id, project_id): a retried
  job-create never double-allocates or resets a running project's budget.
- deduct/evaluate distinguish "budget exhausted" from "no budget row" so the
  callers can react differently (block vs. fall back / fail-loud).
- All amounts are EUR. Monetary math uses Decimal to avoid float drift in the
  ledger; callers pass floats (cost) which we quantize on the way in.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Dict, Optional

from src.db.client import get_pool

# 4 decimal places mirrors the NUMERIC(12,4) ledger columns.
_Q = Decimal("0.0001")


def _eur(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(_Q)


class ProjectBudgetExhausted(Exception):
    """The project's allocated API budget is used up."""

    def __init__(self, project_id: str, plan_id: str) -> None:
        super().__init__(
            f"Project budget exhausted for project '{project_id}' (plan '{plan_id}')"
        )
        self.project_id = project_id
        self.plan_id = plan_id


async def allocate_budget(
    conn,
    *,
    user_id: uuid.UUID,
    tenant_id: str,
    plan_id: str,
    project_id: str,
    limit_eur: float,
    credit_id: Optional[uuid.UUID] = None,
) -> bool:
    """Allocate a project's API budget. Idempotent on (user, plan, project).

    Runs on the caller's connection so it can share the slot-consume
    transaction (allocate-with-consume is atomic). Returns True when a new
    budget row was created, False when one already existed (idempotent retry).
    """
    if not project_id:
        raise ValueError("allocate_budget requires a non-empty project_id")
    row = await conn.fetchrow(
        """
        INSERT INTO project_budgets
            (user_id, tenant_id, plan_id, project_id, limit_eur, used_eur, credit_id)
        VALUES ($1, $2, $3, $4, $5, 0, $6)
        ON CONFLICT (user_id, plan_id, project_id) DO NOTHING
        RETURNING id
        """,
        user_id,
        tenant_id,
        plan_id,
        project_id,
        _eur(limit_eur),
        credit_id,
    )
    return row is not None


async def get_budget(
    user_id: uuid.UUID, plan_id: str, project_id: str
) -> Optional[Dict[str, Any]]:
    """Return {limitEur, usedEur, remainingEur} for a project, or None if unallocated."""
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT limit_eur, used_eur
              FROM project_budgets
             WHERE user_id = $1 AND plan_id = $2 AND project_id = $3
            """,
            user_id,
            plan_id,
            project_id,
        )
    if row is None:
        return None
    limit = Decimal(row["limit_eur"])
    used = Decimal(row["used_eur"])
    return {
        "limitEur": float(limit),
        "usedEur": float(used),
        "remainingEur": float(max(Decimal("0"), limit - used)),
    }


async def evaluate(
    user_id: uuid.UUID,
    plan_id: str,
    project_id: str,
    estimated_cost_eur: float,
) -> Dict[str, Any]:
    """Pre-call evaluation for the budget gate.

    Returns {exists, allowed, remainingEur}. `exists=False` means the project
    has no allocated budget — the caller decides how to treat that (a project
    that never consumed a slot, or a pre-feature in-flight project).
    """
    budget = await get_budget(user_id, plan_id, project_id)
    if budget is None:
        return {"exists": False, "allowed": False, "remainingEur": 0.0}
    remaining = budget["remainingEur"]
    # estimated_cost is a lower-bound hint from the gate; >0 remaining is the
    # real signal (the post-call deduction caps the actual spend at the limit).
    allowed = remaining > 0 or estimated_cost_eur <= remaining
    return {"exists": True, "allowed": allowed, "remainingEur": remaining}


async def deduct(
    user_id: uuid.UUID,
    plan_id: str,
    project_id: str,
    cost_eur: float,
    *,
    allocate_limit_eur: Optional[float] = None,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically draw `cost_eur` from a project's budget.

    Returns {exists, deductedEur, usedEur, remainingEur}. `exists=False` means
    no budget row and no allocation was requested — nothing deducted (the caller
    logs the gap). The deduction is capped at the remaining budget so used_eur
    never exceeds limit_eur (matching the DB CHECK constraint); over-spend beyond
    the cap is absorbed silently here because the call already happened.

    Lazy allocation: when the budget row is missing and both `allocate_limit_eur`
    and `tenant_id` are given, the row is created in-transaction (idempotent on
    conflict) and then drawn from. This is how a project's budget
    self-provisions on its first LLM call — keyed by project_id (== attribution
    workflow_id) — since the slot that entitles the project was already consumed
    by the app.
    """
    amount = _eur(cost_eur)
    if amount <= 0:
        # Nothing to do; still report current state for observability.
        existing = await get_budget(user_id, plan_id, project_id)
        if existing is None:
            return {"exists": False, "deductedEur": 0.0, "usedEur": 0.0, "remainingEur": 0.0}
        return {"exists": True, "deductedEur": 0.0, **existing}

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT limit_eur, used_eur
                  FROM project_budgets
                 WHERE user_id = $1 AND plan_id = $2 AND project_id = $3
                   FOR UPDATE
                """,
                user_id,
                plan_id,
                project_id,
            )
            if row is None and allocate_limit_eur is not None and tenant_id is not None:
                # Lazy-allocate this project's budget, then lock+read it. The
                # INSERT is idempotent; a concurrent first-call serialises on the
                # subsequent SELECT ... FOR UPDATE so neither double-deducts.
                await conn.execute(
                    """
                    INSERT INTO project_budgets
                        (user_id, tenant_id, plan_id, project_id, limit_eur, used_eur)
                    VALUES ($1, $2, $3, $4, $5, 0)
                    ON CONFLICT (user_id, plan_id, project_id) DO NOTHING
                    """,
                    user_id,
                    tenant_id,
                    plan_id,
                    project_id,
                    _eur(allocate_limit_eur),
                )
                row = await conn.fetchrow(
                    """
                    SELECT limit_eur, used_eur
                      FROM project_budgets
                     WHERE user_id = $1 AND plan_id = $2 AND project_id = $3
                       FOR UPDATE
                    """,
                    user_id,
                    plan_id,
                    project_id,
                )
            if row is None:
                return {"exists": False, "deductedEur": 0.0, "usedEur": 0.0, "remainingEur": 0.0}

            limit = Decimal(row["limit_eur"])
            used = Decimal(row["used_eur"])
            remaining = max(Decimal("0"), limit - used)
            deducted = min(amount, remaining)  # cap so used never exceeds limit
            new_used = used + deducted

            await conn.execute(
                """
                UPDATE project_budgets
                   SET used_eur = $4, updated_at = NOW()
                 WHERE user_id = $1 AND plan_id = $2 AND project_id = $3
                """,
                user_id,
                plan_id,
                project_id,
                new_used,
            )

    return {
        "exists": True,
        "deductedEur": float(deducted),
        "usedEur": float(new_used),
        "remainingEur": float(max(Decimal("0"), limit - new_used)),
    }
