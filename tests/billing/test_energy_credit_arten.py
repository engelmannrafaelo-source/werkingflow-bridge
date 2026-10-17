"""
Energy-Credit-Arten (Migration 066/067, Rafael-Entscheid 17.10.2026):
150 / 300 / 600 EUR netto je Berichtsart statt einer Credit-Art.

Ohne Datenbank: Katalog, Topf-Aufloesung, Kauf-Sperre, Verbrauchsreihenfolge
und Migrationsweg der Bestandsguthaben. Der Lauf gegen einen echten,
voll migrierten Postgres steht in test_energy_credit_arten_echte_db.py.
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")
os.environ.setdefault("BRIDGE_PUBLIC_URL", "https://test.bridge.local")

import pytest

from src.billing.project_credits_service import (
    CreditsExhaustedError,
    consume_credit,
    get_available_credits,
    usable_credits_by_plan,
)
from src.budget import plans
from src.budget.plan_resolution import resolve_billing_plan
from src.budget.plans import (
    PLANS,
    AmbiguousPlanCatalog,
    PlanConfig,
    PlanNotSellable,
    accepted_credit_plan_ids,
    assert_catalog_is_unambiguous,
    find_project_plans_for_app,
    require_sellable,
)
from src.platform_client import PlatformResponse

KINDS = {
    "energy-anlagendokumentation": 150.0,
    "energy-objektbericht": 300.0,
    "energy-analysebericht": 600.0,
}


def _plan(pid, price, *, app="werking-energy", interval="project", budget_plan_id=None,
          sellable=True, trial=False):
    return PlanConfig(
        id=pid, app_id=app, name=pid, price=price, interval=interval,
        api_budget_eur=100, description="x", trial=trial, price_basis="net",
        sellable=sellable, budget_plan_id=budget_plan_id,
    )


def _catalog():
    c = {
        "report-standard": _plan("report-standard", 300, app="werking-report", interval="month"),
        "report-check-credit-1": _plan("report-check-credit-1", 19, app="werking-check"),
        "report-check-credit-5": _plan("report-check-credit-5", 15, app="werking-check"),
        "energy-project": _plan("energy-project", 600, sellable=False),
    }
    for pid, price in KINDS.items():
        c[pid] = _plan(pid, price, budget_plan_id="energy-project")
    return c


@pytest.fixture(autouse=True)
def katalog():
    before = dict(PLANS)
    PLANS.clear()
    PLANS.update(_catalog())
    yield
    PLANS.clear()
    PLANS.update(before)


# --------------------------------------------------------------------------
# Katalog
# --------------------------------------------------------------------------

def test_katalog_preise_je_berichtsart_netto():
    for pid, price in KINDS.items():
        p = PLANS[pid]
        assert p.price == price
        assert p.price_basis == "net"
        assert p.sellable is True
        assert p.budget_plan_id == "energy-project"


@pytest.mark.asyncio
async def test_katalog_endpoint_zeigt_netto_kaufbarkeit_und_topf():
    from src.billing.routes import list_plans

    body = await list_plans()
    by_id = {p["id"]: p for p in body["plans"]}
    for pid, price in KINDS.items():
        assert by_id[pid]["priceEur"] == price
        assert by_id[pid]["priceBasis"] == "net"
        assert by_id[pid]["sellable"] is True
        assert by_id[pid]["budgetPlanId"] == "energy-project"
    assert by_id["energy-project"]["sellable"] is False
    assert by_id["energy-project"]["budgetPlanId"] is None


def test_energy_hat_weiter_genau_einen_projekt_topf():
    """Credit-Arten sind keine Toepfe — sonst waeren unallokierte Energy-Calls
    (Wizard vor dem Start, Lazy-Allokation) ab sofort PlanResolutionError."""
    assert [p.id for p in find_project_plans_for_app("werking-energy")] == ["energy-project"]
    # werking-check (mehrere Toepfe ohne budget_plan_id) bleibt unveraendert
    assert len(find_project_plans_for_app("werking-check")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("project_id", ["proj-wizard", None])
async def test_unallokierter_energy_call_bleibt_auf_dem_topf(project_id):
    with patch(
        "src.billing.project_budgets_service.resolve_allocated_plan_id",
        AsyncMock(return_value=None),
    ):
        plan = await resolve_billing_plan("werking-energy", uuid.uuid4(), project_id)
    assert plan is not None and plan.id == "energy-project"


def test_credit_art_mit_falschem_topf_scheitert_beim_laden():
    PLANS["energy-objektbericht"] = _plan(
        "energy-objektbericht", 300, budget_plan_id="report-standard"
    )
    with pytest.raises(AmbiguousPlanCatalog):
        assert_catalog_is_unambiguous()


def test_credit_art_mit_fehlendem_topf_scheitert_beim_laden():
    del PLANS["energy-project"]
    with pytest.raises(AmbiguousPlanCatalog):
        assert_catalog_is_unambiguous()


def test_seed_katalog_ist_eindeutig():
    assert_catalog_is_unambiguous()


@pytest.mark.asyncio
async def test_worker_katalog_ohne_neue_felder_startet_nicht():
    """Ein Worker, der die Felder still auf Default setzte, hielte Credit-Arten
    fuer Toepfe — er muss beim Laden laut scheitern."""
    row = {"id": "energy-objektbericht", "appId": "werking-energy", "name": "x",
           "priceEur": 300, "interval": "project", "apiBudgetEur": 100,
           "description": "x", "trial": False}
    with patch("src.db.client.is_db_enabled", return_value=False), patch(
        "src.platform_client.call_platform",
        new=AsyncMock(return_value=PlatformResponse(200, {"plans": [row]})),
    ):
        with pytest.raises(plans.PlanCatalogUnavailable):
            await plans.reload_plans()


@pytest.mark.asyncio
async def test_worker_katalog_traegt_die_neuen_felder():
    rows = [
        {"id": "energy-project", "appId": "werking-energy", "name": "x", "priceEur": 600,
         "interval": "project", "apiBudgetEur": 100, "description": "x", "trial": False,
         "priceBasis": "net", "sellable": False, "budgetPlanId": None},
        {"id": "energy-objektbericht", "appId": "werking-energy", "name": "x", "priceEur": 300,
         "interval": "project", "apiBudgetEur": 100, "description": "x", "trial": False,
         "priceBasis": "net", "sellable": True, "budgetPlanId": "energy-project"},
    ]
    with patch("src.db.client.is_db_enabled", return_value=False), patch(
        "src.platform_client.call_platform",
        new=AsyncMock(return_value=PlatformResponse(200, {"plans": rows})),
    ):
        assert await plans.reload_plans() == 2
    assert PLANS["energy-objektbericht"].budget_plan_id == "energy-project"
    assert PLANS["energy-project"].sellable is False


# --------------------------------------------------------------------------
# Kaufen
# --------------------------------------------------------------------------

def test_alte_credit_art_ist_nicht_mehr_kaeuflich():
    with pytest.raises(PlanNotSellable) as e:
        require_sellable("energy-project")
    assert "energy-objektbericht" in str(e.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("pid", sorted(KINDS))
async def test_kauf_je_art_legt_bestellung_der_art_zum_artpreis_an(pid):
    from src.billing import billing_service

    from decimal import Decimal

    create = AsyncMock(return_value={"id": uuid.uuid4(), "invoiceId": str(uuid.uuid4())})
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=True)  # Bestandskunde
    # Rechnungsentwurf der Bestellung: Netto = Artpreis, Brutto = +20 % (AT).
    netto = Decimal(str(KINDS[pid]))
    conn.fetchrow = AsyncMock(return_value={"subtotal_eur": netto, "total_eur": netto * Decimal("1.2")})

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    mollie = MagicMock()
    mollie.create_one_time_payment = AsyncMock(
        return_value={"paymentId": "tr_x", "checkoutUrl": "https://x"}
    )
    with patch.object(billing_service, "get_pool", return_value=pool), patch.object(
        billing_service, "_assert_complete_billing_address", AsyncMock()
    ), patch(
        "src.billing.pending_orders_service.create_pending_order", create
    ), patch.object(
        billing_service.purchase_consent, "record_consent", AsyncMock()
    ), patch.object(
        billing_service, "get_or_create_customer",
        AsyncMock(return_value={"mollieCustomerId": "cust"}),
    ), patch.object(billing_service, "get_mollie_adapter", return_value=mollie), patch.object(
        billing_service, "config", MagicMock(mollie_webhook_url="https://test/wh"),
    ):
        await billing_service.start_project_pack_checkout(
            str(uuid.uuid4()), pid, 1, "https://x/ok", "u@x", "U", consent=MagicMock(),
        )
    assert create.await_args.args[1] == pid
    kw = mollie.create_one_time_payment.await_args.kwargs
    # Mollie zieht das Brutto der Rechnung ein (Rafael 17.10.2026): Artpreis netto + USt.
    assert kw["amount_eur"] == round(KINDS[pid] * 1.2, 2)
    assert kw["metadata"]["planId"] == pid


@pytest.mark.asyncio
async def test_kauf_der_alten_art_wird_vor_jeder_bestellung_abgelehnt():
    from src.billing import billing_service

    create = AsyncMock()
    with patch("src.billing.pending_orders_service.create_pending_order", create):
        with pytest.raises(ValueError, match="no longer for sale"):
            await billing_service.start_project_pack_checkout(
                str(uuid.uuid4()), "energy-project", 1, "https://x/ok", "u@x", "U",
                consent=MagicMock(),
            )
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_bestellung_auf_rechnung_der_alten_art_wird_abgelehnt():
    from src.billing.pending_orders_service import create_pending_order

    with pytest.raises(ValueError, match="no longer for sale"):
        await create_pending_order(str(uuid.uuid4()), "energy-project", 1)


# --------------------------------------------------------------------------
# Verbrauchen + Migrationsweg Bestandsguthaben
# --------------------------------------------------------------------------

def test_akzeptierte_credits_eigene_art_zuerst_dann_altguthaben():
    for pid in KINDS:
        assert accepted_credit_plan_ids(pid) == (pid, "energy-project")
    assert accepted_credit_plan_ids("energy-project") == ("energy-project",)
    assert accepted_credit_plan_ids("report-check-credit-1") == ("report-check-credit-1",)


class _FakeCredits:
    """manual_project_credits in memory — genug SQL-Semantik fuer consume_credit."""

    def __init__(self, rows):
        self.rows = rows  # list of dicts
        self.allocations = []

    def pool(self):
        fake = self
        conn = MagicMock()

        async def fetchrow(sql, user_id, plan_id):
            cands = [r for r in fake.rows
                     if r["user_id"] == user_id and r["plan_id"] == plan_id and r["used"] < r["quantity"]]
            cands.sort(key=lambda r: r["granted_at"])
            return dict(cands[0]) if cands else None

        async def execute(sql, row_id):
            for r in fake.rows:
                if r["id"] == row_id:
                    r["used"] += 1

        conn.fetchrow = fetchrow
        conn.execute = execute

        @asynccontextmanager
        async def _tx():
            yield

        @asynccontextmanager
        async def _acquire():
            yield conn

        conn.transaction = _tx
        pool = MagicMock()
        pool.acquire = _acquire
        return pool


def _row(user, plan_id, *, quantity=1, used=0, age_days=0):
    return {
        "id": uuid.uuid4(), "user_id": user, "plan_id": plan_id, "tenant_id": "t1",
        "quantity": quantity, "used": used, "order_id": uuid.uuid4(),
        "granted_at": datetime.now(timezone.utc) - timedelta(days=age_days),
    }


async def _consume(fake, user, pid, project_id=None):
    alloc = AsyncMock(return_value=True)
    with patch("src.billing.project_credits_service.get_pool", return_value=fake.pool()), patch(
        "src.billing.project_budgets_service.allocate_budget", alloc
    ):
        result = await consume_credit(user, pid, project_id)
    return result, alloc


@pytest.mark.asyncio
@pytest.mark.parametrize("pid", sorted(KINDS))
async def test_verbrauch_je_art_nimmt_die_eigene_art(pid):
    user = uuid.uuid4()
    others = [k for k in KINDS if k != pid]
    fake = _FakeCredits([_row(user, o, age_days=9) for o in others]
                        + [_row(user, "energy-project", age_days=30), _row(user, pid)])
    result, alloc = await _consume(fake, user, pid, project_id="proj-1")
    assert result["planId"] == pid
    assert result["requestedPlanId"] == pid
    assert result["legacyCredit"] is False
    # Budget unter dem Topf, nie unter der Credit-Art
    assert alloc.await_args.kwargs["plan_id"] == "energy-project"
    assert alloc.await_args.kwargs["project_id"] == "proj-1"
    assert alloc.await_args.kwargs["limit_eur"] == 100.0
    # fremde Arten und Altguthaben unberuehrt
    assert all(r["used"] == 0 for r in fake.rows if r["plan_id"] != pid)


@pytest.mark.asyncio
@pytest.mark.parametrize("pid", sorted(KINDS))
async def test_altguthaben_gilt_fuer_jede_art(pid):
    user = uuid.uuid4()
    legacy = _row(user, "energy-project", quantity=3, used=1)
    fake = _FakeCredits([legacy])
    result, alloc = await _consume(fake, user, pid, project_id="proj-alt")
    assert result["planId"] == "energy-project"
    assert result["requestedPlanId"] == pid
    assert result["legacyCredit"] is True
    assert legacy["used"] == 2
    assert alloc.await_args.kwargs["plan_id"] == "energy-project"


@pytest.mark.asyncio
async def test_fremde_art_wird_nie_verbraucht():
    """Ein Objektbericht-Credit bezahlt keinen Analysebericht (300 < 600)."""
    user = uuid.uuid4()
    fake = _FakeCredits([_row(user, "energy-objektbericht"), _row(user, "energy-anlagendokumentation")])
    with pytest.raises(CreditsExhaustedError):
        await _consume(fake, user, "energy-analysebericht")
    assert all(r["used"] == 0 for r in fake.rows)


@pytest.mark.asyncio
async def test_alter_client_verbraucht_weiter_energy_project():
    """Energy-App vor dem Umbau ruft .../energy-project/consume — bis zum App-
    Deploy muss das unveraendert gehen (Bridge wird zuerst ausgerollt)."""
    user = uuid.uuid4()
    fake = _FakeCredits([_row(user, "energy-objektbericht"), _row(user, "energy-project")])
    result, _ = await _consume(fake, user, "energy-project")
    assert result["planId"] == "energy-project"
    assert result["legacyCredit"] is False
    assert [r["used"] for r in fake.rows] == [0, 1]


@pytest.mark.asyncio
async def test_unbekannte_art_ist_ein_aufruferfehler():
    with pytest.raises(ValueError):
        await consume_credit(uuid.uuid4(), "energy-gibtsnicht")


@pytest.mark.asyncio
async def test_verfuegbar_zaehlt_eigene_art_und_altguthaben():
    row = MagicMock()
    row.__getitem__ = lambda self, k: 4 if k == "available" else None
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    with patch("src.billing.project_credits_service.get_pool", return_value=pool):
        assert await get_available_credits(uuid.uuid4(), "energy-objektbericht") == 4
    assert conn.fetchrow.await_args.args[2] == ["energy-objektbericht", "energy-project"]


def test_nutzbar_je_plan_rechnet_altguthaben_jeder_art_zu():
    credits = [
        {"planId": "energy-project", "total": 3, "used": 1, "available": 2},
        {"planId": "energy-objektbericht", "total": 1, "used": 0, "available": 1},
    ]
    usable = usable_credits_by_plan(credits)
    assert usable == {
        "energy-project": 2,
        "energy-anlagendokumentation": 2,
        "energy-objektbericht": 3,
        "energy-analysebericht": 2,
    }
