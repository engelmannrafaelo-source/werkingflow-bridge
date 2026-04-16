"""
AdaptiveLoadLimiter — token-budget-based per-worker rate limiter that
auto-tunes its cap from observed Anthropic rate-limit events.

Concept
-------
Instead of a static "max 5 concurrent requests" rule (which treats one giant
request the same as five tiny ones), this limiter tracks the SUM of estimated
input tokens across all currently in-flight requests. New requests are admitted
only if `current_in_flight_tokens + estimated_request_tokens <= cap`.

The cap auto-tunes per worker:
  - SHRINK 10% when the worker observed a real Anthropic rate-limit hit in
    the last 5 minutes (cooldown 10 min between shrinks).
  - GROW 5% when the worker has been clean for 30 minutes AND its peak
    in-flight utilization in the last 10 min was >= 80% of the cap.
  - Bounds: floor (50k tokens) ... ceiling (2M tokens).

State is persisted per worker to JSONL on the shared metrics volume so the
auto-tuned cap survives restarts. Each tune event is appended for forensics
and for the UI's "auto-tune history" display.

This limiter assumes one Anthropic account per worker — that is true for the
WerkflowFlow bridge (worker1 = engelmann, worker2 = office, etc.). If that
ever changes, the per-worker cap interpretation needs to be revisited.
"""

from __future__ import annotations

import os
import json
import time
import asyncio
import logging
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, Any, List

from fastapi import HTTPException, Request

from src.middleware.bridge_error import (
    BridgeError,
    throttle_error,
    queue_timeout_error,
)

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Configuration (env-tunable)
# ----------------------------------------------------------------------
WORKER_NAME = os.getenv("INSTANCE_NAME", "unknown")
METRICS_DIR = os.path.join(os.getenv("METRICS_DIR", "/app/logs"), "bridge-metrics")

INITIAL_CAP_TOKENS = int(os.getenv("ADAPTIVE_INITIAL_CAP_TOKENS", "250000"))
FLOOR_TOKENS       = int(os.getenv("ADAPTIVE_FLOOR_TOKENS", "50000"))
CEILING_TOKENS     = int(os.getenv("ADAPTIVE_CEILING_TOKENS", "2000000"))

SHRINK_FACTOR        = float(os.getenv("ADAPTIVE_SHRINK_FACTOR", "0.9"))   # -10%
GROW_FACTOR          = float(os.getenv("ADAPTIVE_GROW_FACTOR",   "1.05"))  # +5%
SHRINK_TRIGGER_SEC   = int(os.getenv("ADAPTIVE_SHRINK_TRIGGER_SEC", "300"))   # 5 min
GROW_TRIGGER_SEC     = int(os.getenv("ADAPTIVE_GROW_TRIGGER_SEC",   "1800"))  # 30 min
GROW_UTILIZATION_PCT = float(os.getenv("ADAPTIVE_GROW_UTIL_PCT",   "80"))     # %
SHRINK_COOLDOWN_SEC  = int(os.getenv("ADAPTIVE_SHRINK_COOLDOWN_SEC", "600"))  # 10 min
TUNE_INTERVAL_SEC    = int(os.getenv("ADAPTIVE_TUNE_INTERVAL_SEC", "60"))     # tick

HARD_REQUEST_CEILING = int(os.getenv("ADAPTIVE_HARD_REQUEST_CEILING", "100"))
"""Optional safety net: never allow more than this many in-flight requests
regardless of token budget. Protects against runaway memory if estimates
are way off. 100 is generous; this is just a backstop, not a primary limit."""

SAFETY_MARGIN_PCT = float(os.getenv("ADAPTIVE_SAFETY_MARGIN_PCT", "85"))
"""Reserve the top N% of cap as a buffer. New requests are admitted only up
to (cap * SAFETY_MARGIN_PCT / 100). Protects against the first 429 slipping
through while the auto-tuner is still converging on the true ceiling."""

