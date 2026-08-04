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

TopUp fallback (BUDGET-MODELL.md Regel 6 — "TopUp ist das fungible Geld"):
  A project's own EUR-100 budget is intentionally NOT fungible (Regel 1/5 —
  no leftover transfer, a purchased project stays completable on its own
  money). TopUp is the opposite: app-übergreifendes, sichtbares Geld that is
  DESIGNED to back an otherwise-exhausted included pot before the user is
  blocked — the monthly path (src/budget/routes.py apply_budget_deduction)
  already does this; evaluate()/deduct() below mirror it for the per-project
  pot so a customer with unused TopUp is never blocked by an empty project
  budget alone. Both draw on the SAME user_topup_lots via
  src/budget/topup_store.py (shared FIFO + persistence — never a second,
  divergent implementation).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from src.budget.calculator import consume_topup_fifo, topup_balance_eur
from src.budget.topup_store import load_topup_lots, persist_topup_lots
from src.db.client import get_pool

logger = logging.getLogger(__name__)

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


class AmbiguousProjectBudget(RuntimeError):
    """One project carries allocated budgets under several plans.

    Data-level counterpart to AmbiguousPlanCatalog: the allocation is the
    evidence of which pot paid, so two allocations for one project destroy
    that evidence. Never expected — allocation is keyed by the consumed slot.
    """


async def find_allocated_plan_id(
    user_id: uuid.UUID, project_id: str
) -> Optional[str]:
    """
    Which plan holds an allocated budget for this (user, project)?

    This is THE discriminator between a paid per-project pot and the app's
    monthly pot. An allocation exists only because a credit slot was consumed
    for exactly this project, so its presence — not the mere presence of a
    project_id — is what proves the call belongs to a project pot. Report
    relies on that distinction: every ordinary report call also carries a
    project_id, and must keep drawing from the monthly budget.

    Returns None when nothing is allocated (the caller then falls back to the
    monthly plan). Raises AmbiguousProjectBudget if several plans allocated
    the same project — fail loud rather than pick one and mis-bill.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT plan_id
              FROM project_budgets
             WHERE user_id = $1 AND project_id = $2
            """,
            user_id,
            project_id,
        )
    if not rows:
        return None
    plan_ids = {r["plan_id"] for r in rows}
    if len(plan_ids) > 1:
        raise AmbiguousProjectBudget(
            f"project_id={project_id!r} (user={user_id}) has allocated budgets under "
            f"{sorted(plan_ids)}. A project belongs to exactly one paying plan."
        )
    return plan_ids.pop()


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


