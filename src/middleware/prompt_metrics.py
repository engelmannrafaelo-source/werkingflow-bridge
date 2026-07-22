"""
Prompt Performance Metrics Collector

Tracks per-prompt-type (app_id + agent_id) performance stats:
- Duration (avg, p50, p95, min, max)
- Success/Error/Timeout rates
- Token usage
- User/session attribution
- Call count

Data is persisted permanently to JSONL files on a shared Docker volume.
All data is cumulative — no pruning, no retention limit.

File layout (shared volume at /app/logs/bridge-metrics/):
    prompt_calls.worker1.jsonl
    prompt_calls.worker2.jsonl
    ...
Each worker appends only to its own file (no locking needed).
The metrics endpoint reads ALL files and merges them.
"""

import os
import json
import time
import glob
import threading
import logging
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, List, Any, Set

logger = logging.getLogger(__name__)

# Where JSONL files live (shared Docker volume mounted at /app/logs)
METRICS_DIR = os.path.join(
    os.getenv("METRICS_DIR", "/app/logs"),
    "bridge-metrics"
)

# agent_id values used by the document/privacy-service proxy endpoints
# (src/main.py) — single source of truth for the /v1/metrics/document-performance
# filter (worker route + metrics_reader route both import this constant so
# the two stay in sync without duplicating the list).
DOCUMENT_AGENT_IDS: Set[str] = {
    "anonymisierung",
    "dokument-konvertierung",
    "dokument-konvertierung-anonymisiert",
    "pdf-konvertierung",
    "pdf-zu-semantic-html",
    "pdf-zu-html-direkt",
    "html-zu-docx",
    "docx-zu-html",
    "pdf-export",
    "screenshot",
}


@dataclass
class PromptCall:
    """Single prompt call record."""
    timestamp: float
    app_id: str
    agent_id: str
    workflow_id: str
    duration_ms: int
    status: str  # success | error | timeout
    model: str
    input_tokens: int
    output_tokens: int
    error_code: Optional[str] = None
    worker: Optional[str] = None
    # Attribution
    user_id: Optional[str] = None
    session_id: Optional[str] = None
    job_id: Optional[str] = None
    # Capacity: calls already in flight (on the same worker) toward the
    # downstream service when this call started. Only populated by callers
    # that track their own concurrency (currently: privacy-service proxy
    # endpoints); None for everything else (e.g. LLM chat calls).
    concurrent_calls_at_start: Optional[int] = None


@dataclass
class AgentStats:
    """Aggregated stats for one app+agent combo."""
    app_id: str
    agent_id: str
    calls: int = 0
    successes: int = 0
    errors: int = 0
    timeouts: int = 0
    durations_ms: list = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    models: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    users: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    last_call: float = 0
    last_error: Optional[str] = None
    last_error_time: Optional[float] = None
    # Busy-rate: how often a call started while another was already in
    # flight on the same worker (see PromptCall.concurrent_calls_at_start).
    busy_samples: int = 0
    busy_starts: int = 0


def _parse_call(data: dict, fallback_worker: str = "unknown") -> PromptCall:
    """Parse a JSONL dict into a PromptCall. Tolerant of missing fields."""
    return PromptCall(
        timestamp=data["timestamp"],
        app_id=data.get("app_id", "unknown"),
        agent_id=data.get("agent_id", "unknown"),
        workflow_id=data.get("workflow_id", "unknown"),
        duration_ms=data.get("duration_ms", 0),
        status=data.get("status", "unknown"),
        model=data.get("model", "unknown"),
        input_tokens=data.get("input_tokens", 0),
        output_tokens=data.get("output_tokens", 0),
        error_code=data.get("error_code"),
        worker=data.get("worker", fallback_worker),
        user_id=data.get("user_id"),
        session_id=data.get("session_id"),
        job_id=data.get("job_id"),
        concurrent_calls_at_start=data.get("concurrent_calls_at_start"),
    )


