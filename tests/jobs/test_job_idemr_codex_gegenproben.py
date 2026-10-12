"""IDEMR — Codex-Gegenproben (local-storage/idemr-review-20261012/
test_idemr_review.py), unveraendert uebernommen als Abnahmetests."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from tests.jobs.test_job_idempotenz_brref2 import mem, pg_store, _post, _app, KEY, BODY
from src.jobs import routes, store_client, store

async def test_replay_survives_capacity_lock(mem, monkeypatch):
    first = await _post(key=KEY)
    from src.middleware import capacity_lock
    lock = SimpleNamespace(is_locked=lambda _: True, remaining_s=lambda _: 60,
                           get_lock_info=lambda _: {'reason':'session'})
    monkeypatch.setattr(capacity_lock, 'get_capacity_lock', lambda: lock)
    monkeypatch.setattr(routes, '_job_runs_off_pool', AsyncMock(return_value=False))
    replay = await _post(key=KEY)
    assert replay.status_code == 200, (first.json(), replay.status_code, replay.json())

async def test_real_auth_separates_principals(mem, monkeypatch):
    from src import auth, principals
    monkeypatch.setattr(routes, 'verify_api_key', auth.verify_api_key)
    monkeypatch.setattr(auth.auth_manager, 'get_api_key', lambda:'review-shared')
    monkeypatch.setattr(auth, '_principals_enabled', lambda:True)
    async def resolve(token):
        if token not in ('alice','bob'): return None
        return SimpleNamespace(name=token, may_use_app=lambda _:True, may_use_path=lambda _:True)
    monkeypatch.setattr(principals, 'resolve_principal_by_token', resolve)
    a=await _post(key=KEY, headers={'Authorization':'Bearer alice','X-App-ID':'app'})
    b=await _post(key=KEY, headers={'Authorization':'Bearer bob','X-App-ID':'app'})
    c=await _post(key=KEY, headers={'Authorization':'Bearer stranger','X-App-ID':'app'})
    assert (a.status_code,b.status_code,c.status_code)==(201,201,401)
    assert a.json()['job_id']!=b.json()['job_id']

async def test_other_user_cannot_learn_job_from_guessed_key(mem, pg_store, monkeypatch):
    monkeypatch.setattr(store_client, "create_job_idempotent", store.create_job_idempotent)
    monkeypatch.setattr(store_client, "get_job", store.get_job)
    from src import auth
    monkeypatch.setattr(routes, 'verify_api_key', auth.verify_api_key)
    monkeypatch.setattr(auth.auth_manager, 'get_api_key', lambda:'review-shared')
    monkeypatch.setattr(auth, '_principals_enabled', lambda:False)
    ha={'Authorization':'Bearer review-shared','X-App-ID':'app','X-User-ID':'alice'}
    hb={**ha,'X-User-ID':'bob'}
    first=await _post(key=KEY,headers=ha)
    jid=first.json()['job_id']
    probe=await _post(key=KEY,headers=hb,body={**BODY,'payload':{'unknown':'body'}})
    leaked=probe.json().get('error',{}).get('job_id')
    async with pg_store.acquire() as conn:
        await conn.execute("UPDATE ai_jobs SET status='done', result=$1::jsonb WHERE job_id=$2", '{"private":"alice result"}', jid)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()),base_url='http://stub') as c:
        read=await c.get('/v1/jobs/'+str(leaked),headers=hb)
    print('OTHER_USER_READ', probe.status_code, leaked, read.status_code, read.json())
    assert leaked != jid, (probe.status_code, probe.json(), read.status_code, read.json())

async def test_nested_digest_key_order(mem):
    a=await _post(key=KEY,body={**BODY,'payload':{'a':{'x':1,'y':2},'b':[2,1]}})
    b=await _post(key=KEY,body={**BODY,'payload':{'b':[2,1],'a':{'y':2,'x':1}}})
    c=await _post(key=KEY,body={**BODY,'payload':{'b':[1,2],'a':{'y':2,'x':1}}})
    assert (a.status_code,b.status_code,c.status_code)==(201,200,422)

async def test_owner_encoding_separates_valid_principal_app_pairs(mem, monkeypatch):
    from src import auth, principals
    monkeypatch.setattr(routes, 'verify_api_key', auth.verify_api_key)
    monkeypatch.setattr(auth.auth_manager, 'get_api_key', lambda:'review-shared')
    monkeypatch.setattr(auth, '_principals_enabled', lambda:True)
    async def resolve(token):
        name={'token-one':'service|app','token-two':'service'}[token]
        return SimpleNamespace(name=name, may_use_app=lambda _:True, may_use_path=lambda _:True)
    monkeypatch.setattr(principals, 'resolve_principal_by_token', resolve)
    a=await _post(key=KEY,headers={'Authorization':'Bearer token-one','X-App-ID':'report'})
    b=await _post(key=KEY,headers={'Authorization':'Bearer token-two','X-App-ID':'app|report'})
    assert b.status_code == 201 and a.json()['job_id']!=b.json()['job_id'], (a.json(),b.status_code,b.json())
