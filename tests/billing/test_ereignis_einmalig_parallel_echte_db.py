"""Einmaligkeit eines Zahlungsereignisses als DB-Invariante (Migration 071,
Codex R3 18.10.2026) — gegen einen ECHTEN, voll migrierten Postgres.

Bis R3 dedupliziert per SELECT-vor-INSERT: seriell einmalig, bei parallelen
Webhooks nicht. Jetzt: eindeutiger Index auf billing_events.ereignis_schluessel,
INSERT ... ON CONFLICT DO NOTHING vor jeder weiteren Wirkung (Alarm).

1. Datenbankebene, zwei Verbindungen mit offenen Transaktionen: die zweite
   wartet auf die erste und schreibt nichts. Gegenprobe ohne Schluessel mit dem
   alten SELECT-vor-INSERT unter einer Schranke: zwei Zeilen — die Race ist echt.
2. Anwendungsebene: acht gleichzeitige Webhooks zur selben Zahlung (Pool,
   getrennte Verbindungen) -> genau ein Ereignis, genau ein ALARM — fuer
   Erstattung, Betragsabweichung und Netto-Basis-USt.
3. Migration 071: Nachtrag vergibt je Schluessel genau der aeltesten Zeile den
   Schluessel, Duplikate bleiben ohne (nichts geloescht).

Braucht `BRIDGE_TEST_MIGRATED_PG_URL` (Migrationen bis 071); ohne die Variable
ausdruecklich uebersprungen. Nie gegen eine geteilte oder produktive Datenbank.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest

from tests.billing.test_brutto_nachbesserung_echte_db import (  # noqa: F401 — Fixture db
    _brutto_abo,
    _folgezahlung,
    _netto_bestandsabo,
    _zaehle,
    db,
)
from tests.billing.test_energy_credit_arten_echte_db import PG_URL

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="BRIDGE_TEST_MIGRATED_PG_URL fehlt — Ereignis-Einmaligkeit nicht gegen echten Postgres geprueft",
)

PARALLEL = 8
MIGRATION_071 = Path(__file__).resolve().parents[2] / "docker" / "migrations" / "071_billing_events_ereignis_schluessel.sql"

_INSERT = """INSERT INTO billing_events (event_type, mollie_payment_id, source, payload, ereignis_schluessel)
             VALUES ($1, $2, 'test', '{}'::jsonb, $3)
             ON CONFLICT (ereignis_schluessel) WHERE ereignis_schluessel IS NOT NULL DO NOTHING
             RETURNING id"""


async def test_zwei_verbindungen_gleicher_schluessel_genau_eine_zeile(db):
    pay = "tr_par_" + uuid.uuid4().hex[:10]
    key = f"payment.refund_unbooked:{pay}:1.00:0.00"
    a = await asyncpg.connect(PG_URL)
    b = await asyncpg.connect(PG_URL)
    try:
        ta, tb = a.transaction(), b.transaction()
        await ta.start()
        await tb.start()
        id_a = await a.fetchval(_INSERT, "payment.refund_unbooked", pay, key)
        # B trifft auf die noch offene Zeile von A und muss warten
        b_lauf = asyncio.create_task(b.fetchval(_INSERT, "payment.refund_unbooked", pay, key))
        await asyncio.sleep(0.3)
        assert not b_lauf.done(), "zweite Verbindung haette auf die erste warten muessen"
        await ta.commit()
        id_b = await asyncio.wait_for(b_lauf, 10)
        await tb.commit()
    finally:
        await a.close()
        await b.close()
    assert id_a is not None and id_b is None
    async with db.acquire() as conn:
        assert await _zaehle(conn, "SELECT count(*) FROM billing_events WHERE mollie_payment_id=$1", pay) == 1


async def test_gegenprobe_select_vor_insert_ohne_schluessel_doppelt(db):
    """Das bis R3 verwendete Muster bricht unter Gleichzeitigkeit — der Test oben misst also etwas."""
    pay = "tr_alt_" + uuid.uuid4().hex[:10]
    schranke = asyncio.Barrier(2)

    async def altes_muster():
        conn = await asyncpg.connect(PG_URL)
        try:
            schon = await conn.fetchval(
                "SELECT 1 FROM billing_events WHERE event_type='payment.amount_mismatch' "
                "AND mollie_payment_id=$1 LIMIT 1", pay)
            await schranke.wait()
            if not schon:
                await conn.fetchval(_INSERT, "payment.amount_mismatch", pay, None)
        finally:
            await conn.close()

    await asyncio.gather(altes_muster(), altes_muster())
    async with db.acquire() as conn:
        assert await _zaehle(conn, "SELECT count(*) FROM billing_events WHERE mollie_payment_id=$1", pay) == 2


async def _parallel(aufruf):
    return await asyncio.gather(*(aufruf() for _ in range(PARALLEL)), return_exceptions=True)


def _alarme(caplog, pay: str, merkmal: str) -> int:
    return sum(1 for r in caplog.records
               if r.levelno >= logging.ERROR and pay in r.getMessage() and merkmal in r.getMessage())


async def test_parallele_webhooks_erstattung_genau_ein_ereignis(db, caplog):
    from src.billing import billing_service
    from src.billing.mollie_adapter import get_mollie_adapter, reset_mollie_adapter

    reset_mollie_adapter()
    _uid, sub, _netto, brutto = await _brutto_abo(db, "AT")
    pay = _folgezahlung(sub, brutto)
    get_mollie_adapter()._payments[pay]["amount_refunded_eur"] = float(brutto)
    caplog.set_level(logging.WARNING)
    res = await _parallel(lambda: billing_service.handle_webhook(pay))
    assert all(isinstance(r, dict) and r["reason"] == "refund_not_supported" for r in res), res
    async with db.acquire() as conn:
        assert await _zaehle(
            conn, "SELECT count(*) FROM billing_events WHERE mollie_payment_id=$1 "
            "AND event_type='payment.refund_unbooked'", pay) == 1
    assert _alarme(caplog, pay, "ALARM refund/chargeback") == 1


async def test_parallele_webhooks_betragsabweichung_genau_ein_ereignis(db, caplog):
    from src.billing import billing_service
    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    _uid, sub, _netto, brutto = await _brutto_abo(db, "AT")
    pay = _folgezahlung(sub, brutto + Decimal("1.00"))
    caplog.set_level(logging.WARNING)
    res = await _parallel(lambda: billing_service.handle_webhook(pay))
    assert all(isinstance(r, dict) and r.get("handled") for r in res), res
    async with db.acquire() as conn:
        assert await _zaehle(
            conn, "SELECT count(*) FROM billing_events WHERE mollie_payment_id=$1 "
            "AND event_type='payment.amount_mismatch'", pay) == 1
        assert await _zaehle(conn, "SELECT count(*) FROM invoices WHERE mollie_payment_id=$1", pay) == 1
    assert _alarme(caplog, pay, "!= invoice total") == 1


async def test_parallele_webhooks_nettobasis_genau_ein_ereignis(db, caplog):
    from src.billing import billing_service
    from src.billing.mollie_adapter import reset_mollie_adapter

    reset_mollie_adapter()
    _uid, sub, _erst, netto = await _netto_bestandsabo(db, "AT")
    pay = _folgezahlung(sub, netto)
    caplog.set_level(logging.WARNING)
    res = await _parallel(lambda: billing_service.handle_webhook(pay))
    assert all(isinstance(r, dict) and r.get("handled") for r in res), res
    async with db.acquire() as conn:
        assert await _zaehle(
            conn, "SELECT count(*) FROM billing_events WHERE mollie_payment_id=$1 "
            "AND event_type='invoice.net_basis_vat_from_payment'", pay) == 1
        assert await _zaehle(conn, "SELECT count(*) FROM invoices WHERE mollie_payment_id=$1", pay) == 1
    assert _alarme(caplog, pay, "ALARM subscription_recurring") == 1


async def test_migration_071_nachtrag_vergibt_schluessel_nur_der_aeltesten_zeile(db):
    """Altbestand mit doppelten Ereignissen (vor 071 moeglich): genau eine Zeile
    je Schluessel bekommt ihn, die uebrigen bleiben unveraendert stehen."""
    pay = "tr_alt071_" + uuid.uuid4().hex[:8]
    async with db.acquire() as conn:
        for _ in range(3):
            await conn.execute(
                "INSERT INTO billing_events (event_type, mollie_payment_id, source, payload) "
                "VALUES ('payment.amount_mismatch', $1, 'test', '{}'::jsonb)", pay)
        await conn.execute(
            "INSERT INTO billing_events (event_type, mollie_payment_id, source, payload) "
            "VALUES ('payment.refund_unbooked', $1, 'test', $2::jsonb)",
            pay, json.dumps({"refundedEur": "5.00", "chargedBackEur": "0.00"}))
        await conn.execute(MIGRATION_071.read_text())   # idempotent erneut
        zeilen = await conn.fetch(
            "SELECT event_type, ereignis_schluessel FROM billing_events WHERE mollie_payment_id=$1 "
            "ORDER BY timestamp, id", pay)
    assert len(zeilen) == 4
    mm = [z["ereignis_schluessel"] for z in zeilen if z["event_type"] == "payment.amount_mismatch"]
    assert mm == [f"payment.amount_mismatch:{pay}", None, None]
    rf = [z["ereignis_schluessel"] for z in zeilen if z["event_type"] == "payment.refund_unbooked"]
    assert rf == [f"payment.refund_unbooked:{pay}:5.00:0.00"]
    # und danach schreibt der Code kein zweites Ereignis zu dieser Zahlung
    from src.billing import billing_service
    neu = await billing_service.log_billing_event(
        "payment.amount_mismatch", mollie_payment_id=pay, source="test",
        ereignis_schluessel=billing_service.ereignis_schluessel("payment.amount_mismatch", pay))
    assert neu is False
