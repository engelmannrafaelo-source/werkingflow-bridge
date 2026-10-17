"""Energy-Credit-Arten gegen einen ECHTEN, voll migrierten Postgres.

Prueft, was die Attrappen in test_energy_credit_arten.py nicht sehen koennen:
dass 066/067 auf dem Bestandsschema laufen, dass der Katalog aus der Tabelle
die Staffel 150/300/600 netto traegt, und dass Kauf (FakeMollie + Webhook) und
Verbrauch je Art samt Bestandsguthaben echtes SQL ueberstehen.

Braucht `BRIDGE_TEST_MIGRATED_PG_URL` — eine Wegwerf-Datenbank, auf die ALLE
docker/migrations/*.sql angewendet sind (Rezept in ERGEBNIS.md: Container
postgres:16-alpine, jede Datei mit `psql -1 -f`). Ohne sie wird der Test
ausdruecklich als uebersprungen gemeldet, nie still gruen. Nie gegen eine
geteilte oder produktive Datenbank richten: der Test schreibt Mandanten,
Nutzer, Bestellungen und Credits.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

PG_URL = os.environ.get("BRIDGE_TEST_MIGRATED_PG_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="BRIDGE_TEST_MIGRATED_PG_URL fehlt — Credit-Arten nicht gegen echten Postgres geprueft",
)


@pytest.fixture
async def db(monkeypatch):
    monkeypatch.setenv("BRIDGE_DB_URL", PG_URL)
    monkeypatch.setenv("BRIDGE_USE_FAKE_MOLLIE", "true")
    monkeypatch.setenv("BRIDGE_PUBLIC_URL", "https://test.bridge.local")
    monkeypatch.setenv("BRIDGE_JWT_SECRET", "test-secret-for-integration")
    monkeypatch.setenv("BRIDGE_SERVICE_TOKEN", "test-service-token")
    from src.db import client as dbc
    from src.budget import plans

    before = dict(plans.PLANS)
    await dbc.init_pool()
    await plans.reload_plans()
    try:
        yield dbc.get_pool()
    finally:
        await dbc.close_pool()
        plans.PLANS.clear()
        plans.PLANS.update(before)


def _consent():
    from src.billing.purchase_consent import PurchaseConsentIn

    return PurchaseConsentIn(
        termsVersion="test", actingAsBusiness=True, professionallyQualified=True,
        immediateStartRequested=True, acceptedAt=datetime.now(timezone.utc),
    )


async def _bestandskunde_mit_altguthaben(conn, slots: int):
    """Mandant + Nutzer mit einer freigegebenen 'energy-project'-Bestellung und
    `slots` Bestands-Credits der alten Art — der Zustand vor 066/067."""
    tid = "t_" + uuid.uuid4().hex[:10]
    await conn.execute(
        """INSERT INTO tenants (id, name, account_type, billing_name, billing_street,
             billing_city, billing_postcode, billing_country)
           VALUES ($1,'Test GmbH','customer','Test GmbH','Teststrasse 1','Wien','1010','AT')""",
        tid,
    )
    uid = await conn.fetchval(
        "INSERT INTO users (email, name, tenant_id, role) VALUES ($1,'Owner',$2,'owner') RETURNING id",
        f"u-{uuid.uuid4().hex[:8]}@test.local", tid,
    )
    inv = await conn.fetchval(
        """INSERT INTO invoices (invoice_number, user_id, tenant_id, subtotal_eur, tax_eur, total_eur, status)
           VALUES ($1,$2,$3,600,120,720,'paid') RETURNING id""",
        f"INV-{uuid.uuid4().hex[:8]}", uid, tid,
    )
    order = await conn.fetchval(
        """INSERT INTO pending_orders (user_id, tenant_id, plan_id, quantity, total_price_eur, status, invoice_id)
           VALUES ($1,$2,'energy-project',$3,600,'released',$4) RETURNING id""",
        uid, tid, slots, inv,
    )
    await conn.execute(
        """INSERT INTO manual_project_credits (user_id, tenant_id, plan_id, quantity, used, order_id)
           VALUES ($1,$2,'energy-project',$3,0,$4)""",
        uid, tid, slots, order,
    )
    return uid, tid


async def test_katalog_aus_der_tabelle(db):
    from src.billing.routes import list_plans

    by_id = {p["id"]: p for p in (await list_plans())["plans"]}
    assert {k: by_id[k]["priceEur"] for k in (
        "energy-anlagendokumentation", "energy-objektbericht", "energy-analysebericht"
    )} == {"energy-anlagendokumentation": 150.0, "energy-objektbericht": 300.0,
           "energy-analysebericht": 600.0}
    for k in ("energy-anlagendokumentation", "energy-objektbericht", "energy-analysebericht"):
        assert by_id[k]["priceBasis"] == "net"
        assert by_id[k]["sellable"] is True
        assert by_id[k]["budgetPlanId"] == "energy-project"
    assert by_id["energy-project"]["sellable"] is False
    assert by_id["energy-project"]["priceEur"] == 600.0  # Startdaten = Live-Stand


async def test_kauf_verbrauch_und_altguthaben_end_to_end(db):
    from src.billing import billing_service
    from src.billing.project_credits_service import (
        CreditsExhaustedError, consume_credit, get_available_credits,
        list_user_credits, usable_credits_by_plan,
    )

    async with db.acquire() as conn:
        uid, _ = await _bestandskunde_mit_altguthaben(conn, slots=1)
    u = uuid.UUID(str(uid))

    # Kauf: ein Objektbericht-Credit, Self-Service-Paket, FakeMollie zahlt sofort.
    with patch("src.billing.billing_service.send_email", AsyncMock(), create=True):
        checkout = await billing_service.start_project_pack_checkout(
            str(uid), "energy-objektbericht", 1, "https://x/ok", "u@test.local", "Owner",
            consent=_consent(),
        )
        res = await billing_service.handle_webhook(checkout["paymentId"])
    assert res.get("handled"), res
    async with db.acquire() as conn:
        bestellt = await conn.fetchrow(
            "SELECT plan_id, total_price_eur FROM pending_orders WHERE user_id=$1 AND payment_method='mollie'",
            u,
        )
    assert bestellt["plan_id"] == "energy-objektbericht"
    assert float(bestellt["total_price_eur"]) == 300.0

    # Bestand: 1 Objektbericht-Credit + 1 Alt-Credit, der fuer jede Art gilt.
    assert await get_available_credits(u, "energy-objektbericht") == 2
    assert await get_available_credits(u, "energy-analysebericht") == 1
    assert await get_available_credits(u, "energy-anlagendokumentation") == 1
    usable = usable_credits_by_plan(await list_user_credits(u))
    assert usable["energy-objektbericht"] == 2 and usable["energy-analysebericht"] == 1

    # Verbrauch Objektbericht: die eigene Art zuerst, Budget unter dem Topf.
    r1 = await consume_credit(u, "energy-objektbericht", "proj-objekt")
    assert (r1["planId"], r1["legacyCredit"]) == ("energy-objektbericht", False)
    # Verbrauch Analysebericht: nur das Altguthaben passt.
    r2 = await consume_credit(u, "energy-analysebericht", "proj-analyse")
    assert (r2["planId"], r2["legacyCredit"]) == ("energy-project", True)
    # Anlagendokumentation: nichts mehr da — kein Griff auf fremde Arten.
    with pytest.raises(CreditsExhaustedError):
        await consume_credit(u, "energy-anlagendokumentation", "proj-doku")

    async with db.acquire() as conn:
        budgets = await conn.fetch(
            "SELECT project_id, plan_id, limit_eur FROM project_budgets WHERE user_id=$1 ORDER BY project_id",
            u,
        )
    assert [(b["project_id"], b["plan_id"], float(b["limit_eur"])) for b in budgets] == [
        ("proj-analyse", "energy-project", 100.0),
        ("proj-objekt", "energy-project", 100.0),
    ]

    # Topf-Aufloesung fuer allokierte und unallokierte Energy-Calls: energy-project.
    from src.budget.plan_resolution import resolve_billing_plan
    from src.billing.project_budgets_service import find_allocated_plan_id

    with patch(
        "src.billing.project_budgets_service.resolve_allocated_plan_id",
        new=find_allocated_plan_id,
    ):
        assert (await resolve_billing_plan("werking-energy", u, "proj-objekt")).id == "energy-project"
        assert (await resolve_billing_plan("werking-energy", u, "proj-wizard")).id == "energy-project"


async def test_alte_art_ist_nicht_mehr_kaeuflich(db):
    from src.billing import billing_service

    async with db.acquire() as conn:
        uid, _ = await _bestandskunde_mit_altguthaben(conn, slots=1)
        vorher = await conn.fetchval("SELECT count(*) FROM pending_orders WHERE user_id=$1", uid)
    with pytest.raises(ValueError, match="no longer for sale"):
        await billing_service.start_project_pack_checkout(
            str(uid), "energy-project", 1, "https://x/ok", "u@test.local", "Owner",
            consent=_consent(),
        )
    async with db.acquire() as conn:
        nachher = await conn.fetchval("SELECT count(*) FROM pending_orders WHERE user_id=$1", uid)
    assert nachher == vorher
