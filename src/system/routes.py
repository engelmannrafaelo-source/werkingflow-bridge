"""
System-Health endpoint — cross-app status aggregator.

Polls bridge-internal counters, postgres health + size, and (optionally)
each known app frontend's /api/health. Result is a single JSON snapshot
the CUI Platform Admin renders as a status board.

GET  /v1/system/health   require_admin
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from src.api_auth import require_admin, AuthClaims
from src.db.client import get_pool

router = APIRouter(prefix="/v1/system", tags=["system"])

# Known app health checks: (app id, default base URL, health path). The aggregator
# probes each in parallel and folds the responses into one panel-friendly snapshot.
#
# The default base is the app's *.vercel.app alias. Once Vercel deployment
# protection is on, that alias answers 401 without a bypass header, so every
# app can be redirected per environment, nothing is hard-wired:
#   APP_HEALTH_BASE_<APP>    own domain of the app (e.g. https://app.example.com)
#   APP_HEALTH_BYPASS_<APP>  protection bypass secret, sent as
#                            x-vercel-protection-bypass (for apps without own domain)
# <APP> is the app id upper-cased with "-" -> "_" (WERKING_ENERGY).
_APP_HEALTH_TARGETS = [
    ("werking-report", "https://werking-report.vercel.app", "/api/admin/health"),
    ("werking-energy", "https://werking-energy.vercel.app", "/api/health"),
    ("werking-safety", "https://werking-safety.vercel.app", "/api/health"),
    ("werking-noise",  "https://werking-noise.vercel.app",  "/api/health"),
    ("engelmann",      "https://engelmann.vercel.app",      "/api/health"),
]
_ALLOWED_APP_IDS = {name for name, _, _ in _APP_HEALTH_TARGETS}
_PROBE_TIMEOUT_S = 4.0


def _app_env_key(name: str) -> str:
    return name.upper().replace("-", "_")


def _resolve_target(name: str, default_base: str, path: str) -> tuple[str, Dict[str, str]]:
    """URL + request headers for one app, from env overrides (see above)."""
    key = _app_env_key(name)
    base = (os.environ.get(f"APP_HEALTH_BASE_{key}") or default_base).rstrip("/")
    bypass = (os.environ.get(f"APP_HEALTH_BYPASS_{key}") or "").strip()
    headers = {"x-vercel-protection-bypass": bypass} if bypass else {}
    return base + path, headers


async def _probe_app(
    client: httpx.AsyncClient, name: str, url: str, headers: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """Probe an app's health endpoint. Returns a panel-friendly status dict."""
    started = time.monotonic()
    try:
        r = await client.get(url, headers=headers or None, timeout=_PROBE_TIMEOUT_S)
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
    app: Optional[str] = Query(
        default=None,
        description=(
            "Optional app to probe in isolation. When set, only that app's "
            "frontend health endpoint is probed; `apps` contains a single entry. "
            "Bridge DB stats stay cross-app — they describe the bridge, not "
            "one app's usage."
        ),
    ),
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
    if app and app not in _ALLOWED_APP_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown app: {app}")

    targets = [
        (name, *_resolve_target(name, base, path))
        for name, base, path in _APP_HEALTH_TARGETS
        if not app or name == app
    ]

    db_task = asyncio.create_task(_db_snapshot())
    async with httpx.AsyncClient(follow_redirects=True) as client:
        app_results = await asyncio.gather(
            *(_probe_app(client, name, url, hdrs) for name, url, hdrs in targets),
            return_exceptions=False,
        )
    db = await db_task

    all_apps_healthy = all(a["status"] == "healthy" for a in app_results)
    summary = "healthy" if all_apps_healthy and db["status"] == "healthy" else "degraded"

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "app": app,
        "bridge": db,
        "apps": app_results,
    }
