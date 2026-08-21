"""
Budget endpoints.
Mounted under /v1/budget — only active when BRIDGE_DB_URL is set.

Auth model:
  - /v1/budget/check, /deduct   : require_jwt_or_service (app backends call this on every AI call)
  - /v1/budget/topup/credit     : require_service_token (internal/webhook ONLY — credits real money)
  - GET /v1/budget              : require_admin (platform-admin overview of every user)
  - GET /v1/budget/{user_id}    : require_self_or_admin (a user can read own budget; admin reads any)

POST /v1/budget/check          {userId, planId, estimatedCostEur} -> BudgetCheckResult
POST /v1/budget/deduct         {userId, planId, actualCostEur} -> BudgetDeductionResult (atomic)
GET  /v1/budget                -> { items: [BudgetSummary], count }   (admin — all users)
GET  /v1/budget/{user_id}      -> { monthlyBudgets, topUpBalanceEur, topUpNextExpiryAt, updatedAt }
POST /v1/budget/topup/credit   {userId, amountEur} -> { newBalance }  (service-token only)

Budget-Modell (Design: packages/usage-billing-admin/docs/BUDGET-MODELL.md):
  - Monatstopf (interval='month') läuft über user_budgets.monthly_budgets.
  - Projekttopf (interval='project') läuft NICHT hier, sondern per Projekt über
    project_budgets_service (Tabelle project_budgets) — die Monats-Endpoints hier
    lehnen project-Interval-Pläne LAUT ab (kein stilles Einordnen in den Monatstopf).
  - TopUp ist app-übergreifendes, sichtbares Geld: datierte Lots (user_topup_lots),
    FIFO-Abbuchung, 12-Monate-Verfall. Der alte Skalar (user_topup_balances) ist
    Legacy; ein nicht-migrierter Restwert fail-loud't beim Laden.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.db.client import get_pool
from src.api_auth import (
    require_jwt_or_service,
    require_service_token,
    require_self_or_admin,
    require_admin,
    AuthClaims,
)
from src.budget.calculator import (
    rollover_monthly_if_due,
    UserBudget,
    MonthlyBudgetEntry,
    TopUpLot,
    check_budget,
    deduct_budget,
    topup_balance_eur,
    next_topup_expiry,
)
from src.budget.plans import PlanConfig, get_plan, find_trial_plan_for
from src.budget.topup_store import (
    LegacyTopUpBalanceError,
    plus_12_months as _plus_12_months,
    assert_no_legacy_topup_balance as _assert_no_legacy_topup_balance,
    load_topup_lots as _load_topup_lots,
    persist_topup_lots as _persist_topup_lots,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/budget", tags=["budget"])


# ---------------------------------------------------------------------------
# Interval guard — the monthly path must never silently absorb a project plan.
# ---------------------------------------------------------------------------

def _require_month_interval(plan_id: str) -> PlanConfig:
    """Resolve a plan and assert it belongs in the monthly budget path.

    interval='project' → belongs to project_budgets_service (per project_id),
    not this monthly endpoint. interval anything-else ('once', future values)
    → no pot defined. Both raise LOUD — no silent fallback (defensive).
    """
    plan = get_plan(plan_id)  # raises ValueError on unknown plan
    if plan.interval == "project":
        raise ValueError(
            f"[Budget] plan '{plan_id}' has interval='project' — project budgets are "
            f"per-project (project_budgets_service, keyed by project_id), not the monthly "
            f"/v1/budget path. Route this via the workflow-attribution deduction."
        )
    if plan.interval != "month":
        raise ValueError(
            f"[Budget] plan '{plan_id}' has unsupported interval '{plan.interval}' — "
            f"no monthly budget pot defined (kein stiller Fallback)."
        )
    return plan


# ---------------------------------------------------------------------------
# TopUp lot helpers (datierte Lots — user_topup_lots) live in
# src/budget/topup_store.py, shared with the per-project deduction path
# (src/billing/project_budgets_service.py) — imported above.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _load_user_budget(conn: Any, user_id: uuid.UUID) -> UserBudget:
    budget_row = await conn.fetchrow(
        "SELECT monthly_budgets FROM user_budgets WHERE user_id = $1",
        user_id,
    )

    raw_monthly: dict = {}
    if budget_row:
        raw = budget_row["monthly_budgets"]
        # asyncpg returns JSONB as dict; guard against string in edge cases
        if isinstance(raw, str):
            raw = json.loads(raw)
        raw_monthly = raw or {}

    monthly_budgets: Dict[str, MonthlyBudgetEntry] = {}
    for plan_id, entry in raw_monthly.items():
        monthly_budgets[plan_id] = MonthlyBudgetEntry(
            limit_eur=float(entry["limitEur"]),
            used_eur=float(entry["usedEur"]),
            reset_at=entry["resetAt"],
        )

    top_up_lots = await _load_topup_lots(conn, user_id)

    return UserBudget(
        user_id=str(user_id),
        monthly_budgets=monthly_budgets,
        top_up_lots=top_up_lots,
    )


def _serialize_monthly_budgets(monthly_budgets: Dict[str, MonthlyBudgetEntry]) -> dict:
    return {
        plan_id: {
            "limitEur": entry.limit_eur,
            "usedEur": entry.used_eur,
            "resetAt": entry.reset_at,
        }
        for plan_id, entry in monthly_budgets.items()
    }


def _serialize_topup_lots(lots: List[TopUpLot]) -> List[dict]:
    """Wire shape for the TS TopUpLotSchema (camelCase). purchased_at/expires_at
    are already ISO strings (see _load_topup_lots). The client derives the visible
    balance + 'gültig bis' from these lots (topUpBalanceEur/nextTopUpExpiry)."""
    return [
        {
            "id": lot.id,
            "amountEur": lot.amount_eur,
            "purchasedAt": lot.purchased_at,
            "expiresAt": lot.expires_at,
        }
        for lot in lots
    ]


def _parse_user_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid userId (must be UUID)")


async def _provision_trial(conn: Any, user_id: uuid.UUID, trial_plan: PlanConfig) -> None:
    """Atomically insert a trial MonthlyBudgetEntry for the given user.

    Uses INSERT … ON CONFLICT DO UPDATE WHERE to guarantee that concurrent
    callers for the same user never double-provision: the UPDATE is skipped
    when the trial key is already present in the JSONB column.
    """
    valid_until = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    entry_json = json.dumps({
        trial_plan.id: {
            "limitEur": float(trial_plan.api_budget_eur),
            "usedEur": 0.0,
            "resetAt": valid_until,
        }
    })
    await conn.execute(
        """
        INSERT INTO user_budgets (user_id, monthly_budgets, updated_at)
        VALUES ($1, $2::jsonb, NOW())
        ON CONFLICT (user_id) DO UPDATE
        SET monthly_budgets = user_budgets.monthly_budgets || $2::jsonb,
            updated_at = NOW()
        WHERE (user_budgets.monthly_budgets -> $3) IS NULL
        """,
        user_id,
        entry_json,
        trial_plan.id,
    )
    logger.info(
        "[BudgetCalculator] provisioned trial plan=%s user=%s app=%s valid_until=%s",
        trial_plan.id, user_id, trial_plan.app_id, valid_until,
    )


def _is_trial_expired(entry: MonthlyBudgetEntry) -> bool:
    reset_at = datetime.fromisoformat(entry.reset_at)
    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    return reset_at < datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# POST /v1/budget/check
# ---------------------------------------------------------------------------

class CheckRequest(BaseModel):
    userId: str
    planId: str
    # Estimated cost cannot be negative. Zero is allowed (free model probe).
    estimatedCostEur: float = Field(ge=0)


async def evaluate_budget(
    user_id: uuid.UUID,
    plan_id: str,
    estimated_cost_eur: float,
) -> Dict[str, Any]:
    """
    Core budget evaluation — load, auto-provision trial, expiry-check, check.

    Pure logic, no HTTP. Shared by the POST /v1/budget/check endpoint and the
    chat/completions budget gate (src/budget/gate.py). Raises ValueError for
    an unknown plan_id OR a non-monthly interval (project plans belong to
    project_budgets_service); callers map that to 400.

    Returns the same dict shape the /check endpoint returns.
    """
    _require_month_interval(plan_id)  # raises on unknown / project / unsupported interval

    effective_plan_id = plan_id

    # ADR-0009 Schritt 2b/C4: the read and the (conditional) trial write now
    # go through platform-api, each with a direct-DB fallback. They are no
    # longer wrapped in ONE pool connection — see _ensure_trial_via_platform:
    # provisioning and the re-read happen inside a single endpoint call, so
    # the atomicity that mattered here (provision, then observe the result)
    # is preserved on whichever side actually answers.
    budget = await _load_budget_via_platform(user_id)

    # Auto-provision trial when user is unlicensed and a trial sibling exists.
    if budget.monthly_budgets.get(plan_id) is None:
        trial = find_trial_plan_for(plan_id)
        if trial is not None:
            effective_plan_id = trial.id
            if budget.monthly_budgets.get(trial.id) is None:
                budget = await _ensure_trial_via_platform(user_id, plan_id, trial)

    # Faelligen Monatstopf zuruecksetzen, BEVOR das Tor rechnet. Ohne das ist
    # ein "Monatsbudget" ein Lebenszeit-Deckel (s. rollover_monthly_if_due).
    # Nur Nicht-Trials: bei Trials ist reset_at das Ablaufdatum, nicht der
    # Zyklusanker — ein Rollover machte den Trial unsterblich.
    # Nur im Speicher: /check ist ein Vorab-Tor ohne Schreibrecht; persistiert
    # wird der neue Anker im Abbuchungspfad, der ohnehin schreibt.
    if not get_plan(effective_plan_id).trial:
        entry = budget.monthly_budgets.get(effective_plan_id)
        if entry is not None:
            rolled, did_roll = rollover_monthly_if_due(entry)
            if did_roll:
                budget.monthly_budgets[effective_plan_id] = rolled
                logger.info(
                    "[BudgetCheck] Monatstopf zurueckgesetzt user=%s plan=%s "
                    "verbraucht_vorher=%.4f neuer_anker=%s",
                    user_id, effective_plan_id, entry.used_eur, rolled.reset_at,
                )

    # Trial-expiry check (only for trial plans).
    if get_plan(effective_plan_id).trial:
        entry = budget.monthly_budgets.get(effective_plan_id)
        if entry and _is_trial_expired(entry):
            # Best-effort: mark any active trial subscription as expired.
            # Non-fatal — a missing subscription row (trial was budget-only) is fine.
            try:
                from src.billing.billing_service import expire_subscription_for_user_plan
                await expire_subscription_for_user_plan(str(user_id), effective_plan_id)
            except Exception:
                logger.warning(
                    "[BudgetCheck] expire_subscription_for_user_plan failed for user=%s plan=%s",
                    user_id, effective_plan_id, exc_info=True,
                )
            top_up_remaining = topup_balance_eur(budget.top_up_lots, datetime.now(timezone.utc))
            return {
                "allowed": False,
                "reason": "trial_expired",
                "effectivePlanId": effective_plan_id,
                "monthlyRemainingEur": 0.0,
                "topUpRemainingEur": top_up_remaining,
                "totalRemainingEur": top_up_remaining,
            }

    result = check_budget(budget, effective_plan_id, estimated_cost_eur)

    reason = result.reason
    if result.allowed and effective_plan_id != plan_id:
        reason = "trial_active"

    return {
        "allowed": result.allowed,
        "reason": reason,
        "effectivePlanId": effective_plan_id,
        "monthlyRemainingEur": result.monthly_remaining_eur,
        "topUpRemainingEur": result.top_up_remaining_eur,
        "totalRemainingEur": result.total_remaining_eur,
    }


@router.post("/check")
async def budget_check(
    body: CheckRequest,
    _claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    user_id = _parse_user_id(body.userId)
    try:
        return await evaluate_budget(user_id, body.planId, body.estimatedCostEur)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# POST /v1/budget/deduct
# ---------------------------------------------------------------------------

class DeductRequest(BaseModel):
    userId: str
    planId: str
    # Real costs are always positive: zero or negative would let a caller
    # *inflate* the user's remaining budget. The Pydantic Field(gt=0) is the
    # primary guard; deduct_budget's internal arithmetic also assumes positive.
    actualCostEur: float = Field(gt=0)


def _parse_raw_monthly(budget_row: Any) -> dict:
    if not budget_row:
        return {}
    raw = budget_row["monthly_budgets"]
    if isinstance(raw, str):
        raw = json.loads(raw)
    return raw or {}


def _build_monthly_budgets(raw_monthly: dict) -> Dict[str, MonthlyBudgetEntry]:
    return {
        plan_id: MonthlyBudgetEntry(
            limit_eur=float(entry["limitEur"]),
            used_eur=float(entry["usedEur"]),
            reset_at=entry["resetAt"],
        )
        for plan_id, entry in raw_monthly.items()
    }


class BudgetDeductionDenied(Exception):
    """Deduction refused for a budget reason (not a validation error)."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def apply_budget_deduction(
    user_id: uuid.UUID,
    plan_id: str,
    actual_cost_eur: float,
) -> Dict[str, Any]:
    """
    Atomically deduct actual_cost_eur from the user's MONTHLY budget + TopUp lots.

    Monthly-interval plans only — project plans are per-project (routed by the
    caller to project_budgets_service). Raises ValueError on unknown/non-monthly
    plan, BudgetDeductionDenied(reason) on 'BUDGET_EXCEEDED' / 'trial_expired'.

    The TopUp portion is drawn FIFO across the user's datierte Lots (oldest
    purchase first, expired lots skipped) and the reduced lot amounts are
    persisted in the same transaction.
    """
    _require_month_interval(plan_id)  # raises on unknown / project / unsupported interval

    pool = get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock monthly + lot rows for the duration of the transaction.
            budget_row = await conn.fetchrow(
                "SELECT monthly_budgets FROM user_budgets WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            old_lots = await _load_topup_lots(conn, user_id, for_update=True)

            raw_monthly = _parse_raw_monthly(budget_row)
            monthly_budgets = _build_monthly_budgets(raw_monthly)
            budget = UserBudget(
                user_id=str(user_id),
                monthly_budgets=monthly_budgets,
                top_up_lots=old_lots,
            )

            # Auto-provision trial when user is unlicensed and a trial sibling exists.
            effective_plan_id = plan_id
            if budget.monthly_budgets.get(plan_id) is None:
                trial = find_trial_plan_for(plan_id)
                if trial is not None:
                    effective_plan_id = trial.id
                    if budget.monthly_budgets.get(trial.id) is None:
                        await _provision_trial(conn, user_id, trial)
                        budget_row = await conn.fetchrow(
                            "SELECT monthly_budgets FROM user_budgets WHERE user_id = $1 FOR UPDATE",
                            user_id,
                        )
                        raw_monthly = _parse_raw_monthly(budget_row)
                        monthly_budgets = _build_monthly_budgets(raw_monthly)
                        budget = UserBudget(
                            user_id=str(user_id),
                            monthly_budgets=monthly_budgets,
                            top_up_lots=old_lots,
                        )

            # Faelligen Monatstopf zuruecksetzen (gleiche Regel wie im
            # Pruefpfad) — hier MIT Persistenz des neuen Ankers, sonst waere
            # der Reset bei jedem Aufruf neu faellig und der Topf effektiv
            # unbegrenzt. Nur Nicht-Trials (reset_at = Ablauf bei Trials).
            neuer_anker: str | None = None
            if not get_plan(effective_plan_id).trial:
                entry = budget.monthly_budgets.get(effective_plan_id)
                if entry is not None:
                    rolled, did_roll = rollover_monthly_if_due(entry)
                    if did_roll:
                        budget.monthly_budgets[effective_plan_id] = rolled
                        neuer_anker = rolled.reset_at
                        logger.info(
                            "[BudgetDeduct] Monatstopf zurueckgesetzt user=%s plan=%s "
                            "verbraucht_vorher=%.4f neuer_anker=%s",
                            user_id, effective_plan_id, entry.used_eur, rolled.reset_at,
                        )

            # Trial-expiry check (only for trial plans).
            if get_plan(effective_plan_id).trial:
                entry = budget.monthly_budgets.get(effective_plan_id)
                if entry and _is_trial_expired(entry):
                    raise BudgetDeductionDenied("trial_expired")

            try:
                result = deduct_budget(budget, effective_plan_id, actual_cost_eur)
            except ValueError:
                raise BudgetDeductionDenied("BUDGET_EXCEEDED")

            # Write updated monthly usage back against effective_plan_id.
            updated_raw = dict(raw_monthly)
            updated_raw[effective_plan_id] = {
                **updated_raw[effective_plan_id],
                "usedEur": result.new_monthly_used,
            }
            if neuer_anker is not None:
                updated_raw[effective_plan_id]["resetAt"] = neuer_anker

            await conn.execute(
                """
                UPDATE user_budgets
                SET monthly_budgets = $1::jsonb, updated_at = NOW()
                WHERE user_id = $2
                """,
                json.dumps(updated_raw),
                user_id,
            )

            # Persist FIFO-reduced TopUp lots (only the changed ones).
            await _persist_topup_lots(conn, old_lots, result.new_top_up_lots)

    return {
        "fromMonthly": result.from_monthly,
        "fromTopUp": result.from_top_up,
        "newMonthlyUsed": result.new_monthly_used,
        "newTopUpBalance": result.new_top_up_balance_eur,
        "effectivePlanId": effective_plan_id,
    }


async def apply_budget_deduction_via_platform(
    user_id: uuid.UUID,
    plan_id: str,
    actual_cost_eur: float,
) -> Dict[str, Any]:
    """The worker's way to apply_budget_deduction, over POST /v1/budget/deduct
    (ADR-0009 Schritt 2c).

    The endpoint already existed and fits this call exactly: same three
    arguments, same return shape, and its two error answers map back onto the
    two exceptions the in-process caller already handles — 402 → the reason
    carried by BudgetDeductionDenied, 400 → ValueError. Nothing had to be
    reshaped to make it fit, which is the test for reusing an endpoint rather
    than inventing one. It covers ONLY monthly plans (`_require_month_interval`);
    the project half needed a new leaf — project_budgets_service.deduct_via_platform.

    NO retry and NO direct-DB fallback, and here the two are the same rule.
    apply_budget_deduction is a read-modify-write on user_budgets plus a FIFO
    draw through the TopUp lots, with no dedup key: a second attempt after a
    lost ANSWER is indistinguishable from a first attempt and would charge the
    customer twice. Falling back to the local query on a timeout would be
    exactly that second attempt, only disguised as a fallback. So an
    unanswerable deduction stays unapplied, which is the pre-existing
    best-effort contract of this path (the ledger row, not the tally, is the
    authoritative record) — the caller logs it and moves on.

    Raises PlatformUnavailable when platform-api could not answer. The caller's
    existing catch-all treats that identically to the DB error it replaces.
    """
    from src.platform_client import call_platform

    resp = await call_platform(
        "POST", "/v1/budget/deduct",
        json={
            "userId": str(user_id),
            "planId": plan_id,
            "actualCostEur": actual_cost_eur,
        },
        retries=0,  # not idempotent — see above
    )

    if resp.status_code == 200 and isinstance(resp.json, dict):
        return resp.json
    if resp.status_code == 402:
        detail = (resp.json or {}).get("detail")
        raise BudgetDeductionDenied(str(detail) if detail else "denied")
    if resp.status_code == 400:
        raise ValueError(str((resp.json or {}).get("detail") or "invalid deduction"))

    # Anything else is not an answer we can act on. Raising (rather than
    # returning something empty) keeps a mis-deployed route from reading as a
    # successful deduction, which would understate every customer's usage
    # silently.
    raise RuntimeError(
        f"POST /v1/budget/deduct answered status={resp.status_code} "
        f"body={resp.json!r} — deduction not applied"
    )


@router.post("/deduct")
async def budget_deduct(
    body: DeductRequest,
    _claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    user_id = _parse_user_id(body.userId)
    try:
        return await apply_budget_deduction(user_id, body.planId, body.actualCostEur)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BudgetDeductionDenied as e:
        raise HTTPException(status_code=402, detail=e.reason)


# ---------------------------------------------------------------------------
# POST /v1/budget/topup/credit   (must be before /{user_id} to avoid path clash)
# ---------------------------------------------------------------------------

class TopUpCreditRequest(BaseModel):
    userId: str
    # Credits to a user's prepaid balance. Service-token only — every call
    # is real-money equivalent. amountEur must be strictly positive; Pydantic
    # enforces this here, and we double-check below for defence in depth.
    amountEur: float = Field(gt=0)


@router.post("/topup/credit")
async def topup_credit(
    body: TopUpCreditRequest,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    if body.amountEur <= 0:
        # Should never reach here thanks to Field(gt=0), but defence in depth.
        raise HTTPException(status_code=400, detail="amountEur must be > 0")
    user_id = _parse_user_id(body.userId)

    now = datetime.now(timezone.utc)
    expires_at = _plus_12_months(now)

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # A non-migrated legacy scalar balance would make the new lots-based
            # balance wrong — surface it LOUD rather than credit on top of it.
            await _assert_no_legacy_topup_balance(conn, user_id)
            await conn.execute(
                """
                INSERT INTO user_topup_lots (user_id, amount_eur, purchased_at, expires_at)
                VALUES ($1, $2, $3, $4)
                """,
                user_id,
                body.amountEur,
                now,
                expires_at,
            )
            lots = await _load_topup_lots(conn, user_id)

    return {
        "newBalance": topup_balance_eur(lots, now),
        "topUpNextExpiryAt": next_topup_expiry(lots, now),
    }


# ---------------------------------------------------------------------------
# GET /v1/budget/{user_id}
# ---------------------------------------------------------------------------

@router.get("")
async def list_budgets(
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """Admin overview — one budget summary per user in a single query.

    GET /{user_id} stays the per-user detail view; this list is what the
    Platform-Admin UI joins against /v1/users to render the budget column.
    Aggregates usedEur/limitEur across all plan budgets a user holds, and the
    sichtbaren TopUp-Saldo (Summe der aktiven, nicht-abgelaufenen Lots).
    """
    now = datetime.now(timezone.utc)
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT ub.user_id, ub.monthly_budgets, ub.updated_at,
                   COALESCE((
                       SELECT SUM(l.amount_eur)
                       FROM user_topup_lots l
                       WHERE l.user_id = ub.user_id AND l.expires_at > NOW() AND l.amount_eur > 0
                   ), 0) AS topup_balance
            FROM user_budgets ub
            """
        )

    items = []
    for row in rows:
        raw = row["monthly_budgets"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        raw_monthly = raw or {}

        used_eur = 0.0
        limit_eur = 0.0
        monthly: Dict[str, Any] = {}
        for plan_id, entry in raw_monthly.items():
            e_used = float(entry["usedEur"])
            e_limit = float(entry["limitEur"])
            used_eur += e_used
            limit_eur += e_limit
            monthly[plan_id] = {
                "limitEur": e_limit,
                "usedEur": e_used,
                "resetAt": entry["resetAt"],
            }

        topup = float(row["topup_balance"]) if row["topup_balance"] is not None else 0.0

        items.append({
            "userId": str(row["user_id"]),
            "monthlyBudgets": monthly,
            "topUpBalanceEur": topup,
            "usedEur": round(used_eur, 4),
            "limitEur": round(limit_eur, 4),
            "remainingEur": round(limit_eur - used_eur + topup, 4),
            "updatedAt": row["updated_at"].isoformat() if row["updated_at"] else None,
        })

    return {"items": items, "count": len(items)}


@router.get("/{user_id}")
async def get_budget(
    user_id: str,
    _claims: AuthClaims = Depends(require_self_or_admin),
) -> Dict[str, Any]:
    uid = _parse_user_id(user_id)
    now = datetime.now(timezone.utc)
    pool = get_pool()
    async with pool.acquire() as conn:
        budget = await _load_user_budget(conn, uid)
        # updatedAt is required by the TypeScript UserBudgetSchema in
        # @werkingflow/usage-billing-admin/types — Zod parse fails without it.
        ub_row = await conn.fetchrow(
            "SELECT updated_at FROM user_budgets WHERE user_id = $1",
            uid,
        )
        # Current-month token/cost aggregates from usage_events, grouped by source.
        # Counts input+output only (cache tokens excluded — minor contribution).
        # hypothetical_cost_eur is used for both billing modes: for flat-rate users
        # it represents estimated cost at pay-per-token rates (real_cost is 0).
        # Note: rows where user_id=NULL (invalid-UUID user from workflow calls)
        # are not matched here — this is a known gap (P4, separate fix).
        usage_rows = await conn.fetch(
            """
            SELECT
                source,
                COALESCE(SUM(input_tokens + output_tokens), 0)::bigint AS tokens_used,
                COALESCE(SUM(hypothetical_cost_eur), 0.0) AS cost_eur
            FROM usage_events
            WHERE user_id = $1
              AND recorded_at >= date_trunc('month', NOW() AT TIME ZONE 'UTC')
            GROUP BY source
            """,
            uid,
        )

    updated_at = ub_row["updated_at"].isoformat() if ub_row else datetime.now(timezone.utc).isoformat()

    tokens_by_source: Dict[str, int] = {}
    cost_by_source: Dict[str, float] = {}
    for row in usage_rows:
        src = row["source"]
        tokens_by_source[src] = int(row["tokens_used"])
        cost_by_source[src] = float(row["cost_eur"])

    return {
        "userId": budget.user_id,
        "monthlyBudgets": _serialize_monthly_budgets(budget.monthly_budgets),
        # projectBudgets kommt vom separaten project-budgets-Endpoint; hier leer,
        # aber vom TS UserBudgetSchema als Feld verlangt (z.record akzeptiert {}).
        "projectBudgets": {},
        # topUpLots = Raw-Shape, die das TS UserBudgetSchema erwartet; der Client
        # leitet Balance + "gültig bis" daraus ab. Ohne dieses Feld schlägt der
        # Zod-parse im Frontend fehl → Budget-Widget still leer.
        "topUpLots": _serialize_topup_lots(budget.top_up_lots),
        # Abgeleitete Anzeige-Skalare aus den Lots (Summe aktiver Lots + frühestes Ablaufdatum).
        "topUpBalanceEur": topup_balance_eur(budget.top_up_lots, now),
        "topUpNextExpiryAt": next_topup_expiry(budget.top_up_lots, now),
        "updatedAt": updated_at,
        "monthlyTokensUsed": sum(tokens_by_source.values()),
        "sandboxUsedEur": round(cost_by_source.get("sandbox", 0.0), 4),
        "sandboxTokensUsed": tokens_by_source.get("sandbox", 0),
        "workflowTokensUsed": tokens_by_source.get("workflow", 0),
    }


# ── ADR-0009 Schritt 2b: named DB leaves for the worker↔platform-api split ──
#
# These two exist so platform-api's /v1/internal endpoints can call exactly the
# same functions the worker used to call in-process (same pattern as Schritt
# 2a's principals.get_principal_row_by_hash). The query and the provisioning
# SQL stay in ONE place; only the caller's location changes.


async def load_user_budget_state(user_id: uuid.UUID) -> Dict[str, Any]:
    """The read half of evaluate_budget's monthly path, as a serializable dict.

    Deliberately returns DATA, not a verdict. Everything the gate decides on
    top of this — rollover_monthly_if_due, check_budget, trial-expiry handling —
    is stateless pure computation (src/budget/calculator.py) and stays in the
    worker process, where it already is. Moving the verdict here instead would
    mean a second copy of gate.py's branching logic living on the platform-api
    side, which is exactly the duplication Schritt 2b is designed to avoid.

    Raises LegacyTopUpBalanceError (via _load_topup_lots) for a non-migrated
    legacy scalar balance. That is a deliberate fail-loud safeguard, NOT an
    outage — the HTTP layer must surface it as a 4xx, never a 5xx, or the
    worker's client would turn it into PlatformUnavailable and the gate's
    fail-open catch-all would silently swallow a data-integrity alarm.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        budget = await _load_user_budget(conn, user_id)
    return {
        "userId": budget.user_id,
        "monthlyBudgets": _serialize_monthly_budgets(budget.monthly_budgets),
        "topUpLots": _serialize_topup_lots(budget.top_up_lots),
    }


async def ensure_trial_provisioned(
    user_id: uuid.UUID, plan_id: str
) -> Dict[str, Any]:
    """Idempotently provision the trial sibling of `plan_id`, then return the
    refreshed budget state (ADR-0009 Schritt 2b, D2).

    Returns the state as well as the outcome so the caller needs ONE round trip,
    mirroring what evaluate_budget does in-process today (provision, then
    re-load). `provisioned` reports whether this call was the one that created
    the entry — informational only; callers must not branch billing on it,
    because a concurrent caller legitimately wins that race.

    Safe to retry: _provision_trial is INSERT … ON CONFLICT DO UPDATE … WHERE
    the trial key IS NULL, so a replay cannot double-provision or reset an
    existing trial window. That property is what lets the worker-side call site
    opt into platform_client's bounded retry.
    """
    trial = find_trial_plan_for(plan_id)
    if trial is None:
        return {"provisioned": False, "trialPlanId": None, "state": None}

    pool = get_pool()
    async with pool.acquire() as conn:
        before = await _load_user_budget(conn, user_id)
        already = before.monthly_budgets.get(trial.id) is not None
        if not already:
            await _provision_trial(conn, user_id, trial)
        budget = await _load_user_budget(conn, user_id)

    return {
        "provisioned": not already,
        "trialPlanId": trial.id,
        "state": {
            "userId": budget.user_id,
            "monthlyBudgets": _serialize_monthly_budgets(budget.monthly_budgets),
            "topUpLots": _serialize_topup_lots(budget.top_up_lots),
        },
    }


# ── ADR-0009 Schritt 2b/C4: monthly path via platform-api ──────────────────


def _deserialize_user_budget(payload: Dict[str, Any]) -> UserBudget:
    """Inverse of load_user_budget_state's wire shape.

    Kept next to the serializers on purpose: if one side gains a field, the
    other is one screen away. A missing/renamed key raises here rather than
    yielding a silently empty budget — an empty budget would read as
    "unlicensed" and block a paying customer.
    """
    monthly: Dict[str, MonthlyBudgetEntry] = {
        plan_id: MonthlyBudgetEntry(
            limit_eur=float(entry["limitEur"]),
            used_eur=float(entry["usedEur"]),
            reset_at=entry["resetAt"],
        )
        for plan_id, entry in (payload["monthlyBudgets"] or {}).items()
    }
    lots: List[TopUpLot] = [
        TopUpLot(
            id=str(lot["id"]),
            amount_eur=float(lot["amountEur"]),
            purchased_at=lot["purchasedAt"],
            expires_at=lot["expiresAt"],
        )
        for lot in (payload.get("topUpLots") or [])
    ]
    return UserBudget(
        user_id=str(payload["userId"]), monthly_budgets=monthly, top_up_lots=lots
    )


def _reraise_legacy_topup(resp_json: Any) -> None:
    """Turn a 409 legacy-top-up answer back into the identical exception the
    direct path would have raised. Never falls back to the DB: a 409 is a
    definitive answer, and retrying it locally would only hit the same row."""
    detail = (resp_json or {}).get("detail") or {}
    raise LegacyTopUpBalanceError(
        uuid.UUID(detail["userId"]), float(detail["balanceEur"])
    )


async def _load_user_budget_direct(user_id: uuid.UUID) -> UserBudget:
    pool = get_pool()
    async with pool.acquire() as conn:
        return await _load_user_budget(conn, user_id)


async def _load_budget_via_platform(user_id: uuid.UUID) -> UserBudget:
    """platform-api → direct-DB fallback for the monthly read half.

    No cache, deliberately (see project_budgets_service's 2b wrappers): this is
    live money state, and caching it would widen the already-accepted advisory
    gap between the gate's read and the post-call deduction by the whole TTL.

    Opts into one retry — a pure read.
    """
    from src.platform_client import PlatformUnavailable, call_platform

    try:
        resp = await call_platform(
            "POST", "/v1/internal/budget/user-budget-state",
            json={"user_id": str(user_id)}, retries=1,
        )
    except PlatformUnavailable as e:
        logger.error(
            "user budget state via platform-api failed (%s) — falling back to direct DB", e
        )
        return await _load_user_budget_direct(user_id)

    if resp.status_code == 409:
        _reraise_legacy_topup(resp.json)
    if resp.status_code == 200 and isinstance(resp.json, dict) and "monthlyBudgets" in resp.json:
        return _deserialize_user_budget(resp.json)

    logger.error(
        "user budget state via platform-api returned unexpected status=%s body=%r "
        "— falling back to direct DB",
        resp.status_code, resp.json,
    )
    return await _load_user_budget_direct(user_id)


async def _ensure_trial_via_platform(
    user_id: uuid.UUID, plan_id: str, trial_plan: PlanConfig
) -> UserBudget:
    """platform-api → direct-DB fallback for trial provisioning (D2).

    The only write in the gate chain. Opts into one retry, which is safe because
    _provision_trial is INSERT … ON CONFLICT … WHERE the trial key IS NULL: a
    replay after a lost answer cannot double-provision or reset a running trial.

    Returns the REFRESHED budget, exactly as the in-process path did (provision,
    then re-load), so the caller keeps its single-round-trip shape.
    """
    from src.platform_client import PlatformUnavailable, call_platform

    try:
        resp = await call_platform(
            "POST", "/v1/internal/budget/ensure-trial",
            json={"user_id": str(user_id), "plan_id": plan_id}, retries=1,
        )
    except PlatformUnavailable as e:
        logger.error(
            "trial provisioning via platform-api failed (%s) — falling back to direct DB", e
        )
        pool = get_pool()
        async with pool.acquire() as conn:
            await _provision_trial(conn, user_id, trial_plan)
            return await _load_user_budget(conn, user_id)

    if resp.status_code == 409:
        _reraise_legacy_topup(resp.json)
    if (
        resp.status_code == 200
        and isinstance(resp.json, dict)
        and isinstance(resp.json.get("state"), dict)
    ):
        return _deserialize_user_budget(resp.json["state"])

    logger.error(
        "trial provisioning via platform-api returned unexpected status=%s body=%r "
        "— falling back to direct DB",
        resp.status_code, resp.json,
    )
    pool = get_pool()
    async with pool.acquire() as conn:
        await _provision_trial(conn, user_id, trial_plan)
        return await _load_user_budget(conn, user_id)
