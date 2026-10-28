import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException

from src.providers import credit_pool as client
from src.providers import credit_pool_routes as ledger


@pytest.fixture
def configured(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    data = {"accounts": [{"id": "werking", "org_id": "org-one", "api_key": "secret-test-key",
        "observed_at": now.isoformat(), "grants": [{"id": "grant-one", "granted_usd": 200,
        "remaining_usd": 200, "expires_at": (now + timedelta(days=3)).isoformat()}]}]}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_ACCOUNTS_FILE", str(path))
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_TOKEN", "test-internal-token")
    return path, data


def test_expired_and_stale_accounts_are_never_used(configured):
    path, data = configured
    assert len(ledger.manifest()) == 1
    data['accounts'][0]['observed_at'] = (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
    path.write_text(json.dumps(data)); assert ledger.manifest() == []
    data['accounts'][0]['observed_at'] = datetime.now(timezone.utc).isoformat()
    data['accounts'][0]['grants'][0]['expires_at'] = (datetime.now(timezone.utc)+timedelta(minutes=50)).isoformat()
    path.write_text(json.dumps(data)); assert ledger.manifest() == []


def test_price_upper_bound_and_incomplete_usage():
    reserved = ledger.reservation_cost('claude-haiku-5-5', 99_999, 20_000)
    for cache in [0, 5000]:
        charged = ledger.usage_cost('claude-haiku-5-5', {'input_tokens':99_999-cache,
            'output_tokens':20_000,'cache_read_input_tokens':0,'cache_creation_input_tokens':cache})
        assert charged <= reserved
    with pytest.raises(HTTPException):ledger.reservation_cost('unknown',10,10)
    with pytest.raises(HTTPException):ledger.usage_cost('claude-haiku-5-5',{'input_tokens':1})


def test_pool_vision_default_preserves_explicit_gemini(monkeypatch):
    from src.routing.vision_router import default_vision_target, resolve_vision_target
    from src.providers.registry import BackendType
    from types import SimpleNamespace
    monkeypatch.setenv('ANTHROPIC_CREDIT_POOL_URL','http://pool')
    monkeypatch.setenv('BRIDGE_VISION_DEFAULT_PROVIDER','gemini')
    assert default_vision_target()=='anthropic'
    assert resolve_vision_target(SimpleNamespace(backend=BackendType.GEMINI_API))=='gemini'


@pytest.mark.asyncio
async def test_private_routes_require_dedicated_token(configured):
    app=FastAPI();app.include_router(ledger.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
        r=await c.get('/v1/internal/api-credit-pool/accounts')
        assert r.status_code==403
        assert 'secret-test-key' not in r.text


@pytest_asyncio.fixture
async def db(configured, monkeypatch):
    dsn=os.getenv('CREDIT_POOL_TEST_DSN')
    if not dsn:pytest.skip('CREDIT_POOL_TEST_DSN required for real PostgreSQL concurrency tests')
    import asyncpg
    schema='credit_test_'+uuid4().hex
    admin=await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA {schema}')
    pool=await asyncpg.create_pool(dsn,min_size=1,max_size=20,server_settings={'search_path':schema})
    from pathlib import Path
    async with pool.acquire() as c:
        await c.execute(Path('docker/migrations/073_anthropic_credit_pool.sql').read_text())
    monkeypatch.setattr(ledger,'get_pool',lambda:pool)
    yield pool
    await pool.close();await admin.execute(f'DROP SCHEMA {schema} CASCADE');await admin.close()


def request(**kw):
    return ledger.Reserve(id=uuid4(),account_id='werking',grant_id='grant-one',
                         model='claude-fable-5-1',input_tokens=10, max_tokens=1_000_000,**kw)


@pytest.mark.asyncio
async def test_twenty_parallel_workers_cannot_double_spend(db):
    requests=[request() for _ in range(20)]
    results=await asyncio.gather(*(ledger.reserve(r) for r in requests),return_exceptions=True)
    assert sum(isinstance(r,dict) for r in results)==3
    assert all(isinstance(r,dict) or isinstance(r,HTTPException) and r.status_code==429 for r in results)
    status=await ledger.status();a=status['accounts'][0]
    assert a['requests']==3 and a['pending']==3 and a['charged_usd']<=198


@pytest.mark.asyncio
async def test_retry_and_settlement_are_idempotent(db):
    r=request();await ledger.reserve(r);await ledger.reserve(r)
    assert (await ledger.status())['accounts'][0]['requests']==1
    usage={'input_tokens':10,'output_tokens':100,'cache_read_input_tokens':0,'cache_creation_input_tokens':0}
    s=ledger.Settle(id=r.id,usage=usage)
    await ledger.settle(s);before=await ledger.status();await ledger.settle(s)
    assert await ledger.status()==before
    # A later contradictory release cannot erase a settled debit.
    await ledger.settle(ledger.Settle(id=r.id,rejected=True))
    assert await ledger.status()==before


@pytest.mark.asyncio
async def test_unknown_or_incomplete_usage_retains_full_reservation(db):
    r=request();await ledger.reserve(r)
    before=await ledger.status()
    with pytest.raises(HTTPException):await ledger.settle(ledger.Settle(id=r.id,usage={'input_tokens':1}))
    assert await ledger.status()==before
    await ledger.settle(ledger.Settle(id=r.id,rejected=True))
    assert (await ledger.status())['accounts'][0]['charged_usd']==0


@pytest.mark.asyncio
async def test_upstream_remaining_balance_can_reduce_admission(db,configured):
    path,data=configured
    data['accounts'][0]['grants'][0]['remaining_usd']=10
    path.write_text(json.dumps(data))
    with pytest.raises(HTTPException) as e:await ledger.reserve(request())
    assert e.value.status_code==429


@pytest.mark.asyncio
async def test_pool_failure_does_not_use_legacy_key(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_CREDIT_POOL_URL','http://pool')
    monkeypatch.setenv('ANTHROPIC_VISION_API_KEY','paid-key-must-not-be-used')
    async def coord(*args,**kwargs):return {'accounts':[]}
    monkeypatch.setattr(client,'coordinator',coord)
    from src.vision_provider import VisionProvider
    with pytest.raises(HTTPException) as e:
        await VisionProvider().analyze([{'role':'user','content':'test'}],model='claude-haiku-5-5')
    assert e.value.status_code==429


@pytest.mark.asyncio
async def test_count_reserve_generate_settle_order_and_uncertain_cost(monkeypatch):
    events=[]
    async def coord(method,path,data=None):
        events.append(path)
        if path=='/accounts':return {'accounts':[{'id':'working','org_id':'o','grant_id':'g','api_key':'test'}]}
        return {'state':'reserved'}
    async def handler(req):
        if req.url.path.endswith('count_tokens'):
            events.append('count');return httpx.Response(200,json={'input_tokens':123})
        events.append('generate');raise httpx.ReadTimeout('response lost')
    original=httpx.AsyncClient
    monkeypatch.setattr(client,'coordinator',coord)
    monkeypatch.setattr(client.httpx,'AsyncClient',lambda **kw: original(transport=httpx.MockTransport(handler),**kw))
    with pytest.raises(httpx.ReadTimeout):
        await client.post_messages({'model':'claude-haiku-5-5','max_tokens':10,'messages':[]},10)
    assert events==['/accounts','count','/reserve','generate']


@pytest.mark.asyncio
async def test_only_rejected_requests_can_move_to_next_account(monkeypatch):
    events=[];calls=0
    async def coord(method,path,data=None):
        events.append((path,data))
        if path=='/accounts':return {'accounts':[{'id':s,'org_id':s,'grant_id':s,'api_key':s} for s in ['one','two']]}
        return {'state':'reserved'}
    async def handler(req):
        nonlocal calls
        if req.url.path.endswith('count_tokens'):return httpx.Response(200,json={'input_tokens':123})
        calls+=1
        if calls==1:return httpx.Response(429,json={'error':'rate limited'})
        return httpx.Response(200,json={'usage':{'input_tokens':123,'output_tokens':2,'cache_read_input_tokens':0,'cache_creation_input_tokens':0},'content':[]})
    original=httpx.AsyncClient
    monkeypatch.setattr(client,'coordinator',coord)
    monkeypatch.setattr(client.httpx,'AsyncClient',lambda **kw: original(transport=httpx.MockTransport(handler),**kw))
    response,meta=await client.post_messages({'model':'claude-haiku-5-5','max_tokens':10,'messages':[]},10)
    assert response.status_code==200 and meta['credit_pool_account']=='two'
    settled=[d for p,d in events if p=='/settle']
    assert settled[0]['rejected'] is True and 'usage' in settled[1]
