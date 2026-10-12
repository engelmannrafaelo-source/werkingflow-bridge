"""IDEMR R2 — Codex-Gegenproben (local-storage/idemr-r2-review-20261012/
test_idemr_r2_review.py), unveraendert uebernommen als Abnahmetests."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
from fastapi import FastAPI
from tests.jobs.test_job_idempotenz_brref2 import mem, pg_store, _post, _app, KEY, BODY
from src.jobs import routes, store, store_client

async def wire_real_store_auth(monkeypatch):
    from src import auth, principals
    for name in ('create_job_idempotent','find_job_by_idempotency_key','get_job','list_jobs','cancel_job'):
        monkeypatch.setattr(store_client,name,getattr(store,name))
    monkeypatch.setattr(routes,'verify_api_key',auth.verify_api_key)
    monkeypatch.setattr(auth.auth_manager,'get_api_key',lambda:'review-shared')
    monkeypatch.setattr(auth,'_principals_enabled',lambda:True)
    async def resolve(token):
        if token not in ('service-a','service-b'): return None
        return SimpleNamespace(name=token,may_use_app=lambda app:app=='app',may_use_path=lambda _:True)
    monkeypatch.setattr(principals,'resolve_principal_by_token',resolve)

async def test_keyed_job_cannot_be_cancelled_by_other_principal(mem,pg_store,monkeypatch):
    await wire_real_store_auth(monkeypatch)
    a={'Authorization':'Bearer service-a','X-App-ID':'app','X-User-ID':'alice'}
    b={**a,'Authorization':'Bearer service-b'}
    jid=(await _post(key=KEY,headers=a)).json()['job_id']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()),base_url='http://stub') as c:
        get=await c.get('/v1/jobs/'+jid,headers=b)
        delete=await c.delete('/v1/jobs/'+jid,headers=b)
    row=await store.get_job(jid)
    print('FOREIGN_PRINCIPAL',get.status_code,delete.status_code,row['status'])
    assert get.status_code==404
    assert delete.status_code==404 and row['status']=='pending'

async def test_job_list_obeys_user_ownership(mem,pg_store,monkeypatch):
    await wire_real_store_auth(monkeypatch)
    a={'Authorization':'Bearer service-a','X-App-ID':'app','X-User-ID':'alice'}
    b={**a,'X-User-ID':'bob'}
    jid=(await _post(key=KEY,headers=a)).json()['job_id']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()),base_url='http://stub') as c:
        get=await c.get('/v1/jobs/'+jid,headers=b)
        listing=await c.get('/v1/jobs?app_id=app',headers=b)
    print('FOREIGN_LIST',get.status_code,listing.status_code,listing.json())
    assert get.status_code==404
    assert jid not in str(listing.json())

async def test_internal_lookup_requires_actual_service_token(monkeypatch):
    from src import internal_routes
    from src.api_auth import deps
    monkeypatch.setattr(deps,'config',SimpleNamespace(service_token='review-internal-only'))
    lookup=AsyncMock(return_value={'job_id':'synthetic-held','kind':'k','status':'pending'})
    monkeypatch.setattr(store,'find_job_by_idempotency_key',lookup)
    app=FastAPI(); app.include_router(internal_routes.router)
    headers=[{}, {'Authorization':'Bearer review-shared'}, {'X-Bridge-Service-Token':'wrong'},
             {'X-Bridge-Service-Token':'review-internal-only'}]
    body={'kind':'k','payload':{},'idempotency_owner':'owner','idempotency_key':'key'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://stub') as c:
        rs=[await c.post('/v1/internal/jobs/idempotency-lookup',json=body,headers=h) for h in headers]
    print('LOOKUP_AUTH',[r.status_code for r in rs])
    assert [r.status_code for r in rs]==[401,401,401,200]
    assert lookup.await_count==1

async def test_public_worker_has_no_internal_lookup(mem):
    from src.main import app
    assert not any(getattr(r,'path','')=='/v1/internal/jobs/idempotency-lookup' for r in app.routes)

async def test_dynamic_admission_is_deferred_for_both_keyed_and_unkeyed(mem,monkeypatch):
    from src.middleware import capacity_lock, adaptive_limiter
    monkeypatch.setattr(capacity_lock,'get_capacity_lock',lambda:SimpleNamespace(is_locked=lambda _:False))
    # Full dynamic capacity but NO quota lock: these are distinct gates.
    reject=AsyncMock(return_value=(False,'no capacity',{'effective_cap_tokens':100,'current_in_flight_tokens':100}))
    monkeypatch.setattr(adaptive_limiter.AdaptiveLoadLimiter,'can_accept',reject)
    rs=[await _post(key='saturated-'+str(i)) for i in range(20)]
    print('SATURATED_SUBMITS',[r.status_code for r in rs], 'rows',len(mem.rows),'dispatches',len(mem.spawned),'admission_calls',reject.await_count)
    keyed_count=len(mem.spawned)
    plain=[await _post() for i in range(20)]
    print('UNKEYED_COMPARISON',[r.status_code for r in plain], 'additional_dispatches',len(mem.spawned)-keyed_count)
    assert keyed_count==20 and len(mem.spawned)==40
    assert all(r.status_code==201 for r in rs) and all(r.status_code==200 for r in plain)
    assert reject.await_count==0
