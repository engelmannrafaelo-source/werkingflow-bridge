"""
Rolling Metrics — In-memory ring buffer for short-term rates and queue forecasting.

Per-worker, 1-second buckets, 10-minute window. Used by the queue-forecast endpoint
to estimate when the next rate-limit hit is likely and how the queue will drain.

NOT persisted (intentionally). This is a sliding window computed in RAM only.
For historical / cumulative data, see prompt_metrics.py and bridge_metrics_store.py.

Three event types are recorded:
    - arrival:    request entered the worker
    - completion: request finished (success/error)
    - rate_limit: rate-limit event observed (real, non-empty)

Each worker tracks its own in-flight count (incremented on arrival, decremented on
completion). The summary returns rates (per minute) and absolute in-flight counts.
"""

import os
import time
import threading
import logging
from collections import defaultdict
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 600  # 10-minute sliding window
WORKER_NAME = os.getenv("INSTANCE_NAME", "unknown")


class _Bucket:
    """One 1-second bucket of metrics."""
    __slots__ = (
        "arrivals", "completions", "errors", "rate_limit_hits",
        "input_tokens", "output_tokens", "duration_ms_sum",
    )

    def __init__(self) -> None:
        self.arrivals = 0
        self.completions = 0
        self.errors = 0
        self.rate_limit_hits = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.duration_ms_sum = 0


