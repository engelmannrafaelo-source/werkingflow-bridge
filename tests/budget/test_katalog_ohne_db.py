"""
Ein Worker ohne BRIDGE_DB_URL laedt den Plankatalog ueber die platform-api.

Befund 24.09.2026 (Haus Dev, Prod): die vier Prod-Worker laufen ohne
BRIDGE_DB_URL. main.lifespan lud den Katalog nur im DB-Zweig
(reload_plans() → plans-Tabelle), PLANS blieb leer, und _deduct_call_cost
endete fuer JEDEN Aufruf mit "app not in the plan catalog" auf DEBUG. Kein
einziger POST /v1/budget/deduct verliess die Worker — fuer alle Apps, nicht
nur Report. Gemessen: job_prod_2d38585429534243858a96a391063f76
(werking-report, cost_eur 0.037633) → 0 Zeilen in budget_deductions.

Diese Tests verlangen: Katalog ohne DB geladen, danach geht der Abzug als
POST hinaus; ein leerer oder kaputter Katalog laesst den Start laut
scheitern; ein Abzug bei leerem Katalog meldet sich als ERROR.
"""
import logging
import uuid
from unittest.mock import AsyncMock, patch

import pytest

# Bridge-Module erst in den Tests importieren (siehe test_abzug_nicht_gebucht_laut).

USER = str(uuid.uuid4())

KATALOG = {"plans": [
    {"id": "trial", "appId": "werking-report", "name": "Trial", "priceEur": 0,
     "interval": "month", "apiBudgetEur": 5, "description": "", "trial": True},
    {"id": "report-standard", "appId": "werking-report", "name": "Standard", "priceEur": 49,
     "interval": "month", "apiBudgetEur": 100, "description": "", "trial": False},
    {"id": "energy-project", "appId": "werking-energy", "name": "Projekt", "priceEur": 290,
     "interval": "project", "apiBudgetEur": 100, "description": "", "trial": False},
]}


@pytest.fixture
def leerer_katalog(monkeypatch):
    """Worker-Lage: kein BRIDGE_DB_URL, PLANS leer. Stellt den Katalog danach wieder her."""
    from src.budget import plans

    monkeypatch.delenv("BRIDGE_DB_URL", raising=False)
    vorher = dict(plans.PLANS)
    plans.PLANS.clear()
    yield plans
    plans.PLANS.clear()
    plans.PLANS.update(vorher)


class _Plattform:
    """Nimmt call_platform-Aufrufe auf und beantwortet GET plans / POST deduct."""

    def __init__(self, katalog):
        self.katalog = katalog
        self.aufrufe = []

    async def __call__(self, method, path, **kw):
        from src.platform_client import PlatformResponse

        self.aufrufe.append((method, path, kw.get("json")))
        if method == "GET" and path == "/v1/billing/plans":
            return PlatformResponse(status_code=200, json=self.katalog)
        if method == "POST" and path == "/v1/budget/deduct":
            return PlatformResponse(status_code=200, json={
                "fromMonthly": kw["json"]["actualCostEur"], "fromTopUp": 0.0,
                "newMonthlyUsed": kw["json"]["actualCostEur"], "newTopUpBalance": 0.0,
                "effectivePlanId": kw["json"]["planId"], "duplicate": False})
        return PlatformResponse(status_code=404, json=None)


@pytest.mark.asyncio
async def test_ohne_db_laedt_der_worker_den_katalog_und_bucht_ab(leerer_katalog):
    from src.activity import ai_call_writer

    plattform = _Plattform(KATALOG)
    with patch("src.platform_client.call_platform", new=plattform):
        anzahl = await leerer_katalog.reload_plans_from_platform()
        assert anzahl == 3
        assert leerer_katalog.get_plan("report-standard").api_budget_eur == 100

        erledigt = await ai_call_writer._deduct_call_cost(
            USER, "werking-report", 0.037633, call_uid="call-mit-katalog")

    assert erledigt is True
    posts = [a for a in plattform.aufrufe if a[0] == "POST"]
    assert len(posts) == 1, plattform.aufrufe
    _, pfad, rumpf = posts[0]
    assert pfad == "/v1/budget/deduct"
    assert rumpf["planId"] == "report-standard"
    assert rumpf["actualCostEur"] == pytest.approx(0.037633)
    assert rumpf["idempotencyKey"] == "call-mit-katalog"


@pytest.mark.asyncio
@pytest.mark.parametrize("antwort", [
    (200, {"plans": []}),                # leer
    (200, {"falsch": True}),             # kaputte Form
    (503, None),                         # platform-api antwortet nicht sinnvoll
])
async def test_leerer_oder_kaputter_katalog_scheitert_laut(leerer_katalog, antwort):
    from src.platform_client import PlatformResponse

    status, rumpf = antwort
    with patch("src.platform_client.call_platform",
               new=AsyncMock(return_value=PlatformResponse(status_code=status, json=rumpf))):
        with pytest.raises(RuntimeError):
            await leerer_katalog.reload_plans_from_platform()
    assert leerer_katalog.PLANS == {}


@pytest.mark.asyncio
async def test_abzug_bei_leerem_katalog_ist_ein_error(leerer_katalog, caplog):
    from src.activity import ai_call_writer

    caplog.set_level(logging.DEBUG)
    plattform = _Plattform(KATALOG)
    with patch("src.platform_client.call_platform", new=plattform):
        erledigt = await ai_call_writer._deduct_call_cost(
            USER, "werking-report", 0.037633, call_uid="call-leerer-katalog")

    assert erledigt is True
    assert [a for a in plattform.aufrufe if a[0] == "POST"] == []
    fehler = [r for r in caplog.records
              if r.levelno >= logging.ERROR and "post-call deduction NOT APPLIED" in r.getMessage()]
    assert len(fehler) == 1, [(r.levelname, r.getMessage()) for r in caplog.records]
    assert "catalog_empty" in fehler[0].getMessage()
