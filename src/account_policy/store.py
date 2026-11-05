"""Policy lives in the authority's existing platform_config table. CAS is one SQL UPDATE."""
import json
from src.db.client import get_pool
from .model import KEY, validate

class Conflict(Exception):
    pass

async def read_policy():
    async with get_pool().acquire() as conn:
        raw = await conn.fetchval("SELECT value FROM platform_config WHERE key=$1", KEY)
    if raw is None:
        raise RuntimeError("bridge account policy not migrated")
    return validate(json.loads(raw) if isinstance(raw, (str, bytes)) else raw)

async def replace_policy(value, actor):
    proposed = validate(value)
    old_revision = proposed["revision"]
    proposed["revision"] += 1
    async with get_pool().acquire() as conn:
        raw = await conn.fetchval(
            """UPDATE platform_config SET value=$2::jsonb, updated_at=NOW(), updated_by=$3
               WHERE key=$1 AND (value->>'revision')::bigint=$4 RETURNING value""",
            KEY, json.dumps(proposed), actor, old_revision)
    if raw is None:
        raise Conflict()
    return validate(json.loads(raw) if isinstance(raw, (str, bytes)) else raw)
