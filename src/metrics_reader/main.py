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

# Worker hostnames in this docker-compose network (comma-separated).
# Dev-Bridge default: worker1..worker4.  Production: worker-prod.
BRIDGE_WORKERS = [w.strip() for w in os.getenv("BRIDGE_WORKERS", "worker1,worker2,worker3,worker4").split(",") if w.strip()]

# Whether production has a metrics-reader (JSONL endpoints work).
# Probed once on first request; re-probed every 5 minutes.
_prod_has_metrics_reader: bool | None = None  # None = not yet probed
_prod_last_probe: float = 0.0
_PROBE_INTERVAL = 300  # re-probe every 5 minutes

app = FastAPI(
    title="Bridge Metrics Reader",
    description="Decoupled read-only service for historical Bridge metrics.",
    version="1.0.0",
)


# ============================================================================
# Production Aggregation Helper
# ============================================================================

def _prod_metrics_available() -> bool:
    """Check if production has a working metrics-reader. Cached with re-probe."""
    global _prod_has_metrics_reader, _prod_last_probe
    if not PROD_BRIDGE_URL:
        return False
    import time
    now = time.time()
    if _prod_has_metrics_reader is not None and (now - _prod_last_probe) < _PROBE_INTERVAL:
        return _prod_has_metrics_reader

    # Probe: try a fast metrics-reader-only endpoint
    import urllib.request
    import json as _json
    _prod_last_probe = now
    try:
        url = f"{PROD_BRIDGE_URL}/v1/metrics/usage-breakdown?hours=0"
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = _json.loads(resp.read())
            _prod_has_metrics_reader = "summary" in data
            logger.info(f"Prod metrics-reader probe: {'available' if _prod_has_metrics_reader else 'unavailable'}")
    except Exception as e:
        _prod_has_metrics_reader = False
        logger.info(f"Prod metrics-reader probe: unavailable ({e})")
    return _prod_has_metrics_reader


def _fetch_prod_direct(path: str, timeout: float = 5.0) -> dict | None:
    """Fetch any endpoint from production (no metrics-reader check).
    Use for fast endpoints like /health that work on any server."""
    if not PROD_BRIDGE_URL:
        return None
    import urllib.request
    import json as _json
    try:
        url = f"{PROD_BRIDGE_URL}{path}"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return _json.loads(resp.read())
    except Exception as e:
        logger.debug(f"Prod direct fetch {path} failed: {e}")
        return None


def _fetch_prod(path: str, timeout: float = 5.0) -> dict | None:
    """Fetch a JSONL-based endpoint from the production Bridge.

    Returns None immediately if production has no metrics-reader (avoids
    3s timeout penalty on every request). Re-probes every 5 minutes.
    """
    if not _prod_metrics_available():
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
    # Production aggregation: works once production has its own metrics-reader.
    # Without metrics-reader on prod, these calls timeout gracefully (returns local only).
    prod = _fetch_prod(f"/v1/metrics/prompt-performance?hours={hours}", timeout=3)
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
    prod = _fetch_prod(f"/v1/metrics/prompt-performance/calls{params}", timeout=3)
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
    prod = _fetch_prod(f"/v1/metrics/usage-breakdown?hours={hours}", timeout=3)
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
    prod = _fetch_prod(f"/v1/metrics/request-log{params}", timeout=3)
    if prod:
        return _merge_request_log(local, prod, limit)
    return local


# ============================================================================
# Contract Observability (file-based aggregation of request_log)
# ============================================================================

