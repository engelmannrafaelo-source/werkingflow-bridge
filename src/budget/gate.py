"""
Chat-completions budget gate.

Enforced inside /v1/chat/completions BEFORE the LLM call. If the calling
user's budget for the app is exhausted, the call is rejected with 402.

Design (decided 2026-05-16, tightened 2026-05-28):
  • HARD enforcement: a user whose budget is empty gets 402, no call.
  • As of 2026-05-28, `unlicensed` ALSO blocks: registering a user no longer
    implicitly grants access — a subscription row (trial or paid) must
    exist. Run /v1/billing/seed-legacy-trials/<app_id> before deploying
    this tightened gate so pre-existing users keep working via Trial.
  • Apps with a 0-EUR plan (safety/noise/engelmann) still need a row in
    `subscriptions` — the seed-bridge-users.sh provisioner takes care of
    test users; production users go through the register/checkout flow.
  • Blocking reasons now: `all_exhausted`, `monthly_exceeded_no_topup`,
    `trial_expired`, `unlicensed`.
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
from src.identity.user_resolver import resolve_user_id

logger = logging.getLogger(__name__)

# Reasons that hard-block the call.
#   *_exhausted / *_exceeded_no_topup / trial_expired = "had budget, it's gone"
#   unlicensed = "never had a plan entry" (added 2026-05-28 — was implicit grant)
_BLOCKING_REASONS = {
    "all_exhausted",
    "monthly_exceeded_no_topup",
    "trial_expired",
    "unlicensed",
}


async def enforce_budget(
    user_id: Optional[str],
    app_id: Optional[str],
    estimated_cost_eur: float,
    project_id: Optional[str] = None,
) -> None:
    """
    Raise HTTPException(402) if the user's budget for this app is exhausted.
    Returns None (lets the call proceed) in every other case.

    For project-interval plans (e.g. Energy) the budget is per project
    (keyed by project_id == attribution workflow_id). When a per-project budget
    exists it gates the call; if none exists yet (the project's first call) the
    call is allowed — the entitling slot was already consumed by the app and the
    post-call deduction lazily allocates the budget. Project plans never gate on
    the monthly tenant budget.
    """
    # No user / no app → not a user-budgeted call (internal job). Let through.
    if not user_id or not app_id:
        return

    plan = find_plan_for_app(app_id)
    if plan is None:
        # App not in the plan catalog → not budget-gated.
        return

    try:
        uid = await resolve_user_id(user_id)
    except (ValueError, AttributeError, TypeError) as e:
        # Unresolvable identity (malformed, or an email with no Bridge user) —
        # can't evaluate, don't punish the call (gate stays fail-open by design).
        # Email identities that DO resolve are now enforced like any UUID.
        logger.warning("budget gate: unresolvable user_id %r (%s) — letting call through", user_id, e)
        return

    if plan.interval == "project":
        # Project-interval plans are gated PER PROJECT (keyed by project_id),
        # NEVER on the monthly tenant budget — a project plan has no monthly
        # budget entry, so the monthly evaluate_budget path below would always
        # return reason="unlicensed" and falsely block. This branch owns the
        # whole project-plan case so the call never leaks into the monthly path.
        if not project_id:
            # No project attribution. The canonical case is the sandbox-editor
            # OAuth lease (/v1/sandbox/lease-token), which acquires a token with
            # estimated_cost=0 and is not itself a billable call — actual
            # per-token spend during the session is metered + gated by the
            # per-project deduction path, which DOES carry the project_id.
            # Blocking here broke the editor for every Energy user once Energy
            # moved to a project-interval plan (structural "unlicensed").
            logger.debug(
                "budget gate: project plan %s without project_id — letting call "
                "through (per-project deduction is the real gate)", plan.id,
            )
            return

        from src.billing.project_budgets_service import evaluate as _eval_project

        try:
            pr = await _eval_project(uid, plan.id, project_id, estimated_cost_eur)
        except Exception as e:  # noqa: BLE001 — fail-open: infra error must not kill the AI path
            logger.error("budget gate: project budget eval failed (%s) — letting call through", e)
            return
        if pr.get("exists"):
            if pr.get("allowed"):
                return
            # `allowed=False` here means BOTH pots are exhausted — evaluate()
            # already fell back to the user's TopUp balance before reporting
            # this (see project_budgets_service.evaluate). Not a bypassable
            # per-project-only block anymore.
            total_remaining = pr.get("remainingEur", 0.0) + pr.get("topUpRemainingEur", 0.0)
            logger.info(
                "budget gate: BLOCKED (per-project) user=%s app=%s plan=%s project=%s "
                "projectRemaining=%.4f topUpRemaining=%.4f",
                user_id, app_id, plan.id, project_id,
                pr.get("remainingEur", 0.0), pr.get("topUpRemainingEur", 0.0),
            )
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "budget_exhausted",
                    "reason": "project_budget_exhausted",
                    "appId": app_id,
                    "planId": plan.id,
                    "projectId": project_id,
                    "totalRemainingEur": total_remaining,
                    "message": "Projekt- und TopUp-Guthaben aufgebraucht. Bitte ein neues Projekt-Paket buchen oder Guthaben aufladen.",
                },
            )
        # Not allocated yet (the project's first call). The entitling slot was
        # already consumed by the app, and the post-call deduction lazily
        # allocates this project's budget — let the first call through. Project
        # plans never gate on the monthly tenant budget.
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

    # Not allowed for some other reason (future enum values). Let through and log
    # loud so we notice if a new reason needs adding to _BLOCKING_REASONS.
    logger.warning(
        "budget gate: not-allowed reason=%s NOT in _BLOCKING_REASONS — letting call through (check if this reason should block)",
        reason,
    )
