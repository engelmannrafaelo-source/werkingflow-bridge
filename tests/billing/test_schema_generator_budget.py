"""Schema product retains identity and the existing monthly funding lane."""
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from src.budget.plans import PLANS, PlanConfig, assert_catalog_is_unambiguous, monthly_budget_plan, AmbiguousPlanCatalog
from src.budget.routes import evaluate_budget, apply_budget_deduction, BudgetDeductionDenied
from src.budget.calculator import UserBudget, MonthlyBudgetEntry
from src.budget.gate import enforce_budget
from src.billing.billing_service import _ensure_monthly_budget_pot, provision_subscription

UID = uuid.uuid4()
APP = 'werking-schema-generator'
PLAN = 'schema-generator'
POT = 'report-standard'
ANCHOR = '2099-11-30T00:00:00+00:00'

@pytest.fixture(autouse=True)
def catalog():
    saved = dict(PLANS)
    PLANS.clear()
    PLANS[POT] = PlanConfig(POT, 'werking-report', 'Report', 300, 'month', 100, '')
    PLANS[PLAN] = PlanConfig(PLAN, APP, 'WerkIng Schema-Generator', 0, 'month', 0, '', sellable=False, budget_plan_id=POT)
    yield
    PLANS.clear(); PLANS.update(saved)


def test_catalog_alias_and_original_pot():
    assert_catalog_is_unambiguous()
    assert monthly_budget_plan(PLANS[PLAN]) is PLANS[POT]

@pytest.mark.parametrize('changes', [dict(api_budget_eur=1000), dict(price=1), dict(sellable=True), dict(trial=True), dict(budget_plan_id=PLAN)])
def test_alias_cannot_create_allowance_or_checkout(changes):
    PLANS[PLAN] = replace(PLANS[PLAN], **changes)
    with pytest.raises(AmbiguousPlanCatalog): assert_catalog_is_unambiguous()

@pytest.mark.asyncio
async def test_check_uses_actual_1000_limit_not_catalog_100_and_real_user():
    budget = UserBudget(str(UID), {POT: MonthlyBudgetEntry(1000, 990, ANCHOR)}, [])
    with patch('src.budget.routes._load_budget_via_platform', AsyncMock(return_value=budget)) as load:
        ok = await evaluate_budget(UID, PLAN, 10)
        no = await evaluate_budget(UID, PLAN, 10.01)
    assert ok['allowed'] and not no['allowed']
    assert ok['effectivePlanId'] == POT
    assert all(c.args == (UID,) for c in load.call_args_list)
    assert budget.monthly_budgets[POT].used_eur == 990
    assert set(budget.monthly_budgets) == {POT}

@pytest.mark.asyncio
async def test_missing_pot_never_provisions_trial():
    with patch('src.budget.routes._load_budget_via_platform', AsyncMock(return_value=UserBudget(str(UID), {}, []))), patch('src.budget.routes._ensure_trial_via_platform', AsyncMock()) as provision:
        result = await evaluate_budget(UID, PLAN, 0)
    assert not result['allowed']
    assert result['reason'] == 'monthly_exceeded_no_topup'
    provision.assert_not_called()

@pytest.mark.asyncio
async def test_grant_retains_pot_without_write():
    conn = AsyncMock(); conn.fetchval.return_value = True
    await _ensure_monthly_budget_pot(conn, UID, PLAN)
    assert conn.fetchval.call_args.args[1:] == (UID, POT)
    conn.execute.assert_not_called()
    conn.fetchval.return_value = False
    with pytest.raises(ValueError): await _ensure_monthly_budget_pot(conn, UID, PLAN)
    conn.execute.assert_not_called()

@pytest.mark.asyncio
async def test_seed_endpoint_cannot_bypass_transactional_grant():
    with pytest.raises(ValueError, match='transactional operator grant'):
        await provision_subscription(str(UID), PLAN, 1)

@pytest.mark.asyncio
async def test_report_and_schema_deduct_one_locked_lane():
    state = {POT: {'limitEur': 1000, 'usedEur': 990, 'resetAt': ANCHOR}}
    conn = AsyncMock()
    async def fetch(*args): return {'monthly_budgets': json.loads(json.dumps(state))}
    async def execute(sql, raw, uid):
        assert uid == UID
        assert 'UPDATE user_budgets' in sql
        state.clear(); state.update(json.loads(raw))
    conn.fetchrow.side_effect = fetch; conn.execute.side_effect = execute
    @asynccontextmanager
    async def context(): yield conn
    conn.transaction = context
    pool = MagicMock(); pool.acquire = context
    with patch('src.budget.routes.get_pool', return_value=pool), patch('src.budget.routes._load_topup_lots', AsyncMock(return_value=[])), patch('src.budget.routes._persist_topup_lots', AsyncMock()):
        await apply_budget_deduction(UID, PLAN, 4)
        await apply_budget_deduction(UID, POT, 6)
        with pytest.raises(BudgetDeductionDenied): await apply_budget_deduction(UID, PLAN, .01)
    assert state == {POT: {'limitEur': 1000, 'usedEur': 1000, 'resetAt': ANCHOR}}
    assert all('FOR UPDATE' in c.args[0] for c in conn.fetchrow.call_args_list)

@pytest.mark.asyncio
async def test_schema_gate_fails_closed_without_identity_or_catalog():
    with pytest.raises(HTTPException) as e: await enforce_budget(None, APP, 0)
    assert e.value.status_code == 400
    PLANS.pop(PLAN)
    with pytest.raises(HTTPException) as e: await enforce_budget(str(UID), APP, 0)
    assert e.value.status_code == 503

@pytest.mark.asyncio
async def test_schema_gate_fails_closed_on_budget_read_error():
    with patch('src.budget.gate.resolve_user_id', AsyncMock(return_value=UID)), patch('src.budget.gate.resolve_billing_plan', AsyncMock(return_value=PLANS[PLAN])), patch('src.budget.gate.evaluate_budget', AsyncMock(side_effect=RuntimeError('synthetic unavailable'))):
        with pytest.raises(HTTPException) as e: await enforce_budget(str(UID), APP, 0)
    assert e.value.status_code == 503


@pytest.mark.asyncio
async def test_existing_registered_trial_is_reused_not_reset():
    PLANS['trial'] = PlanConfig('trial', 'werking-report', 'Trial', 0, 'month', 5, '', trial=True)
    budget = UserBudget(str(UID), {'trial': MonthlyBudgetEntry(5, 4, ANCHOR)}, [])
    with patch('src.budget.routes._load_budget_via_platform', AsyncMock(return_value=budget)), patch('src.budget.routes._ensure_trial_via_platform', AsyncMock()) as provision:
        ok = await evaluate_budget(UID, PLAN, 1)
        no = await evaluate_budget(UID, PLAN, 1.01)
    assert ok['allowed'] and ok['effectivePlanId'] == 'trial'
    assert not no['allowed']
    assert budget.monthly_budgets['trial'].used_eur == 4
    provision.assert_not_called()
