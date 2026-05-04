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
import re
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
    account_exhausted_error,
)

logger = logging.getLogger(__name__)

_RESET_PATTERN = re.compile(r'(\d+)\s*([dhm])')


def _parse_reset_string(s: str) -> Optional[int]:
    """Parse 'resets in 4h 29m' → 16140 (seconds). '' or unparseable → None."""
    if not isinstance(s, str) or not s:
        return None
    total = 0
    for n, unit in _RESET_PATTERN.findall(s):
        n = int(n)
        if unit == 'd':
            total += n * 86400
        elif unit == 'h':
            total += n * 3600
        elif unit == 'm':
            total += n * 60
    return total if total > 0 else None


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
# Predictive weekly-budget throttle  (DISABLED by default — "capacity egal")
# ----------------------------------------------------------------------
# Historical design: drop the admit budget as the weekly-% climbs toward the
# smart-routing 95% cliff, so apps see gradual back-pressure rather than a
# sudden nginx-drop. In practice this shut apps out with an account_exhausted
# envelope LONG before the actual Anthropic streaming-window was full — weekly
# % is a rolling aggregate, not an instantaneous capacity signal.
#
# New design (Rafael directive 2026-04-16, "capacity egal, streaming window"):
# let the actual 5h streaming-window govern capacity. The adaptive cap already
# auto-shrinks when a real 429 is observed (SHRINK_FACTOR); combined with the
# cross-bridge fallback (WorkerUnavailableError / BridgeError → Sahori prod),
# weekly-budget prediction is no longer needed to prevent hard failures.
#
# The mechanism is kept in code (gated by env) so it can be re-enabled
# instantly if streaming-window behaviour ever proves insufficient.
WEEKLY_PREDICTIVE_THROTTLE_ENABLED = (
    os.getenv("ADAPTIVE_WEEKLY_PREDICTIVE_THROTTLE", "false").lower() == "true"
)
WEEKLY_THROTTLE_START_PCT   = float(os.getenv("ADAPTIVE_WEEKLY_START_PCT", "80"))
WEEKLY_THROTTLE_CEILING_PCT = float(os.getenv("ADAPTIVE_WEEKLY_CEILING_PCT", "95"))
WEEKLY_THROTTLE_MIN_MULT    = float(os.getenv("ADAPTIVE_WEEKLY_MIN_MULT", "0.10"))
WEEKLY_CACHE_TTL_SEC        = int(os.getenv("ADAPTIVE_WEEKLY_CACHE_TTL_SEC", "30"))

# Account name → worker name (keep in sync with smart-worker-routing.sh)
_WORKER_ACCOUNT_MAP = {
    "worker1": "engelmann",
    "worker2": "office",
    "worker3": "gmail",
    "worker4": "werking",
}


def _weekly_budget_multiplier(weekly_pct: float, session_pct: float) -> float:
    """
    Derive an admit-budget multiplier in [0.0, 1.0] from current weekly %
    and session %. The stricter of the two wins.

    Examples with defaults (START=80, CEILING=95, MIN=0.10):
      weekly=50% → 1.00  (no throttle)
      weekly=80% → 1.00  (ramp start)
      weekly=87.5% → 0.55 (half-way down)
      weekly=95% → 0.10  (barely any admission)
      weekly=98% → 0.0   (reject — already past the wall)

    When WEEKLY_PREDICTIVE_THROTTLE_ENABLED=false (default), this always
    returns 1.0 — the 5h streaming-window cap + cross-bridge fallback handle
    exhaustion instead of the predictive weekly-% curve.
    """
    if not WEEKLY_PREDICTIVE_THROTTLE_ENABLED:
        return 1.0

    def _mult(pct: float) -> float:
        if pct <= WEEKLY_THROTTLE_START_PCT:
            return 1.0
        if pct >= WEEKLY_THROTTLE_CEILING_PCT:
            # Above the routing cliff — nginx is about to mark us down. Let
            # the last few calls through ONLY if well under the ceiling, else 0.
            return 0.0 if pct >= WEEKLY_THROTTLE_CEILING_PCT + 2 else WEEKLY_THROTTLE_MIN_MULT
        # Linear ramp from 1.0 → MIN across [START, CEILING]
        span = max(0.01, WEEKLY_THROTTLE_CEILING_PCT - WEEKLY_THROTTLE_START_PCT)
        t = (pct - WEEKLY_THROTTLE_START_PCT) / span
        return max(WEEKLY_THROTTLE_MIN_MULT, 1.0 - t * (1.0 - WEEKLY_THROTTLE_MIN_MULT))

    return min(_mult(weekly_pct), _mult(session_pct))


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
    observed_crash_hits: int = 0
    observed_peak_util_pct: float = 0.0
    # Point-in-time snapshot taken during the tune tick — populated for every
    # event (even "hold") so the panel can plot a continuous trajectory from
    # the same file without a second sampler. Defaults keep old log entries
    # (without these fields) decodable.
    inflight_tokens: int = 0
    inflight_count: int = 0
    queued_count: int = 0
    effective_cap_tokens: int = 0
    account_weekly_pct: float = 0.0


