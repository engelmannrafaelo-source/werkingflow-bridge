"""Real, disposable PostgreSQL; no production DB accepted by this test."""
import asyncio
import os
from pathlib import Path
import pytest
from src.account_policy import store
from src.account_policy.model import initial_policy

@pytest.mark.asyncio
async def test_real_postgres_cas_and_connection_restart(monkeypatch):
    asyncpg=pytest.importorskip("asyncpg")
    dsn=os.environ.get("BRIDGE_POLICY_TEST_DSN")
    if not dsn:pytest.skip("disposable PostgreSQL DSN not supplied")
    assert dsn.startswith("postgresql://postgres@127.0.0.1:") and dsn.endswith("/policy_test")
    pool=await asyncpg.create_pool(dsn,min_size=1,max_size=3)
    try:
        async with pool.acquire() as conn:
            await conn.execute("CREATE TABLE platform_config(key TEXT PRIMARY KEY,value JSONB NOT NULL,updated_at TIMESTAMPTZ DEFAULT NOW(),updated_by TEXT)")
            await conn.execute((Path(__file__).parents[1]/"docker/migrations/074_bridge_account_policy.sql").read_text())
        monkeypatch.setattr(store,"get_pool",lambda:pool)
        first=await store.read_policy()
        left=initial_policy();left["accounts"][3]["enabled"]=False
        right=initial_policy();right["accounts"][0]["enabled"]=False
        results=await asyncio.gather(store.replace_policy(left,"test-left"),store.replace_policy(right,"test-right"),return_exceptions=True)
        assert sum(isinstance(x,store.Conflict) for x in results)==1
        winner=next(x for x in results if isinstance(x,dict))
        assert winner["revision"]==1
        await pool.close()
        pool=await asyncpg.create_pool(dsn,min_size=1,max_size=3)
        assert await store.read_policy()==winner
        async with pool.acquire() as conn:
            # Re-running migration never resets an existing choice.
            await conn.execute((Path(__file__).parents[1]/"docker/migrations/074_bridge_account_policy.sql").read_text())
        assert await store.read_policy()==winner
    finally:
        await pool.close()
