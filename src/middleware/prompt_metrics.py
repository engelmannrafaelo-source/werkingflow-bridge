"""
Prompt Performance Metrics Collector

Tracks per-prompt-type (app_id + agent_id) performance stats:
- Duration (avg, p50, p95, min, max)
- Success/Error/Timeout rates
- Token usage
- Call count

Data is persisted to JSONL files on a shared Docker volume so metrics
survive worker restarts and are visible across all workers.

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
from typing import Optional, Dict, List, Any

logger = logging.getLogger(__name__)

# Where JSONL files live (shared Docker volume mounted at /app/logs)
METRICS_DIR = os.path.join(
    os.getenv("METRICS_DIR", "/app/logs"),
    "bridge-metrics"
)

# Retention: auto-prune calls older than this
RETENTION_DAYS = 7
RETENTION_SECONDS = RETENTION_DAYS * 24 * 3600


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
    last_call: float = 0
    last_error: Optional[str] = None
    last_error_time: Optional[float] = None


class PromptMetricsCollector:
    """
    Persistent collector for prompt performance metrics.

    Thread-safe. Persists to JSONL on shared volume.
    Each worker writes its own file, reads all files for aggregation.
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

        cutoff = time.time() - RETENTION_SECONDS
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
                        if data.get("timestamp", 0) < cutoff:
                            skipped += 1
                            continue
                        call = PromptCall(
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
                            worker=data.get("worker", self._worker_id),
                        )
                        self._calls.append(call)
                        loaded += 1
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.debug(f"Skipping malformed metrics line: {e}")
                        skipped += 1

            logger.info(
                f"Loaded {loaded} metrics from disk "
                f"(skipped {skipped} old/malformed), file: {self._jsonl_path}"
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
                            call = PromptCall(
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
                                worker=data.get("worker", worker_name),
                            )
                            all_calls.append(call)
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
        )
        with self._lock:
            self._calls.append(call)

        # Persist to disk (outside lock — append is atomic enough for JSONL)
        self._append_to_disk(call)

    def _prune(self) -> None:
        """Remove calls older than retention window. Must hold lock."""
        cutoff = time.time() - RETENTION_SECONDS
        self._calls = [c for c in self._calls if c.timestamp >= cutoff]

    def prune_disk(self) -> None:
        """
        Rewrite this worker's JSONL file without expired entries.

        Call periodically (e.g., daily) to keep file size bounded.
        """
        cutoff = time.time() - RETENTION_SECONDS

        with self._lock:
            self._prune()
            current_calls = list(self._calls)

        try:
            tmp_path = self._jsonl_path + ".tmp"
            with open(tmp_path, "w") as f:
                for call in current_calls:
                    f.write(json.dumps(asdict(call), separators=(",", ":")) + "\n")
            os.replace(tmp_path, self._jsonl_path)
            logger.info(f"Pruned metrics file: {len(current_calls)} calls retained")
        except OSError as e:
            logger.warning(f"Cannot prune metrics file: {e}")

    def get_stats(self, hours: int = 24) -> Dict[str, Any]:
        """
        Get aggregated stats per app+agent for the last N hours.

        Reads from ALL worker files for complete cross-worker view.
        """
        cutoff = time.time() - (hours * 3600)

        # Get this worker's in-memory calls
        with self._lock:
            self._prune()
            own_calls = [c for c in self._calls if c.timestamp >= cutoff]

        # Get other workers' calls from disk
        other_calls = self._load_all_workers(cutoff)

        # Merge all calls
        relevant = own_calls + other_calls

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
            b.last_call = max(b.last_call, call.timestamp)

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
                "last_call_ago_s": round(time.time() - b.last_call) if b.last_call > 0 else None,
                "last_error": b.last_error,
                "last_error_ago_s": round(time.time() - b.last_error_time) if b.last_error_time else None,
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


# Singleton
_collector: Optional[PromptMetricsCollector] = None


def get_prompt_metrics() -> PromptMetricsCollector:
    """Get singleton prompt metrics collector."""
    global _collector
    if _collector is None:
        _collector = PromptMetricsCollector()
    return _collector
