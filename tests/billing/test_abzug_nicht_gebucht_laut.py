"""
Ein Aufruf, dessen Kosten NICHT abgebucht werden, muss im Log auffallen.

Befund 24.09.2026: ein echter werking-report-Lauf auf Prod
(job_prod_2d38585429534243858a96a391063f76, cost_eur 0.037633) und zwei
Gegenproben hinterliessen 0 Zeilen in budget_deductions. report-standard wurde
seit Migration 061 nie abgebucht, energy-project dagegen 281-mal. Welcher
Ausgang von _deduct_call_cost gegriffen hat, war nicht feststellbar: "kein Plan
im Katalog" loggte auf DEBUG, "Abzug abgelehnt" auf INFO — beides steht in
einem Prod-Log auf WARNING-Niveau nicht drin. Der Aufruf lief, bezahlt hat
niemand, und nichts hat es gesagt.

Jeder Ausgang, der bezahlte Arbeit ungebucht laesst, meldet sich deshalb als
WARNING mit dem festen Praefix "post-call deduction NOT APPLIED" und Grund,
App, Nutzer, Plan, Betrag und Aufruf-Kennung.
"""
import logging
import uuid
from unittest.mock import AsyncMock, patch

import pytest

# Imports der Bridge-Module bewusst IN den Tests (wie in test_ledger_durability):
# auf Modulebene zieht das Einsammeln sie vor die Tests, die BRIDGE_SERVICE_TOKEN
# setzen, und reisst dort rund 30 fremde Testdateien mit.

USER = str(uuid.uuid4())
PRAEFIX = "post-call deduction NOT APPLIED"


def _writer():
    from src.activity import ai_call_writer
    return ai_call_writer


def _monatsplan():
    from src.budget.plans import PlanConfig
    return PlanConfig(id="report-standard", app_id="werking-report", name="S", price=0,
                      interval="month", api_budget_eur=100, description="", trial=False)


def _warnungen(caplog) -> list:
    return [r for r in caplog.records
            if r.levelno >= logging.WARNING and PRAEFIX in r.getMessage()]


@pytest.mark.asyncio
async def test_kein_plan_im_katalog_ist_eine_warnung(caplog):
    caplog.set_level(logging.DEBUG)
    with patch("src.budget.plan_resolution.resolve_billing_plan", new=AsyncMock(return_value=None)):
        erledigt = await _writer()._deduct_call_cost(
            USER, "werking-report", 0.037633, call_uid="call-ohne-plan")

    assert erledigt is True  # ein richtiger Ausgang, kein geschuldeter Abzug
    w = _warnungen(caplog)
    assert len(w) == 1, [r.getMessage() for r in caplog.records]
    text = w[0].getMessage()
    for teil in ("no_plan", "werking-report", USER, "0.037633", "call-ohne-plan"):
        assert teil in text, text


@pytest.mark.asyncio
async def test_abgelehnter_abzug_ist_eine_warnung(caplog):
    from src.budget.routes import BudgetDeductionDenied

    caplog.set_level(logging.DEBUG)
    with (
        patch("src.budget.plan_resolution.resolve_billing_plan", new=AsyncMock(return_value=_monatsplan())),
        patch("src.budget.routes.apply_budget_deduction_via_platform",
              new=AsyncMock(side_effect=BudgetDeductionDenied("unlicensed"))),
    ):
        erledigt = await _writer()._deduct_call_cost(
            USER, "werking-report", 0.037633, call_uid="call-abgelehnt")

    assert erledigt is True
    w = _warnungen(caplog)
    assert len(w) == 1, [r.getMessage() for r in caplog.records]
    text = w[0].getMessage()
    for teil in ("denied:unlicensed", "werking-report", USER, "report-standard", "0.037633", "call-abgelehnt"):
        assert teil in text, text


@pytest.mark.asyncio
async def test_gebuchter_abzug_meldet_nichts(caplog):
    caplog.set_level(logging.DEBUG)
    with (
        patch("src.budget.plan_resolution.resolve_billing_plan", new=AsyncMock(return_value=_monatsplan())),
        patch("src.budget.routes.apply_budget_deduction_via_platform", new=AsyncMock()) as via,
    ):
        erledigt = await _writer()._deduct_call_cost(
            USER, "werking-report", 0.037633, call_uid="call-gebucht")

    assert erledigt is True
    assert via.await_count == 1
    assert _warnungen(caplog) == []


@pytest.mark.asyncio
async def test_ungeladener_katalog_ohne_schluessel_ist_ein_error(caplog):
    """a66cae6: ein ungeladener Katalog wirft PlanCatalogUnavailable; mit
    Aufruf-Kennung bleibt der Abzug geschuldet (Spool). OHNE Kennung spielt ihn
    niemand nach — dann ist er verloren und muss als ERROR auffallen."""
    from src.budget import plans

    caplog.set_level(logging.DEBUG)
    with patch.dict(plans.PLANS, {}, clear=True):
        erledigt = await _writer()._deduct_call_cost(USER, "werking-report", 0.037633)

    assert erledigt is True
    fehler = [r for r in caplog.records
              if r.levelno >= logging.ERROR and PRAEFIX in r.getMessage()]
    assert len(fehler) == 1, [(r.levelname, r.getMessage()) for r in caplog.records]
    assert "catalog_unavailable" in fehler[0].getMessage()


@pytest.mark.asyncio
async def test_ungeladener_katalog_mit_schluessel_bleibt_geschuldet(caplog):
    from src.budget import plans

    caplog.set_level(logging.DEBUG)
    with patch.dict(plans.PLANS, {}, clear=True):
        erledigt = await _writer()._deduct_call_cost(
            USER, "werking-report", 0.037633, call_uid="call-geschuldet")

    assert erledigt is False  # Spool spielt nach, nicht verloren
    assert _warnungen(caplog) == []
