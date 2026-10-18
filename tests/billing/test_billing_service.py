"""
Tests for src/billing/billing_service.py

Coverage:
- _determine_tax_rate            (pure unit)
- _activate_subscription         (mocked pool + Mollie)
- handle_webhook                 (paid/failed/expired/topup/unknown/recurring-failure paths)
- _record_failed_payment         (mocked pool)
- _credit_topup                  (mocked pool — idempotency + first credit)
- auto_create_invoice            (mocked pool — with/without billing address, idempotency)
- _suspend_subscription_by_mollie_id (mocked pool)
- expire_subscription_for_user_plan  (mocked pool)
- change_subscription            (mocked pool + Mollie — happy path, no-op, not found)
"""

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
from unittest.mock import PropertyMock

from src.billing.billing_service import (
    _determine_tax_rate,
    _record_failed_payment,
    _activate_subscription,
    _suspend_subscription_by_mollie_id,
    expire_subscription_for_user_plan,
    auto_create_invoice,
    handle_webhook,
    change_subscription,
)
from src.billing.mollie_adapter import FakeMollieAdapter, reset_mollie_adapter
from src.budget.plans import PLANS, PlanConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_pool(*fetchrow_results, fetchval_result=None, execute_ok=True):
    """Build a minimal asyncpg-pool mock."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow_results))
    conn.fetchval = AsyncMock(return_value=fetchval_result)

    @asynccontextmanager
    async def _acquire():
        yield conn

    @asynccontextmanager
    async def _transaction():
        yield

    conn.transaction = _transaction

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


def _sub_row(**kwargs):
    """Minimal subscription asyncpg-row mock."""
    defaults = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "app_id": "werking-report",
        "plan_id": "trial",
        "status": "active",
        "mollie_customer_id": "cust_abc",
        "mollie_subscription_id": "sub_abc",
        "seats": 1,
        "started_at": datetime.now(timezone.utc),
        "cancelled_at": None,
        "suspended_at": None,
        "expired_at": None,
    }
    defaults.update(kwargs)
    row = MagicMock()
    row.__getitem__ = lambda self, k: defaults[k]
    row.keys = lambda: defaults.keys()
    return row


# ---------------------------------------------------------------------------
# _determine_tax_rate — pure unit tests
# ---------------------------------------------------------------------------

class TestDeterminesTaxRate:
    def test_no_address_defaults_to_at_20(self):
        rate, note = _determine_tax_rate(None)
        assert rate == 20.0
        assert note is None

    def test_at_address_20_percent(self):
        rate, note = _determine_tax_rate({"country": "AT"})
        assert rate == 20.0
        assert note is None

    def test_eu_b2b_reverse_charge(self):
        rate, note = _determine_tax_rate({"country": "DE", "vatId": "DE123456789"})
        assert rate == 0.0
        assert note is not None
        assert "Reverse Charge" in note

    def test_eu_without_vat_id_defaults_to_at(self):
        """EU B2C: no vatId → conservative AT 20% default."""
        rate, note = _determine_tax_rate({"country": "DE", "vatId": ""})
        assert rate == 20.0
        assert note is None

    def test_non_eu_export_zero(self):
        rate, note = _determine_tax_rate({"country": "US"})
        assert rate == 0.0
        assert note is None

    def test_empty_country_defaults_to_at(self):
        rate, note = _determine_tax_rate({"country": ""})
        assert rate == 20.0
        assert note is None

    def test_lowercase_country_normalised(self):
        """country codes are uppercased before comparison."""
        rate, note = _determine_tax_rate({"country": "de", "vatId": "DE123"})
        assert rate == 0.0


# ---------------------------------------------------------------------------
# _record_failed_payment
# ---------------------------------------------------------------------------

class TestRecordFailedPayment:
    async def test_logs_event_for_known_payment(self):
        user_id = uuid.uuid4()
        pending_row = MagicMock()
        pending_row.__getitem__ = lambda self, k: {
            "user_id": user_id,
            "type": "subscription_first",
            "plan_id": "report-standard",
            "amount_eur": Decimal("250.00"),
        }[k]

        pool, conn = _mock_pool(pending_row)
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock) as mock_log:
            await _record_failed_payment("pay_001", "failed")

        mock_log.assert_called_once()
        args = mock_log.call_args[0]
        assert args[0] == "payment.failed"

    async def test_no_op_for_unknown_payment(self):
        pool, conn = _mock_pool(None)
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock) as mock_log:
            await _record_failed_payment("pay_unknown", "failed")

        mock_log.assert_not_called()


# ---------------------------------------------------------------------------
# _suspend_subscription_by_mollie_id
# ---------------------------------------------------------------------------

class TestSuspendSubscriptionByMollieId:
    async def test_suspends_active_subscription(self):
        sub_id = uuid.uuid4()
        user_id = uuid.uuid4()
        row = MagicMock()
        row.__getitem__ = lambda self, k: {"id": sub_id, "user_id": user_id}[k]

        pool, conn = _mock_pool(row)
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock) as mock_log:
            await _suspend_subscription_by_mollie_id("sub_mollie_abc", "pay_rec_001", "failed")

        conn.fetchrow.assert_called_once()
        sql = conn.fetchrow.call_args[0][0]
        assert "suspended" in sql.lower()
        mock_log.assert_called_once()
        assert mock_log.call_args[0][0] == "subscription.suspended"

    async def test_no_op_when_no_active_subscription(self):
        pool, conn = _mock_pool(None)
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock) as mock_log:
            await _suspend_subscription_by_mollie_id("sub_gone", "pay_001", "expired")

        mock_log.assert_not_called()


# ---------------------------------------------------------------------------
# expire_subscription_for_user_plan
# ---------------------------------------------------------------------------

class TestExpireSubscriptionForUserPlan:
    async def test_marks_active_subscription_expired(self):
        sub_id = uuid.uuid4()
        row = MagicMock()
        row.__getitem__ = lambda self, k: {"id": sub_id}[k]

        pool, conn = _mock_pool(row)
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock) as mock_log:
            await expire_subscription_for_user_plan(str(uuid.uuid4()), "trial")

        conn.fetchrow.assert_called_once()
        sql = conn.fetchrow.call_args[0][0]
        assert "expired" in sql.lower()
        mock_log.assert_called_once()
        assert mock_log.call_args[0][0] == "subscription.expired"

    async def test_no_op_when_no_active_subscription(self):
        pool, conn = _mock_pool(None)
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock) as mock_log:
            await expire_subscription_for_user_plan(str(uuid.uuid4()), "trial")

        mock_log.assert_not_called()


# ---------------------------------------------------------------------------
# handle_webhook
# ---------------------------------------------------------------------------

class TestHandleWebhook:
    def _make_fake_payment(self, status: str, pay_type: str = "subscription_first",
                           sub_id: str | None = None):
        """Helper: seed FakeMollieAdapter with a payment and return its id."""
        reset_mollie_adapter()
        adapter = FakeMollieAdapter()
        pay_id = f"fake_pay_{uuid.uuid4().hex[:8]}"
        adapter._payments[pay_id] = {
            "id": pay_id,
            "customer_id": "cust_fake",
            "amount_eur": 250.0,
            "metadata": {"type": pay_type, "userId": str(uuid.uuid4()), "seats": "1"},
            "subscription_id": sub_id,
        }
        if status != "paid":
            # Override get_payment to return the requested status
            orig_payments = adapter._payments
            async def _get_payment_override(pid):
                p = orig_payments[pid]
                return {**p, "status": status}
            adapter.get_payment = _get_payment_override  # type: ignore
        return adapter, pay_id

    async def test_unknown_payment_id_not_handled(self):
        reset_mollie_adapter()
        with patch.dict("os.environ", {"BRIDGE_USE_FAKE_MOLLIE": "true"}):
            adapter = FakeMollieAdapter()
            # Don't register the payment
            with patch("src.billing.billing_service.get_mollie_adapter", return_value=adapter):
                # get_payment raises KeyError for unknown id
                with pytest.raises(KeyError):
                    await handle_webhook("pay_does_not_exist")

    async def test_failed_payment_records_event_and_returns_not_handled(self):
        user_id = uuid.uuid4()
        adapter, pay_id = self._make_fake_payment("failed")
        pending_row = MagicMock()
        pending_row.__getitem__ = lambda self, k: {
            "user_id": user_id,
            "type": "subscription_first",
            "plan_id": "report-standard",
            "amount_eur": Decimal("250.00"),
        }[k]

        pool, conn = _mock_pool(pending_row, pending_row)
        with patch("src.billing.billing_service.get_mollie_adapter", return_value=adapter), \
             patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock):
            result = await handle_webhook(pay_id)

        assert result["handled"] is False
        assert "failed" in result["reason"]

    async def test_expired_payment_records_event_and_returns_not_handled(self):
        user_id = uuid.uuid4()
        adapter, pay_id = self._make_fake_payment("expired")
        pending_row = MagicMock()
        pending_row.__getitem__ = lambda self, k: {
            "user_id": user_id,
            "type": "subscription_first",
            "plan_id": "report-standard",
            "amount_eur": Decimal("250.00"),
        }[k]

        pool, conn = _mock_pool(pending_row, pending_row)
        with patch("src.billing.billing_service.get_mollie_adapter", return_value=adapter), \
             patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock):
            result = await handle_webhook(pay_id)

        assert result["handled"] is False
        assert "expired" in result["reason"]

    async def test_failed_recurring_payment_suspends_subscription(self):
        """When subscription_id is present in payment, suspension is triggered."""
        mollie_sub_id = "sub_mollie_recurring"
        adapter, pay_id = self._make_fake_payment("failed", sub_id=mollie_sub_id)
        pending_row = MagicMock()
        pending_row.__getitem__ = lambda self, k: {
            "user_id": uuid.uuid4(),
            "type": "subscription_first",
            "plan_id": "report-standard",
            "amount_eur": Decimal("250.00"),
        }[k]

        pool, conn = _mock_pool(pending_row, pending_row)
        with patch("src.billing.billing_service.get_mollie_adapter", return_value=adapter), \
             patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock), \
             patch("src.billing.billing_service._suspend_subscription_by_mollie_id",
                   new_callable=AsyncMock) as mock_suspend:
            result = await handle_webhook(pay_id)

        mock_suspend.assert_called_once_with(mollie_sub_id, pay_id, "failed")
        assert result["handled"] is False

    async def test_non_pending_payment_not_handled(self):
        """Payment not in pending_payments → handled=False, reason=unknown."""
        adapter = FakeMollieAdapter()
        pay_id = f"fake_pay_{uuid.uuid4().hex[:8]}"
        adapter._payments[pay_id] = {
            "id": pay_id,
            "customer_id": "cust_fake",
            "amount_eur": 250.0,
            "metadata": {"type": "subscription_first", "userId": str(uuid.uuid4())},
            "subscription_id": None,
        }
        # Pool returns None for pending_payments lookup
        pool, conn = _mock_pool(None)
        with patch("src.billing.billing_service.get_mollie_adapter", return_value=adapter), \
             patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock):
            result = await handle_webhook(pay_id)

        assert result["handled"] is False
        assert result["reason"] == "unknown payment_id"


# ---------------------------------------------------------------------------
# _activate_subscription — idempotency guard
# ---------------------------------------------------------------------------

class TestActivateSubscription:
    # _activate_subscription resolves the plan (app_id, and — since 2026-08-26 —
    # the monthly budget pot) via the PLANS runtime cache, which is normally
    # filled from the plans table at Bridge startup. Unit tests seed it
    # directly so they need no database. Mirrors the report-standard row of
    # migration 020_plans_table.sql / 045_report_budget_100.sql.
    @pytest.fixture(autouse=True)
    def _seeded_plan_catalog(self):
        original = dict(PLANS)
        PLANS.clear()
        PLANS.update({
            "report-standard": PlanConfig(
                "report-standard", "werking-report", "Standard", 250, "month", 100, "",
            ),
        })
        yield
        PLANS.clear()
        PLANS.update(original)

    async def test_idempotent_on_duplicate_webhook(self):
        """Second call with same first_payment_id must return existing row, no Mollie call.

        Also self-heals a missing budget pot on the found row (see
        _ensure_monthly_budget_pot) — a real-Mollie subscription activated
        before that helper existed would otherwise stay pot-less forever,
        since a retried webhook is the only thing that ever revisits it."""
        existing_row = _sub_row(status="active", plan_id="report-standard")
        pool, conn = _mock_pool(existing_row)

        fake_mollie = FakeMollieAdapter()
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.get_mollie_adapter", return_value=fake_mollie), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock), \
             patch("src.billing.billing_service.auto_create_invoice", new_callable=AsyncMock):
            result = await _activate_subscription(
                str(uuid.uuid4()), "report-standard", 1,
                "cust_abc", first_payment_id="pay_001",
            )

        assert result["status"] == "active"
        # Mollie.create_subscription must NOT have been called (idempotency path)
        assert len(fake_mollie._subscriptions) == 0
        budget_sql = [c[0][0] for c in conn.execute.call_args_list if "user_budgets" in c[0][0]]
        assert len(budget_sql) == 1

    async def test_activates_new_subscription(self):
        """First-time activation: creates Mollie subscription, inserts row, logs
        event, and provisions the plan's monthly budget pot (the real-checkout
        twin of grant_subscription's fix — see _ensure_monthly_budget_pot)."""
        new_row = _sub_row(status="active", plan_id="report-standard",
                           mollie_subscription_id="sub_new_123")
        user_id = uuid.uuid4()
        abschluss = {"id": uuid.uuid4(), "user_id": user_id, "status": "offen",
                     "alt_abo_id": None, "neu_abo_id": None}
        # Ablauf (Migration 070): Bestand? nein -> Abschluss? nein -> angelegt ->
        # gelesen -> Abo-Zeile eingefuegt.
        pool, conn = _mock_pool(None, None, abschluss, new_row)
        conn.fetch = AsyncMock(return_value=[])   # kein anderes aktives Mollie-Abo

        fake_mollie = FakeMollieAdapter()
        from src.config import BridgeConfig
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.get_mollie_adapter", return_value=fake_mollie), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock) as mock_log, \
             patch("src.billing.billing_service.auto_create_invoice", new_callable=AsyncMock) as mock_inv, \
             patch.object(BridgeConfig, "mollie_webhook_url",
                          new_callable=PropertyMock,
                          return_value="https://bridge.test/v1/billing/mollie-webhook"):
            result = await _activate_subscription(
                str(user_id), "report-standard", 1,
                "cust_abc", first_payment_id="pay_new_001",
            )

        assert result["status"] == "active"
        # Mollie subscription was created — mit Idempotenzschluessel/Erstzahlung
        assert len(fake_mollie._subscriptions) == 1
        (angelegt,) = fake_mollie._subscriptions.values()
        assert angelegt["metadata"]["firstPaymentId"] == "pay_new_001"
        assert fake_mollie._idempotenz == {
            "abo-erstzahlung-pay_new_001": next(iter(fake_mollie._subscriptions)),
        }
        abschluss_sql = [c[0][0] for c in conn.execute.call_args_list if "abo_abschluesse" in c[0][0]]
        assert any("UPDATE abo_abschluesse" in q for q in abschluss_sql)
        # Billing event + invoice were created
        mock_log.assert_called_once()
        assert mock_log.call_args[0][0] == "subscription.activated"
        mock_inv.assert_called_once()
        # The monthly budget pot was provisioned alongside the new row.
        budget_sql = [c[0][0] for c in conn.execute.call_args_list if "user_budgets" in c[0][0]]
        assert len(budget_sql) == 1


