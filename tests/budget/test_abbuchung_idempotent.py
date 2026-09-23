"""
Abbuchung idempotent je Aufruf (Migration 061).

Befund 23.09.2026 (dev-Bridge, test-budget@test.werkingflow.com): ein bezahlter
werking-report-Aufruf steht mit 0,006598 EUR im Ledger, der Monatstopf blieb
bei usedEur 0.0. Die Abbuchung hatte keinen Dedup-Schluessel — darum durfte
der Worker sie nie wiederholen, und jede verlorene Antwort verlor sie still.

Diese Tests fahren die echten Abzugsfunktionen gegen eine Transaktions-
Attrappe, die ein Zurueckrollen wirklich zuruecknimmt: nur so ist messbar, dass
ein abgelehnter Abzug den Schluessel nicht verbrennt.
"""
from __future__ import annotations

import copy
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

USER = uuid.uuid4()
KEY = str(uuid.uuid4())


class _FakeDb:
    """Nur die SQL-Formen, die die beiden Abzugspfade wirklich schicken."""

    def __init__(self, *, limit_eur: float = 100.0, used_eur: float = 0.0):
        reset_at = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
        self.state = {
            "user_budgets": {
                USER: {"report-standard": {"limitEur": limit_eur, "usedEur": used_eur, "resetAt": reset_at}},
            },
            "project_budgets": {},
            "budget_deductions": {},
        }

    # ── pool / transaction ────────────────────────────────────────────
    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def _tx(self):
        snapshot = copy.deepcopy(self.state)
        try:
            yield
        except BaseException:
            self.state = snapshot
            raise

    def transaction(self):
        return self._tx()

    # ── queries ───────────────────────────────────────────────────────
    async def fetchval(self, sql, *a):
        assert "INSERT INTO budget_deductions" in sql, sql
        rows = self.state["budget_deductions"]
        if a[0] in rows:
            return None
        rows[a[0]] = {
            "scope": a[1], "user_id": a[2], "plan_id": a[3], "project_id": a[4],
            "amount_eur": Decimal(str(a[5])), "result": None,
        }
        return a[0]

    async def fetchrow(self, sql, *a):
        if "FROM budget_deductions" in sql:
            row = self.state["budget_deductions"].get(a[0])
            return dict(row) if row else None
        if "FROM user_budgets" in sql:
            mb = self.state["user_budgets"].get(a[0])
            return {"monthly_budgets": json.dumps(mb)} if mb is not None else None
        if "FROM user_topup_balances" in sql:
            return None
        if "FROM project_budgets" in sql:
            row = self.state["project_budgets"].get((a[0], a[1], a[2]))
            return dict(row) if row else None
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetch(self, sql, *a):
        assert "FROM user_topup_lots" in sql, sql
        return []

    async def execute(self, sql, *a):
        if "UPDATE user_budgets" in sql:
            self.state["user_budgets"][a[1]] = json.loads(a[0])
        elif "UPDATE budget_deductions" in sql:
            self.state["budget_deductions"][a[0]]["result"] = json.loads(a[1])
        elif "INSERT INTO project_budgets" in sql:
            self.state["project_budgets"].setdefault(
                (a[0], a[2], a[3]), {"limit_eur": Decimal(str(a[4])), "used_eur": Decimal("0")},
            )
        elif "UPDATE project_budgets" in sql:
            self.state["project_budgets"][(a[0], a[1], a[2])]["used_eur"] = Decimal(str(a[3]))
        else:
            raise AssertionError(f"unexpected execute: {sql}")

    # ── readouts ──────────────────────────────────────────────────────
    def monthly_used(self) -> float:
        return float(self.state["user_budgets"][USER]["report-standard"]["usedEur"])

    def project_used(self, plan: str, project: str) -> float:
        return float(self.state["project_budgets"][(USER, plan, project)]["used_eur"])


# ---------------------------------------------------------------------------
# Monatstopf — POST /v1/budget/deduct
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_monatsabzug_mit_gleichem_schluessel_bucht_nur_einmal():
    from src.budget.routes import apply_budget_deduction

    db = _FakeDb()
    with patch("src.budget.routes.get_pool", return_value=db):
        first = await apply_budget_deduction(USER, "report-standard", 0.25, idempotency_key=KEY)
        second = await apply_budget_deduction(USER, "report-standard", 0.25, idempotency_key=KEY)

    assert db.monthly_used() == pytest.approx(0.25)  # nicht 0.50
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["fromMonthly"] == first["fromMonthly"]
    assert second["newMonthlyUsed"] == first["newMonthlyUsed"]


