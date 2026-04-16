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

app = FastAPI(
    title="Bridge Metrics Reader",
    description="Decoupled read-only service for historical Bridge metrics.",
    version="1.0.0",
)


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
    return get_prompt_metrics().get_stats(hours=hours)


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
    return get_prompt_metrics().get_recent_calls(
        hours=hours,
        limit=limit,
        app_id=app_id,
        user_id=user_id,
    )


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
    return get_prompt_metrics().get_usage_breakdown(hours=hours)


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
    return get_request_log().query(
        hours=hours,
        endpoint_filter=endpoint,
        status_filter=status,
        limit=limit,
    )


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

    payload = {
        "load_balancer": "nginx",
        "server": "primary",
        "status": agg,
        "workers": {
            "total": total,
            "up": up_count,
            "down": total - up_count,
            "per_worker": results,
        },
        "pools": {
            "normal": {"workers": total, "strategy": "round-robin"},
            "production": {"target": "server-2", "fallback": "normal-pool"},
        },
        "routing": "X-Priority: production → server-2, default → local",
    }
    http_status = 200 if up_count > 0 else 503
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
