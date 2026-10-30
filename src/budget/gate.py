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
  • Fail-CLOSED on a malformed billing identity (2026-08-03). An infra
    error and a caller sending garbage identity are different classes and
    must not share a policy: the first is transient and outside the
    caller's control, the second is a bug that silently costs money. Only
    the first is fail-open. See MalformedUserIdentity in user_resolver.
  • Calls with no user_id or no app-plan are not gated (internal Bridge
    jobs, apps outside the plan catalog).
  • The plan is resolved PER CALL from the entitlement that paid, not per app
    (2026-08-04). An app may sell both a monthly plan and per-project credits;
    asking for "the" plan of an app then silently charges the wrong pot. See
    src/budget/plan_resolution.py. An unresolvable plan fails CLOSED for the
    same reason a malformed identity does: a call nobody can bill is the
    failure mode, not a transient one. Since BA6 (30.10.2026) it leaves the
    gate as HTTPException 409 `billing_plan_unresolved`: the callers re-raise
    HTTPException and swallow every other error, so a bare PlanResolutionError
    had become a silent pass after all (Befund B1).

See ADR 0007 lineage / token-tracking consolidation.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from fastapi import HTTPException

from src.federation import (
    FederationMisconfigured,
    is_foreign_origin,
    resolve_user_domain_target,
)
from src.budget.plan_resolution import PlanResolutionError, resolve_billing_plan
from src.budget.plans import (
    AmbiguousPlanCatalog,
    PlanCatalogUnavailable,
    find_monthly_plan_for_app,
    find_project_plans_for_app,
)
from src.budget.routes import evaluate_budget
from src.identity.user_resolver import (
    MalformedUserIdentity,
    UnresolvableUserIdentity,
    resolve_user_id,
)

logger = logging.getLogger(__name__)

# Hard-cap projects seen by THIS process: (user_id, project_id) -> monotonic time
# of the last successful evaluation that reported hardCap=true. Used ONLY when
# the budget state cannot be read (infra error): a known hard-cap project then
# fails CLOSED instead of passing unmetered. Every other case keeps the
# fail-open policy below. The cache is a hint that can only make the gate
# stricter; losing it (restart) reverts to today's behaviour for that project.
_HARD_CAP_SEEN: dict = {}
_HARD_CAP_SEEN_TTL_S = 24 * 3600

#: Optional caller hint with the same one-way effect as the cache (never looser).
#: Value "1" or "true" (case-insensitive) = this call belongs to a hard-cap
#: project; anything else, or no header, is ignored.
HARD_CAP_HEADER = "X-Budget-Hard-Cap"

#: Estimate used for a hard-cap project when the caller hands in none (0): research,
#: doc-agent and document-conversion calls have no per-call estimate in the gate, so
#: "remaining > 0" let a whole wave through (A2 measurement 26.10.2026). Roughly the
#: observed p90 of Energy calls of that kind; adjust with measurement 3. Applies ONLY to
#: hard-cap budgets and ONLY when the caller's estimate is 0 (a larger one wins).
HARD_CAP_DEFAULT_ESTIMATE_EUR = {"research": 2.00, "doc-agent": 1.00, "convert": 0.50}

#: Value of X-Bridge-Dependency-Unavailable on the fail-closed 503.
BUDGET_STATE_DEPENDENCY = "budget-state"


def hard_cap_hint_from_request(request) -> bool:
    """True when the caller marked the call as belonging to a hard-cap project."""
    try:
        value = request.headers.get(HARD_CAP_HEADER)
    except AttributeError:  # silent-ok: no request object (internal caller) = no hint
        return False
    return str(value or "").strip().lower() in ("1", "true")


def _known_hard_cap(user_id, project_id, hint: bool) -> bool:
    if hint:
        return True
    ts = _HARD_CAP_SEEN.get((str(user_id), project_id))
    return ts is not None and (time.monotonic() - ts) < _HARD_CAP_SEEN_TTL_S


def _remember_hard_cap(user_id, project_id) -> None:
    now = time.monotonic()
    if len(_HARD_CAP_SEEN) > 1024:  # drop expired entries so the map stays bounded
        for k in [k for k, ts in _HARD_CAP_SEEN.items() if now - ts >= _HARD_CAP_SEEN_TTL_S]:
            del _HARD_CAP_SEEN[k]
    _HARD_CAP_SEEN[(str(user_id), project_id)] = now


def _budget_state_unavailable(user_id, project_id, why: str) -> HTTPException:
    logger.error(
        "budget gate: hard-cap project %s of user %s — budget state unavailable (%s); "
        "failing CLOSED (503, retryable)", project_id, user_id, why,
    )
    return HTTPException(
        status_code=503,
        detail={
            "error": "budget_unavailable",
            "reason": "budget_unavailable",
            "retryable": True,
            "retry_after_s": 30,
            "projectId": project_id,
            "message": (
                "Der Budgetstand dieses Projekts ist gerade nicht lesbar. Der Aufruf wird "
                "nicht ungemessen durchgelassen; bitte in Kürze erneut versuchen."
            ),
        },
        headers={"X-Bridge-Dependency-Unavailable": BUDGET_STATE_DEPENDENCY},
    )