@app.get("/v1/metrics/contract")
def get_contract_stats(
    hours: int = Query(24, ge=1, le=168),
    recent_limit: int = Query(50, ge=1, le=500),
) -> dict:
    """
    Contract-violation & 429-reason aggregation.

    Reads the per-worker request_log JSONL and buckets non-2xx responses by
    status × reason × source. Drives the panel's Contract tab. Goals:

    - "violations" count  → how many 500/502/503/504 escaped to the client
      (after Phase 1+2, should trend toward zero)
    - "capacity_429" count → healthy rejections; expected to be non-zero
    - "reason_breakdown"  → which capacity mechanism fired how often

    Surfaces recent violations separately so operators can drill in.
    """
    # Pull raw entries (bounded large window for stable aggregation).
    raw = get_request_log().query(
        hours=hours,
        endpoint_filter=None,
        status_filter=None,
        limit=10000,
    )
    entries = raw.get("entries") or []

    # Buckets. Reason/source aggregate across all non-2xx so operators can
    # see, for a given reason, which status codes it produced. That catches
    # the case where a reason is emitted at both 429 (expected) and 5xx
    # (contract violation) — a nuance a flat count would hide.
    total = 0
    total_2xx = 0
    total_429 = 0
    total_5xx = 0
    total_4xx_client = 0  # 4xx that are NOT 429 (client-side problems)
    reason_stats: dict[str, dict] = {}
    source_stats: dict[str, dict] = {}
    worker_violations: dict[str, int] = {}
    violations: list[dict] = []

    def _bump_reason(reason: str, status: int, endpoint: str) -> None:
        bucket = reason_stats.setdefault(
            reason, {"count": 0, "statuses": {}, "sample_endpoints": []}
        )
        bucket["count"] += 1
        bucket["statuses"][str(status)] = bucket["statuses"].get(str(status), 0) + 1
        if endpoint and endpoint not in bucket["sample_endpoints"] and len(bucket["sample_endpoints"]) < 5:
            bucket["sample_endpoints"].append(endpoint)

    def _bump_source(source: str, status: int) -> None:
        bucket = source_stats.setdefault(source, {"count": 0, "statuses": {}})
        bucket["count"] += 1
        bucket["statuses"][str(status)] = bucket["statuses"].get(str(status), 0) + 1

    for e in entries:
        status = int(e.get("status") or 0)
        if status == 0:
            continue
        total += 1
        if 200 <= status < 300:
            total_2xx += 1
            continue

        # Non-2xx: always record reason/source for the breakdown.
        reason = e.get("reason") or "unknown"
        source = e.get("source") or "unknown"
        endpoint = e.get("endpoint") or "unknown"
        _bump_reason(reason, status, endpoint)
        _bump_source(source, status)

        if status == 429:
            total_429 += 1
            continue
        if 500 <= status < 600:
            total_5xx += 1
            worker = e.get("worker") or "unknown"
            worker_violations[worker] = worker_violations.get(worker, 0) + 1
            violations.append({
                "ts": e.get("ts"),
                "method": e.get("method"),
                "worker": worker,
                "endpoint": endpoint,
                "status": status,
                "reason": e.get("reason"),
                "source": e.get("source"),
                "duration_s": e.get("duration_s"),
            })
            continue
        if 400 <= status < 500:
            total_4xx_client += 1

    # Recent violations first.
    violations.sort(key=lambda v: v.get("ts") or 0, reverse=True)
    violations = violations[:recent_limit]

    # Contract verdict.
    if total_5xx == 0:
        verdict = "ok"
    elif total_5xx < 5:
        verdict = "degraded"
    else:
        verdict = "violating"

    violation_rate_pct = round((total_5xx / total) * 100, 2) if total > 0 else 0.0

    return {
        "period_hours": hours,
        "verdict": verdict,
        "totals": {
            "requests": total,
            "success_2xx": total_2xx,
            "capacity_429": total_429,
            "client_4xx": total_4xx_client,
            "violations_5xx": total_5xx,
            "violation_rate_pct": violation_rate_pct,
        },
        "reason_breakdown": reason_stats,
        "source_breakdown": source_stats,
        "violations_by_worker": worker_violations,
        "recent_violations": violations,
    }


# ============================================================================
# Upstream Health — Claude API reachability proxy, derived from request log.
# ============================================================================

