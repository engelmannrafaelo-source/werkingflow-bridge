"""
Tests für das TopUp-Lots-/Interval-Modell (Budget-Modell final, 2026-07-05).

Deckt ab:
  - FIFO-Abbuchung (ältester Kauf zuerst), abgelaufene Lots übersprungen
  - 12-Monate-Verfall + abgeleitete Anzeige-Skalare (Balance / nächstes Ablaufdatum)
  - idempotenter Sweep
  - Monatstopf zuerst, dann TopUp; BUDGET_EXCEEDED wenn beides nicht reicht
  - Projekt-Interval-Guard (kein stilles Einordnen in den Monatstopf)
  - Fail-loud beim Laden auf einen nicht-migrierten Legacy-Skalar
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.budget.calculator import (
    UserBudget,
    MonthlyBudgetEntry,
    TopUpLot,
    check_budget,
    deduct_budget,
    is_topup_lot_active,
    topup_balance_eur,
    next_topup_expiry,
    sweep_expired_topup_lots,
    _consume_topup_fifo,
)

NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)


def _lot(id_: str, amount: float, purchased_days_ago: int, expires_in_days: int) -> TopUpLot:
    return TopUpLot(
        id=id_,
        amount_eur=amount,
        purchased_at=(NOW - timedelta(days=purchased_days_ago)).isoformat(),
        expires_at=(NOW + timedelta(days=expires_in_days)).isoformat(),
    )


# ---------------------------------------------------------------------------
# Lot-Accessoren: aktiv / Balance / nächstes Ablaufdatum
# ---------------------------------------------------------------------------

class TestLotAccessors:
    def test_active_lot_counts(self):
        lot = _lot("a", 20.0, purchased_days_ago=10, expires_in_days=300)
        assert is_topup_lot_active(lot, NOW) is True

    def test_expired_lot_not_active(self):
        lot = _lot("a", 20.0, purchased_days_ago=400, expires_in_days=-1)
        assert is_topup_lot_active(lot, NOW) is False

    def test_zero_amount_lot_not_active(self):
        lot = _lot("a", 0.0, purchased_days_ago=1, expires_in_days=300)
        assert is_topup_lot_active(lot, NOW) is False

    def test_balance_sums_only_active_lots(self):
        lots = [
            _lot("a", 20.0, 10, 300),   # active
            _lot("b", 15.0, 400, -1),   # expired → excluded
            _lot("c", 5.0, 1, 300),     # active
        ]
        assert topup_balance_eur(lots, NOW) == pytest.approx(25.0)

    def test_next_expiry_is_earliest_active(self):
        lots = [
            _lot("a", 20.0, 10, 300),
            _lot("b", 5.0, 1, 30),   # expires soonest among active
            _lot("c", 15.0, 400, -1),  # expired → ignored
        ]
        assert next_topup_expiry(lots, NOW) == lots[1].expires_at

    def test_next_expiry_none_when_no_active(self):
        lots = [_lot("a", 20.0, 400, -1)]
        assert next_topup_expiry(lots, NOW) is None


# ---------------------------------------------------------------------------
# FIFO-Abbuchung
# ---------------------------------------------------------------------------

class TestFifo:
    def test_oldest_purchase_consumed_first(self):
        lots = [
            _lot("new", 50.0, purchased_days_ago=1, expires_in_days=300),
            _lot("old", 50.0, purchased_days_ago=100, expires_in_days=300),
        ]
        new_lots, consumed = _consume_topup_fifo(lots, 30.0, NOW)
        assert consumed == pytest.approx(30.0)
        by_id = {lot.id: lot.amount_eur for lot in new_lots}
        assert by_id["old"] == pytest.approx(20.0)   # drawn first
        assert by_id["new"] == pytest.approx(50.0)   # untouched

    def test_spills_across_lots(self):
        lots = [
            _lot("old", 20.0, purchased_days_ago=100, expires_in_days=300),
            _lot("new", 50.0, purchased_days_ago=1, expires_in_days=300),
        ]
        new_lots, consumed = _consume_topup_fifo(lots, 35.0, NOW)
        assert consumed == pytest.approx(35.0)
        by_id = {lot.id: lot.amount_eur for lot in new_lots}
        assert by_id["old"] == pytest.approx(0.0)    # fully drained
        assert by_id["new"] == pytest.approx(35.0)   # remainder 15 taken

    def test_expired_lot_skipped_even_if_oldest(self):
        lots = [
            _lot("expired-old", 100.0, purchased_days_ago=400, expires_in_days=-1),
            _lot("active", 40.0, purchased_days_ago=10, expires_in_days=300),
        ]
        new_lots, consumed = _consume_topup_fifo(lots, 25.0, NOW)
        assert consumed == pytest.approx(25.0)
        by_id = {lot.id: lot.amount_eur for lot in new_lots}
        assert by_id["expired-old"] == pytest.approx(100.0)  # untouched (expired)
        assert by_id["active"] == pytest.approx(15.0)

    def test_partial_consume_when_insufficient(self):
        lots = [_lot("a", 10.0, 10, 300)]
        new_lots, consumed = _consume_topup_fifo(lots, 25.0, NOW)
        assert consumed == pytest.approx(10.0)  # only what's available
        assert new_lots[0].amount_eur == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Sweep (idempotent)
# ---------------------------------------------------------------------------

class TestSweep:
    def test_removes_expired_and_empty(self):
        lots = [
            _lot("active", 20.0, 10, 300),
            _lot("expired", 15.0, 400, -1),
            _lot("empty", 0.0, 1, 300),
        ]
        swept = sweep_expired_topup_lots(lots, NOW)
        assert [lot.id for lot in swept] == ["active"]

    def test_idempotent(self):
        lots = [
            _lot("active", 20.0, 10, 300),
            _lot("expired", 15.0, 400, -1),
        ]
        once = sweep_expired_topup_lots(lots, NOW)
        twice = sweep_expired_topup_lots(once, NOW)
        assert [lot.id for lot in once] == [lot.id for lot in twice]
        assert [lot.amount_eur for lot in once] == [lot.amount_eur for lot in twice]


# ---------------------------------------------------------------------------
# deduct_budget: Monatstopf zuerst, dann TopUp FIFO
# ---------------------------------------------------------------------------

class TestDeductWithLots:
    def _budget(self, monthly_limit, monthly_used, lots) -> UserBudget:
        future = (NOW + timedelta(days=20)).isoformat()
        return UserBudget(
            user_id=str(uuid.uuid4()),
            monthly_budgets={
                "report-standard": MonthlyBudgetEntry(
                    limit_eur=monthly_limit, used_eur=monthly_used, reset_at=future
                )
            },
            top_up_lots=lots,
        )

    def test_monthly_first_then_topup(self):
        budget = self._budget(50.0, 48.0, [_lot("a", 30.0, 10, 300)])
        # remaining monthly = 2, cost 5 → 2 from monthly, 3 from topup
        result = deduct_budget(budget, "report-standard", 5.0, now=NOW)
        assert result.from_monthly == pytest.approx(2.0)
        assert result.from_top_up == pytest.approx(3.0)
        assert result.new_monthly_used == pytest.approx(50.0)
        assert result.new_top_up_balance_eur == pytest.approx(27.0)

    def test_topup_only_when_monthly_exhausted(self):
        budget = self._budget(50.0, 50.0, [_lot("a", 30.0, 10, 300)])
        result = deduct_budget(budget, "report-standard", 10.0, now=NOW)
        assert result.from_monthly == pytest.approx(0.0)
        assert result.from_top_up == pytest.approx(10.0)
        assert result.new_top_up_balance_eur == pytest.approx(20.0)

    def test_budget_exceeded_raises(self):
        budget = self._budget(50.0, 50.0, [_lot("a", 3.0, 10, 300)])
        with pytest.raises(ValueError, match="BUDGET_EXCEEDED"):
            deduct_budget(budget, "report-standard", 10.0, now=NOW)

    def test_expired_topup_does_not_cover(self):
        budget = self._budget(50.0, 50.0, [_lot("expired", 100.0, 400, -1)])
        with pytest.raises(ValueError, match="BUDGET_EXCEEDED"):
            deduct_budget(budget, "report-standard", 10.0, now=NOW)

    def test_check_sees_active_topup_only(self):
        budget = self._budget(50.0, 50.0, [
            _lot("active", 20.0, 10, 300),
            _lot("expired", 100.0, 400, -1),
        ])
        result = check_budget(budget, "report-standard", 15.0, now=NOW)
        assert result.allowed is True
        assert result.top_up_remaining_eur == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# Interval-Guard: Projekt-Plan darf NICHT in den Monatspfad
# ---------------------------------------------------------------------------

class TestIntervalGuard:
    def test_project_plan_rejected(self):
        from src.budget.routes import _require_month_interval
        with pytest.raises(ValueError, match="interval='project'"):
            _require_month_interval("energy-project")

    def test_month_plan_accepted(self):
        from src.budget.routes import _require_month_interval
        plan = _require_month_interval("report-standard")
        assert plan.interval == "month"

    def test_unknown_plan_raises(self):
        from src.budget.routes import _require_month_interval
        with pytest.raises(ValueError, match="Unknown plan"):
            _require_month_interval("does-not-exist")

    @pytest.mark.asyncio
    async def test_evaluate_budget_rejects_project_plan(self):
        from src.budget.routes import evaluate_budget
        with pytest.raises(ValueError, match="interval='project'"):
            await evaluate_budget(uuid.uuid4(), "energy-project", 1.0)

    @pytest.mark.asyncio
    async def test_apply_deduction_rejects_project_plan(self):
        from src.budget.routes import apply_budget_deduction
        with pytest.raises(ValueError, match="interval='project'"):
            await apply_budget_deduction(uuid.uuid4(), "energy-project", 1.0)


# ---------------------------------------------------------------------------
# Fail-loud: nicht-migrierter Legacy-Skalar
# ---------------------------------------------------------------------------

class TestLegacyFailLoud:
    @pytest.mark.asyncio
    async def test_nonzero_legacy_balance_raises(self):
        from src.budget.routes import _assert_no_legacy_topup_balance, LegacyTopUpBalanceError
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"balance_eur": 12.5})
        with pytest.raises(LegacyTopUpBalanceError, match="legacy scalar top-up balance"):
            await _assert_no_legacy_topup_balance(conn, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_zero_legacy_balance_ok(self):
        from src.budget.routes import _assert_no_legacy_topup_balance
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"balance_eur": 0.0})
        # Must not raise.
        await _assert_no_legacy_topup_balance(conn, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_no_legacy_row_ok(self):
        from src.budget.routes import _assert_no_legacy_topup_balance
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        await _assert_no_legacy_topup_balance(conn, uuid.uuid4())


# ---------------------------------------------------------------------------
# 12-Monate-Verfall
# ---------------------------------------------------------------------------

class TestTwelveMonthExpiry:
    def test_plus_12_months_basic(self):
        from src.budget.routes import _plus_12_months
        dt = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
        assert _plus_12_months(dt) == datetime(2027, 7, 5, 12, 0, tzinfo=timezone.utc)

    def test_plus_12_months_leap_day_clamped(self):
        from src.budget.routes import _plus_12_months
        # 2028-02-29 + 12 months → 2029-02-28 (2029 not a leap year)
        dt = datetime(2028, 2, 29, 9, 0, tzinfo=timezone.utc)
        assert _plus_12_months(dt) == datetime(2029, 2, 28, 9, 0, tzinfo=timezone.utc)
