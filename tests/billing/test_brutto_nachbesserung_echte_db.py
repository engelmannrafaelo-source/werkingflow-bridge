"""Brutto-Nachbesserung (Codex-Pruefung 18.10.2026, Befunde 1, 2, 4, 5) — gegen
einen ECHTEN, voll migrierten Postgres.

1. Steuerdaten technisch nicht lesbar (Adresse ODER VIES-Bestaetigung) -> kein
   Checkout, keine Mollie-Zahlung, Alarm; die Route antwortet 503. Eine
   nachweislich UNbestaetigte UID ist ein Sachverhalt -> 20 %.
2. Abo-Folgezahlung bekommt eine Rechnung (gross: vom gespeicherten Netto,
   Rechnungsbrutto == Abbuchung; net: Bestandsverhalten), idempotent je Zahlung.
4. Scheitert die Rechnung beim ersten Webhook (Abo, Top-Up), zieht die
   Wiederholung sie nach — ohne zweite Leistung.
5. Erstattung/Rueckbuchung: nichts wird gebucht, Ereignis genau einmal;
   Rechnungs-PATCH paid -> refunded/cancelled = 409.
Dazu die Probe des Live-Adapters (subscriptionId aus dem dict-Objekt).

FakeMollie, kein Live-Aufruf. Braucht `BRIDGE_TEST_MIGRATED_PG_URL` (Rezept in
test_energy_credit_arten_echte_db.py); ohne die Variable ausdruecklich
uebersprungen. Nie gegen eine geteilte oder produktive Datenbank richten.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.billing.test_abo_topup_brutto_echte_db import _abo_plan, _netto_zahlung_von_frueher, _q
from tests.billing.test_energy_credit_arten_echte_db import (  # noqa: F401 — Fixture db
    PG_URL,
    _bestandskunde_mit_altguthaben,
    _consent,
    db,
)
from tests.billing.test_mollie_brutto_echte_db import STEUERFAELLE, _setze_steuerfall

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="BRIDGE_TEST_MIGRATED_PG_URL fehlt — Brutto-Nachbesserung nicht gegen echten Postgres geprueft",
)


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------

class _AdresseKaputtConn:
    """Echte Verbindung; nur das Nachschlagen der Rechnungsadresse faellt aus."""

    def __init__(self, conn):
        self._c = conn

    def __getattr__(self, name):
        return getattr(self._c, name)

    async def fetchrow(self, sql, *args, **kw):
        if "billing_vat_id" in sql:
            raise ConnectionError("Datenbank voruebergehend nicht erreichbar (Probe)")
        return await self._c.fetchrow(sql, *args, **kw)


class _Acquire:
    def __init__(self, pool):
        self._cm = pool.acquire()

    async def __aenter__(self):
        return _AdresseKaputtConn(await self._cm.__aenter__())

    async def __aexit__(self, *exc):
        return await self._cm.__aexit__(*exc)


class _AdresseKaputtPool:
    def __init__(self, pool):
        self._p = pool

    def __getattr__(self, name):
        return getattr(self._p, name)

    def acquire(self):
        return _Acquire(self._p)


async def _zaehle(conn, sql, *args):
    return await conn.fetchval(sql, *args)


async def _brutto_abo(db, fall="AT", seats=1):
    """Neues Brutto-Abo ueber Checkout + Webhook. -> (uid, sub_row, netto_monat, brutto)."""
    from src.billing import billing_service
    from src.billing.mollie_adapter import get_mollie_adapter

    async with db.acquire() as conn:
        uid, tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        await _setze_steuerfall(conn, tid, fall)
        plan_id, netto = await _abo_plan(conn)
    co = await billing_service.start_subscription_checkout(
        str(uid), plan_id, seats, "https://x/ok", "u@test.local", "Owner", consent=_consent(),
    )
    res = await billing_service.handle_webhook(co["paymentId"])
    assert res.get("handled"), res
    async with db.acquire() as conn:
        sub = await conn.fetchrow(
            "SELECT id, user_id, amount_basis, net_eur, mollie_subscription_id, mollie_customer_id "
            "FROM subscriptions WHERE mollie_first_payment_id=$1", co["paymentId"],
        )
    brutto = _q(get_mollie_adapter()._subscriptions[sub["mollie_subscription_id"]]["amount_eur"])
    return uid, sub, _q(netto * seats), brutto


def _folgezahlung(sub, betrag) -> str:
    """Wiederkehrende Mollie-Zahlung so, wie Mollie sie zum Abo anlegt."""
    from src.billing.mollie_adapter import get_mollie_adapter

    pay_id = "tr_rec_" + uuid.uuid4().hex[:10]
    get_mollie_adapter()._payments[pay_id] = {
        "id": pay_id, "customer_id": sub["mollie_customer_id"], "amount_eur": float(betrag),
        "metadata": {}, "subscription_id": sub["mollie_subscription_id"],
    }
    return pay_id


# ---------------------------------------------------------------------------
# Befund 1 — Steuerdaten strikt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bahn", ["topup", "abo"])
async def test_adresse_nicht_lesbar_kein_checkout(db, bahn, caplog):
    from src.billing import billing_service
    from src.billing.mollie_adapter import get_mollie_adapter, reset_mollie_adapter

    reset_mollie_adapter()
    async with db.acquire() as conn:
        uid, tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        await _setze_steuerfall(conn, tid, "CH-Export")   # 0 % — ein Ersatzsatz waere 20 %
        plan_id, _ = await _abo_plan(conn)
    # Kunde vorher anlegen, damit nur die Steuerabfrage ausfaellt
    await billing_service.get_or_create_customer(str(uid), "u@test.local", "Owner")
    caplog.set_level(logging.ERROR)
    with patch.object(billing_service, "get_pool", return_value=_AdresseKaputtPool(db)):
        with pytest.raises(billing_service.SteuerdatenNichtErmittelbar):
            if bahn == "topup":
                await billing_service.start_topup_checkout(
                    str(uid), 50.0, "https://x/ok", "u@test.local", "Owner", consent=_consent(),
                )
            else:
                await billing_service.start_subscription_checkout(
                    str(uid), plan_id, 1, "https://x/ok", "u@test.local", "Owner", consent=_consent(),
                )
    assert get_mollie_adapter()._payments == {}
    async with db.acquire() as conn:
        assert await _zaehle(conn, "SELECT count(*) FROM pending_payments WHERE user_id=$1", uid) == 0
    assert any("ALARM tax data unavailable (billing address)" in r.getMessage() for r in caplog.records)


async def test_vies_nicht_lesbar_kein_checkout(db, caplog):
    """Bestaetigte DE-UID, aber die Bestaetigung ist nicht abfragbar -> kein 20-%-Abzug."""
    from src.billing import billing_service
    from src.billing.mollie_adapter import get_mollie_adapter, reset_mollie_adapter

    reset_mollie_adapter()
    async with db.acquire() as conn:
        uid, tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        await _setze_steuerfall(conn, tid, "DE-B2B-RC")
        plan_id, _ = await _abo_plan(conn)
    caplog.set_level(logging.ERROR)
    kaputt = AsyncMock(side_effect=ConnectionError("VIES-Tabelle nicht lesbar (Probe)"))
    with patch.object(billing_service.vat_id_module, "has_confirmed_validation", kaputt):
        for start in (
            lambda: billing_service.start_topup_checkout(
                str(uid), 50.0, "https://x/ok", "u@test.local", "Owner", consent=_consent()),
            lambda: billing_service.start_subscription_checkout(
                str(uid), plan_id, 1, "https://x/ok", "u@test.local", "Owner", consent=_consent()),
            lambda: billing_service.price_quote(str(uid), plan_id, 1),
        ):
            with pytest.raises(billing_service.SteuerdatenNichtErmittelbar):
                await start()
    assert get_mollie_adapter()._payments == {}
    async with db.acquire() as conn:
        assert await _zaehle(conn, "SELECT count(*) FROM pending_payments WHERE user_id=$1", uid) == 0
    assert any("ALARM tax data unavailable (VIES confirmation)" in r.getMessage() for r in caplog.records)


async def test_unbestaetigte_uid_ist_sachverhalt_20_prozent(db):
    """UID erfasst, keine VIES-Bestaetigung (kein Ausfall) -> AT-Satz, Checkout laeuft."""
    from src.billing import billing_service
    from src.billing.mollie_adapter import get_mollie_adapter, reset_mollie_adapter

    reset_mollie_adapter()
    async with db.acquire() as conn:
        uid, tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        await conn.execute(
            "UPDATE tenants SET billing_country='DE', billing_vat_id='DE999999999' WHERE id=$1", tid,
        )
    co = await billing_service.start_topup_checkout(
        str(uid), 50.0, "https://x/ok", "u@test.local", "Owner", consent=_consent(),
    )
    assert _q(get_mollie_adapter()._payments[co["paymentId"]]["amount_eur"]) == Decimal("60.00")


# ---------------------------------------------------------------------------
# Befund 2 — Folgezahlung bekommt Rechnung
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fall", sorted(STEUERFAELLE))
async def test_folgezahlung_brutto_abo_rechnung(db, fall):
    from src.billing import billing_service
    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    uid, sub, netto, brutto = await _brutto_abo(db, fall, seats=2)
    assert sub["amount_basis"] == "gross" and _q(sub["net_eur"]) == netto

    pay = _folgezahlung(sub, brutto)
    res = await billing_service.handle_webhook(pay)
    assert res["handled"] and res["type"] == "subscription_recurring", res
    assert res["subscriptionId"] == str(sub["id"])
    await billing_service.handle_webhook(pay)   # Wiederholung

    async with db.acquire() as conn:
        inv = await conn.fetch(
            "SELECT subtotal_eur, total_eur, subscription_id FROM invoices WHERE mollie_payment_id=$1", pay,
        )
        mismatch = await _zaehle(
            conn, "SELECT count(*) FROM billing_events WHERE user_id=$1 AND event_type='payment.amount_mismatch'",
            uid,
        )
        alle = await _zaehle(conn, "SELECT count(*) FROM invoices WHERE subscription_id=$1", sub["id"])
    assert len(inv) == 1
    assert _q(inv[0]["subtotal_eur"]) == netto
    assert _q(inv[0]["total_eur"]) == brutto
    assert inv[0]["subscription_id"] == sub["id"]
    assert mismatch == 0
    assert alle == 2   # Erstzahlung + eine Folgezahlung


async def test_folgezahlung_brutto_abweichung_gemeldet_einmal(db):
    """Mollie bucht etwas anderes ab als das Rechnungsbrutto -> Rechnung + Ereignis, einmal."""
    from src.billing import billing_service
    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    uid, sub, _netto, brutto = await _brutto_abo(db, "AT")
    pay = _folgezahlung(sub, brutto + Decimal("1.00"))
    await billing_service.handle_webhook(pay)
    await billing_service.handle_webhook(pay)
    async with db.acquire() as conn:
        assert await _zaehle(
            conn, "SELECT count(*) FROM billing_events WHERE mollie_payment_id=$1 "
            "AND event_type='payment.amount_mismatch'", pay) == 1
        assert await _zaehle(conn, "SELECT count(*) FROM invoices WHERE mollie_payment_id=$1", pay) == 1


async def _netto_bestandsabo(db, fall="AT"):
    """Bestands-Abo mit Basis net (Erstzahlung vor dem Umstieg angelegt)."""
    from src.billing import billing_service
    from src.billing.mollie_adapter import get_mollie_adapter

    mollie = get_mollie_adapter()
    async with db.acquire() as conn:
        uid, tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        await _setze_steuerfall(conn, tid, fall)
        plan_id, netto = await _abo_plan(conn)
    cust = await billing_service.get_or_create_customer(str(uid), "u@test.local", "Owner")
    erst = mollie._create_payment(cust["mollieCustomerId"], float(netto),
                                  {"type": "subscription_first", "seats": "1"})["paymentId"]
    async with db.acquire() as conn:
        await _netto_zahlung_von_frueher(conn, uid, erst, "subscription_first", plan_id, netto)
    await billing_service.handle_webhook(erst)
    async with db.acquire() as conn:
        sub = await conn.fetchrow(
            "SELECT id, amount_basis, net_eur, mollie_subscription_id, mollie_customer_id "
            "FROM subscriptions WHERE mollie_first_payment_id=$1", erst,
        )
    assert sub["amount_basis"] == "net"
    return uid, sub, erst, _q(netto)


def _aus_brutto(brutto: Decimal, satz: Decimal) -> tuple[Decimal, Decimal]:
    netto = (brutto * 100 / (100 + satz)).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
    return netto, brutto - netto


@pytest.mark.parametrize("fall", sorted(STEUERFAELLE))
async def test_netto_basis_rechnung_nie_ueber_zahlbetrag(db, fall):
    """Codex R2 B1: Bestands-Abo (net) — Erst- und Folgerechnung haben als
    Endbetrag genau den Zahlbetrag, USt. herausgerechnet; Vermerk + Alarm,
    kein amount_mismatch. RC/Export: 0 %, kein Alarm."""
    from src.billing import billing_service
    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    uid, sub, erst, netto = await _netto_bestandsabo(db, fall)
    satz = STEUERFAELLE[fall][3]
    pay = _folgezahlung(sub, netto)
    res = await billing_service.handle_webhook(pay)
    assert res["type"] == "subscription_recurring"
    await billing_service.handle_webhook(pay)   # Wiederholung
    erw_netto, erw_ust = _aus_brutto(netto, satz)
    for zahlung in (erst, pay):
        async with db.acquire() as conn:
            inv = await conn.fetchrow(
                "SELECT subtotal_eur, tax_eur, total_eur, status, metadata FROM invoices "
                "WHERE mollie_payment_id=$1", zahlung,
            )
            alarm = await _zaehle(
                conn, "SELECT count(*) FROM billing_events WHERE mollie_payment_id=$1 "
                "AND event_type='invoice.net_basis_vat_from_payment'", zahlung)
            mismatch = await _zaehle(
                conn, "SELECT count(*) FROM billing_events WHERE mollie_payment_id=$1 "
                "AND event_type='payment.amount_mismatch'", zahlung)
        assert inv["status"] == "paid"
        assert _q(inv["total_eur"]) == netto, (fall, zahlung, inv)   # nie ueber dem Zahlbetrag
        assert _q(inv["subtotal_eur"]) == erw_netto and _q(inv["tax_eur"]) == erw_ust
        meta = inv["metadata"] if isinstance(inv["metadata"], dict) else __import__("json").loads(inv["metadata"])
        assert meta["nettobasis"]["weg"] == "zahlbetrag_brutto"
        assert _q(meta["nettobasis"]["zahlbetragEur"]) == netto
        assert _q(meta["nettobasis"]["fruehererNettopreisEur"]) == netto
        assert alarm == (1 if satz > 0 else 0)
        assert mismatch == 0


async def test_netto_basis_50_euro_at_ergibt_41_67_plus_8_33(db):
    """Das Beispiel aus dem Auftrag: Zahlung 50,00, AT 20 % -> 41,67 + 8,33 = 50,00."""
    from src.billing import billing_service
    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    uid, sub, _erst, _netto = await _netto_bestandsabo(db, "AT")
    pay = _folgezahlung(sub, Decimal("50.00"))
    await billing_service.handle_webhook(pay)
    async with db.acquire() as conn:
        inv = await conn.fetchrow(
            "SELECT subtotal_eur, tax_eur, total_eur FROM invoices WHERE mollie_payment_id=$1", pay)
        ev = await conn.fetchrow(
            "SELECT payload FROM billing_events WHERE mollie_payment_id=$1 "
            "AND event_type='invoice.net_basis_vat_from_payment'", pay)
    assert (_q(inv["subtotal_eur"]), _q(inv["tax_eur"]), _q(inv["total_eur"])) == (
        Decimal("41.67"), Decimal("8.33"), Decimal("50.00"))
    payload = ev["payload"] if isinstance(ev["payload"], dict) else __import__("json").loads(ev["payload"])
    assert payload["rechnungNettoEur"] == 41.67 and payload["ustEur"] == 8.33


async def test_netto_basis_topup_rechnung_gleich_zahlbetrag(db):
    from src.billing import billing_service
    from src.billing.mollie_adapter import get_mollie_adapter, reset_mollie_adapter

    reset_mollie_adapter()
    mollie = get_mollie_adapter()
    async with db.acquire() as conn:
        uid, tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        await _setze_steuerfall(conn, tid, "AT")
    cust = await billing_service.get_or_create_customer(str(uid), "u@test.local", "Owner")
    pay = mollie._create_payment(cust["mollieCustomerId"], 50.0, {"type": "topup"})["paymentId"]
    async with db.acquire() as conn:
        await _netto_zahlung_von_frueher(conn, uid, pay, "topup", None, Decimal("50"))
    await billing_service.handle_webhook(pay)
    async with db.acquire() as conn:
        inv = await conn.fetchrow(
            "SELECT subtotal_eur, tax_eur, total_eur FROM invoices WHERE mollie_payment_id=$1", pay)
        lots = await conn.fetchval(
            "SELECT sum(l.amount_eur) FROM user_topup_lots l JOIN credit_purchases c "
            "ON c.id = l.credit_purchase_id WHERE c.mollie_payment_id=$1", pay)
    assert _q(inv["total_eur"]) == Decimal("50.00")
    assert (_q(inv["subtotal_eur"]), _q(inv["tax_eur"])) == (Decimal("41.67"), Decimal("8.33"))
    assert _q(lots) == Decimal("50.00")     # Gutschrift wie bisher


async def test_netto_basis_abweichender_zahlbetrag_gemeldet(db):
    """Zahlung weicht vom Mollie-Abobetrag ab: Rechnung = Zahlung, Abweichung zum
    frueheren Nettopreis im Ereignis; Rechnung und Zahlung stimmen, kein Mismatch."""
    from src.billing import billing_service
    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    uid, sub, _erst, netto = await _netto_bestandsabo(db, "AT")
    pay = _folgezahlung(sub, netto + Decimal("12.00"))
    await billing_service.handle_webhook(pay)
    async with db.acquire() as conn:
        inv = await conn.fetchrow("SELECT subtotal_eur, total_eur FROM invoices WHERE mollie_payment_id=$1", pay)
        ev = await conn.fetchrow(
            "SELECT payload FROM billing_events WHERE mollie_payment_id=$1 "
            "AND event_type='invoice.net_basis_vat_from_payment'", pay)
    payload = ev["payload"] if isinstance(ev["payload"], dict) else __import__("json").loads(ev["payload"])
    assert _q(inv["total_eur"]) == netto + Decimal("12.00")
    assert _q(payload["fruehererNettopreisEur"]) == netto
    assert _q(payload["abweichungZumFrueherenNettopreisEur"]) == _q(inv["subtotal_eur"]) - netto


async def test_netto_basis_unbekannter_schalterwert_laut(db, monkeypatch):
    from src.billing import billing_service
    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    uid, sub, _erst, netto = await _netto_bestandsabo(db, "AT")
    pay = _folgezahlung(sub, netto)
    monkeypatch.setenv("BRIDGE_NETTOBASIS_RECHNUNGSWEG", "netto_plus_ust")
    with pytest.raises(RuntimeError, match="BRIDGE_NETTOBASIS_RECHNUNGSWEG"):
        await billing_service.handle_webhook(pay)
    async with db.acquire() as conn:
        assert await _zaehle(conn, "SELECT count(*) FROM invoices WHERE mollie_payment_id=$1", pay) == 0


async def test_folgezahlung_ohne_abo_laut(db):
    from src.billing import billing_service
    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    pay = _folgezahlung({"mollie_customer_id": "c", "mollie_subscription_id": "sub_gibt_es_nicht"},
                        Decimal("10"))
    with pytest.raises(RuntimeError, match="no subscription for mollie id"):
        await billing_service.handle_webhook(pay)


# ---------------------------------------------------------------------------
# Befund 4 — Wiederholung zieht fehlende Rechnung nach
# ---------------------------------------------------------------------------

async def test_abo_rechnung_scheitert_wiederholung_zieht_nach(db):
    from src.billing import billing_service
    from src.billing.mollie_adapter import get_mollie_adapter, reset_mollie_adapter

    reset_mollie_adapter()
    async with db.acquire() as conn:
        uid, tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        await _setze_steuerfall(conn, tid, "DE-B2B-RC")
        plan_id, netto = await _abo_plan(conn)
    co = await billing_service.start_subscription_checkout(
        str(uid), plan_id, 1, "https://x/ok", "u@test.local", "Owner", consent=_consent(),
    )
    # Erster Webhook: Steuerdaten (VIES) nicht lesbar -> Rechnung strikt abgebrochen
    kaputt = AsyncMock(side_effect=ConnectionError("VIES weg (Probe)"))
    with patch.object(billing_service.vat_id_module, "has_confirmed_validation", kaputt):
        with pytest.raises(billing_service.SteuerdatenNichtErmittelbar):
            await billing_service.handle_webhook(co["paymentId"])
    async with db.acquire() as conn:
        assert await _zaehle(conn, "SELECT count(*) FROM invoices WHERE mollie_payment_id=$1",
                             co["paymentId"]) == 0
        assert await _zaehle(conn, "SELECT count(*) FROM subscriptions WHERE mollie_first_payment_id=$1",
                             co["paymentId"]) == 1

    # Mollie wiederholt: Rechnung kommt, kein zweites Mollie-Abo
    await billing_service.handle_webhook(co["paymentId"])
    await billing_service.handle_webhook(co["paymentId"])
    async with db.acquire() as conn:
        inv = await conn.fetch(
            "SELECT subtotal_eur, total_eur, tax_rate FROM invoices WHERE mollie_payment_id=$1", co["paymentId"],
        )
    assert len(inv) == 1
    assert _q(inv[0]["total_eur"]) == _q(netto)          # Reverse Charge, 0 %
    assert len(get_mollie_adapter()._subscriptions) == 1


async def test_topup_rechnung_scheitert_wiederholung_zieht_nach(db):
    from src.billing import billing_service
    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    async with db.acquire() as conn:
        uid, tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        await _setze_steuerfall(conn, tid, "AT")
    co = await billing_service.start_topup_checkout(
        str(uid), 50.0, "https://x/ok", "u@test.local", "Owner", consent=_consent(),
    )
    echt = billing_service.auto_create_invoice
    with patch.object(billing_service, "auto_create_invoice",
                      AsyncMock(side_effect=RuntimeError("Rechnung scheitert (Probe)"))):
        with pytest.raises(RuntimeError, match="Probe"):
            await billing_service.handle_webhook(co["paymentId"])
    async with db.acquire() as conn:
        assert _q(await conn.fetchval(
            "SELECT pack_eur FROM credit_purchases WHERE mollie_payment_id=$1", co["paymentId"])) == Decimal("50.00")
        assert await _zaehle(conn, "SELECT count(*) FROM invoices WHERE mollie_payment_id=$1",
                             co["paymentId"]) == 0
    assert billing_service.auto_create_invoice is echt

    res = await billing_service.handle_webhook(co["paymentId"])
    assert res["alreadyCredited"] is True if "alreadyCredited" in res else True
    async with db.acquire() as conn:
        inv = await conn.fetchrow(
            "SELECT subtotal_eur, total_eur FROM invoices WHERE mollie_payment_id=$1", co["paymentId"],
        )
        lots = await conn.fetchval("SELECT sum(amount_eur) FROM user_topup_lots WHERE user_id=$1", uid)
        mismatch = await _zaehle(
            conn, "SELECT count(*) FROM billing_events WHERE mollie_payment_id=$1 "
            "AND event_type='payment.amount_mismatch'", co["paymentId"])
    assert _q(inv["subtotal_eur"]) == Decimal("50.00") and _q(inv["total_eur"]) == Decimal("60.00")
    assert _q(lots) == Decimal("50.00")      # keine zweite Gutschrift
    assert mismatch == 0                     # Vergleich lief nach und passt


# ---------------------------------------------------------------------------
# Befund 5 — Erstattung laut gesperrt
# ---------------------------------------------------------------------------

async def test_erstattung_bucht_nichts_ereignis_einmal(db, caplog):
    from src.billing import billing_service
    from src.billing.mollie_adapter import get_mollie_adapter, reset_mollie_adapter

    reset_mollie_adapter()
    async with db.acquire() as conn:
        uid, tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        await _setze_steuerfall(conn, tid, "AT")
    co = await billing_service.start_topup_checkout(
        str(uid), 50.0, "https://x/ok", "u@test.local", "Owner", consent=_consent(),
    )
    await billing_service.handle_webhook(co["paymentId"])
    async with db.acquire() as conn:
        vorher = await conn.fetchval("SELECT sum(amount_eur) FROM user_topup_lots WHERE user_id=$1", uid)
        inv_vorher = await conn.fetchrow(
            "SELECT status FROM invoices WHERE mollie_payment_id=$1", co["paymentId"])

    get_mollie_adapter()._payments[co["paymentId"]]["amount_refunded_eur"] = 60.0
    caplog.set_level(logging.ERROR)
    r1 = await billing_service.handle_webhook(co["paymentId"])
    r2 = await billing_service.handle_webhook(co["paymentId"])
    assert r1["handled"] is False and r1["reason"] == "refund_not_supported"
    assert r2 == r1
    async with db.acquire() as conn:
        nachher = await conn.fetchval("SELECT sum(amount_eur) FROM user_topup_lots WHERE user_id=$1", uid)
        inv_nachher = await conn.fetchrow(
            "SELECT status FROM invoices WHERE mollie_payment_id=$1", co["paymentId"])
        ev = await conn.fetch(
            "SELECT user_id, payload FROM billing_events WHERE mollie_payment_id=$1 "
            "AND event_type='payment.refund_unbooked'", co["paymentId"])
    assert nachher == vorher and inv_nachher["status"] == inv_vorher["status"] == "paid"
    assert len(ev) == 1 and ev[0]["user_id"] == uid
    assert any("ALARM refund/chargeback" in r.getMessage() for r in caplog.records)

    # Weitere Teil-Erstattung (anderer Betrag) -> neues Ereignis
    get_mollie_adapter()._payments[co["paymentId"]]["amount_charged_back_eur"] = 10.0
    await billing_service.handle_webhook(co["paymentId"])
    async with db.acquire() as conn:
        assert await _zaehle(
            conn, "SELECT count(*) FROM billing_events WHERE mollie_payment_id=$1 "
            "AND event_type='payment.refund_unbooked'", co["paymentId"]) == 2


async def test_erstattung_folgezahlung_ordnet_kunden_zu(db):
    from src.billing import billing_service
    from src.billing.mollie_adapter import get_mollie_adapter, reset_mollie_adapter

    reset_mollie_adapter()
    uid, sub, _netto, brutto = await _brutto_abo(db, "AT")
    pay = _folgezahlung(sub, brutto)
    get_mollie_adapter()._payments[pay]["amount_refunded_eur"] = float(brutto)
    res = await billing_service.handle_webhook(pay)
    assert res["reason"] == "refund_not_supported"
    async with db.acquire() as conn:
        assert await conn.fetchval(
            "SELECT user_id FROM billing_events WHERE mollie_payment_id=$1 "
            "AND event_type='payment.refund_unbooked'", pay) == uid
        assert await _zaehle(conn, "SELECT count(*) FROM invoices WHERE mollie_payment_id=$1", pay) == 0


async def _probe_rechnungen(db):
    from src.billing import billing_service

    async with db.acquire() as conn:
        uid, _ = await _bestandskunde_mit_altguthaben(conn, slots=1)
    bezahlt = await billing_service.auto_create_invoice(
        user_id=str(uid), amount_eur=10.0, description="Probe", mollie_payment_id="tr_" + uuid.uuid4().hex[:8],
    )
    offen = await billing_service.auto_create_invoice(
        user_id=str(uid), amount_eur=10.0, description="Probe", mollie_payment_id="tr_" + uuid.uuid4().hex[:8],
    )
    async with db.acquire() as conn:   # echte offene Rechnung: nie bezahlt
        await conn.execute("UPDATE invoices SET status='issued', paid_at=NULL WHERE id=$1", uuid.UUID(offen))
    return bezahlt, offen


async def test_rechnung_patch_bezahlt_storno_409(db):
    from fastapi import HTTPException

    from src.invoices import routes as inv_routes

    bezahlt, offen = await _probe_rechnungen(db)
    for ziel in ("refunded", "cancelled", "issued", "draft"):
        with pytest.raises(HTTPException) as e:
            await inv_routes.update_invoice(bezahlt, inv_routes.InvoiceUpdate(status=ziel), MagicMock())
        assert e.value.status_code == 409
        assert e.value.detail["error"] == "refund_not_supported"
    # gleicher Status / nur Notiz bleibt moeglich
    r = await inv_routes.update_invoice(bezahlt, inv_routes.InvoiceUpdate(status="paid", notes="ok"), MagicMock())
    assert r["status"] == "paid" and r["notes"] == "ok"
    # Unbezahlte Rechnung bleibt stornierbar
    r = await inv_routes.update_invoice(offen, inv_routes.InvoiceUpdate(status="cancelled"), MagicMock())
    assert r["status"] == "cancelled"
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM invoices WHERE id=$1", uuid.UUID(bezahlt)) == "paid"


async def test_rechnung_umweg_paid_issued_refunded_gesperrt(db):
    """Codex R2 B2: paid -> issued -> refunded umging die Sperre."""
    from fastapi import HTTPException

    from src.invoices import routes as inv_routes

    bezahlt, _ = await _probe_rechnungen(db)
    with pytest.raises(HTTPException) as e:
        await inv_routes.update_invoice(bezahlt, inv_routes.InvoiceUpdate(status="issued"), MagicMock())
    assert e.value.status_code == 409
    # Altlast: schon zurueckgesetzte Rechnung (issued, aber paid_at gesetzt) bleibt gesperrt
    async with db.acquire() as conn:
        await conn.execute("UPDATE invoices SET status='issued' WHERE id=$1", uuid.UUID(bezahlt))
    for ziel in ("refunded", "cancelled"):
        with pytest.raises(HTTPException) as e:
            await inv_routes.update_invoice(bezahlt, inv_routes.InvoiceUpdate(status=ziel), MagicMock())
        assert e.value.status_code == 409 and e.value.detail["error"] == "refund_not_supported"
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT status, refunded_at, cancelled_at FROM invoices WHERE id=$1",
                                  uuid.UUID(bezahlt))
    assert row["status"] == "issued" and row["refunded_at"] is None and row["cancelled_at"] is None


@pytest.mark.parametrize("von,nach,erlaubt", [
    ("draft", "issued", True), ("draft", "paid", True), ("draft", "cancelled", True),
    ("issued", "paid", True), ("issued", "cancelled", True),
    ("issued", "draft", False), ("draft", "refunded", False), ("issued", "refunded", False),
    ("cancelled", "issued", False), ("cancelled", "paid", False), ("refunded", "issued", False),
])
async def test_statuskanten_unbezahlter_rechnungen(db, von, nach, erlaubt):
    from fastapi import HTTPException

    from src.invoices import routes as inv_routes

    _, rechnung = await _probe_rechnungen(db)
    async with db.acquire() as conn:
        await conn.execute("UPDATE invoices SET status=$2::invoice_status, paid_at=NULL WHERE id=$1",
                           uuid.UUID(rechnung), von)
    if erlaubt:
        r = await inv_routes.update_invoice(rechnung, inv_routes.InvoiceUpdate(status=nach), MagicMock())
        assert r["status"] == nach
    else:
        with pytest.raises(HTTPException) as e:
            await inv_routes.update_invoice(rechnung, inv_routes.InvoiceUpdate(status=nach), MagicMock())
        assert e.value.status_code == 409
        async with db.acquire() as conn:
            assert await conn.fetchval("SELECT status FROM invoices WHERE id=$1", uuid.UUID(rechnung)) == von


async def test_paket_bestellrechnung_vies_nicht_lesbar_kein_checkout(db):
    """Self-Service-Paket: das Brutto der Bestell-Rechnung IST der Mollie-Betrag —
    ein VIES-Ausfall darf keine 20-%-Rechnung und keine Zahlung erzeugen."""
    from src.billing import billing_service
    from src.billing.mollie_adapter import get_mollie_adapter, reset_mollie_adapter

    reset_mollie_adapter()
    async with db.acquire() as conn:
        uid, tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        await _setze_steuerfall(conn, tid, "DE-B2B-RC")
        rechnungen_vorher = await _zaehle(conn, "SELECT count(*) FROM invoices WHERE user_id=$1", uid)
    kaputt = AsyncMock(side_effect=ConnectionError("VIES weg (Probe)"))
    with patch.object(billing_service.vat_id_module, "has_confirmed_validation", kaputt), \
         patch("src.invoices.routes.auto_approve_and_send_invoice", AsyncMock()):
        with pytest.raises(billing_service.SteuerdatenNichtErmittelbar):
            await billing_service.start_project_pack_checkout(
                str(uid), "energy-objektbericht", 1, "https://x/ok", "u@test.local", "Owner",
                consent=_consent(),
            )
    assert get_mollie_adapter()._payments == {}
    async with db.acquire() as conn:
        assert await _zaehle(conn, "SELECT count(*) FROM invoices WHERE user_id=$1", uid) == rechnungen_vorher


async def test_folgezahlung_brutto_vies_nicht_lesbar_wiederholung_zieht_nach(db):
    """Folgezahlung eines Brutto-Abos (DE-RC, 0 %): VIES-Ausfall -> keine 20-%-Rechnung,
    Webhook scheitert laut; die Wiederholung legt die richtige Rechnung an."""
    from src.billing import billing_service
    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    uid, sub, netto, brutto = await _brutto_abo(db, "DE-B2B-RC")
    assert brutto == netto
    pay = _folgezahlung(sub, brutto)
    kaputt = AsyncMock(side_effect=ConnectionError("VIES weg (Probe)"))
    with patch.object(billing_service.vat_id_module, "has_confirmed_validation", kaputt):
        with pytest.raises(billing_service.SteuerdatenNichtErmittelbar):
            await billing_service.handle_webhook(pay)
    async with db.acquire() as conn:
        assert await _zaehle(conn, "SELECT count(*) FROM invoices WHERE mollie_payment_id=$1", pay) == 0
    await billing_service.handle_webhook(pay)
    async with db.acquire() as conn:
        inv = await conn.fetchrow("SELECT total_eur FROM invoices WHERE mollie_payment_id=$1", pay)
    assert _q(inv["total_eur"]) == netto