def _known_hard_cap_raw(user_id, project_id, hint: bool) -> bool:
    """_known_hard_cap before the identity is resolved (raw caller value)."""
    if hint:
        return True
    try:
        return _known_hard_cap(uuid.UUID(str(user_id).strip()), project_id, False)
    except ValueError:  # silent-ok: an e-mail identity cannot hit the uid-keyed cache
        return False


def _plan_unresolved(app_id, user_id, project_id, exc: Exception) -> HTTPException:
    """409 for a call that cannot be attributed to exactly one paying plan (B1/BA6).

    Raised as an HTTPException ON PURPOSE: every caller of enforce_budget
    re-raises HTTPException and swallows everything else ("letting call
    through"). A bare PlanResolutionError therefore used to end in exactly the
    silent pass this policy exists to prevent (main.py chat/research/doc-agent/
    convert catch-alls, Befund B1 27.10.2026).
    """
    from src.billing.project_budgets_service import AmbiguousProjectBudget

    if isinstance(exc, AmbiguousPlanCatalog):
        reason = "ambiguous_plan_catalog"
    elif isinstance(exc.__cause__, AmbiguousProjectBudget):
        reason = "ambiguous_project_budget"
    else:
        reason = "billing_plan_unresolved"
    logger.error(
        "budget gate: REJECTED (409 %s) app=%s user=%s project=%s — no single paying plan: %s",
        reason, app_id, user_id, project_id, exc,
    )
    return HTTPException(
        status_code=409,
        detail={
            "error": "billing_plan_unresolved",
            "reason": reason,
            "retryable": False,
            "appId": app_id,
            "projectId": project_id,
            "message": (
                "Dieser Aufruf lässt sich keinem eindeutigen Abrechnungstopf zuordnen "
                f"({reason}). Er wird nicht ungemessen durchgelassen; die Zuordnung "
                "(Projektbudget/Plan) muss bereinigt werden."
            ),
        },
    )


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
    hard_cap_hint: bool = False,
    call_kind: Optional[str] = None,
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

    # Cheap catalog check BEFORE resolving the identity: an app outside the
    # plan catalog is not budget-gated at all, and it must stay that way even
    # if its caller sends a shaky identity (the identity policy below would
    # otherwise start rejecting calls that were never metered to begin with).
    try:
        in_catalog = (
            find_monthly_plan_for_app(app_id) is not None
            or bool(find_project_plans_for_app(app_id))
        )
    except AmbiguousPlanCatalog as e:
        # The catalog lookup itself can already detect an incoherent catalog;
        # same 409 as from resolve_billing_plan below, never a bare raise.
        raise _plan_unresolved(app_id, user_id, project_id, e) from e
    except PlanCatalogUnavailable as e:
        # Infra (catalog not loaded): fail-open like every other infra error,
        # EXCEPT for a known hard-cap project. Before BA6 this escaped the gate
        # entirely and the callers' catch-alls waved it through even with the
        # X-Budget-Hard-Cap hint (dev worker log 29./30.10.2026).
        if project_id and _known_hard_cap_raw(user_id, project_id, hard_cap_hint):
            raise _budget_state_unavailable(user_id, project_id, f"plan catalog: {e}") from e
        logger.error("budget gate: plan catalog unavailable (%s) — letting call through", e)
        return
    if not in_catalog:
        return

    # ADR-0011 pre-flight: a FOREIGN-origin request whose home bridge is not
    # configured as a federation peer must fail CLOSED — this is a deploy
    # error, not a transient one, and the fail-open policy below is reserved
    # for transient infrastructure faults. Letting it through would mean an
    # unbudgeted call evaluated against (and JIT-provisioned into) the WRONG
    # database — the exact defect this ADR removes. Checked once up front so
    # the catch-alls below can stay what they are.
    if is_foreign_origin():
        try:
            resolve_user_domain_target()
        except FederationMisconfigured as e:
            logger.error("budget gate: federation misconfigured — failing closed: %s", e)
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "federation_unconfigured",
                    "appId": app_id,
                    "message": (
                        "Diese Bridge kann die Heimat-Bridge des Requests nicht "
                        "erreichen (Föderations-Konfiguration fehlt). Der Call "
                        "wird nicht lokal bewertet."
                    ),
                },
            ) from e

    try:
        uid = await resolve_user_id(user_id)
    except MalformedUserIdentity as e:
        # CALLER BUG — the identity is not even well-formed (a marker string,
        # an empty value, garbage). No legitimate caller produces this, so it
        # must fail CLOSED: letting it through means an unbudgeted call that
        # nobody can attribute, bill or cap.
        #
        # This used to be fail-open together with the case below, which is how
        # the public check funnel ran unmetered for months — it sent
        # `anonymous:public-check-funnel`, hit this branch and was waved
        # through (43,94 € in one morning, 2026-08-03).
        #
        # 400, not 402: the call is malformed, not out of money. A 402 would
        # tell the user to top up, which would be a lie.
        logger.error(
            "budget gate: MALFORMED user_id %r for app=%s (%s) — rejecting. "
            "The caller must send a resolvable billing identity.",
            user_id, app_id, e,
        )
        raise HTTPException(
            status_code=400,
            detail={
                "error": "malformed_billing_identity",
                "appId": app_id,
                "message": (
                    "Ungültige Abrechnungs-Identität. Der Aufrufer muss eine "
                    "auflösbare Bridge-User-UUID (oder lizenzierte E-Mail) senden."
                ),
            },
        ) from e
    except UnresolvableUserIdentity as e:
        # DATA condition — a well-formed email with no Bridge user behind it.
        # Whether an unlicensed address should be blocked is a business call,
        # not an architectural one, so the pre-existing fail-open policy stays.
        logger.warning("budget gate: unknown user_id %r (%s) — letting call through", user_id, e)
        return

    try:
        plan = await resolve_billing_plan(app_id, uid, project_id)
    except (AmbiguousPlanCatalog, PlanResolutionError) as e:
        # CONFIGURATION / DATA bug, not infra. Same split as the malformed
        # identity above: we cannot name the pot that pays, so we must not
        # serve the call. Guessing is how a paid Akt got charged to a monthly
        # trial pot in the first place. The boot invariant makes the catalog
        # half of this unreachable in a healthy process.
        # Visible 409, never a bare raise: the callers swallow non-HTTP errors.
        raise _plan_unresolved(app_id, user_id, project_id, e) from e
    except Exception as e:  # noqa: BLE001 — fail-open: infra error must not kill the AI path
        if project_id and _known_hard_cap(uid, project_id, hard_cap_hint):
            raise _budget_state_unavailable(uid, project_id, f"plan resolution: {e}") from e
        logger.error("budget gate: plan resolution failed (%s) — letting call through", e)
        return

    if plan is None:
        # App left the plan catalog between the check above and here (hot
        # reload). Not budget-gated.
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

        # ADR-0009 Schritt 2b/C3: platform-api first, direct DB as fallback.
        from src.billing.project_budgets_service import (
            evaluate_via_platform as _eval_project,
        )

        try:
            pr = await _eval_project(uid, plan.id, project_id, estimated_cost_eur)
        except Exception as e:  # noqa: BLE001 — fail-open: infra error must not kill the AI path
            if _known_hard_cap(uid, project_id, hard_cap_hint):
                raise _budget_state_unavailable(uid, project_id, f"evaluation: {e}") from e
            logger.error("budget gate: project budget eval failed (%s) — letting call through", e)
            return
        if pr.get("exists") and pr.get("hardCap"):
            _remember_hard_cap(uid, project_id)
        if pr.get("exists"):
            eff_estimate = estimated_cost_eur
            if pr.get("hardCap") and eff_estimate <= 0:
                eff_estimate = HARD_CAP_DEFAULT_ESTIMATE_EUR.get(call_kind or "", 0.0)
            if pr.get("allowed") and not (
                pr.get("hardCap") and pr.get("remainingEur", 0.0) < eff_estimate
            ):
                return
            if pr.get("hardCap"):
                # Lauf-Deckel: the project limit is a ceiling, TopUp was not
                # consulted (project_budgets_service.evaluate).
                logger.info(
                    "budget gate: BLOCKED (hard cap) user=%s app=%s plan=%s project=%s "
                    "projectRemaining=%.4f estimate=%.4f kind=%s",
                    user_id, app_id, plan.id, project_id, pr.get("remainingEur", 0.0),
                    eff_estimate, call_kind,
                )
                raise HTTPException(
                    status_code=402,
                    detail={
                        "error": "budget_exhausted",
                        "reason": "project_budget_exhausted",
                        "retryable": False,
                        "hardCap": True,
                        "appId": app_id,
                        "planId": plan.id,
                        "projectId": project_id,
                        "totalRemainingEur": pr.get("remainingEur", 0.0),
                        "estimatedCostEur": eff_estimate,
                        "message": (
                            "Das Projektbudget ist aufgebraucht. Bitte ein neues Projekt-Paket buchen."
                            if pr.get("remainingEur", 0.0) <= 0
                            else "Der Rest des Projektbudgets reicht für diesen Aufruf nicht aus "
                                 "(geschätzte Kosten höher als der Rest). Bitte ein neues Projekt-Paket buchen."
                        ),
                    },
                )
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