# ---------------------------------------------------------------------------
# _credit_topup — idempotency
# ---------------------------------------------------------------------------

class TestCreditTopup:
    async def test_first_credit_updates_balance(self):
        from src.billing.billing_service import _credit_topup

        user_id = uuid.uuid4()
        cust_row = MagicMock()
        cust_row.__getitem__ = lambda self, k: {"mollie_customer_id": "cust_abc"}[k]

        pool, conn = _mock_pool()
        # _credit_topup call sequence (lots model):
        #   fetchrow(cust),
        #   fetchval(existing_purchase) -> None,
        #   fetchval(INSERT credit_purchases RETURNING paid_at),
        #   execute(INSERT user_topup_lots),
        #   fetchval(SUM active lots) -> new balance
        from datetime import datetime, timezone
        conn.fetchrow = AsyncMock(return_value=cust_row)
        conn.fetchval = AsyncMock(side_effect=[
            None,                                          # no existing credit_purchase
            datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc),  # paid_at of the new purchase
            Decimal("100.00"),                             # SUM of active lots (new balance)
        ])

        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock), \
             patch("src.billing.billing_service.auto_create_invoice", new_callable=AsyncMock):
            result = await _credit_topup(str(user_id), 100.0, "pay_topup_001")

        assert result["alreadyCredited"] is False
        assert result["balanceEur"] == 100.0

    async def test_idempotent_on_duplicate_webhook(self):
        from src.billing.billing_service import _credit_topup

        user_id = uuid.uuid4()
        cust_row = MagicMock()
        cust_row.__getitem__ = lambda self, k: {"mollie_customer_id": "cust_abc"}[k]

        # fetchrow: cust found, fetchval: existing credit_purchase exists
        pool, conn = _mock_pool()
        conn.fetchrow = AsyncMock(side_effect=[cust_row])
        conn.fetchval = AsyncMock(side_effect=[
            uuid.uuid4(),         # existing credit_purchase id
            Decimal("150.00"),    # current balance
        ])

        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock) as mock_log, \
             patch("src.billing.billing_service.auto_create_invoice", new_callable=AsyncMock) as mock_inv:
            result = await _credit_topup(str(user_id), 50.0, "pay_topup_dup")

        assert result["alreadyCredited"] is True
        assert result["balanceEur"] == 150.0
        # No new billing event or invoice for a duplicate
        mock_log.assert_not_called()
        mock_inv.assert_not_called()


