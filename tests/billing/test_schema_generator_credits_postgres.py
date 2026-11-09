"""Real isolated Postgres, loopback ephemeral port; never uses a configured DB."""
import asyncio
import json
import os
import pathlib
import subprocess
import sys
import time
import uuid

import asyncpg
import pytest

os.environ.setdefault('BRIDGE_JWT_SECRET','test-secret-for-unit-tests')
from src.billing import schema_generator_credits as module
from src.budget import plans

ROOT=pathlib.Path(__file__).resolve().parents[2]

def run(args,**kw):
    return subprocess.run(args,capture_output=True,text=True,check=True,**kw)

@pytest.fixture
async def pg(monkeypatch):
    name='schema-credit-test-'+uuid.uuid4().hex[:10]
    pool=None
    try:
        run(['docker','run','-d','--rm','--name',name,'-p','127.0.0.1::5432','-e','POSTGRES_HOST_AUTH_METHOD=trust','postgres:16-alpine'])
        port=run(['docker','port',name,'5432']).stdout.strip().split(':')[-1]
        dsn=f'postgresql://postgres@127.0.0.1:{port}/postgres'
        for _ in range(50):
            try:
                pool=await asyncpg.create_pool(dsn,min_size=1,max_size=6);break
            except (OSError,asyncpg.PostgresError):await asyncio.sleep(.1)
        assert pool is not None
        async with pool.acquire() as c:
            await c.execute('''CREATE TABLE users(id uuid PRIMARY KEY);
              CREATE TABLE service_principals(id text,name text,allowed_apps text[],allowed_paths text[],active boolean);
              INSERT INTO service_principals VALUES ('generator','schema-generator-dev',ARRAY['werking-schema-generator'],ARRAY['/v1/auth/account-exists','/v1/chat/completions'],true);
              CREATE TABLE manual_project_credits(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),user_id uuid,tenant_id text,plan_id varchar,quantity int,used int DEFAULT 0 CHECK(used<=quantity),granted_at timestamptz DEFAULT now(),order_id uuid);
              CREATE TABLE project_budgets(id uuid PRIMARY KEY DEFAULT gen_random_uuid(),user_id uuid,tenant_id text,plan_id varchar,project_id text,limit_eur numeric,used_eur numeric,credit_id uuid,hard_cap boolean,updated_at timestamptz DEFAULT now(),UNIQUE(user_id,plan_id,project_id));''')
            sql=(ROOT/'docker/migrations/078_schema_generator_credit_receipts.sql').read_text()
            await c.execute(sql);await c.execute(sql)
            assert await c.fetchval("SELECT cardinality(allowed_paths) FROM service_principals WHERE id='generator'")==3
        monkeypatch.setattr(module,'get_pool',lambda:pool)
        catalog={p:plans.PlanConfig(id=p,app_id='werking-check',name=p,price=1,interval='project',api_budget_eur=5,description='fixture') for p in module.PLANS}
        monkeypatch.setattr(plans,'PLANS',catalog)
        yield pool,dsn
    finally:
        if pool:await pool.close()
        subprocess.run(['docker','rm','-f',name],capture_output=True)

async def seed(pool,quantity=5,plan=None):
    uid=uuid.uuid4()
    async with pool.acquire() as c:
        await c.execute('INSERT INTO users VALUES($1)',uid)
        credit=await c.fetchval('INSERT INTO manual_project_credits(user_id,tenant_id,plan_id,quantity,order_id) VALUES($1,$2,$3,$4,$5) RETURNING id',uid,'fixture',plan or module.PLANS[0],quantity,uuid.uuid4())
    return uid,credit

async def test_parallel_retry_process_restart_and_plan_change(pg):
    pool,dsn=pg;uid,credit=await seed(pool);project='schema-'+str(uuid.uuid4())
    results=await asyncio.gather(*(module.consume_once(uid,project) for _ in range(4)))
    assert all(r==results[0] for r in results)
    async with pool.acquire() as c:
        assert await c.fetchval('SELECT used FROM manual_project_credits WHERE id=$1',credit)==1
        assert await c.fetchval('SELECT count(*) FROM schema_generator_credit_receipts')==1
        assert await c.fetchval('SELECT count(*) FROM project_budgets')==1
    # New process has no catalog/cache and opens its own connection pool.
    # Existing receipt must remain authoritative even after catalog changes.
    child='''import asyncio,asyncpg,json,sys,uuid
from src.billing import schema_generator_credits as m
async def main():
 p=await asyncpg.create_pool(sys.argv[1]);m.get_pool=lambda:p
 try:print(json.dumps(await m.consume_once(uuid.UUID(sys.argv[2]),sys.argv[3])))
 finally:await p.close()
asyncio.run(main())'''
    out=await asyncio.to_thread(run,[sys.executable,'-c',child,dsn,str(uid),project],cwd=ROOT)
    assert json.loads(out.stdout)==results[0]
    async with pool.acquire() as c:assert await c.fetchval('SELECT used FROM manual_project_credits WHERE id=$1',credit)==1

