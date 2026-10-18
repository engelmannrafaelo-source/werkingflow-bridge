"""
Mollie zieht brutto ein (Rafael-Entscheid 17.10.2026, e-energy-mollie-brutto-20261017 = a).

Ohne Datenbank: die eine Rundungsstelle (invoice_amounts), der Steuersatz aus
derselben Quelle wie die Rechnung (_determine_tax_rate ueber resolve_invoice_tax)
und Abo/Top-Up: neue Abschluesse brutto, Bestand netto (Rafael-Entscheid
18.10.2026, e-bridge-abos-brutto = a). Rechnungsbrutto == Mollie-Betrag gegen
echten Postgres: test_mollie_brutto_echte_db.py, test_abo_topup_brutto_echte_db.py.
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
# Abo und Top-Up: neue Abschluesse brutto, Bestand netto
# (Rafael-Entscheid 18.10.2026, e-bridge-abos-brutto = a)
# --------------------------------------------------------------------------
# Die Basis wird am Checkout festgelegt und mit dem Abschluss gespeichert
# (Migration 068). Diese Tests brechen, sobald jemand das aendert — dann bewusst.

def _steuer(satz):
    return AsyncMock(return_value={"taxRate": satz, "tenantId": "t1", "billingAddress": {"country": "AT"},
                                   "reverseChargeNote": None, "euB2c": False})


def _checkout_patches(pool, mollie, satz=20.0):
    from contextlib import ExitStack
    st = ExitStack()
    st.enter_context(patch.object(billing_service, "get_pool", return_value=pool))
    st.enter_context(patch.object(billing_service, "_assert_complete_billing_address", AsyncMock()))
    consent = st.enter_context(patch.object(billing_service.purchase_consent, "record_consent", AsyncMock()))
    st.enter_context(patch.object(billing_service, "get_or_create_customer",
                                  AsyncMock(return_value={"mollieCustomerId": "c"})))
    st.enter_context(patch.object(billing_service, "get_mollie_adapter", return_value=mollie))
    st.enter_context(patch.object(billing_service, "_plan_price", return_value=250.0))
    st.enter_context(patch.object(billing_service, "config", MagicMock(mollie_webhook_url="https://t/wh")))
    st.enter_context(patch.object(billing_service, "resolve_invoice_tax", _steuer(satz)))
    return st, consent


def _pool_mit_conn():
    conn = AsyncMock()
    pool = MagicMock()

    class _Ctx:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *a):
            return False

    pool.acquire = lambda: _Ctx()
    return pool, conn


def test_neuer_abschluss_ist_brutto():
    assert billing_service.BASIS_NEUER_ABSCHLUSS == billing_service.BASIS_BRUTTO


@pytest.mark.asyncio
@pytest.mark.parametrize("satz,brutto", [(20.0, Decimal("600.00")), (0.0, Decimal("500.00"))])
async def test_abo_neuer_abschluss_zieht_brutto_ein(satz, brutto):
    mollie = MagicMock()
    mollie.create_first_payment = AsyncMock(return_value={"paymentId": "tr_a", "checkoutUrl": "https://x"})
    pool, conn = _pool_mit_conn()
    st, consent = _checkout_patches(pool, mollie, satz)
    with st:
        await billing_service.start_subscription_checkout(
            str(uuid.uuid4()), "report-standard", 2, "https://x/ok", "u@x", "U", consent=MagicMock(),
        )
    kw = mollie.create_first_payment.await_args.kwargs
    assert Decimal(str(kw["amount_eur"])) == brutto
    assert kw["metadata"]["betragsbasis"] == "gross"
    assert Decimal(str(consent.await_args.kwargs["amount_eur"])) == brutto   # Beleg == Abbuchung
    args = conn.execute.await_args.args                                      # pending_payments
    assert args[4] == brutto and args[5] == "gross" and args[6] == Decimal("500.00")


@pytest.mark.asyncio
async def test_abo_mit_netto_basis_bleibt_bestandsverhalten():
    """Nur ueber change_subscription mit PLANWECHSEL_IST_NEUER_ABSCHLUSS=False erreichbar."""
    mollie = MagicMock()
    mollie.create_first_payment = AsyncMock(return_value={"paymentId": "tr_a", "checkoutUrl": "https://x"})
    pool, conn = _pool_mit_conn()
    st, _ = _checkout_patches(pool, mollie)
    with st:
        await billing_service.start_subscription_checkout(
            str(uuid.uuid4()), "report-standard", 2, "https://x/ok", "u@x", "U", consent=MagicMock(),
            betragsbasis="net",
        )
    assert mollie.create_first_payment.await_args.kwargs["amount_eur"] == 500.0
    assert conn.execute.await_args.args[5] == "net"


@pytest.mark.asyncio
async def test_unbekannte_basis_laut():
    with pytest.raises(ValueError, match="amount basis"):
        await billing_service.start_subscription_checkout(
            str(uuid.uuid4()), "report-standard", 1, "https://x/ok", "u@x", "U", consent=MagicMock(),
            betragsbasis="brutto",
        )


@pytest.mark.asyncio
async def test_top_up_neuer_abschluss_zieht_brutto_ein_und_merkt_das_guthaben():
    mollie = MagicMock()
    mollie.create_one_time_payment = AsyncMock(return_value={"paymentId": "tr_t", "checkoutUrl": "https://x"})
    pool, conn = _pool_mit_conn()
    st, consent = _checkout_patches(pool, mollie)
    with st:
        await billing_service.start_topup_checkout(
            str(uuid.uuid4()), 50.0, "https://x/ok", "u@x", "U", consent=MagicMock(),
        )
    kw = mollie.create_one_time_payment.await_args.kwargs
    assert kw["amount_eur"] == 60.0 and kw["metadata"]["betragsbasis"] == "gross"
    assert consent.await_args.kwargs["amount_eur"] == 60.0
    args = conn.execute.await_args.args
    assert args[3] == Decimal("60.00") and args[4] == "gross" and args[5] == Decimal("50.00")


@pytest.mark.asyncio
@pytest.mark.parametrize("schalter,alte_basis,erwartet", [
    (True, "net", None),      # Planwechsel = neuer Abschluss → Standard (brutto)
    (True, "gross", None),
    (False, "net", "net"),    # Planwechsel erbt die Basis des alten Abos
    (False, "gross", "gross"),
])
async def test_planwechsel_folgt_einer_zeile(schalter, alte_basis, erwartet):
    pool, conn = _pool_mit_conn()
    conn.fetchrow = AsyncMock(return_value={
        "id": uuid.uuid4(), "plan_id": "report-standard", "seats": 1,
        "mollie_customer_id": "c", "mollie_subscription_id": "s", "amount_basis": alte_basis,
    })
    checkout = AsyncMock(return_value={"checkoutUrl": "https://x", "paymentId": "tr_n"})
    with patch.object(billing_service, "get_pool", return_value=pool), patch.object(
        billing_service, "PLANWECHSEL_IST_NEUER_ABSCHLUSS", schalter,
    ), patch.object(billing_service, "_plan_price", return_value=250.0), patch.object(
        billing_service, "cancel_subscription", AsyncMock(),
    ), patch.object(billing_service, "start_subscription_checkout", checkout):
        await billing_service.change_subscription(
            str(uuid.uuid4()), "report-pro", 2, "https://x/ok", "u@x", "U",
        )
    assert checkout.await_args.kwargs["betragsbasis"] == erwartet


def _webhook_patches(pending, paid):
    pool, conn = _pool_mit_conn()
    conn.fetchrow = AsyncMock(return_value=pending)
    mollie = MagicMock()
    mollie.get_payment = AsyncMock(return_value={"status": "paid", "amount_eur": paid, "metadata": {"seats": "2"},
                                                 "customer_id": "c"})
    return pool, mollie


@pytest.mark.asyncio
@pytest.mark.parametrize("basis,amount,net,guthaben", [
    ("gross", Decimal("60.00"), Decimal("50.00"), 50.0),   # neuer Abschluss: Guthaben = Netto
    ("net", Decimal("50.00"), None, 50.0),                  # Bestand: Guthaben = eingezogener Betrag
])
async def test_webhook_top_up_schreibt_immer_das_netto_gut(basis, amount, net, guthaben):
    pending = {"user_id": uuid.uuid4(), "type": "topup", "plan_id": None, "amount_eur": amount,
               "amount_basis": basis, "net_eur": net}
    pool, mollie = _webhook_patches(pending, str(amount))
    gut = AsyncMock(return_value={"alreadyCredited": False, "balanceEur": guthaben})
    with patch.object(billing_service, "get_pool", return_value=pool), patch.object(
        billing_service, "get_mollie_adapter", return_value=mollie,
    ), patch.object(billing_service, "_credit_topup", gut):
        await billing_service.handle_webhook("tr_t")
    assert gut.await_args.args[1] == guthaben
    assert gut.await_args.kwargs["betragsbasis"] == basis


@pytest.mark.asyncio
@pytest.mark.parametrize("basis,amount,net", [
    ("gross", Decimal("600.00"), Decimal("500.00")),
    ("net", Decimal("500.00"), None),
])
async def test_webhook_abo_reicht_basis_und_betraege_weiter(basis, amount, net):
    pending = {"user_id": uuid.uuid4(), "type": "subscription_first", "plan_id": "report-standard",
               "amount_eur": amount, "amount_basis": basis, "net_eur": net}
    pool, mollie = _webhook_patches(pending, str(amount))
    akt = AsyncMock(return_value={"id": "s"})
    with patch.object(billing_service, "get_pool", return_value=pool), patch.object(
        billing_service, "get_mollie_adapter", return_value=mollie,
    ), patch.object(billing_service, "_activate_subscription", akt):
        await billing_service.handle_webhook("tr_a")
    kw = akt.await_args.kwargs
    assert (kw["betragsbasis"], kw["abo_betrag_eur"], kw["netto_eur"]) == (basis, amount, net)


@pytest.mark.asyncio
async def test_brutto_abo_ohne_netto_legt_kein_abo_an():
    mollie = MagicMock()
    mollie.create_subscription = AsyncMock()
    pool, conn = _pool_mit_conn()
    conn.fetchrow = AsyncMock(return_value=None)
    plan = MagicMock(app_id="werking-report")
    with patch.object(billing_service, "get_pool", return_value=pool), patch.object(
        billing_service, "get_mollie_adapter", return_value=mollie,
    ), patch("src.budget.plans.get_plan", return_value=plan):
        with pytest.raises(RuntimeError, match="no subscription created"):
            await billing_service._activate_subscription(
                str(uuid.uuid4()), "report-standard", 1, "c", "tr_a",
                betragsbasis="gross", abo_betrag_eur=Decimal("300.00"), netto_eur=None,
            )
    mollie.create_subscription.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bezahlt,gemeldet", [("600.00", False), ("500.00", True), (None, True)])
async def test_rechnung_gegen_zahlung(bezahlt, gemeldet):
    pool, conn = _pool_mit_conn()
    conn.fetchval = AsyncMock(return_value=Decimal("600.00"))   # Rechnungssumme
    ev = AsyncMock(return_value=True)
    with patch.object(billing_service, "get_pool", return_value=pool), patch.object(
        billing_service, "log_billing_event", ev,
    ):
        await billing_service._pruefe_rechnung_gegen_zahlung(
            invoice_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()), payment_id="tr_a",
            paid_eur=bezahlt, bahn="subscription_first",
        )
    assert (ev.await_count == 1) == gemeldet
    if gemeldet:
        assert ev.await_args.args[0] == "payment.amount_mismatch"
        # Einmaligkeit liegt in der Datenbank (Migration 071), nicht in einem SELECT davor
        assert ev.await_args.kwargs["ereignis_schluessel"] == "payment.amount_mismatch:tr_a"


@pytest.mark.asyncio
async def test_rechnung_gegen_zahlung_wiederholung_nur_warnung(caplog):
    """Ereignis schon vorhanden (ON CONFLICT -> False): kein zweiter Fehler-Alarm."""
    import logging
    pool, conn = _pool_mit_conn()
    conn.fetchval = AsyncMock(return_value=Decimal("600.00"))
    caplog.set_level(logging.WARNING)
    with patch.object(billing_service, "get_pool", return_value=pool), patch.object(
        billing_service, "log_billing_event", AsyncMock(return_value=False),
    ):
        await billing_service._pruefe_rechnung_gegen_zahlung(
            invoice_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()), payment_id="tr_a",
            paid_eur="500.00", bahn="subscription_first",
        )
    stufen = [r.levelno for r in caplog.records if "tr_a" in r.getMessage()]
    assert stufen == [logging.WARNING]
