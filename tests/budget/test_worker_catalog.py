from unittest.mock import AsyncMock, patch
import uuid
import pytest
from src.budget import plans
from src.platform_client import PlatformResponse, PlatformUnavailable

ROW = {"id":"report-standard","appId":"werking-report","name":"Report","priceEur":250,"interval":"month","apiBudgetEur":100,"description":"Report","trial":False}

@pytest.mark.asyncio
async def test_dbfree_worker_loads_catalog_and_deducts_monthly_call_once():
    from src.activity.ai_call_writer import _deduct_call_cost
    with patch("src.db.client.is_db_enabled", return_value=False), patch("src.platform_client.call_platform", new=AsyncMock(return_value=PlatformResponse(200, {"plans":[ROW]}))) as api:
        assert await plans.reload_plans() == 1
        api.assert_awaited_once_with("GET", "/v1/billing/plans", retries=2)
    with patch("src.budget.routes.apply_budget_deduction_via_platform", new=AsyncMock()) as deduct:
        uid=str(uuid.uuid4())
        assert await _deduct_call_cost(uid,"werking-report",0.037633,call_uid="catalog-regression") is True
        deduct.assert_awaited_once_with(uuid.UUID(uid),"report-standard",0.037633,idempotency_key="catalog-regression")

@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(404,{}),(200,{}),(200,{"plans":[]}),(200,{"plans":[{}]})])
async def test_missing_or_invalid_remote_catalog_fails_without_erasing_current_cache(status,body):
    before=dict(plans.PLANS)
    with patch("src.db.client.is_db_enabled",return_value=False), patch("src.platform_client.call_platform",new=AsyncMock(return_value=PlatformResponse(status,body))):
        with pytest.raises(plans.PlanCatalogUnavailable): await plans.reload_plans()
    assert plans.PLANS == before

@pytest.mark.asyncio
async def test_catalog_outage_refuses_startup():
    with patch("src.db.client.is_db_enabled",return_value=False), patch("src.platform_client.call_platform",new=AsyncMock(side_effect=PlatformUnavailable("offline"))):
        with pytest.raises(PlatformUnavailable): await plans.reload_plans()

@pytest.mark.asyncio
async def test_unloaded_catalog_keeps_keyed_deduction_owed():
    from src.activity.ai_call_writer import _deduct_call_cost
    plans.PLANS.clear()
    assert await _deduct_call_cost(str(uuid.uuid4()),"werking-report",0.04,call_uid="owed") is False


def test_unknown_app_is_distinct_from_unloaded_catalog():
    assert plans.find_monthly_plan_for_app("unbilled-internal-app") is None
    plans.PLANS.clear()
    with pytest.raises(plans.PlanCatalogUnavailable): plans.find_monthly_plan_for_app("werking-report")