async def test_exhausted_first_plan_uses_fixed_fallback_and_user_isolation(pg):
    pool,_=pg;uid,credit=await seed(pool,plan=module.PLANS[1]);project='schema-'+str(uuid.uuid4())
    result=await module.consume_once(uid,project)
    assert result['planId']==module.PLANS[1]
    other=uuid.uuid4()
    async with pool.acquire() as c:await c.execute('INSERT INTO users VALUES($1)',other)
    with pytest.raises(module.CreditsExhaustedError):await module.consume_once(other,project)
    async with pool.acquire() as c:assert await c.fetchval('SELECT used FROM manual_project_credits WHERE id=$1',credit)==1

async def test_budget_and_receipt_failures_roll_back_credit_atomically(pg,monkeypatch):
    pool,_=pg;uid,credit=await seed(pool);project='schema-'+str(uuid.uuid4())
    from src.billing import project_budgets_service as budgets
    original=budgets.allocate_budget
    async def broken(*a,**k):raise RuntimeError('test allocator failure')
    monkeypatch.setattr(budgets,'allocate_budget',broken)
    with pytest.raises(RuntimeError):await module.consume_once(uid,project)
    async with pool.acquire() as c:
        assert await c.fetchval('SELECT used FROM manual_project_credits WHERE id=$1',credit)==0
        assert await c.fetchval('SELECT count(*) FROM schema_generator_credit_receipts')==0
        await c.execute('ALTER TABLE schema_generator_credit_receipts ADD CONSTRAINT fail_test CHECK(false) NOT VALID')
    monkeypatch.setattr(budgets,'allocate_budget',original)
    with pytest.raises(asyncpg.CheckViolationError):await module.consume_once(uid,project)
    async with pool.acquire() as c:
        assert await c.fetchval('SELECT used FROM manual_project_credits WHERE id=$1',credit)==0
        assert await c.fetchval('SELECT count(*) FROM project_budgets')==0
        await c.execute('ALTER TABLE schema_generator_credit_receipts DROP CONSTRAINT fail_test')
    await module.consume_once(uid,project)
    async with pool.acquire() as c:assert await c.fetchval('SELECT used FROM manual_project_credits WHERE id=$1',credit)==1

async def test_migration_rejects_null_revoked_and_unexpected_scope(pg):
    pool,_=pg
    sql=(ROOT/'docker/migrations/078_schema_generator_credit_receipts.sql').read_text()
    async with pool.acquire() as c:
        for change in ["allowed_paths=NULL", "active=NULL", "active=false", "allowed_paths=ARRAY['*']", "allowed_apps=ARRAY['other']"]:
            await c.execute("UPDATE service_principals SET active=true,allowed_apps=ARRAY['werking-schema-generator'],allowed_paths=ARRAY['/v1/auth/account-exists','/v1/chat/completions'] WHERE id='generator'")
            await c.execute("UPDATE service_principals SET "+change+" WHERE id='generator'")
            with pytest.raises(asyncpg.RaiseError):await c.execute(sql)

async def test_catalog_alias_cannot_consume_or_allocate_foreign_pot(pg):
    pool,_=pg;uid,credit=await seed(pool);project='schema-'+str(uuid.uuid4())
    from dataclasses import replace
    plans.PLANS[module.PLANS[0]]=replace(plans.PLANS[module.PLANS[0]],budget_plan_id='foreign-pot')
    plans.PLANS['foreign-pot']=replace(plans.PLANS[module.PLANS[0]],id='foreign-pot',budget_plan_id=None)
    with pytest.raises(ValueError,match='scope'):await module.consume_once(uid,project)
    async with pool.acquire() as c:
        assert await c.fetchval('SELECT used FROM manual_project_credits WHERE id=$1',credit)==0
        assert await c.fetchval('SELECT count(*) FROM project_budgets')==0
        assert await c.fetchval('SELECT count(*) FROM schema_generator_credit_receipts')==0
