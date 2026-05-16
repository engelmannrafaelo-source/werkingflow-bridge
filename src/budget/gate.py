"""
Chat-completions budget gate.

Enforced inside /v1/chat/completions BEFORE the LLM call. If the calling
user's budget for the app is exhausted, the call is rejected with 402.

Design (decided 2026-05-16):
  • HARD enforcement: a user whose budget is empty gets 402, no call.
  • The gate is a *budget cap*, not a licence check. A user with no plan
    entry yet (`unlicensed`) is let through — provisioning is a separate
    flow, and apps with a 0-EUR plan (safety/noise/engelmann) would
    otherwise be unusable. Only `all_exhausted` / `monthly_exceeded` /
    `trial_expired` block.
  • Fail-open on infrastructure errors: a DB hiccup in the budget check
    must NEVER take down the AI path. The error is logged loud; the call
    proceeds. Enforcement resumes as soon as the DB recovers.
  • Calls with no user_id or no app-plan are not gated (internal Bridge
    jobs, apps outside the plan catalog).

See ADR 0007 lineage / token-tracking consolidation.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import HTTPException

from src.budget.plans import find_plan_for_app
from src.budget.routes import evaluate_budget

logger = logging.getLogger(__name__)

# Reasons that mean "had budget, it's gone" → hard block.
_BLOCKING_REASONS = {"all_exhausted", "monthly_exceeded_no_topup", "trial_expired"}


async def enforce_budget(
    user_id: Optional[str],
    app_id: Optional[str],
    estimated_cost_eur: float,
) -> None:
    """
    Raise HTTPException(402) if the user's budget for this app is exhausted.
    Returns None (lets the call proceed) in every other case.
    """
    # No user / no app → not a user-budgeted call (internal job). Let through.
    if not user_id or not app_id:
        return

    plan = find_plan_for_app(app_id)
    if plan is None:
        # App not in the plan catalog → not budget-gated.
        return

    try:
        uid = uuid.UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        # Malformed user id — can't evaluate, don't punish the call.
        logger.warning("budget gate: malformed user_id %r — letting call through", user_id)
        return

    try:
        result = await evaluate_budget(uid, plan.id, estimated_cost_eur)
    except Exception as e:  # noqa: BLE001 — fail-open: infra error must not kill the AI path
        logger.error("budget gate: evaluate_budget failed (%s) — letting call through", e)
        return

    if result.get("allowed"):
        return

    reason = result.get("reason")
    if reason in _BLOCKING_REASONS:
        logger.info(
            "budget gate: BLOCKED user=%s app=%s plan=%s reason=%s remaining=%.4f",
            user_id, app_id, plan.id, reason, result.get("totalRemainingEur", 0.0),
        )
        raise HTTPException(
            status_code=402,
            detail={
                "error": "budget_exhausted",
                "reason": reason,
                "appId": app_id,
                "planId": result.get("effectivePlanId", plan.id),
                "totalRemainingEur": result.get("totalRemainingEur", 0.0),
                "message": "API-Budget aufgebraucht. Bitte Guthaben aufladen.",
            },
        )

    # Not allowed but not a blocking reason (e.g. "unlicensed") → let through.
    logger.debug(
        "budget gate: not-allowed but non-blocking (reason=%s) — letting call through",
        reason,
    )