QUEUE_WAIT_TIMEOUT_SEC = int(os.getenv("ADAPTIVE_QUEUE_WAIT_SEC", "60"))
"""When the cap is full, await up to this many seconds for capacity to free
up before responding with `queue_timeout`. Apps see latency, not an error.
Set to 0 to disable queueing (immediate throttle reject)."""

QUEUE_POLL_INTERVAL_SEC = float(os.getenv("ADAPTIVE_QUEUE_POLL_SEC", "0.5"))


# ----------------------------------------------------------------------
# Dataclasses
# ----------------------------------------------------------------------
@dataclass
class TuneEvent:
    ts: float
    direction: str         # "shrink" | "grow" | "hold"
    reason: str
    cap_before: int
    cap_after: int
    observed_rate_limits: int = 0
    observed_peak_util_pct: float = 0.0


@dataclass
class LimiterState:
    """Persisted per-worker auto-tune state."""
    worker: str
    cap_tokens: int
    floor_tokens: int
    ceiling_tokens: int
    last_rate_limit_ts: Optional[float] = None
    last_shrink_ts: Optional[float] = None
    last_tune_ts: float = 0.0
    peak_inflight_window: List[tuple] = field(default_factory=list)
    """Rolling list of (ts, inflight_tokens) — used to compute peak utilization
    over a recent window. Trimmed to keep only entries within GROW_TRIGGER_SEC."""


