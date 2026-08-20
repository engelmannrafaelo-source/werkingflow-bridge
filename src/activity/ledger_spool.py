"""
Ledger spool — write-ahead durability for the billing row.

Why this exists (ADR-0009, Schritt 1):
  `persist_ai_call_activity` writes the authoritative usage row (usage_events)
  from the same process that made the LLM call. Today that is a local socket
  to a Postgres one container away. Once the workers live on their own host it
  becomes a write over the network, and a write that does not arrive is usage
  nobody bills.

  Even today the row is loseable: any failure between the LLM response and the
  INSERT — a DB blip, a 30s command timeout, a client disconnect cancelling the
  request task, an OOM-kill of the worker — ends with an ERROR log and no row.
  The OOM case is the named reason ADR-0009 exists at all.

The mechanism, in one sentence:
  the call's facts are written to local disk and fsync'd BEFORE the first
  database await, and stay there until a write is known to have landed.

  * A record is appended as one JSON line, fsync'd. Only then does the normal
    inline write run — unchanged, same latency budget, same behaviour.
  * On a definitive outcome (row written / row already there / correctly
    skipped) an ack line is appended. Ack lines are NOT fsync'd: losing one
    costs a redundant replay, never a row.
  * A background pass replays everything unacked through the same writer.
    Replay is safe because the row carries `usage_events.idempotency_key`
    (UNIQUE, migration 016) and the INSERT is ON CONFLICT DO NOTHING.

Why write-ahead and not "buffer on failure":
  buffering on failure only helps when the process lives long enough to notice
  the failure. It cannot help when the process dies between the LLM response
  and the write — which is exactly the OOM-kill / container-recreate case.

File layout — the established convention of this repo
(`request_log.<worker>.jsonl`, `limiter_events.<worker>.jsonl` on the shared
`/app/logs` volume), with ONE addition: the pid.

    ${METRICS_DIR}/bridge-billing-spool/ledger.<INSTANCE_NAME>.<pid>.jsonl
    ${METRICS_DIR}/bridge-billing-spool/dead.<INSTANCE_NAME>.jsonl

  A worker container runs several uvicorn processes against the SAME volume.
  One file per process means appends never interleave and need no locking.
  A file whose pid is gone (restart, crash, OOM) is adopted by a living
  process's flush pass — that is what makes the spool survive a restart.
  Adoption takes an flock on a sidecar `.lock` so two uvicorn processes cannot
  adopt the same orphan.

What this module deliberately does NOT do:
  * It does not hold the audit row (`activities`). Losing an audit line is bad;
    losing the money line is worse, and the two are deliberately not
    fate-shared (see ai_call_writer, the 2026-08-01 lesson).
  * It does not repeat the budget deduction. `apply_budget_deduction` is a
    read-modify-write with no dedup key — replaying it would double-charge.
    The deduction is bound to "the INSERT created the row in THIS attempt";
    see ai_call_writer.
"""
from __future__ import annotations

import errno
import fcntl
import glob
import json
import logging
import os
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Outcomes ────────────────────────────────────────────────────────────────
# The vocabulary the writer answers with, and the only thing the spool reasons
# about. The distinction that matters is transient (retry) vs. definitive
# (stop). A SELECT that answers "no such user" is an ANSWER, not an outage —
# retrying it forever would turn a correct skip into an infinite loop.
OUTCOME_WRITTEN = "written"      # the INSERT created the row in this attempt
OUTCOME_DUPLICATE = "duplicate"  # the row was already there (a replay caught up)
OUTCOME_SKIPPED = "skipped"      # correctly no row (no user / no tenant / not a user)
OUTCOME_FAILED = "failed"        # transient — the row is still owed

_DEFINITIVE = (OUTCOME_WRITTEN, OUTCOME_DUPLICATE, OUTCOME_SKIPPED)


def is_definitive(outcome: str) -> bool:
    """True when the call needs no further attempt (written, already there, or
    correctly skipped). Anything else — including an unknown value — is treated
    as still owed: the bias on a billing path is toward retrying, never toward
    quietly dropping."""
    return str(outcome).split(":", 1)[0] in _DEFINITIVE


# ── Configuration ───────────────────────────────────────────────────────────
def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


SPOOL_DIR = os.path.join(os.getenv("METRICS_DIR", "/app/logs"), "bridge-billing-spool")
WORKER_NAME = os.getenv("INSTANCE_NAME", "unknown")

