"""
Globale Plattform-Konfiguration (Tabelle platform_config, key/value als JSONB).

Erlaubt Schalter, die im Platform-Admin ohne Redeploy umgelegt werden koennen.
Erster Key: self_checkout_active — Master-Gate fuer die Selbstbuchung.
"""
from __future__ import annotations

import json
from typing import Any

from src.db.client import get_pool

SELF_CHECKOUT_ACTIVE = "self_checkout_active"


async def get_config(key: str, default: Any = None) -> Any:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM platform_config WHERE key = $1", key)
    if row is None:
        return default
    val = row["value"]
    # asyncpg gibt JSONB je nach Codec als str ODER schon dekodiert zurueck.
    if isinstance(val, (str, bytes, bytearray)):
        return json.loads(val)
    return val


async def set_config(key: str, value: Any, updated_by: str | None = None) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO platform_config (key, value, updated_at, updated_by)
            VALUES ($1, $2::jsonb, NOW(), $3)
            ON CONFLICT (key) DO UPDATE
              SET value = EXCLUDED.value, updated_at = NOW(), updated_by = EXCLUDED.updated_by
            """,
            key, json.dumps(value), updated_by,
        )


async def is_self_checkout_active() -> bool:
    """Master-Schalter: darf ueberhaupt jemand selbst buchen? Beta-Default False."""
    return bool(await get_config(SELF_CHECKOUT_ACTIVE, False))
