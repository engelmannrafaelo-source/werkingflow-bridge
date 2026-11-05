import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.account_policy.model import initial_policy, validate, choose
from src.account_policy import client, routes, store
from src.api_auth.deps import AuthClaims, require_jwt_or_service

def test_rank_and_explicit_selection_do_not_change_each_other():
    p = initial_policy()
    assert choose(p, {"erk", "engelmann"}) == "erk"
    assert choose(p, {"erk", "engelmann"}, "engelmann") == "engelmann"
    assert choose(p, {"erk", "engelmann"}) == "erk"
    p["accounts"][0]["priority"], p["accounts"][4]["priority"] = 5, 1
    assert choose(p, {"sahori", "engelmann"}) == "engelmann"

def test_disabled_selection_never_falls_back():
    p=initial_policy()
    p["accounts"][3]["enabled"]=False
    assert choose(p, {"erk","sahori"}, "erk", strict=True) is None
    assert choose(p, {"erk","sahori"}, "erk") == "sahori"
    assert choose(p, {"erk","sahori"}) == "sahori"
    for r in p["accounts"]: r["enabled"]=False
    assert choose(p, set(r["accountId"] for r in p["accounts"])) is None

@pytest.mark.parametrize("change", [
    lambda p:p["accounts"].pop(),
    lambda p:p["accounts"][0].update(house="dev"),
    lambda p:p["accounts"][0].update(accountId="foreign"),
    lambda p:p["accounts"][0].update(enabled=1),
    lambda p:p["accounts"][0].update(priority=True),
    lambda p:p["accounts"][0].update(priority=2),
    lambda p:p.update(revision=True),
])
def test_malformed_or_foreign_policy_rejected(change):
    p=initial_policy();change(p)
    with pytest.raises(ValueError):validate(p)

@pytest.mark.asyncio
async def test_unconfigured_authority_fails_closed(monkeypatch):
    monkeypatch.delenv("BRIDGE_ACCOUNT_POLICY_URL",raising=False)
    with pytest.raises(client.PolicyUnavailable):await client.fetch_policy()

def test_route_user_rejected_and_operator_cas(monkeypatch):
    app=FastAPI();app.include_router(routes.router)
    monkeypatch.setenv("BRIDGE_ACCOUNT_POLICY_AUTHORITY","1")
    ordinary=AuthClaims("user","u","u@example.test","t",False,"u")
    operator=AuthClaims("service",None,None,None,True,None)
    app.dependency_overrides[require_jwt_or_service]=lambda:ordinary
    c=TestClient(app)
    assert c.get("/v1/admin/bridge-account-policy").status_code==403
    assert c.patch("/v1/admin/bridge-account-policy",json=initial_policy()).status_code==403
    app.dependency_overrides[require_jwt_or_service]=lambda:operator
    monkeypatch.setattr(store,"read_policy",AsyncMock(return_value=initial_policy()))
    assert c.get("/v1/admin/bridge-account-policy").json()["revision"]==0
    monkeypatch.setattr(store,"replace_policy",AsyncMock(side_effect=store.Conflict))
    assert c.patch("/v1/admin/bridge-account-policy",json=initial_policy()).status_code==409
    monkeypatch.setenv("BRIDGE_ACCOUNT_POLICY_AUTHORITY","0")
    assert c.get("/v1/admin/bridge-account-policy").status_code==503

@pytest.mark.asyncio
async def test_sandbox_fresh_star_read_and_explicit_no_fallback(monkeypatch):
    from src.sandbox import account_router as ar
    p=initial_policy()
    monkeypatch.setattr(client,"fetch_policy",AsyncMock(side_effect=lambda:deepcopy(p)))
    monkeypatch.setattr(ar,"_PROD_POOL_STATE_URL","")
    state={x:{"available":True,"headroom_percent":40,"cooldown_remaining_s":0,"usage_known":True} for x in ["sahori","erk"]}
    monkeypatch.setattr(ar,"_load_state",AsyncMock(return_value=state))
    assert (await ar.pick_account()).account_id=="sahori"
    p["accounts"][0]["enabled"]=False
    assert (await ar.pick_account()).account_id=="erk"
    picked = await ar.pick_account("sahori")
    assert picked.account_id == "erk"
    assert picked.selection_reason == "preferred_unavailable_fallback"
    # Existing lease heartbeat/release are separate, unchanged endpoints.

@pytest.mark.asyncio
async def test_runtime_merges_houses_without_foreign_accounts(monkeypatch):
    from src.account_policy.runtime import runtime_snapshot
    class Response:
        status_code=200
        def __init__(self,url):self.url=url
        def json(self):
            return {"accounts":{"erk":{"available":True},"engelmann":{"available":True},"foreign":{"available":True}}}
    class Client:
        def __init__(self,*a,**k):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def get(self,url,**k):return Response(url)
    monkeypatch.setenv("BRIDGE_ACCOUNT_POLICY_PARTNER_POOL_URL","https://partner.test/pool")
    monkeypatch.setenv("BRIDGE_ACCOUNT_POLICY_DEV_POOL_URL","https://dev.test/pool")
    monkeypatch.setattr("src.account_policy.runtime.httpx.AsyncClient",Client)
    p=initial_policy();p["accounts"][3]["enabled"]=False
    r=await runtime_snapshot(p)
    assert "foreign" not in r["accounts"]
    assert r["accounts"]["erk"]["available"] is True
    assert r["accounts"]["erk"]["bridge_enabled"] is False
    assert r["accounts"]["engelmann"]["house"]=="dev"
    assert r["accounts"]["sahori"]["available"] is False
