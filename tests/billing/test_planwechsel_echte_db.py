"""Planwechsel ohne Zwischenzustand ohne Abo — gegen einen ECHTEN, voll migrierten Postgres.

Befund 18.10.2026 (Sitzung 15eb1055, Folgeposten 3 zum Brutto-Schnitt): auf
2c98506 kuendigte change_subscription das laufende Abo zuerst und rief danach
start_subscription_checkout OHNE das Pflichtfeld `consent` auf — TypeError nach
der Kuendigung, der Kunde stand ohne Abo da.

Geprueft (FakeMollie, kein Live-Aufruf):
1. Planwechsel startet den Checkout MIT Zustimmungsbeleg; das alte Abo bleibt
   aktiv, solange die neue Zahlung nicht bezahlt ist.
2. Bezahlt → neues Abo aktiv, ERST DANN altes Abo gekuendigt (Mollie + DB).
   Webhook-Wiederholung aendert nichts.
3. Zahlung gescheitert / abgebrochen → altes Abo bleibt aktiv.
4. Scheitert die Kuendigung des alten Abos (Mollie-Fehler), antwortet der
   Webhook mit Fehler; die Wiederholung kuendigt nach, ohne ein zweites neues
   Abo anzulegen.
5. Migration 069: ersetzt_abo_id nur an Abo-Erstzahlungen.

Braucht `BRIDGE_TEST_MIGRATED_PG_URL` (Rezept in
test_energy_credit_arten_echte_db.py); ohne die Variable ausdruecklich
uebersprungen. Nie gegen eine geteilte oder produktive Datenbank richten.
"""
from __future__ import annotations

import uuid
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
    reason="BRIDGE_TEST_MIGRATED_PG_URL fehlt — Planwechsel nicht gegen echten Postgres geprueft",
)


async def _monatsplan(conn) -> str:
    plan = await conn.fetchval(
        """SELECT id FROM plans
            WHERE is_active AND sellable AND interval = 'month' AND price_eur > 0
            ORDER BY price_eur, id LIMIT 1"""
    )
    assert plan, "kein kaeuflicher Monatsplan im Katalog"
    return plan


async def _kunde_mit_aktivem_abo(db):
    """Bestandskunde mit bezahltem, aktivem Abo auf Plan A — ueber den echten Weg."""
    from src.billing import billing_service

    async with db.acquire() as conn:
        uid, tid = await _bestandskunde_mit_altguthaben(conn, slots=1)
        await conn.execute(
            "UPDATE tenants SET billing_country='AT' WHERE id=$1", tid,
        )
        plan = await _monatsplan(conn)
    first = await billing_service.start_subscription_checkout(
        str(uid), plan, 1, "https://x/ok", "u@test.local", "Owner", consent=_consent(),
    )
    res = await billing_service.handle_webhook(first["paymentId"])
    assert res.get("handled"), res
    # Der Katalog hat nur einen kaeuflichen Monatsplan: der Wechsel ist ein
    # Sitzwechsel (1 -> 2) — change_subscription behandelt ihn identisch.
    return uid, (plan, 1), (plan, 2), str(res["subscription"]["id"])


async def _status(db, sub_id):
    async with db.acquire() as conn:
        return await conn.fetchval("SELECT status FROM subscriptions WHERE id=$1", uuid.UUID(sub_id))


async def _aktive(db, uid):
    async with db.acquire() as conn:
        return [(r["plan_id"], r["seats"]) for r in await conn.fetch(
            "SELECT plan_id, seats FROM subscriptions WHERE user_id=$1 AND status='active' ORDER BY started_at", uid,
        )]


def _mollie():
    from src.billing.mollie_adapter import get_mollie_adapter
    return get_mollie_adapter()