@app.get("/v1/metrics/upstream-health")
def get_upstream_health(
    recent_window_min: int = Query(5, ge=1, le=120),
    lookback_hours: int = Query(6, ge=1, le=168),
) -> dict:
    """
    Claude API reachability indicator derived from recent bridge requests.

    We do NOT actively ping claude.ai (no synthetic traffic). Instead the
    request log acts as a passive probe: every 5xx with a
    `claude_upstream_error` / `claude_upstream_timeout` reason = the worker
    saw a real upstream failure. That's a better signal than a synthetic
    ping because it reflects the actual auth/routing path the users take.

    Response verdict:
      * "ok"         — no upstream errors in the recent window
      * "degraded"   — 1..4 upstream errors in window OR worker-subset failing
      * "unreachable"— ≥5 upstream errors in window (every worker sees it)
    """
    import time as _time
    raw = get_request_log().query(hours=lookback_hours, limit=20000)
    entries = raw.get("entries") or []

    now = _time.time()
    recent_cutoff = now - (recent_window_min * 60)

    upstream_reasons = {"claude_upstream_error", "claude_upstream_timeout"}
    per_worker: dict[str, dict] = {}
    recent_errors = 0
    total_recent = 0
    last_upstream_error_ts: float | None = None

    for e in entries:
        ts = e.get("ts") or 0
        worker = e.get("worker") or "unknown"
        w = per_worker.setdefault(worker, {
            "total": 0, "upstream_errors": 0, "last_error_ts": None,
            "recent_upstream_errors": 0,
        })
        w["total"] += 1
        reason = e.get("reason")
        if reason in upstream_reasons:
            w["upstream_errors"] += 1
            if w["last_error_ts"] is None or ts > w["last_error_ts"]:
                w["last_error_ts"] = ts
            if last_upstream_error_ts is None or ts > last_upstream_error_ts:
                last_upstream_error_ts = ts
            if ts >= recent_cutoff:
                w["recent_upstream_errors"] += 1
                recent_errors += 1
        if ts >= recent_cutoff:
            total_recent += 1

    if recent_errors == 0:
        verdict = "ok"
    elif recent_errors < 5:
        verdict = "degraded"
    else:
        verdict = "unreachable"

    recent_error_rate_pct = (
        round((recent_errors / total_recent) * 100, 2)
        if total_recent > 0 else 0.0
    )

    seconds_since_last_error = (
        int(now - last_upstream_error_ts)
        if last_upstream_error_ts is not None else None
    )

    return {
        "verdict": verdict,
        "recent_window_min": recent_window_min,
        "recent_upstream_errors": recent_errors,
        "recent_total_requests": total_recent,
        "recent_error_rate_pct": recent_error_rate_pct,
        "last_upstream_error_ts": last_upstream_error_ts,
        "seconds_since_last_error": seconds_since_last_error,
        "lookback_hours": lookback_hours,
        "workers": per_worker,
    }


# ============================================================================
# Limiter Trajectory — cap / inflight / utilization over time per worker.
# ============================================================================

import glob as _glob
import json as _json


