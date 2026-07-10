"""
Tests for src/invoices/routes.py

Coverage:
- _render_html          (pure — no DB, no HTTP)
- _human_date           (pure)
- Invoice number format via auto_create_invoice sequence logic
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

import pytest

from src.invoices.routes import _render_html, _human_date


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _inv(overrides: Dict[str, Any] = {}) -> Dict[str, Any]:
    """Build a minimal invoice dict matching the shape _render_html expects."""
    base: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "invoiceNumber": "INV-2026-00001",
        "userId": str(uuid.uuid4()),
        "tenantId": "tenant_test",
        "subscriptionId": None,
        "creditPurchaseId": None,
        "molliePaymentId": "pay_abc",
        "status": "paid",
        "subtotalEur": "250.00",
        "taxRate": "20.00",
        "taxEur": "50.00",
        "totalEur": "300.00",
        "currency": "EUR",
        "lineItems": [
            {
                "description": "WerkING Report Standard — 1 Sitz",
                "quantity": 1,
                "unitPriceEur": 250.0,
                "totalEur": 250.0,
            }
        ],
        "billingAddress": None,
        "issuedAt": "2026-01-15T10:00:00+00:00",
        "paidAt": "2026-01-15T10:05:00+00:00",
        "dueAt": None,
        "cancelledAt": None,
        "refundedAt": None,
        "sentAt": None,
        "notes": None,
        "metadata": {},
        "createdAt": "2026-01-15T10:00:00+00:00",
        "updatedAt": "2026-01-15T10:00:00+00:00",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _human_date
# ---------------------------------------------------------------------------

class TestHumanDate:
    def test_iso_datetime_formatted_as_german_date(self):
        assert _human_date("2026-01-15T10:00:00+00:00") == "15.01.2026"

    def test_none_returns_dash(self):
        assert _human_date(None) == "—"

    def test_empty_string_returns_dash(self):
        assert _human_date("") == "—"

    def test_z_suffix_handled(self):
        assert _human_date("2026-06-01T00:00:00Z") == "01.06.2026"


# ---------------------------------------------------------------------------
# _render_html — pure template rendering
# ---------------------------------------------------------------------------

class TestRenderHtml:
    def test_invoice_number_appears_in_output(self):
        html = _render_html(_inv())
        assert "INV-2026-00001" in html

    def test_paid_invoice_shows_paid_section(self):
        html = _render_html(_inv({"status": "paid"}))
        assert "Bezahlt" in html

    def test_draft_invoice_shows_payment_info(self):
        html = _render_html(_inv({"status": "draft", "paidAt": None}))
        # The paid-badge "✓ Bezahlt" only appears for status==paid.
        # "Bezahlt am" appears in the meta-table for all invoices — don't check that.
        assert "✓ Bezahlt" not in html
        assert "Zahlungsinformationen" in html

    def test_billing_address_rendered_when_present(self):
        inv = _inv({
            "billingAddress": {
                "name": "Musterfirma GmbH",
                "street": "Musterstraße 1",
                "city": "Wien",
                "postcode": "1010",
                "country": "AT",
                "vatId": None,
            }
        })
        html = _render_html(inv)
        assert "Musterfirma GmbH" in html
        assert "Musterstraße 1" in html
        assert "Wien" in html

    def test_missing_billing_address_shows_dash(self):
        inv = _inv({"billingAddress": None})
        html = _render_html(inv)
        # address block should contain the dash placeholder
        assert "—" in html

    def test_vat_id_shown_in_address(self):
        inv = _inv({
            "billingAddress": {
                "name": "Foreign GmbH",
                "street": "Hauptstraße 5",
                "city": "Berlin",
                "postcode": "10115",
                "country": "DE",
                "vatId": "DE123456789",
            }
        })
        html = _render_html(inv)
        assert "DE123456789" in html

    def test_reverse_charge_note_rendered(self):
        inv = _inv({
            "metadata": {
                "reverseChargeNote": (
                    "Steuerschuldnerschaft des Leistungsempfängers "
                    "gem. § 19 Abs. 1 UStG (Reverse Charge)"
                )
            }
        })
        html = _render_html(inv)
        assert "Reverse Charge" in html
        assert "reverse-charge" in html

    def test_notes_block_rendered(self):
        inv = _inv({"notes": "Bitte Zahlungsreferenz angeben."})
        html = _render_html(inv)
        assert "Bitte Zahlungsreferenz angeben." in html

    def test_no_notes_block_when_none(self):
        inv = _inv({"notes": None})
        html = _render_html(inv)
        assert "notes" not in html.lower() or "class='notes'" not in html

    def test_line_items_appear_in_table(self):
        html = _render_html(_inv())
        assert "WerkING Report Standard" in html

    def test_empty_line_items_shows_placeholder(self):
        inv = _inv({"lineItems": []})
        html = _render_html(inv)
        assert "Keine Positionen" in html

    def test_tax_totals_in_output(self):
        html = _render_html(_inv())
        assert "250.00" in html
        assert "300.00" in html

    def test_status_css_class_applied(self):
        """Status drives the CSS class on the status badge."""
        html_paid = _render_html(_inv({"status": "paid"}))
        assert "status-paid" in html_paid

        html_draft = _render_html(_inv({"status": "draft"}))
        assert "status-draft" in html_draft

        html_cancelled = _render_html(_inv({"status": "cancelled"}))
        assert "status-cancelled" in html_cancelled

    def test_issued_date_formatted(self):
        html = _render_html(_inv({"issuedAt": "2026-03-20T08:00:00+00:00"}))
        assert "20.03.2026" in html

    def test_html_is_valid_string_with_doctype(self):
        html = _render_html(_inv())
        assert html.startswith("<!doctype html>")
        assert "</html>" in html


# ---------------------------------------------------------------------------
# _approval_required — outbound-email gate toggle
# ---------------------------------------------------------------------------

import os as _os_test
from datetime import datetime as _dt, timezone as _tz

from src.invoices.routes import _approval_required, _row


class TestApprovalRequired:
    def test_default_is_on(self, monkeypatch):
        monkeypatch.delenv("INVOICE_REQUIRE_APPROVAL", raising=False)
        assert _approval_required() is True

    @pytest.mark.parametrize("val", ["false", "0", "no", "off", "FALSE", "Off", ""])
    def test_disabling_values(self, monkeypatch, val):
        monkeypatch.setenv("INVOICE_REQUIRE_APPROVAL", val)
        assert _approval_required() is False

    @pytest.mark.parametrize("val", ["true", "1", "yes", "on", "anything"])
    def test_enabling_values(self, monkeypatch, val):
        monkeypatch.setenv("INVOICE_REQUIRE_APPROVAL", val)
        assert _approval_required() is True


# ---------------------------------------------------------------------------
# _row — approval fields surfaced in the API shape
# ---------------------------------------------------------------------------

def _db_row(overrides=None):
    """Minimal DB-row (dict) matching every column _row() reads."""
    now = _dt(2026, 1, 15, 10, 0, tzinfo=_tz.utc)
    base = {
        "id": uuid.uuid4(),
        "invoice_number": "INV-2026-00042",
        "user_id": uuid.uuid4(),
        "tenant_id": "tenant_test",
        "subscription_id": None,
        "credit_purchase_id": None,
        "mollie_payment_id": None,
        "status": "issued",
        "subtotal_eur": "250.00",
        "tax_rate": "20.00",
        "tax_eur": "50.00",
        "total_eur": "300.00",
        "currency": "EUR",
        "line_items": [],
        "billing_address": None,
        "issued_at": now,
        "paid_at": None,
        "due_at": None,
        "cancelled_at": None,
        "refunded_at": None,
        "sent_at": None,
        "approved_at": None,
        "approved_by": None,
        "notes": None,
        "metadata": {},
        "created_at": now,
        "updated_at": now,
    }
    if overrides:
        base.update(overrides)
    return base


class TestRowApprovalFields:
    def test_unapproved_row_maps_to_nulls(self):
        out = _row(_db_row())
        assert out["approvedAt"] is None
        assert out["approvedBy"] is None

    def test_approved_row_maps_timestamp_and_actor(self):
        ts = _dt(2026, 2, 1, 9, 30, tzinfo=_tz.utc)
        out = _row(_db_row({"approved_at": ts, "approved_by": "rafael"}))
        assert out["approvedAt"] == ts.isoformat()
        assert out["approvedBy"] == "rafael"