@dataclass
class LimiterState:
    """Persisted per-worker auto-tune state."""
    worker: str
    cap_tokens: int
    floor_tokens: int
    ceiling_tokens: int
    last_rate_limit_ts: Optional[float] = None
    last_crash_ts: Optional[float] = None
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

        # Cached view of own-account weekly + session usage (refreshed via
        # _refresh_account_usage every WEEKLY_CACHE_TTL_SEC). Keeps the hot
        # path O(1) — no file I/O on every request.
        self._account_usage: Dict[str, float] = {
            "weekly_pct": 0.0,
            "session_pct": 0.0,
            "ts": 0.0,
            "budget_multiplier": 1.0,
        }

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
                    last_crash_ts=raw.get("last_crash_ts"),
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

    def _refresh_account_usage(self) -> None:
        """
        Update the cached own-account weekly+session %. Reads the shared
        cc_usage_snapshots store; cheap enough to call once per admission
        decision thanks to the TTL check. Silent-on-error (keeps last value).
        """
        now = time.time()
        last_ts = self._account_usage.get("ts", 0.0)
        if now - last_ts < WEEKLY_CACHE_TTL_SEC:
            return
        try:
            from src.middleware.bridge_metrics_store import get_cc_usage_store
            history = get_cc_usage_store().get_history(hours=1, limit=1)
            snapshots = history.get("snapshots") or []
            if not snapshots:
                self._account_usage["ts"] = now  # mark attempted
                return
            target_account = _WORKER_ACCOUNT_MAP.get(self.worker)
            for acc in snapshots[0].get("accounts", []):
                if acc.get("account") != target_account:
                    continue
                weekly  = float(acc.get("weeklyAllModels", {}).get("percent", 0) or 0)
                session = float(acc.get("currentSession", {}).get("percent", 0) or 0)
                self._maybe_set_capacity_lock(acc)
                mult = _weekly_budget_multiplier(weekly, session)
                self._account_usage = {
                    "weekly_pct": weekly,
                    "session_pct": session,
                    "budget_multiplier": mult,
                    "ts": now,
                }
                return
            # No entry for us in the snapshot — mark attempted, keep prior
            self._account_usage["ts"] = now
        except Exception as e:
            logger.debug(f"_refresh_account_usage failed: {e}")
            self._account_usage["ts"] = now  # back off until next TTL

    def _maybe_set_capacity_lock(self, acc: dict) -> None:
        """Lock this worker until Anthropic's reported reset time when at quota wall (≥95%)."""
        try:
            from src.middleware.capacity_lock import get_capacity_lock
            cap_lock = get_capacity_lock()
            weekly_pct  = float(acc.get("weeklyAllModels", {}).get("percent", 0) or 0)
            session_pct = float(acc.get("currentSession",  {}).get("percent", 0) or 0)
            if weekly_pct >= 95:
                reset_in = _parse_reset_string(
                    acc.get("weeklyAllModels", {}).get("resetDate", "")
                )
                if reset_in:
                    cap_lock.lock_until(self.worker, time.time() + reset_in, "weekly_window")
                    return
            if session_pct >= 95:
                reset_in = _parse_reset_string(
                    acc.get("currentSession", {}).get("resetIn", "")
                )
                if reset_in:
                    cap_lock.lock_until(self.worker, time.time() + reset_in, "session_window")
        except Exception as e:
            logger.error(f"_maybe_set_capacity_lock failed: {e}")

    def _effective_cap(self) -> int:
        """
        Cap after safety margin AND predictive weekly-budget throttle.

        Admit threshold = cap * (SAFETY_MARGIN_PCT/100) * budget_multiplier

        When the account's weekly usage approaches the nginx-routing cliff
        (95% marks worker `down`), the multiplier ramps from 1.0 to 0.1 across
        the 80–95% band. Above 97% the multiplier becomes 0.0, rejecting all
        admissions — nginx will mark us down shortly anyway, so refusing now
        prevents in-flight requests from wasting the last few tokens of budget.
        """
        self._refresh_account_usage()
        margin = max(0.0, min(100.0, SAFETY_MARGIN_PCT))
        mult = float(self._account_usage.get("budget_multiplier", 1.0))
        base = self.state.cap_tokens * margin / 100.0 * mult
        return max(0, int(base))

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
            acct = self._account_usage
            weekly_pct = float(acct.get("weekly_pct", 0.0))
            session_pct = float(acct.get("session_pct", 0.0))
            budget_mult = float(acct.get("budget_multiplier", 1.0))
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
                "account_weekly_pct": weekly_pct,
                "account_session_pct": session_pct,
                "weekly_budget_multiplier": round(budget_mult, 3),
            }
            if inflight_count >= HARD_REQUEST_CEILING:
                return False, (
                    f"Hard request ceiling reached "
                    f"({inflight_count}/{HARD_REQUEST_CEILING})"
                ), snapshot
            if would_be > effective_cap:
                # If the weekly-budget multiplier has throttled us to near-zero,
                # surface that as the primary reason (the cap_tokens number is
                # irrelevant — the weekly limit is what's actually blocking).
                if budget_mult < 0.5 and weekly_pct >= WEEKLY_THROTTLE_START_PCT:
                    reason = (
                        f"Weekly budget {weekly_pct:.1f}% (session {session_pct:.1f}%) "
                        f"triggered predictive throttle (mult={budget_mult:.2f}). "
                        f"Effective cap shrunk to {effective_cap:,} tokens; "
                        f"would_be={would_be:,}."
                    )
                else:
                    reason = (
                        f"In-flight token budget would exceed effective cap "
                        f"({inflight_tokens:,} + {est_tokens:,} = {would_be:,} > "
                        f"{effective_cap:,} = {SAFETY_MARGIN_PCT:.0f}% of {cap:,}"
                        + (f" * {budget_mult:.2f} weekly)" if budget_mult < 1.0 else ")")
                    )
                return False, reason, snapshot
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
        effective_cap = snap.get("effective_cap_tokens", 0)
        if est_tokens > effective_cap:
            mult = snap.get("weekly_budget_multiplier", 1.0)
            weekly = snap.get("account_weekly_pct", 0.0)
            # When the weekly throttle has squeezed the cap to near-zero, the
            # real blocker is the account budget — say so explicitly so apps
            # can surface "account near limit" rather than "request too big".
            if mult < 0.5 and weekly >= WEEKLY_THROTTLE_START_PCT:
                reason = (
                    f"Weekly budget {weekly:.1f}% has throttled effective cap "
                    f"to {effective_cap:,} tokens (mult={mult:.2f}); "
                    f"{est_tokens:,}-token request cannot be admitted. "
                    f"Retry after weekly reset or try another worker."
                )
            else:
                reason = (
                    f"Request size {est_tokens:,} tokens exceeds effective cap "
                    f"{effective_cap:,} on its own; cannot be admitted."
                )
            return False, reason, snap, time.time() - start

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

            # Recent rate-limit and crash hits via rolling_metrics
            rate_limit_hits = 0
            crash_hits = 0
            try:
                from src.middleware.rolling_metrics import get_rolling_metrics
                summ = get_rolling_metrics().get_summary(window_seconds=SHRINK_TRIGGER_SEC)
                wstats = summ.get("workers", {}).get(self.worker, {})
                rate_limit_hits = int(wstats.get("rate_limit_hits", 0) or 0)
                crash_hits = int(wstats.get("crash_hits", 0) or 0)
                if rate_limit_hits > 0:
                    self.state.last_rate_limit_ts = now
                if crash_hits > 0:
                    self.state.last_crash_ts = now
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
            had_recent_crash = (
                self.state.last_crash_ts is not None
                and (now - self.state.last_crash_ts) < SHRINK_TRIGGER_SEC
            )
            clean_long_enough = (
                (self.state.last_rate_limit_ts is None
                 or (now - self.state.last_rate_limit_ts) >= GROW_TRIGGER_SEC)
                and
                (self.state.last_crash_ts is None
                 or (now - self.state.last_crash_ts) >= GROW_TRIGGER_SEC)
            )

            direction = "hold"
            reason = "no signal"

            if (had_recent_rate_limit or had_recent_crash) and shrink_cooldown_ok:
                new_cap = max(self.state.floor_tokens, int(cap_before * SHRINK_FACTOR))
                if new_cap < cap_before:
                    direction = "shrink"
                    if had_recent_rate_limit and had_recent_crash:
                        reason = (
                            f"rate_limit ({rate_limit_hits}) + worker_crash ({crash_hits}) "
                            f"in last {SHRINK_TRIGGER_SEC}s"
                        )
                    elif had_recent_rate_limit:
                        reason = (
                            f"rate-limit observed within last {SHRINK_TRIGGER_SEC}s "
                            f"(hits={rate_limit_hits})"
                        )
                    else:
                        reason = (
                            f"worker_crash ({crash_hits}) in last {SHRINK_TRIGGER_SEC}s"
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
                if had_recent_rate_limit or had_recent_crash:
                    reason = "rate-limit/crash seen but in shrink-cooldown"
                elif not clean_long_enough:
                    secs_since = min(
                        int(now - self.state.last_rate_limit_ts) if self.state.last_rate_limit_ts else 999999,
                        int(now - self.state.last_crash_ts) if self.state.last_crash_ts else 999999,
                    )
                    reason = (
                        f"recent signal ({secs_since}s ago); "
                        f"waiting {GROW_TRIGGER_SEC}s clean before growing"
                    )
                else:
                    reason = (
                        f"peak util {peak_pct:.0f}% < {GROW_UTILIZATION_PCT}%; no growth pressure"
                    )

            self.state.last_tune_ts = now

            # Snapshot point-in-time load for the trajectory graph. The
            # effective cap is what actually gates admissions (cap × safety
            # margin × weekly multiplier) so operators see the real ceiling,
            # not just the theoretical one.
            effective_cap_now = self._effective_cap()
            weekly_pct_now = float(self._account_usage.get("weekly_pct", 0.0))

            ev = TuneEvent(
                ts=now,
                direction=direction,
                reason=reason,
                cap_before=cap_before,
                cap_after=self.state.cap_tokens,
                observed_rate_limits=rate_limit_hits,
                observed_crash_hits=crash_hits,
                observed_peak_util_pct=round(peak_pct, 1),
                inflight_tokens=inflight_now,
                inflight_count=self._current_inflight_count(),
                queued_count=self._queued_count,
                effective_cap_tokens=effective_cap_now,
                account_weekly_pct=round(weekly_pct_now, 2),
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
        # Ensure account_usage is fresh (also populates self._account_usage).
        effective_cap = self._effective_cap()
        acct = self._account_usage
        return {
            "worker": self.worker,
            "cap_tokens": cap,
            "effective_cap_tokens": effective_cap,
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
            "account_weekly_pct": round(float(acct.get("weekly_pct", 0.0)), 2),
            "account_session_pct": round(float(acct.get("session_pct", 0.0)), 2),
            "weekly_budget_multiplier": round(float(acct.get("budget_multiplier", 1.0)), 3),
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
                "weekly_throttle_start_pct": WEEKLY_THROTTLE_START_PCT,
                "weekly_throttle_ceiling_pct": WEEKLY_THROTTLE_CEILING_PCT,
                "weekly_throttle_min_mult": WEEKLY_THROTTLE_MIN_MULT,
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
    # Defensive: this runs BEFORE FastAPI's pydantic validation, so the body
    # may be malformed (e.g. messages=string). Treat any unexpected shape as
    # zero-chars and let the schema validator return a proper 422 envelope.
    if not isinstance(body_dict, dict):
        return 1
    sys = body_dict.get("system")
    if isinstance(sys, str):
        chars += len(sys)
    elif isinstance(sys, list):
        for blk in sys:
            if isinstance(blk, dict):
                t = blk.get("text") or ""
                chars += len(t) if isinstance(t, str) else 0
    messages = body_dict.get("messages")
    if isinstance(messages, list):
        for m in messages:
            if not isinstance(m, dict):
                continue
            c = m.get("content")
            if isinstance(c, str):
                chars += len(c)
            elif isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict):
                        t = blk.get("text") or ""
                        chars += len(t) if isinstance(t, str) else 0
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
    # header and a clear body tagged by the reason:
    #   * weekly budget exhausted → account_exhausted (source=bridge_account)
    #   * queue timeout after wait → queue_timeout     (source=bridge_internal)
    #   * cap-full w/o waiting     → throttle          (source=bridge_internal)
    cap = snap.get("cap_tokens", 0)
    inflight = snap.get("inflight_tokens", 0)
    mult = snap.get("weekly_budget_multiplier", 1.0)
    weekly_pct = snap.get("account_weekly_pct", 0.0)

    # Predictive weekly-budget throttle (kept for opt-in use). With the new
    # "capacity egal, streaming window" policy this path is dormant because
    # the multiplier is always 1.0 — only reachable if an operator re-enables
    # ADAPTIVE_WEEKLY_PREDICTIVE_THROTTLE=true.
    if (
        WEEKLY_PREDICTIVE_THROTTLE_ENABLED
        and mult <= WEEKLY_THROTTLE_MIN_MULT
        and weekly_pct >= WEEKLY_THROTTLE_START_PCT
    ):
        raise BridgeError(account_exhausted_error(retry_after_s=3600))

    if waited_s >= max(1.0, QUEUE_WAIT_TIMEOUT_SEC * 0.5):
        raise BridgeError(queue_timeout_error(cap, inflight, waited_s))
    # If we never actually waited (queue disabled), report as plain throttle.
    raise BridgeError(throttle_error(cap, inflight))
