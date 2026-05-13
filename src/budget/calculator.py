"""
Budget calculation logic (Python port of BudgetCalculator.ts).
Stateless pure functions — data comes from outside (DB).

Consumption order:
  1. Monthly budget of the specific plan (resets monthly)
  2. Shared top-up pool (never expires)
"""
from dataclasses import dataclass
from typing import Dict


@dataclass
class MonthlyBudgetEntry:
    limit_eur: float
    used_eur: float
    reset_at: str  # ISO datetime


@dataclass
class UserBudget:
    user_id: str
    monthly_budgets: Dict[str, MonthlyBudgetEntry]  # keyed by plan_id
    top_up_balance_eur: float


@dataclass
class BudgetCheckResult:
    allowed: bool
    reason: str
    monthly_remaining_eur: float
    top_up_remaining_eur: float
    total_remaining_eur: float


@dataclass
class BudgetDeductionResult:
    from_monthly: float
    from_top_up: float
    new_monthly_used: float
    new_top_up_balance: float


def check_budget(
    budget: UserBudget,
    plan_id: str,
    estimated_cost_eur: float,
) -> BudgetCheckResult:
    monthly = budget.monthly_budgets.get(plan_id)
    if not monthly:
        return BudgetCheckResult(
            allowed=False,
            reason="unlicensed",
            monthly_remaining_eur=0.0,
            top_up_remaining_eur=budget.top_up_balance_eur,
            total_remaining_eur=budget.top_up_balance_eur,
        )

    monthly_remaining = max(0.0, monthly.limit_eur - monthly.used_eur)
    total_remaining = monthly_remaining + budget.top_up_balance_eur

    if estimated_cost_eur <= total_remaining:
        return BudgetCheckResult(
            allowed=True,
            reason="ok",
            monthly_remaining_eur=monthly_remaining,
            top_up_remaining_eur=budget.top_up_balance_eur,
            total_remaining_eur=total_remaining,
        )

    return BudgetCheckResult(
        allowed=False,
        reason="monthly_exceeded_no_topup" if monthly_remaining > 0 else "all_exhausted",
        monthly_remaining_eur=monthly_remaining,
        top_up_remaining_eur=budget.top_up_balance_eur,
        total_remaining_eur=total_remaining,
    )


def deduct_budget(
    budget: UserBudget,
    plan_id: str,
    actual_cost_eur: float,
) -> BudgetDeductionResult:
    monthly = budget.monthly_budgets.get(plan_id)
    if not monthly:
        raise ValueError(f"[BudgetCalculator] No monthly budget for plan {plan_id}")

    monthly_remaining = max(0.0, monthly.limit_eur - monthly.used_eur)
    from_monthly = min(actual_cost_eur, monthly_remaining)
    remainder = actual_cost_eur - from_monthly
    from_top_up = min(remainder, budget.top_up_balance_eur)

    if from_monthly + from_top_up < actual_cost_eur:
        raise ValueError(
            f"[BudgetCalculator] BUDGET_EXCEEDED user={budget.user_id} plan={plan_id} "
            f"cost={actual_cost_eur} monthly={monthly_remaining} topup={budget.top_up_balance_eur}"
        )

    return BudgetDeductionResult(
        from_monthly=from_monthly,
        from_top_up=from_top_up,
        new_monthly_used=monthly.used_eur + from_monthly,
        new_top_up_balance=budget.top_up_balance_eur - from_top_up,
    )
