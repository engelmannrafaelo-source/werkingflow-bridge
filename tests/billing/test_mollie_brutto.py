"""
Mollie zieht brutto ein (Rafael-Entscheid 17.10.2026, e-energy-mollie-brutto-20261017 = a).

Ohne Datenbank: die eine Rundungsstelle (invoice_amounts), der Steuersatz aus
derselben Quelle wie die Rechnung (_determine_tax_rate ueber resolve_invoice_tax)
und die Bahnen, die NICHT umgestellt sind (Abo, Top-Up) — deren Bestandsverhalten
ist hier festgehalten. Rechnungsbrutto == Mollie-Betrag gegen echten Postgres:
test_mollie_brutto_echte_db.py.
"""
from __future__ import annotations

import os
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")
os.environ.setdefault("BRIDGE_PUBLIC_URL", "https://test.bridge.local")

import pytest

from src.billing import billing_service
from src.billing.billing_service import invoice_amounts


# --------------------------------------------------------------------------
# Rundung: genau eine Stelle, kaufmaennisch auf Cent
# --------------------------------------------------------------------------

@pytest.mark.parametrize("netto,satz,steuer,brutto", [
    ("150", 20.0, "30.00", "180.00"),
    ("300", 20.0, "60.00", "360.00"),
    ("600", 20.0, "120.00", "720.00"),
    ("600", 0.0, "0.00", "600.00"),       # Reverse Charge / Export
    ("0.025", None, None, None),          # kein Centbetrag → laut
    ("0.13", 20.0, "0.03", "0.16"),       # 0.026 → 0.03
    ("0.125", None, None, None),
    ("0.38", 20.0, "0.08", "0.46"),       # 0.076 → 0.08
    ("2.50", 19.0, "0.48", "2.98"),       # 0.475 → 0.48 kaufmaennisch (Bankrundung gaebe 0.48 auch)
    ("0.50", 19.0, "0.10", "0.60"),       # 0.095 → 0.10 kaufmaennisch (Bankrundung: 0.10)
    ("1.50", 19.0, "0.29", "1.79"),       # 0.285 → 0.29 kaufmaennisch (Bankrundung: 0.28)
])
def test_eine_rundungsstelle_kaufmaennisch(netto, satz, steuer, brutto):
    if satz is None:
        with pytest.raises(ValueError, match="cent amount"):
            invoice_amounts(netto, 20.0)
        return
    sub, tax, total = invoice_amounts(netto, satz)
    assert (str(sub), str(tax), str(total)) == (str(Decimal(netto)), steuer, brutto)
    assert total == sub + tax  # Brutto wird nicht ein zweites Mal gerundet


def test_negativer_betrag_laut():
    with pytest.raises(ValueError):
        invoice_amounts("-1.00", 20.0)


# --------------------------------------------------------------------------
# Steuersatz: dieselbe Quelle wie die Rechnung, keine zweite Konstante
# --------------------------------------------------------------------------

def _pool_mit(adresse_row, vies_bestaetigt):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=adresse_row)
    pool = MagicMock()

    class _Ctx:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool.acquire = lambda: _Ctx()
    return pool, AsyncMock(return_value=vies_bestaetigt)


def _row(country, vat=None):
    return {"tenant_id": "t1", "billing_name": "X GmbH", "billing_street": "S 1",
            "billing_city": "C", "billing_postcode": "1", "billing_country": country,
            "billing_vat_id": vat}


@pytest.mark.asyncio
@pytest.mark.parametrize("row,vies,satz,hinweis", [
    (_row("AT"), False, 20.0, False),
    (_row("DE", "DE123456789"), True, 0.0, True),     # Reverse Charge nur mit VIES-Bestaetigung
    (_row("DE", "DE123456789"), False, 20.0, False),  # UID unbestaetigt → AT-Satz
    (_row("DE"), False, 20.0, False),                 # EU B2C (OSS offen) → AT-Satz, markiert
    (_row("CH"), False, 0.0, False),                  # Export
    (None, False, 20.0, False),                       # keine Adresse → AT-Satz
])
async def test_steuersatz_aus_der_rechnungslogik(row, vies, satz, hinweis):
    pool, vies_mock = _pool_mit(row, vies)
    with patch.object(billing_service, "get_pool", return_value=pool), patch.object(
        billing_service.vat_id_module, "has_confirmed_validation", vies_mock,
    ):
        info = await billing_service.resolve_invoice_tax(str(uuid.uuid4()))
    assert info["taxRate"] == satz
    assert (info["reverseChargeNote"] is not None) == hinweis
    # Exakt die Funktion der Rechnung, keine zweite Tabelle.
    assert info["taxRate"] == billing_service._determine_tax_rate(
        info["billingAddress"], vat_id_confirmed=vies,
    )[0]


