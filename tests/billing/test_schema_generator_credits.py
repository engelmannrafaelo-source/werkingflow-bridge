import os
os.environ.setdefault('BRIDGE_JWT_SECRET', 'test-secret-for-unit-tests')
os.environ.setdefault('BRIDGE_SERVICE_TOKEN', 'test-service-token')
import time
import uuid
from unittest.mock import AsyncMock
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.billing import schema_generator_credits as module

UID = str(uuid.uuid4())
PROJECT = 'schema-' + str(uuid.uuid4())

@pytest.fixture
def setup(monkeypatch):
    principal = {'id':'test-principal','allowed_apps':[module.APP],'allowed_paths':[module.PATH]}
    lookup = AsyncMock(return_value=principal)
    consume = AsyncMock(return_value={'creditId':str(uuid.uuid4()),'planId':module.PLANS[0],'orderId':str(uuid.uuid4())})
    monkeypatch.setattr(module,'get_principal_row_by_hash',lookup)
    monkeypatch.setattr(module,'consume_once',consume)
    app=FastAPI();app.include_router(module.router)
    return TestClient(app), principal, consume, lookup

def headers(**claims):
    payload={'sub':UID,'exp':int(time.time())+300,'email':'fixture@example.com','role':'user',**claims}
    return {'Authorization':'Bearer '+jwt.encode(payload,os.environ['BRIDGE_JWT_SECRET'],algorithm='HS256'),
            'X-Schema-Generator-Key':'fixture-app-key','X-App-ID':module.APP}

def test_valid_self_jwt_and_principal_no_cache(setup):
    client,_,consume,_=setup
    result=client.post(module.PATH,headers=headers(),json={'project_id':PROJECT})
    assert result.status_code==200,result.text
    assert result.headers['Cache-Control']=='no-store'
    consume.assert_awaited_once_with(uuid.UUID(UID),PROJECT)

@pytest.mark.parametrize('which',['missing-jwt','expired-jwt','missing-app-key','wrong-app','wildcard','missing-path','revoked'])
def test_auth_denials_never_consume(setup,which):
    client,p,consume,lookup=setup;h=headers()
    if which=='missing-jwt':h.pop('Authorization')
    if which=='expired-jwt':h=headers(exp=int(time.time())-10)
    if which=='missing-app-key':h.pop('X-Schema-Generator-Key')
    if which=='wrong-app':h['X-App-ID']='werking-energy'
    if which=='wildcard':p['allowed_paths']=['*',module.PATH]
    if which=='missing-path':p['allowed_paths']=['/v1/chat/completions']
    if which=='revoked':lookup.return_value=None
    assert client.post(module.PATH,headers=h,json={'project_id':PROJECT}).status_code in (401,403)
    consume.assert_not_awaited()

def test_x_user_id_cannot_change_billed_user(setup):
    client,_,consume,_=setup;h=headers();h['X-User-ID']=str(uuid.uuid4())
    assert client.post(module.PATH,headers=h,json={'project_id':PROJECT}).status_code==200
    consume.assert_awaited_once_with(uuid.UUID(UID),PROJECT)

@pytest.mark.parametrize('extra',[{'user_id':str(uuid.uuid4())},{'plan_id':'energy-project'},{'hard_cap':True},{'project_id':'foreign-project'}])
def test_no_caller_identity_plan_or_budget_overrides(setup,extra):
    client,_,consume,_=setup
    assert client.post(module.PATH,headers=headers(),json={'project_id':PROJECT,**extra}).status_code==422
    consume.assert_not_awaited()

@pytest.mark.parametrize('failure,status',[(module.CreditsExhaustedError('report-check-credit'),402),(RuntimeError('private internal failure'),503)])
def test_fail_closed_with_sanitized_error(setup,failure,status):
    client,_,consume,_=setup;consume.side_effect=failure
    r=client.post(module.PATH,headers=headers(),json={'project_id':PROJECT})
    assert r.status_code==status
    assert 'private internal' not in r.text
