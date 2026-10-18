"""Brutto-Nachbesserung (Codex-Pruefung 18.10.2026) — Proben OHNE Datenbank.

- Befund 1: jede Kauf-/Angebotsroute antwortet bei SteuerdatenNichtErmittelbar
  mit 503 statt mit einem Ersatz-Steuersatz weiterzumachen.
- Live-Mollie-Adapter: Payment-Objekte sind dicts; subscriptionId und
  Erstattungsbetraege kommen per Schluessel (getattr lieferte still None).
Die Echt-DB-Gegenstuecke stehen in test_brutto_nachbesserung_echte_db.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.parametrize("route", ["sub", "pack", "first_pack", "topup", "quote"])
async def test_routen_antworten_503(route):
    from fastapi import HTTPException

    from src.billing import billing_service, routes

    fehler = AsyncMock(side_effect=billing_service.SteuerdatenNichtErmittelbar("probe"))
    body = MagicMock(planId="p", seats=1, quantity=1, amountEur=50, successRedirect="https://x",
                     consent=None, userId=None, email=None, name=None)
    claims = MagicMock()
    ziel, aufruf = {
        "sub": ("start_subscription_checkout", lambda: routes.billing_sub_checkout(body, claims)),
        "pack": ("start_project_pack_checkout", lambda: routes.billing_project_pack_checkout(body, claims)),
        "first_pack": ("start_first_purchase_pack_checkout",
                       lambda: routes.billing_first_purchase_pack_checkout(body, claims)),
        "topup": ("start_topup_checkout", lambda: routes.billing_topup_checkout(body, claims)),
        "quote": ("price_quote", lambda: routes.billing_price_quote("u", "p", 1, claims)),
    }[route]
    with patch.object(routes, "_resolve_billing_identity", AsyncMock(return_value=("u", "e", "n"))), \
         patch.object(routes, "_assert_self_checkout_allowed", AsyncMock()), \
         patch.object(billing_service, ziel, fehler, create=False):
        with pytest.raises(HTTPException) as e:
            await aufruf()
    assert e.value.status_code == 503
    assert e.value.detail["error"] == "tax_data_unavailable"


# ---------------------------------------------------------------------------
# Live-Adapter: Mollie-Objekte sind dicts
# ---------------------------------------------------------------------------

async def test_live_adapter_liest_subscription_und_erstattung():
    from mollie.api.objects.payment import Payment

    from src.billing.mollie_adapter import LiveMollieAdapter

    daten = {
        "resource": "payment", "id": "tr_rec_1", "status": "paid",
        "amount": {"value": "60.00", "currency": "EUR"},
        "amountRefunded": {"value": "12.50", "currency": "EUR"},
        "customerId": "cst_1", "subscriptionId": "sub_1", "metadata": None,
    }
    adapter = LiveMollieAdapter.__new__(LiveMollieAdapter)
    adapter._client = MagicMock()
    adapter._client.payments.get = MagicMock(return_value=Payment(daten, MagicMock()))
    p = await adapter.get_payment("tr_rec_1")
    assert p["subscription_id"] == "sub_1"
    assert p["amount_eur"] == 60.0
    assert p["amount_refunded_eur"] == 12.5
    assert p["amount_charged_back_eur"] == 0.0
    # Gegenprobe: genau so lieferte der alte Zugriff immer None
    assert getattr(Payment(daten, MagicMock()), "subscriptionId", None) is None
