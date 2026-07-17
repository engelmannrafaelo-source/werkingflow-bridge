"""
Budget calculation logic (Python port of BudgetCalculator.ts).
Stateless pure functions — data comes from outside (DB).

Datenmodell (Design: packages/usage-billing-admin/docs/BUDGET-MODELL.md):
Drei Töpfe, semantisch getrennt —
  - monthly_budgets — inkludiert, interval='month', use-it-or-lose-it, Monatsreset.
  - project_budgets — inkludiert, interval='project', projektgebunden, KEIN Reset.
    (In der Bridge in eigener Tabelle/Service `project_budgets_service`; dieser
    Rechner deckt den Monats-/TopUp-Pfad ab, der über user_budgets läuft.)
  - top_up_lots    — app-übergreifendes, sichtbares Geld: datierte Lots, FIFO,
                     12-Monate-Verfall.

Verbrauchs-Reihenfolge:
  1. Inkludiertes Monatsbudget des Plans (plan-/monatsgebunden, kein Cross-App-Spend).
  2. TopUp-Lots (app-übergreifend, FIFO — ältester Kauf zuerst, Abgelaufenes übersprungen).
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# Datetime helper — ISO strings, naive treated as UTC (matches _is_trial_expired).
# ---------------------------------------------------------------------------

def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class MonthlyBudgetEntry:
    limit_eur: float
    used_eur: float
    reset_at: str  # ISO datetime


@dataclass
class TopUpLot:
    """Ein einzelner TopUp-Kauf als datiertes Lot.

    `amount_eur` ist der VERBLEIBENDE Betrag (FIFO-reduziert). `purchased_at`
    ist der reale Kaufzeitpunkt; `expires_at` = purchased_at + 12 Monate. Beides
    sind ISO-Strings — nie erfunden, immer aus dem Kauf abgeleitet.
    """
    id: str
    amount_eur: float
    purchased_at: str  # ISO
    expires_at: str    # ISO — purchased_at + 12 Monate


@dataclass
class UserBudget:
    user_id: str
    monthly_budgets: Dict[str, MonthlyBudgetEntry]  # keyed by plan_id
    top_up_lots: List[TopUpLot]


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
    # Neuer Lot-Zustand nach FIFO-Abbuchung (Quelle der Wahrheit für den Aufrufer).
    new_top_up_lots: List[TopUpLot]
    # Abgeleiteter Anzeige-Saldo (Summe aktiver Lots nach Abbuchung).
    new_top_up_balance_eur: float


# ---------------------------------------------------------------------------
# Reine Lot-Accessoren (mirror types/budget.ts isTopUpLotActive / topUpBalanceEur / nextTopUpExpiry)
# ---------------------------------------------------------------------------

def is_topup_lot_active(lot: TopUpLot, now: datetime) -> bool:
    """Aktiv, wenn noch nicht abgelaufen UND Restbetrag > 0."""
    return _parse_dt(lot.expires_at) > now and lot.amount_eur > 0


def topup_balance_eur(lots: List[TopUpLot], now: datetime) -> float:
    """Sichtbarer TopUp-Saldo = Summe der Restbeträge aller aktiven Lots."""
    return sum(lot.amount_eur for lot in lots if is_topup_lot_active(lot, now))


def next_topup_expiry(lots: List[TopUpLot], now: datetime) -> "str | None":
    """Frühestes Ablaufdatum unter den aktiven Lots ("gültig bis …"), oder None."""
    active = [lot for lot in lots if is_topup_lot_active(lot, now)]
    if not active:
        return None
    return min(active, key=lambda lot: _parse_dt(lot.expires_at)).expires_at


def consume_topup_fifo(
    lots: List[TopUpLot], amount_eur: float, now: datetime
) -> Tuple[List[TopUpLot], float]:
    """FIFO-Abbuchung: ältester Kauf zuerst, abgelaufene Lots übersprungen.

    Rein — mutiert nichts, gibt (neue Lots, tatsächlich abgebuchter Betrag) zurück.
    Public: geteilt zwischen dem Monatspfad (deduct_budget unten) und dem
    Projekt-Pfad (project_budgets_service.deduct) — dieselbe TopUp-FIFO-Logik
    für beide, kein Zweit-Implementierung.
    """
    ordered = sorted(
        lots,
        key=lambda lot: (_parse_dt(lot.purchased_at), _parse_dt(lot.expires_at)),
    )
    remaining = amount_eur
    consumed = 0.0
    new_lots: List[TopUpLot] = []
    for lot in ordered:
        if remaining <= 0 or not is_topup_lot_active(lot, now):
            new_lots.append(lot)
            continue
        take = min(remaining, lot.amount_eur)
        remaining -= take
        consumed += take
        new_lots.append(
            TopUpLot(
                id=lot.id,
                amount_eur=lot.amount_eur - take,
                purchased_at=lot.purchased_at,
                expires_at=lot.expires_at,
            )
        )
    return new_lots, consumed


def sweep_expired_topup_lots(lots: List[TopUpLot], now: datetime) -> List[TopUpLot]:
    """Idempotenter Sweep: entfernt abgelaufene (und leergebuchte) Lots.

    Ein zweiter Lauf ohne neue Abläufe ändert nichts (Identität auf bereits
    gesweepten Lots). Nicht-abgelaufene Lots mit Restbetrag bleiben erhalten.
    """
    return [lot for lot in lots if is_topup_lot_active(lot, now)]


# ---------------------------------------------------------------------------
# Check / Deduct
# ---------------------------------------------------------------------------

def check_budget(
    budget: UserBudget,
    plan_id: str,
    estimated_cost_eur: float,
    now: "datetime | None" = None,
) -> BudgetCheckResult:
    now = now or datetime.now(timezone.utc)
    top_up_remaining = topup_balance_eur(budget.top_up_lots, now)

    monthly = budget.monthly_budgets.get(plan_id)
    if not monthly:
        # Keine Lizenz für diesen Plan — TopUp allein berechtigt nicht zur Nutzung.
        return BudgetCheckResult(
            allowed=False,
            reason="unlicensed",
            monthly_remaining_eur=0.0,
            top_up_remaining_eur=top_up_remaining,
            total_remaining_eur=top_up_remaining,
        )

    monthly_remaining = max(0.0, monthly.limit_eur - monthly.used_eur)
    total_remaining = monthly_remaining + top_up_remaining

    if estimated_cost_eur <= total_remaining:
        return BudgetCheckResult(
            allowed=True,
            reason="ok",
            monthly_remaining_eur=monthly_remaining,
            top_up_remaining_eur=top_up_remaining,
            total_remaining_eur=total_remaining,
        )

    return BudgetCheckResult(
        allowed=False,
        reason="monthly_exceeded_no_topup" if monthly_remaining > 0 else "all_exhausted",
        monthly_remaining_eur=monthly_remaining,
        top_up_remaining_eur=top_up_remaining,
        total_remaining_eur=total_remaining,
    )


def deduct_budget(
    budget: UserBudget,
    plan_id: str,
    actual_cost_eur: float,
    now: "datetime | None" = None,
) -> BudgetDeductionResult:
    now = now or datetime.now(timezone.utc)
    monthly = budget.monthly_budgets.get(plan_id)
    if not monthly:
        raise ValueError(f"[BudgetCalculator] No monthly budget for plan {plan_id}")

    monthly_remaining = max(0.0, monthly.limit_eur - monthly.used_eur)
    from_monthly = min(actual_cost_eur, monthly_remaining)
    remainder = actual_cost_eur - from_monthly

    new_lots, from_top_up = consume_topup_fifo(budget.top_up_lots, remainder, now)

    if from_monthly + from_top_up < actual_cost_eur:
        raise ValueError(
            f"[BudgetCalculator] BUDGET_EXCEEDED user={budget.user_id} plan={plan_id} "
            f"cost={actual_cost_eur} monthly={monthly_remaining} "
            f"topup={topup_balance_eur(budget.top_up_lots, now)}"
        )

    return BudgetDeductionResult(
        from_monthly=from_monthly,
        from_top_up=from_top_up,
        new_monthly_used=monthly.used_eur + from_monthly,
        new_top_up_lots=new_lots,
        new_top_up_balance_eur=topup_balance_eur(new_lots, now),
    )
