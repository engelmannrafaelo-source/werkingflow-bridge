"""
TopUp lot store — shared DB-access layer for the user's TopUp balance
(user_topup_lots).

TopUp is app-übergreifendes, fungibles Geld (BUDGET-MODELL.md Leitprinzip):
the SAME pot backs both budget paths —

  - the monthly path (src/budget/routes.py: apply_budget_deduction), and
  - the per-project path (src/billing/project_budgets_service.py: deduct),

so both draw from and write back to user_topup_lots through these exact
functions. Two independent load/consume/persist implementations would risk
diverging semantics (e.g. one honouring the 12-month expiry, the other not)
and, worse, a lost update if both raced on the same user's lots without a
shared FOR UPDATE contract.

Extracted 2026-07 from src/budget/routes.py when the per-project path gained
a TopUp fallback (see BUDGET-MODELL.md Regel 6 — a project's own EUR-100
budget is not fungible, but TopUp is, so an exhausted project must still be
able to draw on it before the call is blocked).
"""
from __future__ import annotations

import calendar
import uuid
from datetime import datetime
from typing import Any, List

from src.budget.calculator import TopUpLot


class LegacyTopUpBalanceError(RuntimeError):
    """A user still carries a non-zero scalar top-up balance (old model).

    Raised on load so unmigrated customer money surfaces LOUD instead of
    silently vanishing behind the lots-only read path. Backfill is a
    deliberate, gated step (siehe Migrations-Skizze im Report) — never a
    silent runtime fixup.
    """
    def __init__(self, user_id: uuid.UUID, balance_eur: float):
        # Kept as attributes so the same failure can be reconstructed faithfully
        # on the other side of the worker→platform-api hop (ADR-0009 Schritt 2b):
        # same type, same message, whichever channel answered.
        self.user_id = user_id
        self.balance_eur = balance_eur
        super().__init__(
            f"[Budget] user {user_id} has legacy scalar top-up balance "
            f"{balance_eur:.4f} EUR in user_topup_balances but the model now uses "
            f"datierte Lots (user_topup_lots). Refusing to serve a lots-only view "
            f"that hides this money. Backfill required (see migration sketch)."
        )
        self.user_id = user_id
        self.balance_eur = balance_eur


def plus_12_months(dt: datetime) -> datetime:
    """dt + 12 Monate (Tag auf Monatslänge geklemmt). Kein Raten — deterministisch."""
    month_index = dt.month - 1 + 12
    year = dt.year + month_index // 12
    month = month_index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


async def assert_no_legacy_topup_balance(conn: Any, user_id: uuid.UUID) -> None:
    row = await conn.fetchrow(
        "SELECT balance_eur FROM user_topup_balances WHERE user_id = $1",
        user_id,
    )
    if row is not None and float(row["balance_eur"]) > 0:
        raise LegacyTopUpBalanceError(user_id, float(row["balance_eur"]))


async def load_topup_lots(conn: Any, user_id: uuid.UUID, *, for_update: bool = False) -> List[TopUpLot]:
    """Load a user's TopUp lots. Fail-loud on a non-migrated legacy scalar balance."""
    await assert_no_legacy_topup_balance(conn, user_id)
    sql = (
        "SELECT id, amount_eur, purchased_at, expires_at "
        "FROM user_topup_lots WHERE user_id = $1"
    )
    if for_update:
        sql += " FOR UPDATE"
    rows = await conn.fetch(sql, user_id)
    return [
        TopUpLot(
            id=str(r["id"]),
            amount_eur=float(r["amount_eur"]),
            purchased_at=r["purchased_at"].isoformat(),
            expires_at=r["expires_at"].isoformat(),
        )
        for r in rows
    ]


async def persist_topup_lots(
    conn: Any, old_lots: List[TopUpLot], new_lots: List[TopUpLot]
) -> None:
    """Write back only the lots whose remaining amount changed (FIFO deduction)."""
    old_by_id = {lot.id: lot.amount_eur for lot in old_lots}
    for lot in new_lots:
        if old_by_id.get(lot.id) != lot.amount_eur:
            await conn.execute(
                "UPDATE user_topup_lots SET amount_eur = $2, updated_at = NOW() WHERE id = $1",
                uuid.UUID(lot.id),
                lot.amount_eur,
            )