async def test_planwechsel_kuendigt_erst_nach_bezahlung(db):
    from src.billing import billing_service
    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    alt_mollie = None
    async with db.acquire() as conn:
        alt_mollie = await conn.fetchval(
            "SELECT mollie_subscription_id FROM subscriptions WHERE id=$1", uuid.UUID(alt))

    wechsel = await billing_service.change_subscription(
        str(uid), plan_b[0], plan_b[1], "https://x/ok", "u@test.local", "Owner", consent=_consent(),
    )
    assert wechsel["replacesSubscriptionId"] == alt
    # Checkout gestartet: altes Abo weiter aktiv, bei Mollie nicht gekuendigt.
    assert await _status(db, alt) == "active"
    assert _mollie()._subscriptions[alt_mollie]["status"] == "active"
    async with db.acquire() as conn:
        pend = await conn.fetchrow(
            "SELECT ersetzt_abo_id FROM pending_payments WHERE payment_id=$1", wechsel["paymentId"])
        belege = await conn.fetchval(
            "SELECT count(*) FROM purchase_consents WHERE user_id=$1 AND lane='subscription'", uid)
    assert str(pend["ersetzt_abo_id"]) == alt
    assert belege == 2  # Erstkauf + Planwechsel: jeder Abschluss hat seinen Beleg

    res = await billing_service.handle_webhook(wechsel["paymentId"])
    assert res["replacedSubscriptionId"] == alt
    assert await _status(db, alt) == "cancelled"
    assert _mollie()._subscriptions[alt_mollie]["status"] == "canceled"
    assert await _aktive(db, uid) == [plan_b]

    # Wiederholung: idempotent, kein zweites Abo.
    await billing_service.handle_webhook(wechsel["paymentId"])
    assert await _aktive(db, uid) == [plan_b]


@pytest.mark.parametrize("ausgang", ["failed", "expired", "canceled", "open"])
async def test_planwechsel_ohne_bezahlung_behaelt_altes_abo(db, ausgang):
    from src.billing import billing_service
    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    wechsel = await billing_service.change_subscription(
        str(uid), plan_b[0], plan_b[1], "https://x/ok", "u@test.local", "Owner", consent=_consent(),
    )
    # FakeMollie meldet jede Zahlung als bezahlt — hier den echten Ausgang vorgeben.
    zahlung = {**_mollie()._payments[wechsel["paymentId"]], "status": ausgang}
    with patch.object(_mollie(), "get_payment", AsyncMock(return_value=zahlung)):
        res = await billing_service.handle_webhook(wechsel["paymentId"])
    assert not res.get("handled"), res
    assert await _status(db, alt) == "active"
    assert await _aktive(db, uid) == [plan_a]


async def test_planwechsel_checkout_scheitert_altes_abo_bleibt(db):
    """Scheitert schon der Checkout (hier: Mollie wirft), bleibt alles beim Alten."""
    from src.billing import billing_service
    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    with patch.object(_mollie(), "create_first_payment", AsyncMock(side_effect=RuntimeError("mollie down"))):
        with pytest.raises(RuntimeError, match="mollie down"):
            await billing_service.change_subscription(
                str(uid), plan_b[0], plan_b[1], "https://x/ok", "u@test.local", "Owner", consent=_consent(),
            )
    assert await _status(db, alt) == "active"
    assert await _aktive(db, uid) == [plan_a]


async def test_kuendigung_scheitert_webhook_wiederholt(db):
    from src.billing import billing_service
    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    uid, plan_a, plan_b, alt = await _kunde_mit_aktivem_abo(db)
    wechsel = await billing_service.change_subscription(
        str(uid), plan_b[0], plan_b[1], "https://x/ok", "u@test.local", "Owner", consent=_consent(),
    )
    with patch.object(_mollie(), "cancel_subscription", AsyncMock(side_effect=RuntimeError("mollie 503"))):
        with pytest.raises(RuntimeError, match="mollie 503"):
            await billing_service.handle_webhook(wechsel["paymentId"])
    # Neues Abo ist aktiv, altes noch nicht gekuendigt — der Webhook hat laut versagt.
    assert sorted(await _aktive(db, uid)) == sorted([plan_a, plan_b])

    await billing_service.handle_webhook(wechsel["paymentId"])   # Mollie wiederholt
    assert await _status(db, alt) == "cancelled"
    assert await _aktive(db, uid) == [plan_b]


async def test_migration_069_nur_abo_erstzahlung(db):
    import asyncpg

    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    uid, _a, _b, alt = await _kunde_mit_aktivem_abo(db)
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "INSERT INTO pending_payments (payment_id, user_id, type, amount_eur, ersetzt_abo_id) "
                "VALUES ($1, $2, 'topup', 50, $3)",
                "tr_" + uuid.uuid4().hex[:8], uid, uuid.UUID(alt),
            )
