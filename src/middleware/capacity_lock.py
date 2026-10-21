"""Worker capacity lock — deterministic until Anthropic-reset_at expires.

Independent of adaptive_limiter (which handles concurrency). On a
rate-limit caused by quota exhaustion (session_pct≥95 or weekly_pct≥95
or explicit reset_at from Anthropic), the worker is hard-locked until
the reset timestamp passes. No multipliers, no ramps, no MAX_COOLDOWN
cap. Anthropic told us when to retry — we listen.
"""
from dataclasses import dataclass, asdict
from typing import Dict, Optional
import json, os, time, threading, logging

logger = logging.getLogger(__name__)

# Locks we set ourselves from a usage measurement at or above this percent.
# Only these may be lifted early by a newer, lower measurement.
QUOTA_LOCK_REASONS = ("weekly_window", "session_window")
QUOTA_LOCK_THRESHOLD_PCT = 95.0


@dataclass
class WorkerLock:
    worker: str
    locked_until_ts: float    # absolute unix ts
    reason: str               # "session_window" | "weekly_window" | "anthropic_explicit"
    set_at_ts: float


class CapacityLock:
    """Process-local, on-disk-persisted worker lock state."""

    PERSIST_PATH = "/tmp/capacity_lock_state.json"

    def __init__(self):
        self._locks: Dict[str, WorkerLock] = {}
        self._lock = threading.RLock()
        self._load()

    def lock_until(self, worker: str, reset_ts: float, reason: str) -> None:
        """Lock `worker` until reset_ts (unix seconds). If a longer lock
        already exists, keep the longer one (Anthropic occasionally sends
        shorter reset_at after we already saw a longer one)."""
        with self._lock:
            existing = self._locks.get(worker)
            if existing and existing.locked_until_ts > reset_ts:
                logger.info(
                    f"capacity_lock: keeping longer lock on {worker} "
                    f"(existing={existing.locked_until_ts:.0f}, new={reset_ts:.0f})"
                )
                return
            self._locks[worker] = WorkerLock(
                worker=worker,
                locked_until_ts=float(reset_ts),
                reason=reason,
                set_at_ts=time.time(),
            )
            logger.warning(
                f"🔒 capacity_lock: {worker} locked until {reset_ts:.0f} "
                f"(in {int(reset_ts - time.time())}s, reason={reason})"
            )
            self._persist()

    def is_locked(self, worker: str) -> bool:
        with self._lock:
            lock = self._locks.get(worker)
            if not lock:
                return False
            if time.time() >= lock.locked_until_ts:
                del self._locks[worker]
                self._persist()
                logger.info(f"🔓 capacity_lock: {worker} unlocked (expired)")
                return False
            return True

    def release_quota_lock(
        self, worker: str, measured_pct: Dict[str, float], measured_ts: float
    ) -> Optional[WorkerLock]:
        """Lift a quota lock that a newer measurement of the same account
        contradicts. Returns the lifted lock, or None if nothing was lifted.

        `measured_pct` maps a lock reason ("weekly_window", "session_window")
        to the percent measured for exactly that window. Only those two reasons
        are quota locks: we set them ourselves from a usage snapshot, so a
        later snapshot may also end them. Every other reason (Anthropic's own
        reset_at, org_subscription_disabled) is an external signal and keeps
        running until it expires.

        The measurement must postdate the lock: a snapshot taken before the
        lock was set is the one that set it (or an even older one) and says
        nothing about the account today. A weekly reset since the lock shows up
        here as the window's percent dropping below the threshold.
        """
        with self._lock:
            lock = self._locks.get(worker)
            if not lock or lock.reason not in QUOTA_LOCK_REASONS:
                return None
            pct = measured_pct.get(lock.reason)
            if pct is None or pct >= QUOTA_LOCK_THRESHOLD_PCT:
                return None
            if measured_ts <= lock.set_at_ts:
                return None
            del self._locks[worker]
            self._persist()
            logger.warning(
                f"🔓 capacity_lock: {worker} unlocked early (reason was "
                f"{lock.reason}, measured {pct:.1f}% "
                f"< {QUOTA_LOCK_THRESHOLD_PCT:.0f}%, "
                f"measurement {int(time.time() - measured_ts)}s old, lock set "
                f"{int(time.time() - lock.set_at_ts)}s ago, would have run "
                f"{int(lock.locked_until_ts - time.time())}s more)"
            )
            return lock

    def remaining_s(self, worker: str) -> int:
        with self._lock:
            lock = self._locks.get(worker)
            if not lock:
                return 0
            return max(0, int(lock.locked_until_ts - time.time()))

    def get_lock_info(self, worker: str) -> Optional[dict]:
        with self._lock:
            lock = self._locks.get(worker)
            if not lock or time.time() >= lock.locked_until_ts:
                return None
            return asdict(lock)

    def _persist(self) -> None:
        try:
            data = {w: asdict(l) for w, l in self._locks.items()}
            tmp = self.PERSIST_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.PERSIST_PATH)
        except Exception as e:
            logger.error(f"capacity_lock: persist failed: {e}")

    def _load(self) -> None:
        if not os.path.exists(self.PERSIST_PATH):
            return
        try:
            with open(self.PERSIST_PATH) as f:
                data = json.load(f)
            now = time.time()
            for w, raw in data.items():
                if raw["locked_until_ts"] > now:
                    self._locks[w] = WorkerLock(**raw)
            logger.info(f"capacity_lock: loaded {len(self._locks)} active locks from disk")
        except Exception as e:
            logger.error(f"capacity_lock: load failed: {e}")


_INSTANCE: Optional[CapacityLock] = None


def get_capacity_lock() -> CapacityLock:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = CapacityLock()
    return _INSTANCE