# ----------------------------------------------------------------------
# Limiter
# ----------------------------------------------------------------------
class AdaptiveLoadLimiter:
    """Per-worker adaptive token-budget limiter."""

    def __init__(self) -> None:
        self.worker = WORKER_NAME
        self.state_path = os.path.join(METRICS_DIR, f"limiter_state.{self.worker}.json")
        self.events_path = os.path.join(METRICS_DIR, f"limiter_events.{self.worker}.jsonl")
        self._lock = asyncio.Lock()
        self._tune_task: Optional[asyncio.Task] = None
        self._stopped = False
        # Signaled whenever a request completes — wakes up any tasks waiting
        # for capacity. Created lazily so we bind it to the running event loop.
        self._capacity_event: Optional[asyncio.Event] = None
        # Counter of how many requests are currently parked in the queue.
        # Useful for the snapshot endpoint and for backpressure awareness.
        self._queued_count = 0

        os.makedirs(METRICS_DIR, exist_ok=True)
        self.state = self._load_state()
        logger.info(
            f"AdaptiveLoadLimiter[{self.worker}] initialized: "
            f"cap={self.state.cap_tokens:,} tokens "
            f"(floor={self.state.floor_tokens:,}, ceiling={self.state.ceiling_tokens:,})"
        )

    # ----- persistence -----
    def _load_state(self) -> LimiterState:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path) as f:
                    raw = json.load(f)
                return LimiterState(
                    worker=raw.get("worker", self.worker),
                    cap_tokens=int(raw.get("cap_tokens", INITIAL_CAP_TOKENS)),
                    floor_tokens=int(raw.get("floor_tokens", FLOOR_TOKENS)),
                    ceiling_tokens=int(raw.get("ceiling_tokens", CEILING_TOKENS)),
                    last_rate_limit_ts=raw.get("last_rate_limit_ts"),
                    last_shrink_ts=raw.get("last_shrink_ts"),
                    last_tune_ts=float(raw.get("last_tune_ts", 0.0)),
                    peak_inflight_window=[tuple(x) for x in raw.get("peak_inflight_window", [])],
                )
            except Exception as e:
                logger.warning(f"limiter state {self.state_path} unreadable, resetting: {e}")
        return LimiterState(
            worker=self.worker,
            cap_tokens=INITIAL_CAP_TOKENS,
            floor_tokens=FLOOR_TOKENS,
            ceiling_tokens=CEILING_TOKENS,
        )

    def _save_state(self) -> None:
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(asdict(self.state), f)
            os.replace(tmp, self.state_path)
        except Exception as e:
            logger.warning(f"failed to persist limiter state: {e}")

    def _append_event(self, ev: TuneEvent) -> None:
        try:
            with open(self.events_path, "a") as f:
                f.write(json.dumps(asdict(ev)) + "\n")
        except Exception as e:
            logger.debug(f"failed to append tune event: {e}")

    # ----- decision -----
    def _current_inflight_tokens(self) -> int:
        """Live in-flight token sum for this worker, from RollingMetrics."""
        try:
            from src.middleware.rolling_metrics import get_rolling_metrics
            return get_rolling_metrics()._in_flight_input_tokens.get(self.worker, 0)  # noqa: SLF001
        except Exception:
            return 0

    def _current_inflight_count(self) -> int:
        try:
            from src.middleware.rolling_metrics import get_rolling_metrics
            return get_rolling_metrics()._in_flight.get(self.worker, 0)  # noqa: SLF001
        except Exception:
            return 0

    def _effective_cap(self) -> int:
        """Cap reduced by the safety margin — the actual admit threshold."""
        margin = max(0.0, min(100.0, SAFETY_MARGIN_PCT))
        return max(1, int(self.state.cap_tokens * margin / 100.0))

    async def can_accept(self, est_tokens: int) -> tuple[bool, Optional[str], Dict[str, Any]]:
        """
        Returns (accepted, reject_reason, snapshot).
        snapshot is always populated for logging/observability.
        """
        async with self._lock:
            inflight_tokens = self._current_inflight_tokens()
            inflight_count = self._current_inflight_count()
            cap = self.state.cap_tokens
            effective_cap = self._effective_cap()
            would_be = inflight_tokens + max(0, est_tokens)
            snapshot = {
                "worker": self.worker,
                "inflight_tokens": inflight_tokens,
                "inflight_count": inflight_count,
                "estimated_request_tokens": est_tokens,
                "would_be_total": would_be,
                "cap_tokens": cap,
                "effective_cap_tokens": effective_cap,
                "safety_margin_pct": SAFETY_MARGIN_PCT,
                "utilization_pct": round(inflight_tokens * 100.0 / cap, 1) if cap > 0 else 0.0,
                "hard_request_ceiling": HARD_REQUEST_CEILING,
            }
            if inflight_count >= HARD_REQUEST_CEILING:
                return False, (
                    f"Hard request ceiling reached "
                    f"({inflight_count}/{HARD_REQUEST_CEILING})"
                ), snapshot
            if would_be > effective_cap:
                return False, (
                    f"In-flight token budget would exceed effective cap "
                    f"({inflight_tokens:,} + {est_tokens:,} = {would_be:,} > "
                    f"{effective_cap:,} = {SAFETY_MARGIN_PCT:.0f}% of {cap:,})"
                ), snapshot
            # Track peak utilization
            self._track_peak(time.time(), inflight_tokens)
            return True, None, snapshot

    def _ensure_capacity_event(self) -> asyncio.Event:
        """Lazy-create the capacity event on the running loop."""
        if self._capacity_event is None:
            self._capacity_event = asyncio.Event()
        return self._capacity_event

    def signal_capacity_freed(self) -> None:
        """
        Called by the request lifecycle (record_completion path) whenever a
        request ends, so any task parked in `acquire_with_wait` can re-check
        capacity. Cheap; safe to call from the request thread.
        """
        ev = self._capacity_event
        if ev is not None and not ev.is_set():
            try:
                ev.set()
            except Exception:
                pass

    async def acquire_with_wait(
        self,
        est_tokens: int,
        timeout_s: Optional[float] = None,
    ) -> tuple[bool, Optional[str], Dict[str, Any], float]:
        """
        Try to admit a request, awaiting up to `timeout_s` for capacity if
        currently full. Returns (accepted, reason, snapshot, waited_s).

        If `timeout_s` is None, uses QUEUE_WAIT_TIMEOUT_SEC. Pass 0 for
        immediate-reject behaviour (no queueing).
        """
        if timeout_s is None:
            timeout_s = float(QUEUE_WAIT_TIMEOUT_SEC)

        start = time.time()
        # Fast path — usually capacity is available, no queueing needed.
        accepted, reason, snap = await self.can_accept(est_tokens)
        if accepted or timeout_s <= 0:
            return accepted, reason, snap, time.time() - start

        # Fast-fail when the request size ALONE exceeds the effective cap.
        # No amount of waiting will admit it — let the caller know now so
        # they don't sit in the queue for the full timeout window.
        if est_tokens > snap.get("effective_cap_tokens", 0):
            return False, (
                f"Request size {est_tokens:,} tokens exceeds effective cap "
                f"{snap.get('effective_cap_tokens'):,} on its own; cannot be admitted."
            ), snap, time.time() - start

        # Slow path: park until either capacity frees up or we time out.
        ev = self._ensure_capacity_event()
        deadline = start + timeout_s
        self._queued_count += 1
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                ev.clear()
                try:
                    await asyncio.wait_for(
                        ev.wait(),
                        timeout=min(QUEUE_POLL_INTERVAL_SEC, remaining),
                    )
                except asyncio.TimeoutError:
                    pass  # poll-tick — re-check capacity
                accepted, reason, snap = await self.can_accept(est_tokens)
                if accepted:
                    return accepted, None, snap, time.time() - start
            # Timed out — final snapshot for diagnostics
            _, reason_final, snap_final = await self.can_accept(est_tokens)
            return False, reason_final or reason, snap_final, time.time() - start
        finally:
            self._queued_count = max(0, self._queued_count - 1)

    def _track_peak(self, now: float, inflight_tokens: int) -> None:
        cutoff = now - max(GROW_TRIGGER_SEC, 600)
        self.state.peak_inflight_window = [
            (ts, v) for ts, v in self.state.peak_inflight_window if ts >= cutoff
        ]
        self.state.peak_inflight_window.append((now, inflight_tokens))

    # ----- tuning -----
    async def tune(self) -> TuneEvent:
        """Single tune-tick. Returns the resulting event."""
        async with self._lock:
            now = time.time()
            cap_before = self.state.cap_tokens
            inflight_now = self._current_inflight_tokens()
            self._track_peak(now, inflight_now)

            # Recent rate-limit hits via rolling_metrics
            rate_limit_hits = 0
            try:
                from src.middleware.rolling_metrics import get_rolling_metrics
                summ = get_rolling_metrics().get_summary(window_seconds=SHRINK_TRIGGER_SEC)
                wstats = summ.get("workers", {}).get(self.worker, {})
                rate_limit_hits = int(wstats.get("rate_limit_hits", 0) or 0)
                if rate_limit_hits > 0:
                    self.state.last_rate_limit_ts = now
            except Exception as e:
                logger.debug(f"tune: cannot read rolling_metrics: {e}")

            # Peak utilization in growth window
            grow_cutoff = now - GROW_TRIGGER_SEC
            peaks_in_grow = [v for ts, v in self.state.peak_inflight_window if ts >= grow_cutoff]
            peak = max(peaks_in_grow) if peaks_in_grow else 0
            peak_pct = (peak * 100.0 / cap_before) if cap_before > 0 else 0.0

            shrink_cooldown_ok = (
                self.state.last_shrink_ts is None
                or (now - self.state.last_shrink_ts) >= SHRINK_COOLDOWN_SEC
            )
            had_recent_rate_limit = (
                self.state.last_rate_limit_ts is not None
                and (now - self.state.last_rate_limit_ts) < SHRINK_TRIGGER_SEC
            )
            clean_long_enough = (
                self.state.last_rate_limit_ts is None
                or (now - self.state.last_rate_limit_ts) >= GROW_TRIGGER_SEC
            )

            direction = "hold"
            reason = "no signal"

            if had_recent_rate_limit and shrink_cooldown_ok:
                new_cap = max(self.state.floor_tokens, int(cap_before * SHRINK_FACTOR))
                if new_cap < cap_before:
                    direction = "shrink"
                    reason = (
                        f"rate-limit observed within last {SHRINK_TRIGGER_SEC}s "
                        f"(hits={rate_limit_hits})"
                    )
                    self.state.cap_tokens = new_cap
                    self.state.last_shrink_ts = now
                else:
                    direction = "hold"
                    reason = "at floor; cannot shrink further"
            elif clean_long_enough and peak_pct >= GROW_UTILIZATION_PCT:
                new_cap = min(self.state.ceiling_tokens, int(cap_before * GROW_FACTOR))
                if new_cap > cap_before:
                    direction = "grow"
                    reason = (
                        f"clean for >={GROW_TRIGGER_SEC}s and "
                        f"peak util {peak_pct:.0f}% >= {GROW_UTILIZATION_PCT}%"
                    )
                    self.state.cap_tokens = new_cap
                else:
                    direction = "hold"
                    reason = "at ceiling; cannot grow further"
            else:
                if had_recent_rate_limit:
                    reason = "rate-limit seen but in shrink-cooldown"
                elif not clean_long_enough:
                    reason = (
                        f"recent rate-limit ({int(now - self.state.last_rate_limit_ts)}s ago); "
                        f"waiting {GROW_TRIGGER_SEC}s clean before growing"
                    )
                else:
                    reason = (
                        f"peak util {peak_pct:.0f}% < {GROW_UTILIZATION_PCT}%; no growth pressure"
                    )

            self.state.last_tune_ts = now
            ev = TuneEvent(
                ts=now,
                direction=direction,
                reason=reason,
                cap_before=cap_before,
                cap_after=self.state.cap_tokens,
                observed_rate_limits=rate_limit_hits,
                observed_peak_util_pct=round(peak_pct, 1),
            )
            self._save_state()
            self._append_event(ev)
            if direction != "hold":
                logger.info(
                    f"AdaptiveLoadLimiter[{self.worker}] {direction.upper()}: "
                    f"{cap_before:,} -> {self.state.cap_tokens:,} tokens ({reason})"
                )
            return ev

    async def _tune_loop(self) -> None:
        logger.info(
            f"AdaptiveLoadLimiter[{self.worker}] tune loop started (interval={TUNE_INTERVAL_SEC}s)"
        )
        while not self._stopped:
            try:
                await asyncio.sleep(TUNE_INTERVAL_SEC)
                if self._stopped:
                    break
                await self.tune()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"tune loop iteration failed: {e}")

    def start_tune_loop(self) -> None:
        """Start the background auto-tuning task. Idempotent."""
        if self._tune_task is None or self._tune_task.done():
            self._tune_task = asyncio.create_task(self._tune_loop())

    def stop(self) -> None:
        self._stopped = True
        if self._tune_task and not self._tune_task.done():
            self._tune_task.cancel()

    # ----- observability -----
    def snapshot(self) -> Dict[str, Any]:
        inflight_tokens = self._current_inflight_tokens()
        cap = self.state.cap_tokens
        recent_events = self._read_recent_events(limit=20)
        return {
            "worker": self.worker,
            "cap_tokens": cap,
            "effective_cap_tokens": self._effective_cap(),
            "safety_margin_pct": SAFETY_MARGIN_PCT,
            "floor_tokens": self.state.floor_tokens,
            "ceiling_tokens": self.state.ceiling_tokens,
            "inflight_tokens": inflight_tokens,
            "inflight_count": self._current_inflight_count(),
            "queued_count": self._queued_count,
            "utilization_pct": round(inflight_tokens * 100.0 / cap, 1) if cap > 0 else 0.0,
            "last_rate_limit_ts": self.state.last_rate_limit_ts,
            "last_shrink_ts": self.state.last_shrink_ts,
            "last_tune_ts": self.state.last_tune_ts,
            "hard_request_ceiling": HARD_REQUEST_CEILING,
            "recent_events": recent_events,
            "config": {
                "shrink_factor": SHRINK_FACTOR,
                "grow_factor": GROW_FACTOR,
                "shrink_trigger_sec": SHRINK_TRIGGER_SEC,
                "grow_trigger_sec": GROW_TRIGGER_SEC,
                "shrink_cooldown_sec": SHRINK_COOLDOWN_SEC,
                "grow_utilization_pct": GROW_UTILIZATION_PCT,
                "tune_interval_sec": TUNE_INTERVAL_SEC,
                "queue_wait_timeout_sec": QUEUE_WAIT_TIMEOUT_SEC,
            },
        }

    def _read_recent_events(self, limit: int = 20) -> List[Dict[str, Any]]:
        if not os.path.exists(self.events_path):
            return []
        try:
            with open(self.events_path) as f:
                lines = f.readlines()[-limit:]
            out = []
            for line in lines:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
            return out
        except Exception:
            return []