# Retry ceilings. A record that cannot be written after this many passes, or
# that is older than this, is moved to the dead file with an ERROR — a money
# row is never dropped silently, but it is also never retried forever.
MAX_ATTEMPTS = int(os.getenv("BRIDGE_LEDGER_SPOOL_MAX_ATTEMPTS", "50"))
MAX_AGE_S = int(os.getenv("BRIDGE_LEDGER_SPOOL_MAX_AGE_S", str(24 * 3600)))

# Disk guard. A database that is down for days must not fill the volume — on
# this fleet a full disk has already killed a Postgres and taken everything
# with it. Past the cap the spool refuses new records LOUDLY rather than
# trading the whole host for them.
MAX_BYTES = int(os.getenv("BRIDGE_LEDGER_SPOOL_MAX_BYTES", str(256 * 1024 * 1024)))

FLUSH_INTERVAL_S = int(os.getenv("BRIDGE_LEDGER_SPOOL_FLUSH_INTERVAL_S", "20"))

# Depth past which every flush pass shouts. A spool that is filling up is a
# database problem in disguise and must be visible before it is a billing gap.
ALERT_DEPTH = int(os.getenv("BRIDGE_LEDGER_SPOOL_ALERT_DEPTH", "100"))
ALERT_AGE_S = int(os.getenv("BRIDGE_LEDGER_SPOOL_ALERT_AGE_S", "600"))


def spool_enabled() -> bool:
    """Kill switch. Off = exactly today's behaviour (inline write, no spool)."""
    return _env_flag("BRIDGE_LEDGER_SPOOL_ENABLED", True)


def new_call_uid() -> str:
    """The idempotency key for one LLM call. Generated where the call happened,
    not where it is written — that is what makes a later replay the SAME row
    rather than a second one."""
    return str(uuid.uuid4())


# ── Paths ───────────────────────────────────────────────────────────────────
def _own_path() -> str:
    return os.path.join(SPOOL_DIR, f"ledger.{WORKER_NAME}.{os.getpid()}.jsonl")


def _dead_path() -> str:
    return os.path.join(SPOOL_DIR, f"dead.{WORKER_NAME}.jsonl")


def _pid_of(path: str) -> Optional[int]:
    """pid embedded in `ledger.<worker>.<pid>.jsonl`, or None if unparsable."""
    base = os.path.basename(path)
    try:
        return int(base.rsplit(".", 2)[-2])
    except (ValueError, IndexError):
        return None


def _pid_alive(pid: int) -> bool:
    """Whether the process that owns a spool file is still running.

    /proc is the truth here, not os.kill(pid, 0): inside a container the pid
    namespace is our own, and a dead worker's pid is simply gone from /proc.
    A pid we cannot decide about counts as ALIVE — refusing to adopt costs a
    delayed replay, adopting a live process's file costs a concurrent rewrite.
    """
    try:
        return os.path.exists(f"/proc/{pid}")
    except OSError:
        return True


_dir_ready: Optional[bool] = None


def _ensure_dir() -> bool:
    global _dir_ready
    if _dir_ready is not None:
        return _dir_ready
    try:
        os.makedirs(SPOOL_DIR, exist_ok=True)
        _dir_ready = True
    except OSError as e:
        # Loud: without a spool directory the durability promise of this module
        # does not hold, and the caller must not believe it does.
        logger.error(
            "ledger spool: cannot create %s (%s) — the billing row is NOT "
            "write-ahead protected on this worker; a DB outage loses it, "
            "exactly as before this mechanism existed",
            SPOOL_DIR, e,
        )
        _dir_ready = False
    return _dir_ready


# ── Append path (hot) ───────────────────────────────────────────────────────
_over_cap_logged_at = 0.0
# Calls this process could not spool (over cap / unwritable). Surfaced in
# spool_stats() so "the safety net is off" is a visible number, not a log line
# somebody has to happen to read.
_undurable_calls = 0


