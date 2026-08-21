"""
Globale Plattform-Konfiguration (Tabelle platform_config, key/value als JSONB).

Erlaubt Schalter, die im Platform-Admin ohne Redeploy umgelegt werden koennen.
Erster Key: self_checkout_active — Master-Gate fuer die Selbstbuchung.

ADR-0009 Schritt 2 (Worker ohne BRIDGE_DB_URL): dieses Modul bleibt bewusst am
direkten Pool und wird NICHT ueber platform-api geleitet. Es laeuft schon heute
ausschliesslich IN platform-api — die einzigen Aufrufer sind
``src/billing/routes.py`` und ``src/db/admin_routes.py``, und beide Router
haengen nur an ``src/platform_main.py``, nie an ``src/main.py`` (nachgemessen
2026-08-21 ueber die transitive Import-Huelle von ``src.main``: dieses Modul ist
von dort aus nicht erreichbar, weder direkt noch ueber eine Aufrufkette).

Ein Umbau haette also nichts aus einem Worker entfernt, sondern platform-api
einen HTTP-Aufruf an sich selbst gegeben — Latenz und eine zusaetzliche
Fehlerquelle fuer null Gewinn. Der Ort, an dem der Pool gehalten wird, IST der
richtige Ort fuer diesen Code.

Falls hier jemals ein Aufrufer aus dem Worker-Pfad dazukommt, ist das der
Moment fuer den Umbau — nicht vorher. Er faellt dann laut auf: ``get_pool()``
wirft im Worker ohne ``BRIDGE_DB_URL``, statt still zu degradieren.
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