@pytest.mark.asyncio
async def test_monatsabzug_ohne_schluessel_behaelt_den_alten_vertrag():
    """Bestehende Aufrufer ohne Schluessel: jeder Aufruf bucht (wie bisher)."""
    from src.budget.routes import apply_budget_deduction

    db = _FakeDb()
    with patch("src.budget.routes.get_pool", return_value=db):
        await apply_budget_deduction(USER, "report-standard", 0.25)
        await apply_budget_deduction(USER, "report-standard", 0.25)

    assert db.monthly_used() == pytest.approx(0.50)
    assert db.state["budget_deductions"] == {}


@pytest.mark.asyncio
async def test_abgelehnter_abzug_verbrennt_den_schluessel_nicht():
    from src.budget.routes import BudgetDeductionDenied, apply_budget_deduction

    db = _FakeDb(limit_eur=0.10, used_eur=0.10)  # Topf leer, keine Lots
    with patch("src.budget.routes.get_pool", return_value=db):
        with pytest.raises(BudgetDeductionDenied):
            await apply_budget_deduction(USER, "report-standard", 0.25, idempotency_key=KEY)

    assert KEY not in db.state["budget_deductions"]
    assert db.monthly_used() == pytest.approx(0.10)


@pytest.mark.asyncio
async def test_schluessel_fuer_einen_anderen_abzug_wird_laut_abgewiesen():
    """Derselbe Schluessel mit anderem Betrag ist ein Aufruferfehler — nie mit
    dem gespeicherten Ergebnis beantworten (das bestaetigte eine Buchung, die
    so nicht verlangt war), nie ein zweites Mal buchen."""
    from src.budget.deduction_keys import DeductionKeyConflict
    from src.budget.routes import apply_budget_deduction

    db = _FakeDb()
    with patch("src.budget.routes.get_pool", return_value=db):
        await apply_budget_deduction(USER, "report-standard", 0.25, idempotency_key=KEY)
        with pytest.raises(DeductionKeyConflict):
            await apply_budget_deduction(USER, "report-standard", 0.30, idempotency_key=KEY)

    assert db.monthly_used() == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_worker_schickt_schluessel_und_darf_dann_wiederholen():
    from src.budget.deduction_keys import DEDUCT_RETRIES_WITH_KEY
    from src.budget.routes import apply_budget_deduction_via_platform
    from src.platform_client import PlatformResponse

    calls = []

    async def fake_call_platform(method, path, **kw):
        calls.append((path, kw))
        return PlatformResponse(200, {"fromMonthly": 0.25, "duplicate": False})

    with patch("src.platform_client.call_platform", new=fake_call_platform):
        await apply_budget_deduction_via_platform(USER, "report-standard", 0.25, idempotency_key=KEY)
        await apply_budget_deduction_via_platform(USER, "report-standard", 0.25)

    keyed, keyless = calls
    assert keyed[1]["json"]["idempotencyKey"] == KEY
    assert keyed[1]["retries"] == DEDUCT_RETRIES_WITH_KEY > 0
    assert "idempotencyKey" not in keyless[1]["json"]
    assert keyless[1]["retries"] == 0


# ---------------------------------------------------------------------------
# Projekttopf — POST /v1/internal/project-budgets/deduct
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_projektabzug_mit_gleichem_schluessel_bucht_nur_einmal():
    from src.billing import project_budgets_service as svc

    db = _FakeDb()
    with patch.object(svc, "get_pool", return_value=db):
        first = await svc.deduct(
            USER, "report-check-credit", "chk-1", 0.4,
            allocate_limit_eur=5.0, tenant_id="t", idempotency_key=KEY,
        )
        second = await svc.deduct(
            USER, "report-check-credit", "chk-1", 0.4,
            allocate_limit_eur=5.0, tenant_id="t", idempotency_key=KEY,
        )

    assert db.project_used("report-check-credit", "chk-1") == pytest.approx(0.4)
    assert first["duplicate"] is False and second["duplicate"] is True
    assert second["deductedEur"] == first["deductedEur"]


@pytest.mark.asyncio
async def test_projektabzug_worker_schickt_schluessel():
    from src.billing import project_budgets_service as svc
    from src.budget.deduction_keys import DEDUCT_RETRIES_WITH_KEY
    from src.platform_client import PlatformResponse

    seen = {}

    async def fake_call_platform(method, path, **kw):
        seen.update(kw)
        return PlatformResponse(200, {"exists": True, "duplicate": False})

    with patch("src.platform_client.call_platform", new=fake_call_platform):
        await svc.deduct_via_platform(USER, "report-check-credit", "chk-1", 0.4, idempotency_key=KEY)

    assert seen["json"]["idempotency_key"] == KEY
    assert seen["retries"] == DEDUCT_RETRIES_WITH_KEY