def append_call(uid: str, record: Dict[str, Any]) -> bool:
    """Durably record one call BEFORE any database await. Returns whether the
    record is now on disk — the caller uses that to decide whether it owes an
    ack later.

    Synchronous on purpose: a synchronous write is not a cancellation point, so
    a client disconnect cannot interrupt it half-way. Everything after this
    line may be cancelled; the row survives it.
    """
    global _over_cap_logged_at, _undurable_calls
    if not _ensure_dir():
        _undurable_calls += 1
        return False
    path = _own_path()
    try:
        if MAX_BYTES > 0:
            try:
                if os.path.getsize(path) > MAX_BYTES:
                    _undurable_calls += 1
                    now = time.time()
                    if now - _over_cap_logged_at > 60:
                        _over_cap_logged_at = now
                        logger.error(
                            "ledger spool: %s is over the %d-byte cap — NOT "
                            "spooling further calls (%d so far). The database "
                            "has been unreachable long enough to threaten the "
                            "volume, and a full disk on this fleet has already "
                            "killed a Postgres once. Billing rows written from "
                            "here on are only as durable as the inline write. "
                            "Fix the DB; the spool drains by itself.",
                            path, MAX_BYTES, _undurable_calls,
                        )
                    return False
            except FileNotFoundError:
                pass
        line = json.dumps(
            {"t": "c", "uid": uid, "ts": round(time.time(), 3), "n": 0, "r": record},
            separators=(",", ":"), default=str,
        )
        with open(path, "a") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True
    except (OSError, TypeError, ValueError) as e:
        _undurable_calls += 1
        logger.error(
            "ledger spool: append failed (%s) — this call's billing row is not "
            "write-ahead protected (%d so far): %s",
            type(e).__name__, _undurable_calls, e,
        )
        return False


def ack(uid: str, outcome: str) -> None:
    """Mark a record as settled. Not fsync'd: a lost ack costs one redundant
    (and idempotent) replay, never a row."""
    if not _dir_ready:
        return
    try:
        with open(_own_path(), "a") as f:
            f.write(
                json.dumps({"t": "a", "uid": uid, "o": outcome}, separators=(",", ":"))
                + "\n"
            )
    except OSError as e:
        logger.warning("ledger spool: ack write failed (harmless, will replay): %s", e)


# ── File reading / compaction ───────────────────────────────────────────────
def _read_file(path: str) -> Tuple[Dict[str, Dict[str, Any]], set]:
    """Return ({uid: call_record}, acked_uids). Unparsable lines are reported
    and skipped — a corrupt tail (torn write at an OOM-kill) must not stop the
    rest of the file from draining."""
    calls: Dict[str, Dict[str, Any]] = {}
    acked: set = set()
    bad = 0
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if rec.get("t") == "c" and rec.get("uid"):
                    calls[rec["uid"]] = rec
                elif rec.get("t") == "a" and rec.get("uid"):
                    acked.add(rec["uid"])
    except FileNotFoundError:
        return {}, set()
    except OSError as e:
        logger.error("ledger spool: cannot read %s: %s", path, e)
        return {}, set()
    if bad:
        logger.warning(
            "ledger spool: %d unparsable line(s) in %s skipped (torn write?)",
            bad, path,
        )
    return calls, acked


def _pending(path: str) -> List[Dict[str, Any]]:
    calls, acked = _read_file(path)
    return sorted(
        (r for uid, r in calls.items() if uid not in acked),
        key=lambda r: r.get("ts", 0),
    )


def _rewrite(path: str, keep: Iterable[Dict[str, Any]], delete_if_empty: bool) -> None:
    """Replace the file with exactly the still-owed records.

    Synchronous and await-free between read and rename, so an append from this
    same (single-threaded) event loop cannot interleave. `keep` is derived from
    a fresh read by the caller for the same reason.
    """
    keep = list(keep)
    try:
        if not keep and delete_if_empty:
            os.unlink(path)
            return
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w") as f:
            for rec in keep:
                f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.error("ledger spool: compaction of %s failed: %s", path, e)


def _bury(records: List[Dict[str, Any]], why: str) -> None:
    """Move records that can no longer be retried into the dead file. Loud:
    these are calls that really happened and will never be billed unless
    someone acts on them."""
    if not records:
        return
    try:
        with open(_dead_path(), "a") as f:
            for rec in records:
                rec["dead_reason"] = why
                rec["dead_at"] = round(time.time(), 3)
                f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        logger.error("ledger spool: cannot write dead file: %s", e)
    logger.error(
        "ledger spool: %d billing row(s) GIVEN UP (%s) and moved to %s — these "
        "calls happened and are NOT metered. They need manual replay; the file "
        "holds the full original arguments.",
        len(records), why, _dead_path(),
    )


