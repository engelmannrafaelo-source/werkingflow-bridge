"""
Prompt Performance Metrics Collector

Tracks per-prompt-type (app_id + agent_id) performance stats:
- Duration (avg, p50, p95, min, max)
- Success/Error/Timeout rates
- Token usage
- Call count

Data is kept in-memory with rolling 7-day window.
Designed to answer: "Which AI function is slow/broken?"
"""

import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any


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
    In-memory collector for prompt performance metrics.

    Thread-safe. Keeps raw calls for 7 days, aggregates on read.
    """

    RETENTION_SECONDS = 7 * 24 * 3600  # 7 days

    def __init__(self):
        self._calls: List[PromptCall] = []
        self._lock = threading.Lock()

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
        """Record a single prompt call."""
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
        )
        with self._lock:
            self._calls.append(call)

    def _prune(self) -> None:
        """Remove calls older than retention window. Must hold lock."""
        cutoff = time.time() - self.RETENTION_SECONDS
        self._calls = [c for c in self._calls if c.timestamp >= cutoff]

    def get_stats(self, hours: int = 24) -> Dict[str, Any]:
        """
        Get aggregated stats per app+agent for the last N hours.

        Returns dict with:
        - agents: list of per-agent stats
        - summary: overall totals
        - period_hours: time window
        """
        cutoff = time.time() - (hours * 3600)

        with self._lock:
            self._prune()
            relevant = [c for c in self._calls if c.timestamp >= cutoff]

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
        }

    def get_timeline(self, app_id: str, agent_id: str, hours: int = 24, bucket_minutes: int = 60) -> Dict[str, Any]:
        """
        Get timeline data for a specific agent (for charts).

        Groups calls into time buckets and returns avg duration + error rate per bucket.
        """
        cutoff = time.time() - (hours * 3600)
        bucket_seconds = bucket_minutes * 60

        with self._lock:
            relevant = [
                c for c in self._calls
                if c.timestamp >= cutoff and c.app_id == app_id and c.agent_id == agent_id
            ]

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