@pytest.mark.asyncio
async def test_bestell_rechnung_und_angebot_nutzen_dieselbe_aufloesung():
    """pending_orders_service hat keine eigene Steuerlogik mehr."""
    import inspect
    from src.billing import pending_orders_service

    src = inspect.getsource(pending_orders_service._create_order_invoice)
    assert "resolve_invoice_tax" in src and "invoice_amounts" in src
    assert "_determine_tax_rate" not in inspect.getsource(pending_orders_service)
    assert "resolve_invoice_tax" in inspect.getsource(billing_service.price_quote)
    assert "invoice_amounts" in inspect.getsource(billing_service.auto_create_invoice)


# --------------------------------------------------------------------------
# Self-Service-Paket: Mollie-Betrag = total_eur der Bestell-Rechnung
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rechnung_netto_weicht_vom_katalog_ab_keine_zahlung():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"subtotal_eur": Decimal("299.00"), "total_eur": Decimal("358.80")})
    pool, _ = _pool_mit(None, False)
    pool.acquire = lambda: type("C", (), {
        "__aenter__": AsyncMock(return_value=conn), "__aexit__": AsyncMock(return_value=False),
    })()
    with patch.object(billing_service, "get_pool", return_value=pool):
        with pytest.raises(RuntimeError, match="no payment started"):
            await billing_service._order_invoice_gross(
                {"id": "o1", "invoiceId": str(uuid.uuid4())}, expected_net=300.0,
            )


# --------------------------------------------------------------------------
# Abo und Top-Up: NICHT umgestellt — Bestandsverhalten festgehalten
# --------------------------------------------------------------------------
# Beide Bahnen ziehen bei Mollie den NETTO-Betrag ein (Abo: plan.price × Sitze,
# Top-Up: der Guthabenbetrag) und schreiben die Rechnung danach mit USt. darauf.
# Abgebucht und Rechnung weichen also ab. Der Rafael-Entscheid betraf die
# Self-Service-Pakete (Energy-Arten, Check-Pakete); Abo und Top-Up brauchen
# einen eigenen Entscheid (Abo-Folgezahlungen laufen ueber eine feste
# Mollie-Subscription, Top-Up-Betraege sind Guthaben, nicht Preise). Diese Tests
# brechen, sobald jemand das aendert — dann bewusst.

@pytest.mark.asyncio
async def test_abo_zieht_bisher_netto_ein():
    mollie = MagicMock()
    mollie.create_first_payment = AsyncMock(return_value={"paymentId": "tr_a", "checkoutUrl": "https://x"})
    pool, _ = _pool_mit(None, False)
    with patch.object(billing_service, "get_pool", return_value=pool), patch.object(
        billing_service, "_assert_complete_billing_address", AsyncMock(),
    ), patch.object(billing_service.purchase_consent, "record_consent", AsyncMock()), patch.object(
        billing_service, "get_or_create_customer", AsyncMock(return_value={"mollieCustomerId": "c"}),
    ), patch.object(billing_service, "get_mollie_adapter", return_value=mollie), patch.object(
        billing_service, "_plan_price", return_value=250.0,
    ), patch.object(billing_service, "config", MagicMock(mollie_webhook_url="https://t/wh")), patch(
        "src.budget.plans.get_plan", return_value=MagicMock(app_id="werking-report"),
    ), patch.object(billing_service, "_abschluss_reservieren", AsyncMock(return_value=uuid.uuid4())), \
            patch.object(billing_service, "_abschluss_checkout_eintragen", AsyncMock()):
        await billing_service.start_subscription_checkout(
            str(uuid.uuid4()), "report-standard", 2, "https://x/ok", "u@x", "U", consent=MagicMock(),
        )
    assert mollie.create_first_payment.await_args.kwargs["amount_eur"] == 500.0  # netto


@pytest.mark.asyncio
async def test_top_up_zieht_bisher_den_guthabenbetrag_ein():
    mollie = MagicMock()
    mollie.create_one_time_payment = AsyncMock(return_value={"paymentId": "tr_t", "checkoutUrl": "https://x"})
    pool, _ = _pool_mit(None, False)
    with patch.object(billing_service, "get_pool", return_value=pool), patch.object(
        billing_service.purchase_consent, "record_consent", AsyncMock(),
    ), patch.object(
        billing_service, "get_or_create_customer", AsyncMock(return_value={"mollieCustomerId": "c"}),
    ), patch.object(billing_service, "get_mollie_adapter", return_value=mollie), patch.object(
        billing_service, "config", MagicMock(mollie_webhook_url="https://t/wh"),
    ):
        await billing_service.start_topup_checkout(
            str(uuid.uuid4()), 50.0, "https://x/ok", "u@x", "U", consent=MagicMock(),
        )
    assert mollie.create_one_time_payment.await_args.kwargs["amount_eur"] == 50.0