# ----------------------------------------------------------------------
# Singleton + FastAPI integration
# ----------------------------------------------------------------------
_limiter: Optional[AdaptiveLoadLimiter] = None


def get_adaptive_limiter() -> AdaptiveLoadLimiter:
    global _limiter
    if _limiter is None:
        _limiter = AdaptiveLoadLimiter()
    return _limiter


def estimate_request_tokens(body_dict: Dict[str, Any]) -> int:
    """
    Cheap pre-flight estimate of input tokens for an OpenAI/Anthropic-style
    chat-completions body.

    Rule of thumb: ~4 chars per token. Includes system prompt + all messages
    text content. We deliberately do NOT add max_tokens — output is tracked
    separately by the sliding-window TPM if/when needed.
    """
    chars = 0
    sys = body_dict.get("system")
    if isinstance(sys, str):
        chars += len(sys)
    elif isinstance(sys, list):
        for blk in sys:
            if isinstance(blk, dict):
                t = blk.get("text") or ""
                chars += len(t)
    for m in body_dict.get("messages", []) or []:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for blk in c:
                if isinstance(blk, dict):
                    t = blk.get("text") or ""
                    chars += len(t)
    return max(1, chars // 4)


async def adaptive_limit_dependency(request: Request) -> None:
    """
    FastAPI dependency for chat-completions endpoints. Reads the request body
    once, stashes it on the request for the handler to reuse, then enforces
    the adaptive token budget — queueing if necessary so the caller sees
    latency, not an error.

    Raises BridgeError (handled globally → structured envelope) only when the
    queue itself times out.

    NOTE: `request` MUST have the `Request` type annotation; otherwise FastAPI
    treats it as a required query parameter and rejects every request with 422.
    """

    # Read+cache the JSON body so the handler can reuse it without re-reading.
    body_dict: Dict[str, Any] = {}
    try:
        body_bytes = await request.body()
        if body_bytes:
            body_dict = json.loads(body_bytes)
        # cache for reuse
        request.state.cached_body_bytes = body_bytes
        request.state.cached_body_dict = body_dict
    except Exception as e:
        logger.debug(f"adaptive_limit: body read failed (will pass through): {e}")
        return

    est = estimate_request_tokens(body_dict)
    request.state.adaptive_est_tokens = est

    limiter = get_adaptive_limiter()
    accepted, reason, snap, waited_s = await limiter.acquire_with_wait(est)
    if accepted:
        if waited_s > 0.5:
            logger.info(
                f"adaptive_limit: queued {waited_s:.2f}s before admission "
                f"(est_tokens={est}, inflight={snap.get('inflight_tokens')})"
            )
        return

    # Queue exhausted — emit structured envelope. Caller sees a Retry-After
    # header and a clear "bridge_internal/queue_timeout" body.
    cap = snap.get("cap_tokens", 0)
    inflight = snap.get("inflight_tokens", 0)
    if waited_s >= max(1.0, QUEUE_WAIT_TIMEOUT_SEC * 0.5):
        raise BridgeError(queue_timeout_error(cap, inflight, waited_s))
    # If we never actually waited (queue disabled), report as plain throttle.
    raise BridgeError(throttle_error(cap, inflight))