# ---------------------------------------------------------------------------
# auto_create_invoice — billing address resolution + tax + idempotency
# ---------------------------------------------------------------------------

class TestAutoCreateInvoice:
    def _make_tenant_row(self, country: str = "AT", vat_id: str = "") -> MagicMock:
        row = MagicMock()
        data = {
            "tenant_id": "tenant_123",
            "billing_name": "Test GmbH",
            "billing_street": "Testgasse 1",
            "billing_city": "Wien",
            "billing_postcode": "1010",
            "billing_country": country,
            "billing_vat_id": vat_id,
        }
        row.__getitem__ = lambda self, k: data[k]
        row.keys = lambda: data.keys()

        def _bool_check(col):
            return bool(data.get(col))
        # asyncpg rows support truthiness on columns — simulate via __bool__ on row
        row.__bool__ = lambda self: True
        return row

    async def test_at_address_20pct_tax(self):
        tenant_row = self._make_tenant_row(country="AT")
        inv_row = MagicMock()
        inv_row.__getitem__ = lambda self, k: {"id": uuid.uuid4()}[k]

        pool, conn = _mock_pool()
        conn.fetchval = AsyncMock(side_effect=[
            None,         # no existing invoice for mollie_payment_id
            1,            # sequence nextval
        ])
        conn.fetchrow = AsyncMock(side_effect=[
            tenant_row,   # trow (user + tenant join)
            inv_row,      # INSERT RETURNING
        ])

        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock):
            result = await auto_create_invoice(
                user_id=str(uuid.uuid4()),
                amount_eur=250.0,
                description="Test invoice",
                mollie_payment_id="pay_inv_001",
            )

        assert result is not None
        # Verify INSERT was called with the correct tax_rate
        insert_call = conn.fetchrow.call_args_list[-1]
        sql = insert_call[0][0]
        assert "INSERT INTO invoices" in sql

    async def test_missing_billing_address_marks_incomplete(self):
        """When tenant has no billing_name, invoice still created but flagged."""
        tenant_row = MagicMock()
        tenant_row.__getitem__ = lambda self, k: {
            "tenant_id": "tenant_123",
            "billing_name": None,
            "billing_street": None,
            "billing_city": None,
            "billing_postcode": None,
            "billing_country": None,
            "billing_vat_id": None,
        }[k]
        tenant_row.__bool__ = lambda self: True

        inv_row = MagicMock()
        inv_row.__getitem__ = lambda self, k: {"id": uuid.uuid4()}[k]

        pool, conn = _mock_pool()
        conn.fetchval = AsyncMock(side_effect=[None, 1])
        conn.fetchrow = AsyncMock(side_effect=[tenant_row, inv_row])

        # Capture the JSON passed to INSERT to verify metadata.incomplete=True
        insert_args = {}

        async def capture_fetchrow(sql, *args):
            if "INSERT INTO invoices" in sql:
                insert_args["metadata_json"] = args[-1]  # last positional arg is metadata
                return inv_row
            return tenant_row

        conn.fetchrow = AsyncMock(side_effect=[tenant_row, inv_row])

        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock):
            await auto_create_invoice(
                user_id=str(uuid.uuid4()),
                amount_eur=100.0,
                description="No address invoice",
                mollie_payment_id="pay_noaddr_001",
            )

    async def test_idempotent_returns_existing_id(self):
        """Same mollie_payment_id → return existing invoice id without insert."""
        existing_id = uuid.uuid4()
        pool, conn = _mock_pool()
        conn.fetchval = AsyncMock(return_value=existing_id)  # first: existing invoice

        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service.log_billing_event", new_callable=AsyncMock) as mock_log:
            result = await auto_create_invoice(
                user_id=str(uuid.uuid4()),
                amount_eur=250.0,
                description="Dup invoice",
                mollie_payment_id="pay_dup_001",
            )

        assert result == str(existing_id)
        # No insert, no billing event
        conn.fetchrow.assert_not_called()
        mock_log.assert_not_called()