class RollingMetrics:
    """
    Thread-safe rolling window of per-worker rates. Single-process, in-memory.

    All public methods are O(1) amortised; pruning happens lazily on each write.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (worker, bucket_ts) -> _Bucket
        self._buckets: Dict[tuple, _Bucket] = {}
        # worker -> in-flight request count
        self._in_flight: Dict[str, int] = defaultdict(int)
        # worker -> sum of estimated input tokens for in-flight requests
        self._in_flight_input_tokens: Dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _now_bucket(self) -> int:
        return int(time.time())

    def _bucket(self, worker: str, ts: int) -> _Bucket:
        key = (worker, ts)
        b = self._buckets.get(key)
        if b is None:
            b = _Bucket()
            self._buckets[key] = b
        return b

    def _prune(self, now_bucket: int) -> None:
        cutoff = now_bucket - WINDOW_SECONDS
        # Cheap-ish prune: only sweep ~once per second worst-case
        stale = [k for k in self._buckets.keys() if k[1] < cutoff]
        for k in stale:
            del self._buckets[k]

    # ------------------------------------------------------------------
    # Recording API (called from request lifecycle)
    # ------------------------------------------------------------------
    def record_arrival(self, worker: str, est_input_tokens: int = 0) -> None:
        """Mark a request as received by `worker`."""
        with self._lock:
            ts = self._now_bucket()
            self._prune(ts)
            b = self._bucket(worker, ts)
            b.arrivals += 1
            self._in_flight[worker] += 1
            self._in_flight_input_tokens[worker] += max(0, est_input_tokens)

    def record_completion(
        self,
        worker: str,
        status: str,
        duration_ms: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
        est_input_tokens_at_arrival: int = 0,
    ) -> None:
        """
        Mark a request as finished. `status` is "success" / "error" / "timeout".
        """
        with self._lock:
            ts = self._now_bucket()
            self._prune(ts)
            b = self._bucket(worker, ts)
            b.completions += 1
            if status != "success":
                b.errors += 1
            b.input_tokens += max(0, input_tokens)
            b.output_tokens += max(0, output_tokens)
            b.duration_ms_sum += max(0, duration_ms)

            # Decrement in-flight (clamp to zero defensively)
            self._in_flight[worker] = max(0, self._in_flight[worker] - 1)
            decrement = est_input_tokens_at_arrival or input_tokens
            self._in_flight_input_tokens[worker] = max(
                0, self._in_flight_input_tokens[worker] - decrement
            )

    def record_rate_limit(self, worker: str) -> None:
        """Mark that a real rate-limit event was observed on `worker`."""
        with self._lock:
            ts = self._now_bucket()
            self._prune(ts)
            self._bucket(worker, ts).rate_limit_hits += 1

    # ------------------------------------------------------------------
    # Read API (called from /v1/metrics/queue-forecast)
    # ------------------------------------------------------------------
    def get_summary(self, window_seconds: int = 60) -> Dict[str, Any]:
        """
        Aggregate buckets across the given window. Returns per-worker counters
        and global totals. Window is clamped to [1, WINDOW_SECONDS].
        """
        window_seconds = max(1, min(WINDOW_SECONDS, window_seconds))
        with self._lock:
            now_ts = self._now_bucket()
            self._prune(now_ts)
            cutoff = now_ts - window_seconds

            # worker -> aggregated counters
            per_worker: Dict[str, Dict[str, int]] = {}
            for (worker, ts), b in self._buckets.items():
                if ts < cutoff:
                    continue
                w = per_worker.setdefault(worker, {
                    "arrivals": 0, "completions": 0, "errors": 0,
                    "rate_limit_hits": 0, "input_tokens": 0,
                    "output_tokens": 0, "duration_ms_sum": 0,
                })
                w["arrivals"] += b.arrivals
                w["completions"] += b.completions
                w["errors"] += b.errors
                w["rate_limit_hits"] += b.rate_limit_hits
                w["input_tokens"] += b.input_tokens
                w["output_tokens"] += b.output_tokens
                w["duration_ms_sum"] += b.duration_ms_sum

            # Build per-worker output with rates
            workers_out: Dict[str, Dict[str, Any]] = {}
            for w_name, agg in per_worker.items():
                comp = agg["completions"]
                workers_out[w_name] = {
                    "arrivals": agg["arrivals"],
                    "completions": comp,
                    "errors": agg["errors"],
                    "rate_limit_hits": agg["rate_limit_hits"],
                    "input_tokens": agg["input_tokens"],
                    "output_tokens": agg["output_tokens"],
                    "arrivals_per_min": round(agg["arrivals"] * 60.0 / window_seconds, 2),
                    "completions_per_min": round(comp * 60.0 / window_seconds, 2),
                    "input_tokens_per_min": round(agg["input_tokens"] * 60.0 / window_seconds),
                    "output_tokens_per_min": round(agg["output_tokens"] * 60.0 / window_seconds),
                    "avg_duration_ms": round(agg["duration_ms_sum"] / comp) if comp > 0 else 0,
                    "in_flight": self._in_flight.get(w_name, 0),
                    "in_flight_input_tokens_est": self._in_flight_input_tokens.get(w_name, 0),
                }

            # Add workers that have in-flight but no recent activity
            for w_name, ifc in self._in_flight.items():
                if w_name not in workers_out:
                    workers_out[w_name] = {
                        "arrivals": 0, "completions": 0, "errors": 0,
                        "rate_limit_hits": 0,
                        "input_tokens": 0, "output_tokens": 0,
                        "arrivals_per_min": 0, "completions_per_min": 0,
                        "input_tokens_per_min": 0, "output_tokens_per_min": 0,
                        "avg_duration_ms": 0,
                        "in_flight": ifc,
                        "in_flight_input_tokens_est": self._in_flight_input_tokens.get(w_name, 0),
                    }

            # Global totals
            totals = {
                "arrivals": sum(w["arrivals"] for w in workers_out.values()),
                "completions": sum(w["completions"] for w in workers_out.values()),
                "errors": sum(w["errors"] for w in workers_out.values()),
                "rate_limit_hits": sum(w["rate_limit_hits"] for w in workers_out.values()),
                "input_tokens": sum(w["input_tokens"] for w in workers_out.values()),
                "output_tokens": sum(w["output_tokens"] for w in workers_out.values()),
                "in_flight": sum(w["in_flight"] for w in workers_out.values()),
                "in_flight_input_tokens_est": sum(w["in_flight_input_tokens_est"] for w in workers_out.values()),
            }
            totals["arrivals_per_min"] = round(totals["arrivals"] * 60.0 / window_seconds, 2)
            totals["completions_per_min"] = round(totals["completions"] * 60.0 / window_seconds, 2)
            totals["input_tokens_per_min"] = round(totals["input_tokens"] * 60.0 / window_seconds)
            totals["output_tokens_per_min"] = round(totals["output_tokens"] * 60.0 / window_seconds)

            return {
                "window_seconds": window_seconds,
                "now": now_ts,
                "worker_self": WORKER_NAME,
                "workers": workers_out,
                "totals": totals,
            }


# Singleton
_rolling: Optional[RollingMetrics] = None
_rolling_lock = threading.Lock()


def get_rolling_metrics() -> RollingMetrics:
    """Get singleton RollingMetrics instance."""
    global _rolling
    if _rolling is None:
        with _rolling_lock:
            if _rolling is None:
                _rolling = RollingMetrics()
    return _rolling