# ── Flush ───────────────────────────────────────────────────────────────────
async def _drain_file(
    path: str, writer: Callable, *, delete_if_empty: bool
) -> Dict[str, int]:
    stats = {"replayed": 0, "written": 0, "still_owed": 0, "buried": 0}
    pending = _pending(path)
    if not pending:
        if delete_if_empty:
            _rewrite(path, [], True)
        return stats

    settled: set = set()
    retry: Dict[str, int] = {}
    dead: List[Dict[str, Any]] = []
    now = time.time()

    for rec in pending:
        uid = rec["uid"]
        age = now - float(rec.get("ts", now))
        attempts = int(rec.get("n", 0))
        if attempts >= MAX_ATTEMPTS or age > MAX_AGE_S:
            dead.append(rec)
            continue
        try:
            # The ORIGIN timestamp travels with the record, not the replay
            # time. It decides the ledger row's recorded_at and lets the
            # deduction notice when it is landing in a different month than
            # the call — see ai_call_writer.
            outcome = await writer(
                **rec.get("r", {}), _call_uid=uid, _call_ts=float(rec.get("ts", now))
            )
        except Exception as e:  # noqa: BLE001 — one bad record must not stop the drain
            logger.warning("ledger spool: replay of %s raised: %s", uid, e)
            outcome = OUTCOME_FAILED
        stats["replayed"] += 1
        if is_definitive(outcome):
            settled.add(uid)
            if str(outcome).startswith(OUTCOME_WRITTEN):
                stats["written"] += 1
        else:
            retry[uid] = attempts + 1

    if dead:
        _bury(dead, f"max_attempts={MAX_ATTEMPTS} or max_age={MAX_AGE_S}s exceeded")
        settled.update(r["uid"] for r in dead)
        stats["buried"] = len(dead)

    # Re-read: appends may have landed while we awaited the writer above.
    calls, acked = _read_file(path)
    acked |= settled
    keep = []
    for uid, rec in calls.items():
        if uid in acked:
            continue
        if uid in retry:
            rec["n"] = retry[uid]
        keep.append(rec)
    keep.sort(key=lambda r: r.get("ts", 0))
    stats["still_owed"] = len(keep)
    _rewrite(path, keep, delete_if_empty)
    return stats


def _orphan_files() -> List[str]:
    """Spool files of processes that are gone — a restarted, crashed or
    OOM-killed uvicorn worker. Adopting these is what makes the spool survive
    a restart rather than merely a hiccup."""
    own = _own_path()
    out = []
    for path in glob.glob(os.path.join(SPOOL_DIR, f"ledger.{WORKER_NAME}.*.jsonl")):
        if path == own:
            continue
        pid = _pid_of(path)
        if pid is None or _pid_alive(pid):
            continue
        out.append(path)
    return sorted(out)


class _FileLock:
    """flock on a sidecar, so two uvicorn processes never adopt the same orphan.
    Non-blocking: the loser simply skips this pass."""

    def __init__(self, path: str):
        self._lock_path = f"{path}.lock"
        self._fd = None

    def __enter__(self) -> bool:
        try:
            self._fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            if e.errno not in (errno.EAGAIN, errno.EACCES):
                logger.warning("ledger spool: lock %s failed: %s", self._lock_path, e)
            return False

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
            try:
                os.unlink(self._lock_path)
            except OSError:
                pass


async def flush_once(writer: Callable) -> Dict[str, int]:
    """One drain pass: this process's own file, then any orphaned ones."""
    if not _ensure_dir():
        return {"replayed": 0, "written": 0, "still_owed": 0, "buried": 0}
    total = {"replayed": 0, "written": 0, "still_owed": 0, "buried": 0}

    for k, v in (await _drain_file(_own_path(), writer, delete_if_empty=False)).items():
        total[k] += v

    for path in _orphan_files():
        with _FileLock(path) as got:
            if not got:
                continue
            logger.info("ledger spool: adopting orphaned spool %s", path)
            for k, v in (await _drain_file(path, writer, delete_if_empty=True)).items():
                total[k] += v
    return total


def spool_stats() -> Dict[str, Any]:
    """Depth and age of what is still owed, across own + orphaned files. Meant
    for the health/metrics surface: a spool that is filling up is a database
    problem that has not been noticed yet."""
    if not _dir_ready and not _ensure_dir():
        return {
            "enabled": spool_enabled(), "available": False, "pending": 0,
            "oldest_age_s": 0.0, "undurable_calls": _undurable_calls,
        }
    pending = 0
    oldest = None
    # dict.fromkeys, not a list: the glob already contains this process's own
    # file, and counting it twice would report a backlog that is not there.
    paths = dict.fromkeys(
        [_own_path()]
        + sorted(glob.glob(os.path.join(SPOOL_DIR, f"ledger.{WORKER_NAME}.*.jsonl")))
    )
    for path in paths:
        for rec in _pending(path):
            pending += 1
            ts = float(rec.get("ts", 0))
            if oldest is None or ts < oldest:
                oldest = ts
    return {
        "enabled": spool_enabled(),
        "available": True,
        "pending": pending,
        "oldest_age_s": round(time.time() - oldest, 1) if oldest else 0.0,
        "undurable_calls": _undurable_calls,
        "dir": SPOOL_DIR,
    }