class PromptMetricsCollector:
    """
    Persistent collector for prompt performance metrics.

    Thread-safe. Persists to JSONL on shared volume.
    Each worker writes its own file, reads all files for aggregation.
    All data is kept permanently (cumulative).
    """

    def __init__(self):
        self._calls: List[PromptCall] = []
        self._lock = threading.Lock()
        self._worker_id = os.getenv("INSTANCE_NAME", "unknown")
        self._jsonl_path = os.path.join(
            METRICS_DIR, f"prompt_calls.{self._worker_id}.jsonl"
        )
        self._ensure_dir()
        self._load_from_disk()

    def _ensure_dir(self) -> None:
        """Create metrics directory if it doesn't exist."""
        try:
            os.makedirs(METRICS_DIR, exist_ok=True)
        except OSError as e:
            logger.warning(f"Cannot create metrics dir {METRICS_DIR}: {e}")

    def _load_from_disk(self) -> None:
        """Load this worker's JSONL file into memory on startup."""
        if not os.path.exists(self._jsonl_path):
            logger.info(f"No existing metrics file: {self._jsonl_path}")
            return

        loaded = 0
        skipped = 0

        try:
            with open(self._jsonl_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        self._calls.append(_parse_call(data, self._worker_id))
                        loaded += 1
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.debug(f"Skipping malformed metrics line: {e}")
                        skipped += 1

            logger.info(
                f"Loaded {loaded} metrics from disk "
                f"(skipped {skipped} malformed), file: {self._jsonl_path}"
            )
        except OSError as e:
            logger.warning(f"Cannot read metrics file: {e}")

    def _append_to_disk(self, call: PromptCall) -> None:
        """Append a single call to this worker's JSONL file."""
        try:
            data = asdict(call)
            with open(self._jsonl_path, "a") as f:
                f.write(json.dumps(data, separators=(",", ":")) + "\n")
        except OSError as e:
            logger.warning(f"Cannot write metrics to disk: {e}")

    def _load_all_workers(self, cutoff: float) -> List[PromptCall]:
        """
        Read calls from ALL worker JSONL files (for cross-worker aggregation).

        Returns calls newer than cutoff timestamp.
        """
        all_calls: List[PromptCall] = []
        pattern = os.path.join(METRICS_DIR, "prompt_calls.*.jsonl")

        for filepath in glob.glob(pattern):
            worker_name = os.path.basename(filepath).replace(
                "prompt_calls.", ""
            ).replace(".jsonl", "")

            # For our own file, use in-memory data (faster, already loaded)
            if worker_name == self._worker_id:
                continue

            try:
                with open(filepath, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            if data.get("timestamp", 0) < cutoff:
                                continue
                            all_calls.append(_parse_call(data, worker_name))
                        except (json.JSONDecodeError, KeyError):
                            continue
            except OSError:
                continue

        return all_calls

    def record(
        self,
        app_id: Optional[str],
        agent_id: Optional[str],
        workflow_id: Optional[str],
        duration_ms: int,
        status: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        error_code: Optional[str] = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        job_id: Optional[str] = None,
        concurrent_calls_at_start: Optional[int] = None,
    ) -> None:
        """Record a single prompt call (in-memory + disk)."""
        call = PromptCall(
            timestamp=time.time(),
            app_id=app_id or "unknown",
            agent_id=agent_id or "unknown",
            workflow_id=workflow_id or "unknown",
            duration_ms=duration_ms,
            status=status,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error_code=error_code,
            worker=self._worker_id,
            user_id=user_id,
            session_id=session_id,
            job_id=job_id,
            concurrent_calls_at_start=concurrent_calls_at_start,
        )
        with self._lock:
            self._calls.append(call)

        # Persist to disk (outside lock — append is atomic enough for JSONL)
        self._append_to_disk(call)

    def get_stats(self, hours: int = 24, agent_ids: Optional[Set[str]] = None) -> Dict[str, Any]:
        """
        Get aggregated stats per app+agent for the last N hours.

        Reads from ALL worker files for complete cross-worker view.

        Args:
            hours: Time window (0 = all time).
            agent_ids: If given, only include calls whose agent_id is in this
                set (e.g. DOCUMENT_AGENT_IDS for the document-performance view).
        """
        # hours=0 means all time
        cutoff = time.time() - (hours * 3600) if hours > 0 else 0

        # Get this worker's in-memory calls
        with self._lock:
            own_calls = [c for c in self._calls if c.timestamp >= cutoff]

        # Get other workers' calls from disk
        other_calls = self._load_all_workers(cutoff)

        # Merge all calls
        relevant = own_calls + other_calls
        if agent_ids is not None:
            relevant = [c for c in relevant if c.agent_id in agent_ids]

        # Aggregate by app_id + agent_id
        buckets: Dict[str, AgentStats] = {}
        for call in relevant:
            key = f"{call.app_id}::{call.agent_id}"
            if key not in buckets:
                buckets[key] = AgentStats(app_id=call.app_id, agent_id=call.agent_id)
            b = buckets[key]
            b.calls += 1
            b.durations_ms.append(call.duration_ms)
            b.total_input_tokens += call.input_tokens
            b.total_output_tokens += call.output_tokens
            b.models[call.model] += 1
            if call.user_id:
                b.users[call.user_id] += 1
            b.last_call = max(b.last_call, call.timestamp)

            if call.concurrent_calls_at_start is not None:
                b.busy_samples += 1
                if call.concurrent_calls_at_start > 0:
                    b.busy_starts += 1

            if call.status == "success":
                b.successes += 1
            elif call.status == "timeout":
                b.timeouts += 1
            else:
                b.errors += 1
                b.last_error = call.error_code
                b.last_error_time = call.timestamp

        # Build response
        agents = []
        total_calls = 0
        total_errors = 0

        for key, b in sorted(buckets.items(), key=lambda x: x[1].calls, reverse=True):
            durations = sorted(b.durations_ms)
            n = len(durations)
            agents.append({
                "app_id": b.app_id,
                "agent_id": b.agent_id,
                "calls": b.calls,
                "successes": b.successes,
                "errors": b.errors,
                "timeouts": b.timeouts,
                "error_rate": round(((b.errors + b.timeouts) / b.calls) * 100, 1) if b.calls > 0 else 0,
                "duration_ms": {
                    "avg": round(sum(durations) / n) if n > 0 else 0,
                    "p50": durations[n // 2] if n > 0 else 0,
                    "p95": durations[int(n * 0.95)] if n > 0 else 0,
                    "min": durations[0] if n > 0 else 0,
                    "max": durations[-1] if n > 0 else 0,
                },
                "tokens": {
                    "avg_input": round(b.total_input_tokens / b.calls) if b.calls > 0 else 0,
                    "avg_output": round(b.total_output_tokens / b.calls) if b.calls > 0 else 0,
                    "total_input": b.total_input_tokens,
                    "total_output": b.total_output_tokens,
                },
                "models": dict(b.models),
                "users": dict(b.users),
                "last_call_ago_s": round(time.time() - b.last_call) if b.last_call > 0 else None,
                "last_error": b.last_error,
                "last_error_ago_s": round(time.time() - b.last_error_time) if b.last_error_time else None,
                # Only meaningful for callers that pass concurrent_calls_at_start
                # (currently the privacy-service proxy endpoints). None = not tracked.
                "busy_samples": b.busy_samples,
                "busy_starts": b.busy_starts,
                "busy_rate_pct": round((b.busy_starts / b.busy_samples) * 100, 1) if b.busy_samples > 0 else None,
            })
            total_calls += b.calls
            total_errors += b.errors + b.timeouts

        return {
            "agents": agents,
            "summary": {
                "total_calls": total_calls,
                "total_agents": len(agents),
                "total_errors": total_errors,
                "overall_error_rate": round((total_errors / total_calls) * 100, 1) if total_calls > 0 else 0,
            },
            "period_hours": hours,
            "raw_calls_stored": len(self._calls),
            "total_calls_all_workers": len(relevant),
        }

    def get_timeline(self, app_id: str, agent_id: str, hours: int = 24, bucket_minutes: int = 60) -> Dict[str, Any]:
        """
        Get timeline data for a specific agent (for charts).

        Groups calls into time buckets and returns avg duration + error rate per bucket.
        Reads from ALL workers.
        """
        cutoff = time.time() - (hours * 3600)
        bucket_seconds = bucket_minutes * 60

        # Merge own + other workers
        with self._lock:
            own_calls = [
                c for c in self._calls
                if c.timestamp >= cutoff and c.app_id == app_id and c.agent_id == agent_id
            ]

        other_calls = [
            c for c in self._load_all_workers(cutoff)
            if c.app_id == app_id and c.agent_id == agent_id
        ]

        relevant = own_calls + other_calls

        # Group into time buckets
        time_buckets: Dict[int, List[PromptCall]] = defaultdict(list)
        for call in relevant:
            bucket_key = int(call.timestamp // bucket_seconds) * bucket_seconds
            time_buckets[bucket_key].append(call)

        timeline = []
        for ts in sorted(time_buckets.keys()):
            calls = time_buckets[ts]
            durations = [c.duration_ms for c in calls]
            errors = sum(1 for c in calls if c.status != "success")
            timeline.append({
                "timestamp": ts,
                "calls": len(calls),
                "avg_duration_ms": round(sum(durations) / len(durations)),
                "max_duration_ms": max(durations),
                "error_rate": round((errors / len(calls)) * 100, 1),
            })

        return {
            "app_id": app_id,
            "agent_id": agent_id,
            "period_hours": hours,
            "bucket_minutes": bucket_minutes,
            "timeline": timeline,
        }

    def get_usage_breakdown(self, hours: int = 24) -> Dict[str, Any]:
        """
        Token/cost breakdown grouped by app, user, model.

        Used by the Stats tab and Sankey visualisation.

        Returns:
          summary       — totals (calls, in/out/total tokens, errors)
          apps          — per app_id with nested agents+users counts
          users         — per user_id with nested apps counts
          models        — per model
          sankey_links  — app → user → model flow links
        """
        cutoff = time.time() - (hours * 3600) if hours > 0 else 0

        with self._lock:
            own_calls = [c for c in self._calls if c.timestamp >= cutoff]
        other_calls = self._load_all_workers(cutoff)
        all_calls = own_calls + other_calls

        # Per-app
        app_buckets: Dict[str, Dict[str, Any]] = {}
        # Per-user
        user_buckets: Dict[str, Dict[str, Any]] = {}
        # Per-model
        model_buckets: Dict[str, Dict[str, Any]] = {}
        # Sankey edges (counts)
        app_user: Dict[tuple, int] = defaultdict(int)
        user_model: Dict[tuple, int] = defaultdict(int)

        total_calls = 0
        total_in = 0
        total_out = 0
        total_err = 0

        for c in all_calls:
            in_tok = c.input_tokens or 0
            out_tok = c.output_tokens or 0
            tot_tok = in_tok + out_tok
            is_err = c.status != "success"

            total_calls += 1
            total_in += in_tok
            total_out += out_tok
            if is_err:
                total_err += 1

            # App
            a = app_buckets.setdefault(c.app_id, {
                "app_id": c.app_id,
                "calls": 0, "input_tokens": 0, "output_tokens": 0,
                "total_tokens": 0, "errors": 0,
                "agents": defaultdict(int), "users": defaultdict(int),
            })
            a["calls"] += 1
            a["input_tokens"] += in_tok
            a["output_tokens"] += out_tok
            a["total_tokens"] += tot_tok
            if is_err:
                a["errors"] += 1
            a["agents"][c.agent_id] += 1
            if c.user_id:
                a["users"][c.user_id] += 1

            # User
            uid = c.user_id or "anonymous"
            u = user_buckets.setdefault(uid, {
                "user_id": uid,
                "calls": 0, "input_tokens": 0, "output_tokens": 0,
                "total_tokens": 0, "apps": defaultdict(int),
            })
            u["calls"] += 1
            u["input_tokens"] += in_tok
            u["output_tokens"] += out_tok
            u["total_tokens"] += tot_tok
            u["apps"][c.app_id] += 1

            # Model
            m = model_buckets.setdefault(c.model, {
                "model": c.model,
                "calls": 0, "input_tokens": 0, "output_tokens": 0,
                "total_tokens": 0,
            })
            m["calls"] += 1
            m["input_tokens"] += in_tok
            m["output_tokens"] += out_tok
            m["total_tokens"] += tot_tok

            # Sankey
            app_user[(c.app_id, uid)] += 1
            user_model[(uid, c.model)] += 1

        # Convert defaultdicts → regular dicts and compute error_rate
        apps_list = []
        for a in sorted(app_buckets.values(), key=lambda x: x["total_tokens"], reverse=True):
            apps_list.append({
                **a,
                "agents": dict(a["agents"]),
                "users": dict(a["users"]),
                "error_rate": round((a["errors"] / a["calls"]) * 100, 1) if a["calls"] > 0 else 0.0,
            })

        users_list = []
        for u in sorted(user_buckets.values(), key=lambda x: x["total_tokens"], reverse=True):
            users_list.append({**u, "apps": dict(u["apps"])})

        models_list = sorted(model_buckets.values(), key=lambda x: x["total_tokens"], reverse=True)

        sankey_links = []
        for (src, tgt), val in app_user.items():
            sankey_links.append({"source": f"app:{src}", "target": f"user:{tgt}", "value": val})
        for (src, tgt), val in user_model.items():
            sankey_links.append({"source": f"user:{src}", "target": f"model:{tgt}", "value": val})

        return {
            "summary": {
                "total_calls": total_calls,
                "total_input_tokens": total_in,
                "total_output_tokens": total_out,
                "total_tokens": total_in + total_out,
                "total_errors": total_err,
            },
            "apps": apps_list,
            "users": users_list,
            "models": models_list,
            "sankey_links": sankey_links,
            "period_hours": hours,
        }

    def get_recent_calls(
        self,
        hours: int = 24,
        limit: int = 200,
        app_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Return raw individual calls (newest first) with full attribution.

        Used by the Stats / Activity tab to show a live feed of recent prompts
        with model, tokens, status, duration, and user.
        """
        cutoff = time.time() - (hours * 3600) if hours > 0 else 0

        with self._lock:
            own_calls = [c for c in self._calls if c.timestamp >= cutoff]
        other_calls = self._load_all_workers(cutoff)

        all_calls = own_calls + other_calls

        # Optional filters
        if app_id:
            all_calls = [c for c in all_calls if c.app_id == app_id]
        if user_id:
            all_calls = [c for c in all_calls if c.user_id == user_id]

        # Newest first
        all_calls.sort(key=lambda c: c.timestamp, reverse=True)
        truncated = all_calls[:limit]

        return {
            "calls": [asdict(c) for c in truncated],
            "period_hours": hours,
            "limit": limit,
            "returned": len(truncated),
            "total_matching": len(all_calls),
            "filters": {"app_id": app_id, "user_id": user_id},
        }

    def get_throughput(self, hours: int = 24, bucket_seconds: int = 60) -> Dict[str, Any]:
        """
        Per-worker throughput timeline + empirical capacity ceiling.

        Each prompt-call record falls into one of three categories:
          1. success                — worker handled the call, upstream OK
          2. bridge_concurrency_503 — worker rejected (max_concurrent reached);
                                      logged once per rejection, then nginx
                                      retries the next worker. This is the
                                      bridge's BACKPRESSURE signal — when this
                                      starts firing, we are at internal capacity.
          3. upstream_error         — worker tried, upstream returned non-OK
                                      (429 rate limit, 5xx, timeout, fallback fail)

        Per bucket we expose all three rates so the operator can SEE which
        ceiling we hit first (bridge concurrency vs Anthropic limits).

        Per bucket fields:
          - success_rpm     : successful requests per minute
          - upstream_err_rpm: non-503 errors per minute (real upstream failures)
          - reject_rpm      : 503 concurrency rejections per minute (backpressure)
          - offered_rpm     : success + upstream_err + reject  (= total demand)
          - in_tpm/out_tpm  : tokens per minute (success calls only)
          - had_429         : any "rate_limit" / 429 marker in bucket
          - had_5xx         : any non-503 5xx in bucket

        Empirical capacity ceiling (per worker):
          - first_reject_rpm  : offered_rpm at the bucket where 503 rejections
                                first appeared (= the rate at which this worker
                                started saturating its concurrency slot)
          - max_clean_rpm     : highest sustained success_rpm with 0 rejects + 0 errors
          - recommendation_rpm: safe per-worker throttle =
                                  min(max_clean × 0.9, first_reject × 0.8)

        The recommendation is an instruction to the CALLER:
          "keep your offered load below this many req/min PER WORKER and the
           bridge will not start dropping concurrency-limited requests."
        Multiply by N workers for total bridge capacity.
        """
        cutoff = time.time() - (hours * 3600) if hours > 0 else 0
        norm_factor = 60.0 / bucket_seconds

        with self._lock:
            own_calls = [c for c in self._calls if c.timestamp >= cutoff]
        other_calls = self._load_all_workers(cutoff)
        all_calls = own_calls + other_calls

        per_worker: Dict[str, List[PromptCall]] = defaultdict(list)
        for call in all_calls:
            wkey = call.worker or "unknown"
            per_worker[wkey].append(call)

        result: Dict[str, Any] = {}

        for wkey, calls in per_worker.items():
            buckets: Dict[int, Dict[str, Any]] = defaultdict(
                lambda: {"success": 0, "upstream_err": 0, "reject_503": 0,
                         "in": 0, "out": 0,
                         "had_429": False, "had_5xx": False}
            )
            errors_list: List[Dict[str, Any]] = []

            for c in calls:
                bk = int(c.timestamp // bucket_seconds) * bucket_seconds
                b = buckets[bk]

                code = (c.error_code or "").lower()
                is_success = c.status == "success"
                is_503 = (not is_success) and ("503" in code)

                if is_success:
                    b["success"] += 1
                    b["in"] += c.input_tokens or 0
                    b["out"] += c.output_tokens or 0
                elif is_503:
                    b["reject_503"] += 1
                else:
                    b["upstream_err"] += 1
                    if "rate_limit" in code or "429" in code:
                        b["had_429"] = True
                    elif code:
                        b["had_5xx"] = True
                    errors_list.append({
                        "ts": c.timestamp,
                        "code": c.error_code,
                        "status": c.status,
                        "duration_ms": c.duration_ms,
                    })

            sorted_buckets = []
            for ts in sorted(buckets.keys()):
                b = buckets[ts]
                offered = b["success"] + b["upstream_err"] + b["reject_503"]
                sorted_buckets.append({
                    "ts": ts,
                    "success_rpm": round(b["success"] * norm_factor, 2),
                    "upstream_err_rpm": round(b["upstream_err"] * norm_factor, 2),
                    "reject_rpm": round(b["reject_503"] * norm_factor, 2),
                    "offered_rpm": round(offered * norm_factor, 2),
                    "in_tpm": round(b["in"] * norm_factor),
                    "out_tpm": round(b["out"] * norm_factor),
                    "had_429": b["had_429"],
                    "had_5xx": b["had_5xx"],
                    # legacy aliases for older clients
                    "rpm": round(b["success"] * norm_factor, 2),
                    "err_count": b["upstream_err"],
                    "rejected": b["reject_503"],
                    "rejected_per_min": round(b["reject_503"] * norm_factor, 2),
                })

            # Empirical ceiling
            reject_buckets = [b for b in sorted_buckets if b["reject_rpm"] > 0]
            err_buckets = [b for b in sorted_buckets if b["upstream_err_rpm"] > 0]
            clean_buckets = [
                b for b in sorted_buckets
                if b["reject_rpm"] == 0 and b["upstream_err_rpm"] == 0 and b["success_rpm"] > 0
            ]

            ceiling: Dict[str, Any] = {
                "samples_total": len(sorted_buckets),
                "samples_clean": len(clean_buckets),
                "samples_with_rejects": len(reject_buckets),
                "samples_with_upstream_errors": len(err_buckets),
                "first_reject_offered_rpm": None,
                "first_reject_success_rpm": None,
                "first_reject_ts": None,
                "first_upstream_err_offered_rpm": None,
                "first_upstream_err_ts": None,
                "max_clean_success_rpm": None,
                "max_clean_in_tpm": None,
                "max_clean_out_tpm": None,
                "recommendation_rpm": None,
                "recommendation_in_tpm": None,
                "recommendation_out_tpm": None,
                "basis": "no_data",
            }

            if reject_buckets:
                first_r = reject_buckets[0]
                ceiling["first_reject_offered_rpm"] = first_r["offered_rpm"]
                ceiling["first_reject_success_rpm"] = first_r["success_rpm"]
                ceiling["first_reject_ts"] = first_r["ts"]

            if err_buckets:
                first_e = err_buckets[0]
                ceiling["first_upstream_err_offered_rpm"] = first_e["offered_rpm"]
                ceiling["first_upstream_err_ts"] = first_e["ts"]

            if clean_buckets:
                ceiling["max_clean_success_rpm"] = max(b["success_rpm"] for b in clean_buckets)
                ceiling["max_clean_in_tpm"] = max(b["in_tpm"] for b in clean_buckets)
                ceiling["max_clean_out_tpm"] = max(b["out_tpm"] for b in clean_buckets)

            # Recommendation: stay below whichever ceiling we hit first
            candidates = []
            if ceiling["max_clean_success_rpm"] is not None:
                candidates.append(("clean", ceiling["max_clean_success_rpm"] * 0.9))
            if ceiling["first_reject_offered_rpm"] is not None:
                candidates.append(("reject", ceiling["first_reject_offered_rpm"] * 0.8))
            if ceiling["first_upstream_err_offered_rpm"] is not None:
                candidates.append(("upstream", ceiling["first_upstream_err_offered_rpm"] * 0.8))

            if candidates:
                basis, rec = min(candidates, key=lambda x: x[1])
                ceiling["recommendation_rpm"] = round(rec, 2)
                ceiling["basis"] = basis
                if ceiling["max_clean_in_tpm"] is not None:
                    # scale token recommendation proportionally to rpm reduction
                    if ceiling["max_clean_success_rpm"]:
                        scale = rec / ceiling["max_clean_success_rpm"]
                    else:
                        scale = 1.0
                    ceiling["recommendation_in_tpm"] = round(ceiling["max_clean_in_tpm"] * scale)
                    ceiling["recommendation_out_tpm"] = round(ceiling["max_clean_out_tpm"] * scale)

            current = sorted_buckets[-1] if sorted_buckets else None

            result[wkey] = {
                "buckets": sorted_buckets,
                "errors": errors_list,
                "ceiling": ceiling,
                "current": current,
            }

        # Aggregate totals across all workers
        total_success = sum(1 for c in all_calls if c.status == "success")
        total_503 = sum(
            1 for c in all_calls
            if c.status != "success" and "503" in (c.error_code or "")
        )
        total_upstream_err = sum(
            1 for c in all_calls
            if c.status != "success" and "503" not in (c.error_code or "")
        )
        bridge_recommendation = sum(
            (w["ceiling"].get("recommendation_rpm") or 0) for w in result.values()
        )

        return {
            "lookback_hours": hours,
            "bucket_seconds": bucket_seconds,
            "now": time.time(),
            "workers": result,
            "totals": {
                "calls": len(all_calls),
                "successes": total_success,
                "concurrency_rejects_503": total_503,
                "upstream_errors": total_upstream_err,
                "workers_seen": len(per_worker),
                "bridge_recommendation_rpm": round(bridge_recommendation, 2),
            },
        }


# Singleton
_collector: Optional[PromptMetricsCollector] = None


def get_prompt_metrics() -> PromptMetricsCollector:
    """Get singleton prompt metrics collector."""
    global _collector
    if _collector is None:
        _collector = PromptMetricsCollector()
    return _collector
