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


def is_db_unreachable(exc: BaseException) -> bool:
    """Did this DB call fail because the database could not be REACHED (this
    time), rather than because it answered something?

    True for: connection refused / reset / DNS / timeout (OSError, which
    covers TimeoutError), a connection that broke mid-query, and the server
    refusing connections while it shuts down, starts or recovers (asyncpg's
    PostgresConnectionError, OperatorInterventionError — AdminShutdown,
    CannotConnectNow, CrashShutdown — and TooManyConnections). That is the
    window of a postgres-prod recreate or a DB hiccup: the same query can
    succeed later unchanged.

    False for everything else — a SQL error, an unexpected row shape, a pool
    that was never initialised. Those are answers or defects, not a gap, and
    callers must not promise them as retryable (BR9b, BR8R2 MUSS 1).
    """
    if isinstance(exc, OSError):
        return True
    if not ASYNCPG_AVAILABLE:
        return False
    exc_mod = asyncpg.exceptions
    return isinstance(exc, (
        exc_mod.PostgresConnectionError,
        exc_mod.OperatorInterventionError,
        exc_mod.TooManyConnectionsError,
    ))