# ── Boot gate ───────────────────────────────────────────────────────────────
def assert_spool_ready() -> None:
    """Fail-fast at startup when the spool is switched on but cannot actually
    work. Raises RuntimeError; the caller must NOT swallow it.

    Why this is a boot gate and not a per-call warning: a safety net that turns
    itself off quietly is worse than no safety net, because it looks finished
    and protects nothing. The house precedent is fresh — a usage scraper whose
    contract was tightened but whose configuration was never rolled out failed
    every five minutes for thirteen hours and nobody saw it. A default-on
    switch that degrades silently is the same trap.

    Same shape as this worker's other boot invariants
    (`validate_billing_integrity`, `assert_declared_db_features_have_a_database`):
    a worker that cannot keep a promise it declares must not serve traffic
    while looking healthy.

    Switching the spool OFF deliberately (BRIDGE_LEDGER_SPOOL_ENABLED=false) is
    a decision and boots fine — the promise is simply not made.
    """
    if not spool_enabled():
        logger.warning(
            "ledger spool DISABLED by BRIDGE_LEDGER_SPOOL_ENABLED — billing "
            "rows are only as durable as the inline write, i.e. a DB outage "
            "loses them. This is the pre-ADR-0009 behaviour and is only correct "
            "as a deliberate fallback."
        )
        return

    global _dir_ready
    _dir_ready = None  # re-probe; the module may have been imported earlier
    if not _ensure_dir():
        raise RuntimeError(
            f"ledger spool enabled but its directory {SPOOL_DIR} cannot be "
            f"created. The billing row would silently lose its write-ahead "
            f"protection. Fix the volume/permissions, or set "
            f"BRIDGE_LEDGER_SPOOL_ENABLED=false to decide against it explicitly."
        )

    probe = os.path.join(SPOOL_DIR, f".probe.{WORKER_NAME}.{os.getpid()}")
    try:
        # A writable directory is not the same as a writable volume — a full
        # disk passes makedirs() and fails the first real write. Probe with the
        # exact operations the hot path uses, fsync included.
        with open(probe, "w") as f:
            f.write("ledger-spool-probe\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        raise RuntimeError(
            f"ledger spool enabled but {SPOOL_DIR} is not writable ({e}). "
            f"The billing row would silently lose its write-ahead protection. "
            f"Fix the volume (disk full?), or set "
            f"BRIDGE_LEDGER_SPOOL_ENABLED=false to decide against it explicitly."
        ) from e
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass

    logger.info(
        "✅ Ledger spool ready at %s — the billing row is written to disk and "
        "fsync'd before the first database await (ADR-0009 Schritt 1)",
        SPOOL_DIR,
    )


# ── Background drain ────────────────────────────────────────────────────────
async def flusher_loop() -> None:
    """Periodic drain of everything the inline path did not get written.

    Started from the worker's lifespan next to the other background loops. It
    is a no-op cheap when the spool is empty (one directory listing).

    The import of the writer is deliberately lazy: ai_call_writer imports THIS
    module for the hot path, so a module-level import here would be circular.
    """
    import asyncio

    if not spool_enabled():
        return

    from src.activity.ai_call_writer import persist_ai_call_activity

    # First pass immediately: after a restart the orphaned spool of the process
    # that died is the whole point — waiting a full interval to adopt it would
    # leave known-owed billing rows on disk for no reason.
    while True:
        try:
            stats = await flush_once(persist_ai_call_activity)
            if stats["replayed"] or stats["buried"]:
                logger.info(
                    "ledger spool: replayed=%d written=%d still_owed=%d buried=%d",
                    stats["replayed"], stats["written"],
                    stats["still_owed"], stats["buried"],
                )
            depth = stats["still_owed"]
            if depth:
                st = spool_stats()
                age = st.get("oldest_age_s", 0.0)
                if depth >= ALERT_DEPTH or age >= ALERT_AGE_S:
                    # A filling spool is a database problem wearing a disguise.
                    # It must be visible BEFORE it turns into a billing gap.
                    logger.error(
                        "ledger spool BACKLOG: %d billing row(s) still owed, "
                        "oldest %.0fs old — the ledger write has been failing "
                        "for a while. Nothing is lost yet (that is what the "
                        "spool is for), but the database needs looking at.",
                        depth, age,
                    )
        except Exception as e:  # noqa: BLE001 — the drain must outlive one bad pass
            logger.warning("ledger spool: flush pass failed: %s", e)
        await asyncio.sleep(FLUSH_INTERVAL_S)
