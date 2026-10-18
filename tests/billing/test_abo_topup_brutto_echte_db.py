"""Abo und Top-Up brutto bei NEUEN Abschluessen — gegen einen ECHTEN, voll migrierten Postgres.

Rafael-Entscheid 18.10.2026 (e-bridge-abos-brutto = a): "ja, aber erst fuer neue
Abschluesse; bestehende Abos bleiben unveraendert". Geprueft fuer die Steuerfaelle
der Rechnungslogik (AT 20 %, DE-B2B mit VIES-bestaetigter UID 0 %, CH 0 %):

- Abo: Mollie-Erstzahlung == Mollie-Abobetrag == Rechnungsbrutto == Zustimmungsbeleg;
  subscriptions.amount_basis = 'gross'; kein payment.amount_mismatch.
- Top-Up: Mollie == Rechnungsbrutto == Beleg, gutgeschrieben wird das NETTO.
- Bestand: eine vor dem Umstieg netto angelegte Zahlung (pending_payments ohne
  Basis = Default 'net') wird netto behandelt — Abo-Betrag und Gutschrift wie bisher.
- Migration 068: Brutto-Abschluss ohne Netto lehnt die Datenbank ab.

FakeMollie, kein Live-Aufruf. Braucht `BRIDGE_TEST_MIGRATED_PG_URL` (Rezept in
test_energy_credit_arten_echte_db.py); ohne die Variable ausdruecklich
uebersprungen. Nie gegen eine geteilte oder produktive Datenbank richten.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

from tests.billing.test_energy_credit_arten_echte_db import (  # noqa: F401 — Fixture db
    PG_URL,
    _bestandskunde_mit_altguthaben,
    _consent,
    db,
)
from tests.billing.test_mollie_brutto_echte_db import STEUERFAELLE, _setze_steuerfall

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="BRIDGE_TEST_MIGRATED_PG_URL fehlt — Abo/Top-Up brutto nicht gegen echten Postgres geprueft",
)

_CENT = Decimal("0.01")


async def _abo_plan(conn) -> tuple[str, Decimal]:
    row = await conn.fetchrow(
        """SELECT id, price_eur FROM plans
            WHERE is_active AND sellable AND interval = 'month' AND price_eur > 0
            ORDER BY price_eur, id LIMIT 1"""
    )
    assert row, "kein kaeuflicher Monatsplan im Katalog"
    return row["id"], Decimal(str(row["price_eur"]))


def _q(x) -> Decimal:
    return Decimal(str(x)).quantize(_CENT)


async def _events(conn, uid, art):
    return await conn.fetchval(
        "SELECT count(*) FROM billing_events WHERE user_id=$1 AND event_type=$2", uid, art,
    )


@pytest.mark.parametrize("fall", sorted(STEUERFAELLE))
async def test_abo_neuer_abschluss_brutto(db, fall):
    from src.billing import billing_service
    from src.billing.mollie_adapter import get_mollie_adapter, reset_mollie_adapter

    reset_mollie_adapter()
    async with db.acquire() as conn:
        uid, tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        await _setze_steuerfall(conn, tid, fall)
        plan_id, netto = await _abo_plan(conn)
    satz = STEUERFAELLE[fall][3]
    seats = 2
    erwartet = _q(netto * seats + (netto * seats * satz / 100).quantize(_CENT))

    checkout = await billing_service.start_subscription_checkout(
        str(uid), plan_id, seats, "https://x/ok", "u@test.local", "Owner", consent=_consent(),
    )
    mollie = get_mollie_adapter()
    erst = _q(mollie._payments[checkout["paymentId"]]["amount_eur"])
    res = await billing_service.handle_webhook(checkout["paymentId"])
    assert res.get("handled") and res["type"] == "subscription_first", res

    async with db.acquire() as conn:
        sub = await conn.fetchrow(
            "SELECT id, amount_basis, mollie_subscription_id FROM subscriptions WHERE mollie_first_payment_id=$1",
            checkout["paymentId"],
        )
        inv = await conn.fetchrow(
            "SELECT subtotal_eur, tax_rate, total_eur FROM invoices WHERE mollie_payment_id=$1",
            checkout["paymentId"],
        )
        beleg = await conn.fetchval(
            "SELECT amount_eur FROM purchase_consents WHERE user_id=$1 AND lane='subscription' "
            "ORDER BY recorded_at DESC LIMIT 1", uid,
        )
        mismatch = await _events(conn, uid, "payment.amount_mismatch")
    abo = _q(mollie._subscriptions[sub["mollie_subscription_id"]]["amount_eur"])

    assert sub["amount_basis"] == "gross"
    assert erst == erwartet == abo == _q(inv["total_eur"]) == _q(beleg), (fall, erst, abo, inv, beleg)
    assert _q(inv["subtotal_eur"]) == _q(netto * seats)
    assert Decimal(str(inv["tax_rate"])) == satz
    assert mismatch == 0


@pytest.mark.parametrize("fall", sorted(STEUERFAELLE))
async def test_top_up_neuer_abschluss_brutto_gutschrift_netto(db, fall):
    from src.billing import billing_service
    from src.billing.mollie_adapter import get_mollie_adapter, reset_mollie_adapter

    reset_mollie_adapter()
    async with db.acquire() as conn:
        uid, tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        await _setze_steuerfall(conn, tid, fall)
    satz = STEUERFAELLE[fall][3]
    erwartet = _q(Decimal("50") + (Decimal("50") * satz / 100).quantize(_CENT))

    checkout = await billing_service.start_topup_checkout(
        str(uid), 50.0, "https://x/ok", "u@test.local", "Owner", consent=_consent(),
    )
    bezahlt = _q(get_mollie_adapter()._payments[checkout["paymentId"]]["amount_eur"])
    res = await billing_service.handle_webhook(checkout["paymentId"])
    assert res.get("handled") and res["type"] == "topup", res

    async with db.acquire() as conn:
        gut = await conn.fetchval(
            "SELECT pack_eur FROM credit_purchases WHERE mollie_payment_id=$1", checkout["paymentId"],
        )
        lot = await conn.fetchval(
            "SELECT sum(amount_eur) FROM user_topup_lots WHERE user_id=$1", uid,
        )
        inv = await conn.fetchrow(
            "SELECT subtotal_eur, total_eur FROM invoices WHERE mollie_payment_id=$1", checkout["paymentId"],
        )
        beleg = await conn.fetchval(
            "SELECT amount_eur FROM purchase_consents WHERE user_id=$1 AND lane='topup' "
            "ORDER BY recorded_at DESC LIMIT 1", uid,
        )
        mismatch = await _events(conn, uid, "payment.amount_mismatch")

    assert bezahlt == erwartet == _q(inv["total_eur"]) == _q(beleg)
    assert _q(gut) == _q(lot) == _q(inv["subtotal_eur"]) == Decimal("50.00")   # Guthaben = Netto
    assert mismatch == 0

    # Webhook-Wiederholung: keine zweite Gutschrift, keine zweite Rechnung.
    await billing_service.handle_webhook(checkout["paymentId"])
    async with db.acquire() as conn:
        assert _q(await conn.fetchval(
            "SELECT sum(amount_eur) FROM user_topup_lots WHERE user_id=$1", uid)) == Decimal("50.00")
        assert await conn.fetchval(
            "SELECT count(*) FROM invoices WHERE mollie_payment_id=$1", checkout["paymentId"]) == 1


async def _netto_zahlung_von_frueher(conn, uid, pay_id, typ, plan_id, betrag):
    """So, wie der Code vor 068 die Zeile schrieb: ohne Basis-Spalten."""
    await conn.execute(
        "INSERT INTO pending_payments (payment_id, user_id, type, plan_id, amount_eur, created_at) "
        "VALUES ($1, $2, $3, $4, $5, NOW())",
        pay_id, uid, typ, plan_id, betrag,
    )


async def test_bestand_netto_zahlung_bleibt_netto(db):
    """Vor dem Umstieg gestartet, danach bezahlt: Abo und Top-Up bleiben netto."""
    from src.billing import billing_service
    from src.billing.mollie_adapter import get_mollie_adapter, reset_mollie_adapter

    reset_mollie_adapter()
    mollie = get_mollie_adapter()
    async with db.acquire() as conn:
        uid, _tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        plan_id, netto = await _abo_plan(conn)
    cust = await billing_service.get_or_create_customer(str(uid), "u@test.local", "Owner")
    # Zahlungen so anlegen, wie der alte Code sie bei Mollie anlegte (netto).
    abo_pay = mollie._create_payment(cust["mollieCustomerId"], float(netto),
                                     {"type": "subscription_first", "seats": "1"})["paymentId"]
    top_pay = mollie._create_payment(cust["mollieCustomerId"], 50.0, {"type": "topup"})["paymentId"]
    async with db.acquire() as conn:
        await _netto_zahlung_von_frueher(conn, uid, abo_pay, "subscription_first", plan_id, netto)
        await _netto_zahlung_von_frueher(conn, uid, top_pay, "topup", None, Decimal("50"))
        assert await conn.fetchval(
            "SELECT amount_basis FROM pending_payments WHERE payment_id=$1", abo_pay) == "net"

    with patch.object(billing_service, "log_billing_event", wraps=billing_service.log_billing_event) as ev:
        await billing_service.handle_webhook(abo_pay)
        await billing_service.handle_webhook(top_pay)
    assert "payment.amount_mismatch" not in [c.args[0] for c in ev.call_args_list]

    async with db.acquire() as conn:
        sub = await conn.fetchrow(
            "SELECT amount_basis, mollie_subscription_id FROM subscriptions WHERE mollie_first_payment_id=$1",
            abo_pay,
        )
        gut = await conn.fetchval("SELECT pack_eur FROM credit_purchases WHERE mollie_payment_id=$1", top_pay)
    assert sub["amount_basis"] == "net"
    assert _q(mollie._subscriptions[sub["mollie_subscription_id"]]["amount_eur"]) == _q(netto)  # wie bisher
    assert _q(gut) == Decimal("50.00")


async def test_migration_068_brutto_ohne_netto_abgelehnt(db):
    import asyncpg

    async with db.acquire() as conn:
        uid, _ = await _bestandskunde_mit_altguthaben(conn, slots=1)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO pending_payments (payment_id, user_id, type, amount_eur, amount_basis) "
                "VALUES ($1, $2, 'topup', 60, 'gross')",
                "tr_" + uuid.uuid4().hex[:8], uid,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO pending_payments (payment_id, user_id, type, amount_eur, amount_basis, net_eur) "
                "VALUES ($1, $2, 'topup', 60, 'brutto', 50)",
                "tr_" + uuid.uuid4().hex[:8], uid,
            )
        # Self-Service-Paket: brutto ohne Netto ist erlaubt (Betrag aus der Bestell-Rechnung).
        await conn.execute(
            "INSERT INTO pending_payments (payment_id, user_id, type, plan_id, amount_eur, amount_basis) "
            "VALUES ($1, $2, 'project_pack', 'energy-objektbericht', 360, 'gross')",
            "tr_" + uuid.uuid4().hex[:8], uid,
        )