async def list_budgets(user_id: uuid.UUID, plan_id: str) -> Dict[str, Any]:
    """All project budgets for (user, plan) plus per-project token usage.

    Read path for the app's budget display. Returns the per-project EUR ledger
    from project_budgets joined with the per-project token total aggregated from
    usage_events (workflow rows, attributed by provider_metadata.workflow_id ==
    project_id), and the aggregated totals across all of the user's projects:

      {
        "projects": [
          {projectId, limitEur, usedEur, remainingEur, tokensUsed}, ...
        ],
        "totals": {limitEur, usedEur, remainingEur, tokensUsed, projectCount},
      }

    The aggregated total is the product's "pot" model: N projects × EUR 100 =
    EUR N*100 shared headroom, while tokensUsed per row answers "which project
    spent how much". A project with a budget row but no LLM calls yet reports
    tokensUsed=0; token rows without a matching budget row are ignored (a budget
    is always allocated before/at the first call, so this only drops pre-feature
    or cross-plan noise).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        budget_rows = await conn.fetch(
            """
            SELECT project_id, limit_eur, used_eur
              FROM project_budgets
             WHERE user_id = $1 AND plan_id = $2
             ORDER BY created_at ASC
            """,
            user_id,
            plan_id,
        )
        # Per-project token totals from the usage ledger. workflow_id lives in
        # provider_metadata JSONB (== app project_id). Only workflow rows carry it.
        token_rows = await conn.fetch(
            """
            SELECT provider_metadata->>'workflow_id' AS project_id,
                   COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens_used
              FROM usage_events
             WHERE user_id = $1
               AND source = 'workflow'
               AND provider_metadata->>'workflow_id' IS NOT NULL
             GROUP BY provider_metadata->>'workflow_id'
            """,
            user_id,
        )

    tokens_by_project = {r["project_id"]: int(r["tokens_used"]) for r in token_rows}

    projects = []
    total_limit = Decimal("0")
    total_used = Decimal("0")
    total_tokens = 0
    for row in budget_rows:
        project_id = row["project_id"]
        limit = Decimal(row["limit_eur"])
        used = Decimal(row["used_eur"])
        tokens = tokens_by_project.get(project_id, 0)
        total_limit += limit
        total_used += used
        total_tokens += tokens
        projects.append(
            {
                "projectId": project_id,
                "limitEur": float(limit),
                "usedEur": float(used),
                "remainingEur": float(max(Decimal("0"), limit - used)),
                "tokensUsed": tokens,
            }
        )

    return {
        "projects": projects,
        "totals": {
            "limitEur": float(total_limit),
            "usedEur": float(total_used),
            "remainingEur": float(max(Decimal("0"), total_limit - total_used)),
            "tokensUsed": total_tokens,
            "projectCount": len(projects),
        },
    }


async def evaluate(
    user_id: uuid.UUID,
    plan_id: str,
    project_id: str,
    estimated_cost_eur: float,
) -> Dict[str, Any]:
    """Pre-call evaluation for the budget gate.

    Returns {exists, allowed, remainingEur, topUpRemainingEur}. `exists=False`
    means the project has no allocated budget — the caller decides how to
    treat that (a project that never consumed a slot, or a pre-feature
    in-flight project).

    When the project's own budget alone can't cover the call, the user's
    TopUp balance is checked as a fallback (see module docstring) before
    reporting `allowed=False` — a project is only truly blocked once BOTH
    pots are empty. `topUpRemainingEur` is 0.0 whenever the project budget
    alone already covers the call (TopUp wasn't consulted, so its remaining
    amount is irrelevant to this decision).
    """
    budget = await get_budget(user_id, plan_id, project_id)
    if budget is None:
        return {"exists": False, "allowed": False, "remainingEur": 0.0, "topUpRemainingEur": 0.0}
    remaining = budget["remainingEur"]
    # estimated_cost is a lower-bound hint from the gate; >0 remaining is the
    # real signal (the post-call deduction caps the actual spend at the limit).
    if remaining > 0 or estimated_cost_eur <= remaining:
        return {"exists": True, "allowed": True, "remainingEur": remaining, "topUpRemainingEur": 0.0}

    top_up_remaining = await _topup_remaining(user_id)
    total_remaining = remaining + top_up_remaining
    allowed = total_remaining > 0 or estimated_cost_eur <= total_remaining
    return {
        "exists": True,
        "allowed": allowed,
        "remainingEur": remaining,
        "topUpRemainingEur": top_up_remaining,
    }


async def _topup_remaining(user_id: uuid.UUID) -> float:
    """Read-only TopUp balance check for the gate (no lock — deduct() takes
    the FOR UPDATE lock when it actually spends)."""
    pool = get_pool()
    async with pool.acquire() as conn:
        lots = await load_topup_lots(conn, user_id)
    return topup_balance_eur(lots, datetime.now(timezone.utc))


async def deduct(
    user_id: uuid.UUID,
    plan_id: str,
    project_id: str,
    cost_eur: float,
    *,
    allocate_limit_eur: Optional[float] = None,
    tenant_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically draw `cost_eur` from a project's budget, falling back to the
    user's TopUp lots for any shortfall (see module docstring — TopUp is the
    designed fallback for an exhausted included pot).

    Returns {exists, deductedEur, fromProjectEur, fromTopUpEur, usedEur,
    remainingEur}. `exists=False` means no budget row and no allocation was
    requested — nothing deducted (the caller logs the gap). `usedEur`/
    `remainingEur` describe the PROJECT pot only (limit_eur never exceeded,
    matching the DB CHECK constraint); `deductedEur` is the total actually
    covered (project + TopUp), which can still be less than `cost_eur` if
    both pots are exhausted — the call already happened (best-effort
    post-call accounting), so the shortfall is absorbed with a loud warning
    rather than raised.

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
            return {
                "exists": False, "deductedEur": 0.0, "fromProjectEur": 0.0,
                "fromTopUpEur": 0.0, "usedEur": 0.0, "remainingEur": 0.0,
            }
        return {"exists": True, "deductedEur": 0.0, "fromProjectEur": 0.0, "fromTopUpEur": 0.0, **existing}

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
                return {
                    "exists": False, "deductedEur": 0.0, "fromProjectEur": 0.0,
                    "fromTopUpEur": 0.0, "usedEur": 0.0, "remainingEur": 0.0,
                }

            limit = Decimal(row["limit_eur"])
            used = Decimal(row["used_eur"])
            remaining = max(Decimal("0"), limit - used)
            from_project = min(amount, remaining)  # cap so used never exceeds limit
            new_used = used + from_project

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

            from_top_up = Decimal("0")
            remainder = amount - from_project
            if remainder > 0:
                # Project pot couldn't cover the full cost — draw the rest
                # from the user's TopUp lots (FIFO, same pot the monthly path
                # draws from). Locked for the rest of this transaction so a
                # concurrent call can't double-spend the same TopUp euro.
                old_lots = await load_topup_lots(conn, user_id, for_update=True)
                now = datetime.now(timezone.utc)
                new_lots, consumed = consume_topup_fifo(old_lots, float(remainder), now)
                from_top_up = _eur(consumed)
                await persist_topup_lots(conn, old_lots, new_lots)

    total_deducted = from_project + from_top_up
    if total_deducted < amount:
        logger.warning(
            "project_budgets_service.deduct: project+TopUp insufficient to cover "
            "the full cost — absorbing shortfall (call already happened) "
            "user=%s plan=%s project=%s cost=%.4f covered=%.4f (project=%.4f top_up=%.4f)",
            user_id, plan_id, project_id, float(amount), float(total_deducted),
            float(from_project), float(from_top_up),
        )

    return {
        "exists": True,
        "deductedEur": float(total_deducted),
        "fromProjectEur": float(from_project),
        "fromTopUpEur": float(from_top_up),
        "usedEur": float(new_used),
        "remainingEur": float(max(Decimal("0"), limit - new_used)),
    }


async def reset_budget(
    conn: Any,
    *,
    user_id: uuid.UUID,
    plan_id: str,
    project_id: str,
) -> bool:
    """Reset a project's API budget back to full (used_eur = 0).

    This is the operator-approved "start this project over" action (redeem of a
    project_reset_requests grant): the same project_id keeps its limit_eur, but
    its accumulated spend is zeroed so a finished-and-exhausted project can be
    re-run within a fresh EUR 100.

    Deliberately SEPARATE from allocate_budget, which is idempotent and MUST NOT
    reset a running project's budget (a retried job-create would otherwise wipe
    spend). reset_budget is the only path that intentionally clears used_eur, and
    it is reachable only behind an approved, one-shot grant.

    Runs on the caller-supplied `conn` so the redeem can mark the grant consumed
    and reset the budget atomically in one transaction. Returns True when a
    budget row existed and was reset, False when there was none (a project that
    never spent — nothing to reset; the next LLM call lazy-allocates a fresh
    budget anyway, so the redeem still succeeds).
    """
    result = await conn.execute(
        """
        UPDATE project_budgets
           SET used_eur = 0, updated_at = NOW()
         WHERE user_id = $1 AND plan_id = $2 AND project_id = $3
        """,
        user_id,
        plan_id,
        project_id,
    )
    # asyncpg execute() returns a tag like "UPDATE 1" / "UPDATE 0".
    return not str(result).endswith(" 0")
