"""Rechnungsbrutto == Mollie-Betrag, gegen einen ECHTEN, voll migrierten Postgres.

Rafael-Entscheid 17.10.2026 (e-energy-mollie-brutto-20261017 = a): Mollie zieht
brutto ein. Geprueft fuer alle Bahnen ueber die Bestell-Rechnung
(start_project_pack_checkout: drei Energy-Arten; start_first_purchase_pack_checkout:
drei Check-Pakete) und fuer die Steuerfaelle der Rechnungslogik
(_determine_tax_rate): AT 20 %, EU-B2B mit VIES-bestaetigter UID 0 % Reverse
Charge, Drittland 0 %. Dazu: das Angebot vor dem Kauf (price_quote) nennt
denselben Betrag, und der Webhook gibt frei, ohne einen Betragsfehler zu melden.

FakeMollie, kein Live-Aufruf. Braucht `BRIDGE_TEST_MIGRATED_PG_URL` (Rezept in
test_energy_credit_arten_echte_db.py); ohne die Variable ausdruecklich
uebersprungen. Nie gegen eine geteilte oder produktive Datenbank richten.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from tests.billing.test_energy_credit_arten_echte_db import (  # noqa: F401 — Fixture db
    PG_URL,
    _bestandskunde_mit_altguthaben,
    _consent,
    db,
)

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="BRIDGE_TEST_MIGRATED_PG_URL fehlt — Mollie brutto nicht gegen echten Postgres geprueft",
)

STEUERFAELLE = {
    # Fall: (Land, UID, VIES bestaetigt, erwarteter Satz, Reverse-Charge-Hinweis)
    "AT": ("AT", None, False, Decimal("20"), False),
    "DE-B2B-RC": ("DE", "DE123456789", True, Decimal("0"), True),
    "CH-Export": ("CH", None, False, Decimal("0"), False),
}

ENERGY = {"energy-anlagendokumentation": (1, Decimal("150")),
          "energy-objektbericht": (1, Decimal("300")),
          "energy-analysebericht": (1, Decimal("600"))}
CHECK = {"report-check-credit-1": 1, "report-check-credit-5": 5, "report-check-credit": 20}


async def _setze_steuerfall(conn, tid: str, fall: str) -> None:
    land, uid, bestaetigt, _, _ = STEUERFAELLE[fall]
    await conn.execute(
        "UPDATE tenants SET billing_country=$2, billing_vat_id=$3 WHERE id=$1", tid, land, uid,
    )
    if uid and bestaetigt:
        await conn.execute(
            """INSERT INTO vat_id_validations (id, tenant_id, vat_id, country_code, is_valid, response_raw)
               VALUES ($1,$2,$3,$4,TRUE,$5::jsonb)""",
            uuid.uuid4(), tid, uid, land, json.dumps({"valid": True, "test": True}),
        )


async def _kaufen_und_pruefen(db, uid, tid, fall, lane, plan_id, menge):
    from src.billing import billing_service
    from src.billing.mollie_adapter import get_mollie_adapter

    erwarteter_satz = STEUERFAELLE[fall][3]
    angebot = await billing_service.price_quote(str(uid), plan_id, menge)

    start = (billing_service.start_project_pack_checkout if lane == "pack"
             else billing_service.start_first_purchase_pack_checkout)
    with patch("src.invoices.routes.auto_approve_and_send_invoice", AsyncMock()):
        checkout = await start(str(uid), plan_id, menge, "https://x/ok", "u@test.local", "Owner",
                               consent=_consent())
        mollie_betrag = Decimal(str(get_mollie_adapter()._payments[checkout["paymentId"]]["amount_eur"]))
        with patch.object(billing_service, "log_billing_event", wraps=billing_service.log_billing_event) as ev:
            res = await billing_service.handle_webhook(checkout["paymentId"])
    assert res.get("handled"), res
    assert "payment.amount_mismatch" not in [c.args[0] for c in ev.call_args_list]

    async with db.acquire() as conn:
        inv = await conn.fetchrow(
            """SELECT i.subtotal_eur, i.tax_rate, i.tax_eur, i.total_eur, i.status, i.mollie_payment_id, i.metadata
                 FROM pending_orders o JOIN invoices i ON i.id = o.invoice_id
                WHERE o.user_id=$1 AND o.plan_id=$2 AND o.payment_method='mollie'
                ORDER BY o.created_at DESC LIMIT 1""",
            uid, plan_id,
        )
        consent_betrag = await conn.fetchval(
            "SELECT amount_eur FROM purchase_consents WHERE user_id=$1 AND plan_id=$2 ORDER BY recorded_at DESC LIMIT 1",
            uid, plan_id,
        )
    # Kern: abgebucht == Rechnungsbrutto.
    assert mollie_betrag == inv["total_eur"], (fall, plan_id, mollie_betrag, inv["total_eur"])
    assert Decimal(str(inv["tax_rate"])) == erwarteter_satz
    assert inv["total_eur"] == inv["subtotal_eur"] + inv["tax_eur"]
    assert inv["status"] == "paid" and inv["mollie_payment_id"] == checkout["paymentId"]
    meta = inv["metadata"] if isinstance(inv["metadata"], dict) else json.loads(inv["metadata"])
    assert ("reverseChargeNote" in meta) == STEUERFAELLE[fall][4]
    # Angebot vor dem Kauf == Rechnung == Zustimmungsbeleg.
    assert Decimal(str(angebot["grossEur"])) == inv["total_eur"]
    assert Decimal(str(consent_betrag)).quantize(Decimal("0.01")) == inv["total_eur"]
    return inv


@pytest.mark.parametrize("fall", sorted(STEUERFAELLE))
async def test_energy_arten_rechnungsbrutto_gleich_mollie(db, fall):
    async with db.acquire() as conn:
        uid, tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        await _setze_steuerfall(conn, tid, fall)
    for plan_id, (menge, netto) in ENERGY.items():
        inv = await _kaufen_und_pruefen(db, uid, tid, fall, "pack", plan_id, menge)
        assert inv["subtotal_eur"] == netto
        if fall == "AT":
            assert inv["total_eur"] == netto * Decimal("1.2")


@pytest.mark.parametrize("fall", sorted(STEUERFAELLE))
async def test_check_pakete_rechnungsbrutto_gleich_mollie(db, fall):
    """Andere Produkte auf demselben Pfad (Bestell-Rechnung + Mollie-Einmalzahlung)."""
    async with db.acquire() as conn:
        uid, tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        await _setze_steuerfall(conn, tid, fall)
    for plan_id, menge in CHECK.items():
        await _kaufen_und_pruefen(db, uid, tid, fall, "erstkauf", plan_id, menge)
