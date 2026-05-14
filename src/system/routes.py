"""
System-Health endpoint — cross-app status aggregator.

Polls bridge-internal counters, postgres health + size, and (optionally)
each known app frontend's /api/health. Result is a single JSON snapshot
the CUI Platform Admin renders as a status board.

GET  /v1/system/health   require_admin
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends

from src.api_auth import require_admin, AuthClaims
from src.db.client import get_pool

router = APIRouter(prefix="/v1/system", tags=["system"])

# Known app health-check URLs. The aggregator probes each in parallel and
# folds the responses into one panel-friendly snapshot. URLs configurable
# via env later — hard-coded here for v1 to keep the surface small.
_APP_HEALTH_TARGETS = [
    ("werking-report", "https://werking-report.vercel.app/api/admin/health"),
    ("werking-energy",  "https://werking-energy.vercel.app/api/health"),
    ("werking-safety",  "https://werking-safety.vercel.app/api/health"),
    ("werking-noise",   "https://werking-noise.vercel.app/api/health"),
    ("engelmann",       "https://engelmann.vercel.app/api/health"),
]
_PROBE_TIMEOUT_S = 4.0


async def _probe_app(client: httpx.AsyncClient, name: str, url: str) -> Dict[str, Any]:
    """Probe an app's health endpoint. Returns a panel-friendly status dict."""
    started = time.monotonic()
    try:
        r = await client.get(url, timeout=_PROBE_TIMEOUT_S)
        latency_ms = int((time.monotonic() - started) * 1000)
        return {
            "name": name,
            "url": url,
            "status": "healthy" if r.is_success else "unhealthy",
            "httpStatus": r.status_code,
            "latencyMs": latency_ms,
        }
    except httpx.TimeoutException:
        return {"name": name, "url": url, "status": "timeout", "httpStatus": None, "latencyMs": int(_PROBE_TIMEOUT_S * 1000)}
    except Exception as e:
        return {"name": name, "url": url, "status": "unreachable", "httpStatus": None, "latencyMs": None, "error": str(e)[:200]}


async def _db_snapshot() -> Dict[str, Any]:
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
              current_setting('server_version') AS pg_version,
              pg_database_size(current_database()) AS db_size_bytes,
              (SELECT count(*) FROM users)         AS users_count,
              (SELECT count(*) FROM tenants)       AS tenants_count,
              (SELECT count(*) FROM subscriptions) AS subscriptions_count,
              (SELECT count(*) FROM activities)    AS activities_count,
              (SELECT count(*) FROM invoices)      AS invoices_count,
              (SELECT count(*) FROM feedback)      AS feedback_count
            """
        )
        applied = await conn.fetch("SELECT filename, applied_at FROM schema_migrations ORDER BY applied_at")
    return {
        "status": "healthy",
        "pgVersion": row["pg_version"],
        "dbSizeBytes": int(row["db_size_bytes"]),
        "counts": {
            "users":         int(row["users_count"]),
            "tenants":       int(row["tenants_count"]),
            "subscriptions": int(row["subscriptions_count"]),
            "activities":    int(row["activities_count"]),
            "invoices":      int(row["invoices_count"]),
            "feedback":      int(row["feedback_count"]),
        },
        "migrations": [
            {"filename": r["filename"], "appliedAt": r["applied_at"].isoformat()} for r in applied
        ],
    }


@router.get("/health")
async def system_health_overview(
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    """
    Cross-app status snapshot for the admin dashboard.

    Layout:
      - generatedAt       ISO timestamp of probe time
      - bridge            internal DB stats (pg version, size, row counts, migrations)
      - apps              parallel results of /api/health probes against the 5 apps
      - summary           "healthy" if everything is healthy, else "degraded"

    Defensive: a single app being unreachable does NOT fail the whole call —
    we report each result individually so the operator sees the full picture.
    """
    db_task = asyncio.create_task(_db_snapshot())
    async with httpx.AsyncClient(follow_redirects=True) as client:
        app_results = await asyncio.gather(
            *(_probe_app(client, name, url) for name, url in _APP_HEALTH_TARGETS),
            return_exceptions=False,
        )
    db = await db_task

    all_apps_healthy = all(a["status"] == "healthy" for a in app_results)
    summary = "healthy" if all_apps_healthy and db["status"] == "healthy" else "degraded"

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "bridge": db,
        "apps": app_results,
    }
