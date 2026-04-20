"""
Bridge Metrics Reader — standalone FastAPI service.

PURPOSE
-------
Decouples historical metric read-endpoints from the live Bridge worker pool.
If all workers die / hang / 503-storm, the reader still serves /v1/metrics/*
from the JSONL files on disk. The CUI Bridge Monitor keeps working.

WHAT IT SERVES
--------------
Read-only, file-based endpoints only (all other Bridge endpoints stay on workers):

    GET  /v1/metrics/prompt-performance
    GET  /v1/metrics/prompt-performance/timeline
    GET  /v1/metrics/prompt-performance/calls
    GET  /v1/metrics/throughput
    GET  /v1/metrics/usage-breakdown
    GET  /v1/metrics/request-log
    GET  /v1/metrics/cc-usage-history
    GET  /lb-status                  (real worker-health aggregate — Phase 3)
    GET  /health                     (self health)

NOT SERVED (stay on workers):
    /v1/metrics                       — in-memory performance_monitor per worker
    /v1/metrics/queue-forecast        — rolling in-memory metrics per worker
    POST /v1/metrics/cc-usage-snapshot — WRITE; reader volume is read-only

IMPLEMENTATION NOTES
--------------------
* Reuses the existing middleware modules (bridge_metrics_store.py,
  prompt_metrics.py) — single source of truth for JSONL parsing.
* Volume is mounted read-only. Defensive: we do not open any file for write.
* No authentication (mirrors current worker behaviour: credentials are
  Optional on these endpoints). If API_KEY is ever enforced, add here too.
* Output schema is identical to the worker endpoints (the CUI consumer
  in cui/server/routes/bridge.ts expects specific shapes).

DEFENSIVE PROGRAMMING
---------------------
* Errors from the underlying store bubble up as 500 with the original message.
  No silent fallbacks.
* Missing JSONL files return empty result sets (store handles this already).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

# Reuse the SAME middleware modules as the Bridge workers.
# These files come via COPY in Dockerfile.metrics-reader and are importable
# from /app/src/middleware/... inside the container.
from src.middleware.bridge_metrics_store import get_request_log, get_cc_usage_store
from src.middleware.prompt_metrics import get_prompt_metrics

logger = logging.getLogger("metrics-reader")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# Production Bridge URL — if set, aggregate metrics from both servers.
PROD_BRIDGE_URL = os.getenv("BRIDGE_PROD_URL", "").rstrip("/")

app = FastAPI(
    title="Bridge Metrics Reader",
    description="Decoupled read-only service for historical Bridge metrics.",
    version="1.0.0",
)


# ============================================================================
# Production Aggregation Helper
# ============================================================================

def _fetch_prod(path: str, timeout: float = 15.0) -> dict | None:
    """Fetch an endpoint from the production Bridge. Returns None on any error.

    Default timeout is 15s because production has no metrics-reader — JSONL
    endpoints are served by the worker inline and can be slow.
    """
    if not PROD_BRIDGE_URL:
        return None
    import urllib.request
    import urllib.error
    import json as _json
    try:
        url = f"{PROD_BRIDGE_URL}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return _json.loads(resp.read())
    except Exception as e:
        logger.debug(f"Prod fetch {path} failed: {e}")
        return None


def _merge_usage_breakdown(local: dict, prod: dict) -> dict:
    """Merge two usage-breakdown responses (sum totals, merge lists)."""
    # Merge summary
    ls = local.get("summary", {})
    ps = prod.get("summary", {})
    merged_summary = {
        "total_calls": ls.get("total_calls", 0) + ps.get("total_calls", 0),
        "total_input_tokens": ls.get("total_input_tokens", 0) + ps.get("total_input_tokens", 0),
        "total_output_tokens": ls.get("total_output_tokens", 0) + ps.get("total_output_tokens", 0),
        "total_tokens": ls.get("total_tokens", 0) + ps.get("total_tokens", 0),
        "total_errors": ls.get("total_errors", 0) + ps.get("total_errors", 0),
    }

    # Merge apps by app_id
    apps_map: dict[str, dict] = {}
    for app_entry in local.get("apps", []) + prod.get("apps", []):
        aid = app_entry.get("app_id", "unknown")
        if aid not in apps_map:
            apps_map[aid] = {**app_entry}
        else:
            existing = apps_map[aid]
            for key in ("calls", "input_tokens", "output_tokens", "total_tokens", "errors"):
                existing[key] = existing.get(key, 0) + app_entry.get(key, 0)
            # Merge users dict
            for uid, cnt in app_entry.get("users", {}).items():
                existing.setdefault("users", {})[uid] = existing.get("users", {}).get(uid, 0) + cnt
            # Merge agents dict
            for agid, cnt in app_entry.get("agents", {}).items():
                existing.setdefault("agents", {})[agid] = existing.get("agents", {}).get(agid, 0) + cnt

    # Merge users by user_id
    users_map: dict[str, dict] = {}
    for user_entry in local.get("users", []) + prod.get("users", []):
        uid = user_entry.get("user_id", "anonymous")
        if uid not in users_map:
            users_map[uid] = {**user_entry}
        else:
            users_map[uid]["calls"] = users_map[uid].get("calls", 0) + user_entry.get("calls", 0)

    # Merge models by model name
    models_map: dict[str, dict] = {}
    for model_entry in local.get("models", []) + prod.get("models", []):
        mid = model_entry.get("model", "unknown")
        if mid not in models_map:
            models_map[mid] = {**model_entry}
        else:
            for key in ("calls", "input_tokens", "output_tokens", "total_tokens"):
                models_map[mid][key] = models_map[mid].get(key, 0) + model_entry.get(key, 0)

    # Recalculate error rates for apps
    for app_entry in apps_map.values():
        c = app_entry.get("calls", 0)
        e = app_entry.get("errors", 0)
        app_entry["error_rate"] = round((e / c) * 100, 1) if c > 0 else 0

    return {
        **local,
        "summary": merged_summary,
        "apps": list(apps_map.values()),
        "users": list(users_map.values()),
        "models": list(models_map.values()),
        "_sources": ["primary", "production"],
    }


def _merge_prompt_performance(local: dict, prod: dict) -> dict:
    """Merge two prompt-performance responses."""
    # Merge agents by (app_id, agent_id)
    agents_map: dict[str, dict] = {}
    for agent in local.get("agents", []) + prod.get("agents", []):
        key = f"{agent.get('app_id', '?')}:{agent.get('agent_id', '?')}"
        if key not in agents_map:
            agents_map[key] = {**agent}
        else:
            existing = agents_map[key]
            for k in ("calls", "successes", "errors"):
                existing[k] = existing.get(k, 0) + agent.get(k, 0)
            # Token sums
            for tk in ("input_tokens", "output_tokens", "total_tokens"):
                et = existing.get("tokens", {})
                at = agent.get("tokens", {})
                if isinstance(et, dict) and isinstance(at, dict):
                    et[tk] = et.get(tk, 0) + at.get(tk, 0)

    # Merge summary
    ls = local.get("summary", {})
    ps = prod.get("summary", {})
    merged_summary = {
        "total_calls": ls.get("total_calls", 0) + ps.get("total_calls", 0),
        "total_agents": len(agents_map),
        "total_errors": ls.get("total_errors", 0) + ps.get("total_errors", 0),
    }
    total = merged_summary["total_calls"]
    merged_summary["overall_error_rate"] = round(
        (merged_summary["total_errors"] / total) * 100, 1
    ) if total > 0 else 0

    return {
        **local,
        "agents": list(agents_map.values()),
        "summary": merged_summary,
        "_sources": ["primary", "production"],
    }


def _merge_request_log(local: dict, prod: dict, limit: int) -> dict:
    """Merge two request-log responses, interleave by timestamp."""
    local_entries = local.get("entries", [])
    prod_entries = prod.get("entries", [])
    # Tag entries with source
    for e in prod_entries:
        e["_server"] = "production"
    merged = sorted(
        local_entries + prod_entries,
        key=lambda x: x.get("timestamp", 0),
        reverse=True,
    )[:limit]
    return {
        **local,
        "entries": merged,
        "total": local.get("total", 0) + prod.get("total", 0),
        "_sources": ["primary", "production"],
    }


def _merge_calls(local: dict, prod: dict, limit: int) -> dict:
    """Merge two prompt-performance/calls responses."""
    local_calls = local.get("calls", [])
    prod_calls = prod.get("calls", [])
    for c in prod_calls:
        c["_server"] = "production"
    merged = sorted(
        local_calls + prod_calls,
        key=lambda x: x.get("timestamp", 0),
        reverse=True,
    )[:limit]
    return {
        **local,
        "calls": merged,
        "_sources": ["primary", "production"],
    }


# ============================================================================
# Health
# ============================================================================

@app.get("/health")
def health() -> dict:
    """Self health. Does not depend on workers or network."""
    return {"status": "ok", "service": "bridge-metrics-reader"}


# ============================================================================
# Prompt Performance Metrics (file-based)
# ============================================================================

@app.get("/v1/metrics/prompt-performance")
def get_prompt_performance(hours: int = Query(24, ge=0)) -> dict:
    """Per-prompt-type stats. hours=0 ⇒ all time."""
    local = get_prompt_metrics().get_stats(hours=hours)
    prod = _fetch_prod(f"/v1/metrics/prompt-performance?hours={hours}", timeout=30)
    if prod:
        return _merge_prompt_performance(local, prod)
    return local


@app.get("/v1/metrics/prompt-performance/timeline")
def get_prompt_timeline(
    app_id: str,
    agent_id: str,
    hours: int = Query(24, ge=1, le=168),
    bucket_minutes: int = Query(60, ge=5, le=360),
) -> dict:
    """Timeline buckets for a specific app+agent combo."""
    return get_prompt_metrics().get_timeline(
        app_id=app_id,
        agent_id=agent_id,
        hours=hours,
        bucket_minutes=bucket_minutes,
    )


@app.get("/v1/metrics/prompt-performance/calls")
def get_prompt_calls(
    hours: int = Query(24, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    app_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> dict:
    """Raw individual prompt calls (newest first)."""
    local = get_prompt_metrics().get_recent_calls(
        hours=hours,
        limit=limit,
        app_id=app_id,
        user_id=user_id,
    )
    params = f"?hours={hours}&limit={limit}"
    if app_id:
        params += f"&app_id={app_id}"
    if user_id:
        params += f"&user_id={user_id}"
    prod = _fetch_prod(f"/v1/metrics/prompt-performance/calls{params}", timeout=30)
    if prod:
        return _merge_calls(local, prod, limit)
    return local


@app.get("/v1/metrics/throughput")
def get_throughput(
    hours: int = Query(24, ge=1, le=168),
    bucket_seconds: int = Query(60, ge=10, le=600),
) -> dict:
    """Per-worker throughput timeline + empirical rate-limit ceiling."""
    return get_prompt_metrics().get_throughput(
        hours=hours,
        bucket_seconds=bucket_seconds,
    )


@app.get("/v1/metrics/usage-breakdown")
def get_usage_breakdown(hours: int = Query(24, ge=0)) -> dict:
    """Token/cost breakdown per app, per user, per model."""
    local = get_prompt_metrics().get_usage_breakdown(hours=hours)
    prod = _fetch_prod(f"/v1/metrics/usage-breakdown?hours={hours}", timeout=30)
    if prod:
        return _merge_usage_breakdown(local, prod)
    return local


# ============================================================================
# Persistent Request Log (file-based)
# ============================================================================

@app.get("/v1/metrics/request-log")
def get_request_log_endpoint(
    hours: int = Query(24, ge=0),
    endpoint: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
) -> dict:
    """Persistent request log from all workers (JSONL on shared volume)."""
    local = get_request_log().query(
        hours=hours,
        endpoint_filter=endpoint,
        status_filter=status,
        limit=limit,
    )
    params = f"?hours={hours}&limit={limit}"
    if endpoint:
        params += f"&endpoint={endpoint}"
    if status:
        params += f"&status={status}"
    prod = _fetch_prod(f"/v1/metrics/request-log{params}", timeout=30)
    if prod:
        return _merge_request_log(local, prod, limit)
    return local


# ============================================================================
# CC-Usage Snapshots (file-based, read-only)
# ============================================================================

@app.get("/v1/metrics/cc-usage-history")
def get_cc_usage_history(
    hours: int = Query(168, ge=0),
    limit: int = Query(500, ge=1, le=2000),
) -> dict:
    """Claude Code account usage snapshot history."""
    return get_cc_usage_store().get_history(
        hours=hours,
        limit=limit,
    )


# ============================================================================
# Real /lb-status (Phase 3)
# ============================================================================

@app.get("/lb-status")
def lb_status() -> JSONResponse:
    """
    Real worker-health aggregate. Pings each worker's /health endpoint
    and reports up/down per worker plus aggregate.

    Replaces the hardcoded "healthy" JSON in nginx.conf.
    """
    import urllib.request
    import urllib.error
    import socket

    # These are the worker hostnames inside the docker-compose network.
    workers = ["worker1", "worker2", "worker3", "worker4"]

    results: dict[str, dict] = {}
    up_count = 0

    for w in workers:
        url = f"http://{w}:8000/health"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                ok = 200 <= resp.status < 300
                results[w] = {"status": "up" if ok else "down", "http_code": resp.status}
                if ok:
                    up_count += 1
        except urllib.error.HTTPError as e:
            results[w] = {"status": "down", "http_code": e.code, "error": str(e)}
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            results[w] = {"status": "down", "error": str(e)}

    total = len(workers)
    # aggregate: healthy if ≥1 worker is up; degraded if some down; critical if all down.
    if up_count == total:
        agg = "healthy"
    elif up_count == 0:
        agg = "critical"
    else:
        agg = "degraded"

    # Also check production server if configured
    prod_results: dict[str, dict] = {}
    prod_up = 0
    if PROD_BRIDGE_URL:
        prod_health = _fetch_prod("/health", timeout=3)
        if prod_health and prod_health.get("status") == "healthy":
            worker_name = prod_health.get("worker_instance", "worker-prod")
            prod_results[worker_name] = {"status": "up", "server": "production"}
            prod_up = 1
        else:
            prod_results["worker-prod"] = {"status": "down", "server": "production",
                                            "error": "unreachable" if not prod_health else prod_health.get("status", "?")}

    all_workers = {**results, **prod_results}
    total_all = total + len(prod_results)
    up_all = up_count + prod_up

    if up_all == total_all:
        agg = "healthy"
    elif up_all == 0:
        agg = "critical"
    else:
        agg = "degraded"

    payload = {
        "load_balancer": "nginx",
        "server": "primary",
        "status": agg,
        "workers": {
            "total": total_all,
            "up": up_all,
            "down": total_all - up_all,
            "per_worker": all_workers,
        },
        "pools": {
            "normal": {"workers": total, "strategy": "round-robin"},
            "production": {"target": "server-2", "fallback": "normal-pool", "workers": len(prod_results)},
        },
        "routing": "X-Priority: production → server-2, default → local",
    }
    http_status = 200 if up_all > 0 else 503
    return JSONResponse(content=payload, status_code=http_status)


# ============================================================================
# Catch-all for misrouted requests — loud fail, not silent
# ============================================================================

@app.get("/v1/metrics/{rest:path}")
def catch_unknown_metrics(rest: str):
    """
    If nginx accidentally routes an endpoint here that the reader does NOT
    implement (e.g. /v1/metrics or /v1/metrics/queue-forecast), fail loud.
    """
    raise HTTPException(
        status_code=404,
        detail={
            "error": "metrics_reader_unsupported",
            "message": (
                f"Endpoint /v1/metrics/{rest} is not served by the reader. "
                "Check nginx routing — in-memory endpoints (performance_monitor, "
                "rolling_metrics) must stay on worker pool."
            ),
        },
    )
