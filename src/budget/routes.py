"""
Budget endpoints.
Mounted under /v1/budget — only active when BRIDGE_DB_URL is set.

Auth model:
  - /v1/budget/check, /deduct   : require_jwt_or_service (app backends call this on every AI call)
  - /v1/budget/topup/credit     : require_service_token (internal/webhook ONLY — credits real money)
  - GET /v1/budget/{user_id}    : require_self_or_admin (a user can read own budget; admin reads any)

POST /v1/budget/check          {userId, planId, estimatedCostEur} -> BudgetCheckResult
POST /v1/budget/deduct         {userId, planId, actualCostEur} -> BudgetDeductionResult (atomic)
GET  /v1/budget/{user_id}      -> { monthlyBudgets, topUpBalanceEur, updatedAt }
POST /v1/budget/topup/credit   {userId, amountEur} -> { newBalance }  (service-token only)
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.db.client import get_pool
from src.api_auth import (
    require_jwt_or_service,
    require_service_token,
    require_self_or_admin,
    AuthClaims,
)
from src.budget.calculator import (
    UserBudget,
    MonthlyBudgetEntry,
    check_budget,
    deduct_budget,
)
from src.budget.plans import PlanConfig, get_plan, find_trial_plan_for

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/budget", tags=["budget"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _load_user_budget(conn: Any, user_id: uuid.UUID) -> UserBudget:
    budget_row = await conn.fetchrow(
        "SELECT monthly_budgets FROM user_budgets WHERE user_id = $1",
        user_id,
    )
    topup_row = await conn.fetchrow(
        "SELECT balance_eur FROM user_topup_balances WHERE user_id = $1",
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

    top_up = float(topup_row["balance_eur"]) if topup_row else 0.0

    return UserBudget(
        user_id=str(user_id),
        monthly_budgets=monthly_budgets,
        top_up_balance_eur=top_up,
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


@router.post("/check")
async def budget_check(
    body: CheckRequest,
    _claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    user_id = _parse_user_id(body.userId)
    try:
        get_plan(body.planId)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    pool = get_pool()
    effective_plan_id = body.planId

    async with pool.acquire() as conn:
        budget = await _load_user_budget(conn, user_id)

        # Auto-provision trial when user is unlicensed and a trial sibling exists.
        if budget.monthly_budgets.get(body.planId) is None:
            trial = find_trial_plan_for(body.planId)
            if trial is not None:
                effective_plan_id = trial.id
                if budget.monthly_budgets.get(trial.id) is None:
                    await _provision_trial(conn, user_id, trial)
                    budget = await _load_user_budget(conn, user_id)

    # Trial-expiry check (only for trial plans).
    if get_plan(effective_plan_id).trial:
        entry = budget.monthly_budgets.get(effective_plan_id)
        if entry and _is_trial_expired(entry):
            return {
                "allowed": False,
                "reason": "trial_expired",
                "effectivePlanId": effective_plan_id,
                "monthlyRemainingEur": 0.0,
                "topUpRemainingEur": budget.top_up_balance_eur,
                "totalRemainingEur": budget.top_up_balance_eur,
            }

    result = check_budget(budget, effective_plan_id, body.estimatedCostEur)

    reason = result.reason
    if result.allowed and effective_plan_id != body.planId:
        reason = "trial_active"

    return {
        "allowed": result.allowed,
        "reason": reason,
        "effectivePlanId": effective_plan_id,
        "monthlyRemainingEur": result.monthly_remaining_eur,
        "topUpRemainingEur": result.top_up_remaining_eur,
        "totalRemainingEur": result.total_remaining_eur,
    }


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


@router.post("/deduct")
async def budget_deduct(
    body: DeductRequest,
    _claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    user_id = _parse_user_id(body.userId)
    try:
        get_plan(body.planId)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    pool = get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock both rows for the duration of the transaction.
            budget_row = await conn.fetchrow(
                "SELECT monthly_budgets FROM user_budgets WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            topup_row = await conn.fetchrow(
                "SELECT balance_eur FROM user_topup_balances WHERE user_id = $1 FOR UPDATE",
                user_id,
            )

            raw_monthly = _parse_raw_monthly(budget_row)
            monthly_budgets = _build_monthly_budgets(raw_monthly)
            top_up = float(topup_row["balance_eur"]) if topup_row else 0.0
            budget = UserBudget(
                user_id=str(user_id),
                monthly_budgets=monthly_budgets,
                top_up_balance_eur=top_up,
            )

            # Auto-provision trial when user is unlicensed and a trial sibling exists.
            effective_plan_id = body.planId
            if budget.monthly_budgets.get(body.planId) is None:
                trial = find_trial_plan_for(body.planId)
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
                            top_up_balance_eur=top_up,
                        )

            # Trial-expiry check (only for trial plans).
            if get_plan(effective_plan_id).trial:
                entry = budget.monthly_budgets.get(effective_plan_id)
                if entry and _is_trial_expired(entry):
                    raise HTTPException(status_code=402, detail="trial_expired")

            try:
                result = deduct_budget(budget, effective_plan_id, body.actualCostEur)
            except ValueError:
                raise HTTPException(status_code=402, detail="BUDGET_EXCEEDED")

            # Write updated monthly usage back against effective_plan_id.
            updated_raw = dict(raw_monthly)
            updated_raw[effective_plan_id] = {
                **updated_raw[effective_plan_id],
                "usedEur": result.new_monthly_used,
            }

            await conn.execute(
                """
                UPDATE user_budgets
                SET monthly_budgets = $1::jsonb, updated_at = NOW()
                WHERE user_id = $2
                """,
                json.dumps(updated_raw),
                user_id,
            )

            # Upsert top-up balance.
            await conn.execute(
                """
                INSERT INTO user_topup_balances (user_id, balance_eur, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (user_id) DO UPDATE
                SET balance_eur = $2, updated_at = NOW()
                """,
                user_id,
                result.new_top_up_balance,
            )

    return {
        "fromMonthly": result.from_monthly,
        "fromTopUp": result.from_top_up,
        "newMonthlyUsed": result.new_monthly_used,
        "newTopUpBalance": result.new_top_up_balance,
        "effectivePlanId": effective_plan_id,
    }


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

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO user_topup_balances (user_id, balance_eur, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET balance_eur = user_topup_balances.balance_eur + $2, updated_at = NOW()
            RETURNING balance_eur
            """,
            user_id,
            body.amountEur,
        )

    return {"newBalance": float(row["balance_eur"])}


# ---------------------------------------------------------------------------
# GET /v1/budget/{user_id}
# ---------------------------------------------------------------------------

@router.get("/{user_id}")
async def get_budget(
    user_id: str,
    _claims: AuthClaims = Depends(require_self_or_admin),
) -> Dict[str, Any]:
    uid = _parse_user_id(user_id)
    pool = get_pool()
    async with pool.acquire() as conn:
        budget = await _load_user_budget(conn, uid)
        # updatedAt is required by the TypeScript UserBudgetSchema in
        # @werkingflow/usage-billing-admin/types — Zod parse fails without it.
        ub_row = await conn.fetchrow(
            "SELECT updated_at FROM user_budgets WHERE user_id = $1",
            uid,
        )

    updated_at = ub_row["updated_at"].isoformat() if ub_row else datetime.now(timezone.utc).isoformat()

    return {
        "userId": budget.user_id,
        "monthlyBudgets": _serialize_monthly_budgets(budget.monthly_budgets),
        "topUpBalanceEur": budget.top_up_balance_eur,
        "updatedAt": updated_at,
    }