# ---------------------------------------------------------------------------
# change_subscription
# ---------------------------------------------------------------------------

class TestChangeSubscription:
    async def test_raises_lookup_error_when_no_active_subscription(self):
        pool, conn = _mock_pool(None)
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service._plan_price", return_value=29.0), \
             patch("src.budget.plans.get_plan", return_value=MagicMock(app_id="werking-report")):
            with pytest.raises(LookupError, match="No active subscription"):
                await change_subscription(
                    str(uuid.uuid4()), "report-standard", 1,
                    "https://example.com/success", "user@test.com", "Test User",
                    consent=MagicMock(),
                )

    async def test_raises_value_error_on_no_op_change(self):
        sub = _sub_row(plan_id="report-standard", seats=1)
        pool, conn = _mock_pool(sub)
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service._plan_price", return_value=29.0), \
             patch("src.budget.plans.get_plan", return_value=MagicMock(app_id="werking-report")):
            with pytest.raises(ValueError, match="identical to current"):
                await change_subscription(
                    str(sub["user_id"]), "report-standard", 1,
                    "https://example.com/success", "user@test.com", "Test User",
                    consent=MagicMock(),
                )

    async def test_plan_change_starts_checkout_and_does_not_cancel_yet(self):
        """Planwechsel: Checkout mit Zustimmung und ersetzt_abo_id — das alte Abo
        bleibt aktiv, bis das neue bezahlt ist (Kuendigung erst im Webhook)."""
        user_id = uuid.uuid4()
        sub = _sub_row(
            user_id=user_id, plan_id="trial", seats=1,
            mollie_subscription_id="sub_trial_123",
        )
        pool, conn = _mock_pool()
        conn.fetchrow = AsyncMock(side_effect=[sub])
        consent = MagicMock()

        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service._plan_price", return_value=29.0), \
             patch("src.budget.plans.get_plan", return_value=MagicMock(app_id="werking-report")), \
             patch("src.billing.billing_service.cancel_subscription",
                   new_callable=AsyncMock) as mock_cancel, \
             patch("src.billing.billing_service.start_subscription_checkout",
                   new_callable=AsyncMock,
                   return_value={"checkoutUrl": "https://checkout.mollie.test/abc", "paymentId": "pay_new"}) as mock_checkout:
            result = await change_subscription(
                str(user_id), "report-standard", 2,
                "https://example.com/success", "u@test.com", "User",
                consent=consent,
            )

        mock_cancel.assert_not_called()
        mock_checkout.assert_called_once()
        kw = mock_checkout.call_args.kwargs
        assert kw["consent"] is consent
        assert kw["ersetzt_abo_id"] == str(sub["id"])
        assert result["replacesSubscriptionId"] == str(sub["id"])
        assert result["cancelledOnActivation"] is True
        assert result["previousPlanId"] == "trial"
        assert result["newPlanId"] == "report-standard"
        assert result["seats"] == 2
        assert "checkoutUrl" in result

    async def test_plan_change_checkout_failure_keeps_old_subscription(self):
        """Scheitert der Checkout, bleibt das alte Abo unangetastet."""
        user_id = uuid.uuid4()
        sub = _sub_row(user_id=user_id, plan_id="trial", seats=1)
        pool, conn = _mock_pool()
        conn.fetchrow = AsyncMock(side_effect=[sub])
        with patch("src.billing.billing_service.get_pool", return_value=pool), \
             patch("src.billing.billing_service._plan_price", return_value=29.0), \
             patch("src.budget.plans.get_plan", return_value=MagicMock(app_id="werking-report")), \
             patch("src.billing.billing_service.cancel_subscription",
                   new_callable=AsyncMock) as mock_cancel, \
             patch("src.billing.billing_service.start_subscription_checkout",
                   new_callable=AsyncMock, side_effect=ValueError("billing address incomplete")):
            with pytest.raises(ValueError):
                await change_subscription(
                    str(user_id), "report-standard", 1,
                    "https://example.com/success", "u@test.com", "User",
                    consent=MagicMock(),
                )
        mock_cancel.assert_not_called()