@app.get("/v1/metrics/limiter-trajectory")
def get_limiter_trajectory(
    hours: int = Query(24, ge=1, le=168),
    max_points: int = Query(500, ge=50, le=5000),
) -> dict:
    """
    Time-series of limiter cap auto-tune + load per worker.

    Reads `limiter_events.{worker}.jsonl` files written by AdaptiveLoadLimiter
    every TUNE_INTERVAL_SEC (~60s). Each point carries cap_after,
    effective_cap_tokens, inflight_tokens, queued_count, and direction
    (shrink/grow/hold).

    When the raw series exceeds max_points, it's uniformly downsampled
    (simple stride) to keep the payload bounded. Tune *events* (shrink/grow)
    are always preserved in the downsample output.
    """
    import time as _time
    cutoff = _time.time() - (hours * 3600) if hours > 0 else 0
    pattern = os.path.join(
        os.getenv("METRICS_DIR", "/app/logs"),
        "bridge-metrics",
        "limiter_events.*.jsonl",
    )

    workers: dict[str, list[dict]] = {}
    tune_counts: dict[str, dict[str, int]] = {}

    for filepath in sorted(_glob.glob(pattern)):
        # Worker name = filename between "limiter_events." and ".jsonl"
        base = os.path.basename(filepath)
        worker = base.removeprefix("limiter_events.").removesuffix(".jsonl") or "unknown"
        workers.setdefault(worker, [])
        tune_counts.setdefault(worker, {"shrink": 0, "grow": 0, "hold": 0})
        try:
            with open(filepath, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = _json.loads(line)
                    except _json.JSONDecodeError:
                        continue
                    ts = ev.get("ts") or 0
                    if ts < cutoff:
                        continue
                    direction = ev.get("direction", "hold")
                    tune_counts[worker][direction] = tune_counts[worker].get(direction, 0) + 1
                    # Compact point — keep payload small for long windows.
                    workers[worker].append({
                        "ts": ts,
                        "direction": direction,
                        "cap": ev.get("cap_after", ev.get("cap_before", 0)),
                        "effective_cap": ev.get("effective_cap_tokens", 0),
                        "inflight_tokens": ev.get("inflight_tokens", 0),
                        "inflight_count": ev.get("inflight_count", 0),
                        "queued": ev.get("queued_count", 0),
                        "peak_util_pct": ev.get("observed_peak_util_pct", 0.0),
                        "rate_limits": ev.get("observed_rate_limits", 0),
                        "weekly_pct": ev.get("account_weekly_pct", 0.0),
                    })
        except OSError:
            continue

    # Downsample per worker if oversized. Always keep tune events (non-"hold").
    for worker, points in workers.items():
        points.sort(key=lambda p: p["ts"])
        if len(points) > max_points:
            tune_events = [p for p in points if p["direction"] != "hold"]
            holds = [p for p in points if p["direction"] == "hold"]
            # Remaining budget for hold points after reserving tune events.
            budget = max(0, max_points - len(tune_events))
            if budget > 0 and len(holds) > budget:
                stride = max(1, len(holds) // budget)
                holds = holds[::stride][:budget]
            merged = sorted(tune_events + holds, key=lambda p: p["ts"])
            workers[worker] = merged

    # Build per-worker summary: current cap, latest util, recent tune activity.
    summary: dict[str, dict] = {}
    for worker, points in workers.items():
        if not points:
            summary[worker] = {
                "points": 0, "current_cap": 0, "current_inflight": 0,
                "utilization_pct": 0.0, "tune_counts": tune_counts.get(worker, {}),
            }
            continue
        last = points[-1]
        cap = last.get("cap") or 0
        util = round((last.get("inflight_tokens", 0) / cap) * 100, 1) if cap > 0 else 0.0
        summary[worker] = {
            "points": len(points),
            "current_cap": cap,
            "current_effective_cap": last.get("effective_cap", 0),
            "current_inflight": last.get("inflight_tokens", 0),
            "current_inflight_count": last.get("inflight_count", 0),
            "current_queued": last.get("queued", 0),
            "utilization_pct": util,
            "weekly_pct": last.get("weekly_pct", 0.0),
            "tune_counts": tune_counts.get(worker, {}),
        }

    return {
        "period_hours": hours,
        "workers": workers,
        "summary": summary,
    }


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

    # Worker hostnames from env (BRIDGE_WORKERS) — adapts to dev/prod compose.
    workers = BRIDGE_WORKERS

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

    # Also check production server if configured (direct fetch, not via _fetch_prod
    # which requires metrics-reader — /health works on any server)
    prod_results: dict[str, dict] = {}
    prod_up = 0
    if PROD_BRIDGE_URL:
        prod_health = _fetch_prod_direct("/health", timeout=3)
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
# Account Pool State — aggregated from all workers for nginx Lua pool-router
# ============================================================================

@app.get("/v1/metrics/account-pool-state")
def get_account_pool_state():
    """Poll each worker for its limiter state and return a combined snapshot."""
    import time as _time
    import urllib.request
    import json as _json

    now = int(_time.time())
    accounts: dict = {}
    errors: list = []

    for worker in BRIDGE_WORKERS:
        url = f"http://{worker}:8000/v1/metrics/account-pool-state"
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = _json.loads(resp.read())
            account = data.get("account", worker)
            accounts[account] = {
                "worker": data.get("worker", worker),
                "session_percent": data.get("session_percent", 0),
                "session_reset_in_s": data.get("session_reset_in_s", 0),
                "weekly_percent": data.get("weekly_percent", 0),
                "adaptive_cap_tokens": data.get("adaptive_cap_tokens", 0),
                "current_in_flight_tokens": data.get("current_in_flight_tokens", 0),
                "headroom_tokens": data.get("headroom_tokens", 0),
                "headroom_percent": data.get("headroom_percent", 0.0),
                "last_rate_limit_ts": data.get("last_rate_limit_ts"),
                "cooldown_remaining_s": data.get("cooldown_remaining_s", 0),
                "available": data.get("available", False),
            }
        except Exception as e:
            errors.append({"worker": worker, "error": str(e)})
            logger.warning(f"account-pool-state: {worker} unreachable: {e}")

    result: dict = {"ts": now, "accounts": accounts}
    if errors:
        result["errors"] = errors
    return result


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
