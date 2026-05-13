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
import uuid
from datetime import datetime, timezone
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
    pool = get_pool()
    async with pool.acquire() as conn:
        budget = await _load_user_budget(conn, user_id)

    result = check_budget(budget, body.planId, body.estimatedCostEur)
    return {
        "allowed": result.allowed,
        "reason": result.reason,
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


@router.post("/deduct")
async def budget_deduct(
    body: DeductRequest,
    _claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    user_id = _parse_user_id(body.userId)
    pool = get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock both rows for the duration of the transaction
            budget_row = await conn.fetchrow(
                "SELECT monthly_budgets FROM user_budgets WHERE user_id = $1 FOR UPDATE",
                user_id,
            )
            topup_row = await conn.fetchrow(
                "SELECT balance_eur FROM user_topup_balances WHERE user_id = $1 FOR UPDATE",
                user_id,
            )

            raw_monthly: dict = {}
            if budget_row:
                raw = budget_row["monthly_budgets"]
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
            budget = UserBudget(
                user_id=str(user_id),
                monthly_budgets=monthly_budgets,
                top_up_balance_eur=top_up,
            )

            try:
                result = deduct_budget(budget, body.planId, body.actualCostEur)
            except ValueError:
                raise HTTPException(status_code=402, detail="BUDGET_EXCEEDED")

            # Write updated monthly usage back
            updated_raw = dict(raw_monthly)
            updated_raw[body.planId] = {
                **updated_raw[body.planId],
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

            # Upsert top-up balance
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
