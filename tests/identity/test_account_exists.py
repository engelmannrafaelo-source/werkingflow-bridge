import uuid
from unittest.mock import AsyncMock,patch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.identity import account_exists as target

@pytest.fixture
def client():
 app=FastAPI();app.include_router(target.router,prefix='/v1/auth');target._windows.clear()
 with TestClient(app) as c:yield c

@pytest.fixture
def principal():
 return {'id':'reader-1','allowed_apps':[target.APP],'allowed_paths':[target.PATH]}

HEADERS={'Authorization':'Bearer test-reader','X-App-ID':target.APP}

@pytest.mark.parametrize('uid,expected',[(None,False),(uuid.uuid4(),True)])
def test_boolean_only(client,principal,uid,expected):
 with patch.object(target,'get_principal_row_by_hash',AsyncMock(return_value=principal)),patch.object(target,'lookup_user_id_by_email',AsyncMock(return_value=uid)) as lookup:
  r=client.post(target.PATH,headers=HEADERS,json={'email':'RAFAEL@werking.at'})
 assert r.status_code==200 and r.json()=={'exists':expected}
 assert r.headers['cache-control']=='no-store';lookup.assert_awaited_once_with('rafael@werking.at')

@pytest.mark.parametrize('headers',[{}, {'Authorization':'Bearer test-reader','X-App-ID':'wrong-app'}])
def test_auth_before_lookup(client,principal,headers):
 with patch.object(target,'get_principal_row_by_hash',AsyncMock(return_value=principal)),patch.object(target,'lookup_user_id_by_email',AsyncMock()) as lookup:
  r=client.post(target.PATH,headers=headers,json={'email':'x@example.com'})
 assert r.status_code in [401,403];lookup.assert_not_awaited()

@pytest.mark.parametrize('field,value',[('allowed_apps',['*']),('allowed_paths',['*']),('allowed_paths',['/v1/chat/completions']),('allowed_apps',['werking-report'])])
def test_scope_is_explicit(client,principal,field,value):
 principal[field]=value
 with patch.object(target,'get_principal_row_by_hash',AsyncMock(return_value=principal)),patch.object(target,'lookup_user_id_by_email',AsyncMock()) as lookup:
  r=client.post(target.PATH,headers=HEADERS,json={'email':'x@example.com'})
 assert r.status_code==403;lookup.assert_not_awaited()

def test_revocation_rechecked(client,principal):
 with patch.object(target,'get_principal_row_by_hash',AsyncMock(side_effect=[principal,None])),patch.object(target,'lookup_user_id_by_email',AsyncMock(return_value=None)) as lookup:
  assert client.post(target.PATH,headers=HEADERS,json={'email':'x@example.com'}).status_code==200
  assert client.post(target.PATH,headers=HEADERS,json={'email':'x@example.com'}).status_code==401
 assert lookup.await_count==1

@pytest.mark.parametrize('where',['principal','identity'])
def test_db_failure_never_false(client,principal,where):
 with patch.object(target,'get_principal_row_by_hash',AsyncMock(return_value=principal,side_effect=RuntimeError('private details') if where=='principal' else None)),patch.object(target,'lookup_user_id_by_email',AsyncMock(side_effect=RuntimeError('private details'))):
  r=client.post(target.PATH,headers=HEADERS,json={'email':'x@example.com'})
 assert r.status_code==503;assert 'private details' not in r.text;assert 'exists' not in r.json()

def test_rate_bound_before_identity_lookup(client,principal):
 with patch.object(target,'get_principal_row_by_hash',AsyncMock(return_value=principal)),patch.object(target,'lookup_user_id_by_email',AsyncMock(return_value=None)) as lookup:
  for _ in range(30):assert client.post(target.PATH,headers=HEADERS,json={'email':'x@example.com'}).status_code==200
  r=client.post(target.PATH,headers=HEADERS,json={'email':'x@example.com'})
 assert r.status_code==429 and r.headers['retry-after']=='60';assert lookup.await_count==30

@pytest.mark.parametrize('body',[{'email':'bad'}, {'email':'x@example.com','extra':'no'}])
def test_bad_body(client,principal,body):
 with patch.object(target,'get_principal_row_by_hash',AsyncMock(return_value=principal)),patch.object(target,'lookup_user_id_by_email',AsyncMock()) as lookup:
  r=client.post(target.PATH,headers=HEADERS,json=body)
 assert r.status_code==422;lookup.assert_not_awaited()