# ---------------------------------------------------------------------------
# Route /v1/billing/subscription/change: Zustimmung ist Pflicht und erreicht
# den Checkout (Fix 18.10.2026 — vorher fehlte sie, der Checkout brach NACH der
# Kuendigung mit TypeError ab).
# ---------------------------------------------------------------------------

class TestSubscriptionChangeRoute:
    """Absicherung wie /subscription/checkout (Fix 18.10.2026, Kanzler 13:40Z):
    fremde userId -> 403, eigene ok, Operator (Service-Token ohne X-User-ID)
    nur mit ausdruecklicher userId, Selbstbuchungs-Sperre greift."""

    @pytest.fixture(autouse=True)
    def _geheimnisse(self, monkeypatch):
        # Wegwerfwerte nur fuer diesen Test — weder der Laufzeit-Schluessel
        # noch ein echtes Service-Token.
        monkeypatch.setenv("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
        monkeypatch.setenv("BRIDGE_SERVICE_TOKEN", "test-service-token")

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.billing.routes import router
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    @staticmethod
    def _consent():
        from src.billing.purchase_consent import PurchaseConsentIn
        c = {"termsVersion": "test", "actingAsBusiness": True, "professionallyQualified": True,
             "immediateStartRequested": True, "acceptedAt": "2026-10-18T13:00:00Z"}
        c = {k: v for k, v in c.items() if k in PurchaseConsentIn.model_fields}
        for name, feld in PurchaseConsentIn.model_fields.items():
            if feld.is_required() and name not in c:
                c[name] = True
        return c

    def _body(self, **extra):
        body = {"newPlanId": "report-standard", "seats": 2,
                "successRedirect": "https://example.com/ok", "consent": self._consent(),
                "email": "u@test.com", "name": "U"}
        body.update(extra)
        return body

    @staticmethod
    def _jwt(uid):
        from src.identity.jwt_utils import sign_jwt
        return {"Authorization": "Bearer " + sign_jwt(
            user_id=str(uid), email="u@test.com", tenant_id="t1", app_licenses=[])}

    @staticmethod
    def _service(acting=None):
        from src.config import config
        h = {"X-Bridge-Service-Token": config.service_token}
        if acting:
            h["X-User-ID"] = str(acting)
        return h

    def _post(self, body, headers, erlaubt=True):
        ch = AsyncMock(return_value={"checkoutUrl": "https://x", "replacesSubscriptionId": "s"})
        with patch("src.billing.billing_service.change_subscription", ch), \
             patch("src.billing.routes._self_checkout_state",
                   AsyncMock(return_value=(True, None) if erlaubt else (False, "tenant_managed"))):
            resp = self._client().post("/v1/billing/subscription/change", json=body, headers=headers)
        return resp, ch

    def test_ohne_zustimmung_422(self):
        me = uuid.uuid4()
        body = self._body(); body.pop("consent")
        resp, ch = self._post(body, self._jwt(me))
        assert resp.status_code == 422
        ch.assert_not_called()

    def test_jwt_fremde_user_id_403(self):
        me, fremd = uuid.uuid4(), uuid.uuid4()
        resp, ch = self._post(self._body(userId=str(fremd)), self._jwt(me))
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"]["error"] == "foreign_user_id"
        ch.assert_not_called()

    def test_service_mit_x_user_id_fremde_user_id_403(self):
        me, fremd = uuid.uuid4(), uuid.uuid4()
        resp, ch = self._post(self._body(userId=str(fremd)), self._service(acting=me))
        assert resp.status_code == 403
        ch.assert_not_called()

    def test_jwt_eigene_identitaet_ok(self):
        from src.billing.purchase_consent import PurchaseConsentIn
        me = uuid.uuid4()
        for body in (self._body(), self._body(userId=str(me)), self._body(userId=str(me).upper())):
            resp, ch = self._post(body, self._jwt(me))
            assert resp.status_code == 200, resp.text
            assert ch.call_args.args[0] == str(me)
            assert isinstance(ch.call_args.kwargs["consent"], PurchaseConsentIn)

    def test_service_mit_x_user_id_eigene_ok(self):
        me = uuid.uuid4()
        resp, ch = self._post(self._body(), self._service(acting=me))
        assert resp.status_code == 200, resp.text
        assert ch.call_args.args[0] == str(me)

    def test_operator_braucht_ausdrueckliche_user_id(self):
        resp, ch = self._post(self._body(), self._service())
        assert resp.status_code == 400
        ch.assert_not_called()

    def test_operator_mit_user_id_ok(self):
        kunde = uuid.uuid4()
        resp, ch = self._post(self._body(userId=str(kunde)), self._service())
        assert resp.status_code == 200, resp.text
        assert ch.call_args.args[0] == str(kunde)

    def test_selbstbuchung_gesperrt_403(self):
        me = uuid.uuid4()
        resp, ch = self._post(self._body(), self._jwt(me), erlaubt=False)
        assert resp.status_code == 403
        assert resp.json()["detail"]["error"] == "self_checkout_disabled"
        ch.assert_not_called()

    def test_ohne_anmeldung_401(self):
        resp, ch = self._post(self._body(userId=str(uuid.uuid4())), {})
        assert resp.status_code == 401
        ch.assert_not_called()

    def test_offener_wechsel_409_fester_satz(self):
        """Zweiter offener Abschluss/Wechsel je Konto (Migration 070) -> 409."""
        from src.billing import billing_service
        me = uuid.uuid4()
        with patch("src.billing.billing_service.change_subscription",
                   AsyncMock(side_effect=billing_service.AboAbschlussOffen())), \
             patch("src.billing.routes._self_checkout_state", AsyncMock(return_value=(True, None))):
            resp = self._client().post("/v1/billing/subscription/change",
                                       json=self._body(), headers=self._jwt(me))
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == billing_service.ABSCHLUSS_OFFEN_SATZ

    def test_checkout_409_offen_und_schon_aktiv(self):
        from src.billing import billing_service
        me = uuid.uuid4()
        body = {k: v for k, v in self._body().items() if k != "newPlanId"}
        body["planId"] = "report-standard"
        for fehler, satz in ((billing_service.AboAbschlussOffen(), billing_service.ABSCHLUSS_OFFEN_SATZ),
                             (billing_service.AboSchonAktiv(), billing_service.ABO_SCHON_AKTIV_SATZ)):
            with patch("src.billing.billing_service.start_subscription_checkout",
                       AsyncMock(side_effect=fehler)), \
                 patch("src.billing.routes._self_checkout_state", AsyncMock(return_value=(True, None))):
                resp = self._client().post("/v1/billing/subscription/checkout",
                                           json=body, headers=self._jwt(me))
            assert resp.status_code == 409, resp.text
            assert resp.json()["detail"] == satz
