"""
asyncpg connection pool for Bridge Postgres.
Only active when BRIDGE_DB_URL is set in environment.
"""
import os
from typing import Optional

try:
    import asyncpg
    ASYNCPG_AVAILABLE = True
except ImportError:
    ASYNCPG_AVAILABLE = False

_pool: Optional[object] = None


async def init_pool() -> None:
    """Initialize the asyncpg pool from BRIDGE_DB_URL. No-op if URL not set."""
    global _pool
    db_url = os.getenv("BRIDGE_DB_URL")
    if not db_url:
        return
    if not ASYNCPG_AVAILABLE:
        raise RuntimeError("asyncpg not installed but BRIDGE_DB_URL is set")
    _pool = await asyncpg.create_pool(
        dsn=db_url,
        min_size=2,
        max_size=10,
        command_timeout=30,
    )


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool():
    if _pool is None:
        raise RuntimeError("DB pool not initialized — BRIDGE_DB_URL not set or init_pool() not called")
    return _pool


def is_db_enabled() -> bool:
    return os.getenv("BRIDGE_DB_URL") is not None
