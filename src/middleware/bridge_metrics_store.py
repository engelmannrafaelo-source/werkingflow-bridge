"""
Bridge Metrics Store — Persistent JSONL storage for all Bridge metrics.

Three streams, all cumulative (no pruning):

1. request_log.{worker}.jsonl
   Every HTTP request through the Bridge (endpoint, status, duration, model, tokens, cost, user, app).
   Replaces the in-memory RequestMetrics for historical queries.

2. prompt_calls.{worker}.jsonl
   Already handled by prompt_metrics.py — not duplicated here.

3. cc_usage_snapshots.jsonl
   Account limit snapshots (written by CUI scraper, read by Bridge).

All files live on the shared Docker volume at /app/logs/bridge-metrics/.
"""

import os
import json
import time
import glob
import threading
import logging
from collections import defaultdict
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

METRICS_DIR = os.path.join(
    os.getenv("METRICS_DIR", "/app/logs"),
    "bridge-metrics"
)


def _ensure_dir():
    try:
        os.makedirs(METRICS_DIR, exist_ok=True)
    except OSError:
        pass


# =============================================================================
# 1. Request Log — every HTTP request
# =============================================================================

class RequestLogStore:
    """
    Persistent request log. Each worker appends to its own JSONL file.
    Replaces in-memory RequestMetrics for historical data.

    Fields per entry:
        timestamp, method, endpoint, status_code, duration_s,
        tools_enabled, worker, client_ip
    """

    def __init__(self):
        self._worker_id = os.getenv("INSTANCE_NAME", "unknown")
        self._jsonl_path = os.path.join(
            METRICS_DIR, f"request_log.{self._worker_id}.jsonl"
        )
        _ensure_dir()

    def record(
        self,
        method: str,
        endpoint: str,
        status_code: int,
        duration_s: float,
        tools_enabled: bool = False,
        client_ip: str = "unknown",
    ) -> None:
        """Append a request log entry to disk."""
        entry = {
            "ts": round(time.time(), 3),
            "method": method,
            "endpoint": endpoint,
            "status": status_code,
            "duration_s": round(duration_s, 4),
            "tools": tools_enabled,
            "worker": self._worker_id,
            "client": client_ip,
        }
        try:
            with open(self._jsonl_path, "a") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError as e:
            logger.warning(f"Cannot write request log: {e}")

    def query(
        self,
        hours: int = 24,
        endpoint_filter: Optional[str] = None,
        status_filter: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """
        Read request logs from ALL workers.

        Returns recent entries + aggregated endpoint stats.
        """
        cutoff = time.time() - (hours * 3600) if hours > 0 else 0
        pattern = os.path.join(METRICS_DIR, "request_log.*.jsonl")

        all_entries: List[dict] = []
        endpoint_stats: Dict[str, dict] = defaultdict(
            lambda: {"count": 0, "total_duration": 0.0, "errors": 0,
                     "min_duration": float("inf"), "max_duration": 0.0}
        )

        for filepath in sorted(glob.glob(pattern)):
            try:
                with open(filepath, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            ts = entry.get("ts", 0)
                            if ts < cutoff:
                                continue

                            ep = entry.get("endpoint", "unknown")

                            # Apply filters
                            if endpoint_filter and endpoint_filter not in ep:
                                continue
                            if status_filter == "success" and entry.get("status", 0) >= 400:
                                continue
                            if status_filter == "error" and entry.get("status", 0) < 400:
                                continue

                            all_entries.append(entry)

                            # Aggregate endpoint stats
                            s = endpoint_stats[ep]
                            s["count"] += 1
                            dur = entry.get("duration_s", 0)
                            s["total_duration"] += dur
                            s["min_duration"] = min(s["min_duration"], dur)
                            s["max_duration"] = max(s["max_duration"], dur)
                            if entry.get("status", 200) >= 400:
                                s["errors"] += 1
                        except (json.JSONDecodeError, KeyError):
                            continue
            except OSError:
                continue

        # Sort by timestamp descending, limit
        all_entries.sort(key=lambda e: e.get("ts", 0), reverse=True)
        recent = all_entries[:limit]

        # Build endpoint summary
        endpoints_summary = {}
        for ep, s in sorted(endpoint_stats.items(), key=lambda x: x[1]["count"], reverse=True):
            n = s["count"]
            endpoints_summary[ep] = {
                "count": n,
                "avg_duration_s": round(s["total_duration"] / n, 4) if n > 0 else 0,
                "min_duration_s": round(s["min_duration"], 4) if s["min_duration"] != float("inf") else 0,
                "max_duration_s": round(s["max_duration"], 4),
                "errors": s["errors"],
                "error_rate": round((s["errors"] / n) * 100, 1) if n > 0 else 0,
            }

        total_requests = sum(s["count"] for s in endpoint_stats.values())
        total_errors = sum(s["errors"] for s in endpoint_stats.values())

        return {
            "entries": recent,
            "summary": {
                "total_requests": total_requests,
                "total_errors": total_errors,
                "error_rate": round((total_errors / total_requests) * 100, 1) if total_requests > 0 else 0,
                "unique_endpoints": len(endpoint_stats),
            },
            "endpoints": endpoints_summary,
            "period_hours": hours,
        }


# Singleton
_request_log: Optional[RequestLogStore] = None


def get_request_log() -> RequestLogStore:
    global _request_log
    if _request_log is None:
        _request_log = RequestLogStore()
    return _request_log


# =============================================================================
# 2. CC-Usage Snapshots — account limit history
# =============================================================================

class CCUsageStore:
    """
    Stores Claude Code account usage snapshots over time.

    Written by CUI scraper (one file, not per-worker).
    Each entry is a point-in-time snapshot of all accounts.
    """

    JSONL_PATH = os.path.join(METRICS_DIR, "cc_usage_snapshots.jsonl")

    def __init__(self):
        _ensure_dir()

    def record_snapshot(self, accounts: List[dict]) -> None:
        """
        Append a snapshot of all account usage data.

        Args:
            accounts: List of account dicts from the scraper, each with:
                account, plan, currentSession, weeklyAllModels,
                weeklySonnet, extraUsage, etc.
        """
        entry = {
            "ts": round(time.time(), 3),
            "accounts": accounts,
        }
        try:
            with open(self.JSONL_PATH, "a") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
            logger.debug(f"CC usage snapshot saved: {len(accounts)} accounts")
        except OSError as e:
            logger.warning(f"Cannot write CC usage snapshot: {e}")

    def get_history(self, hours: int = 168, limit: int = 500) -> Dict[str, Any]:
        """
        Get CC usage snapshots for the given time window.

        Returns chronological list of snapshots.
        """
        cutoff = time.time() - (hours * 3600) if hours > 0 else 0
        snapshots: List[dict] = []

        if not os.path.exists(self.JSONL_PATH):
            return {"snapshots": [], "period_hours": hours}

        try:
            with open(self.JSONL_PATH, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("ts", 0) >= cutoff:
                            snapshots.append(entry)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass

        # Keep most recent entries if over limit
        if len(snapshots) > limit:
            snapshots = snapshots[-limit:]

        return {
            "snapshots": snapshots,
            "total_snapshots": len(snapshots),
            "period_hours": hours,
        }


# Singleton
_cc_usage: Optional[CCUsageStore] = None


def get_cc_usage_store() -> CCUsageStore:
    global _cc_usage
    if _cc_usage is None:
        _cc_usage = CCUsageStore()
    return _cc_usage
