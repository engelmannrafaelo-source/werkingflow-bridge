"""
Bridge Metrics Store — Persistent JSONL storage for all Bridge metrics.

Three streams:

1. request_log.{worker}.jsonl
   Every HTTP request through the Bridge (endpoint, status, duration, model, tokens, cost, user, app).
   Replaces the in-memory RequestMetrics for historical queries.
   Rotiert bei REQUEST_LOG_MAX_BYTES (Default 100 MB) nach .jsonl.1 (eine
   Alt-Generation). Ohne Rotation wuchsen die Dateien auf 4×750 MB und jede
   query() parste 3 GB pro Aufruf — das sättigte den metrics-reader bis zum
   Healthcheck-Tod (2026-07-22).

2. prompt_calls.{worker}.jsonl
   Already handled by prompt_metrics.py — not duplicated here.

3. cc_usage_snapshots.jsonl
   Account limit snapshots (written by CUI scraper, read by Bridge).

All files live on the shared Docker volume at /app/logs/bridge-metrics/.
"""

import os
import fcntl
import json
import time
import glob
import threading
import logging
from collections import defaultdict
from typing import Callable, Optional, Dict, List, Any

logger = logging.getLogger(__name__)

METRICS_DIR = os.path.join(
    os.getenv("METRICS_DIR", "/app/logs"),
    "bridge-metrics"
)

# Rotation: aktive Datei + genau eine Alt-Generation (.jsonl.1). Bei 100 MB und
# realen ~5 MB/Tag deckt das zusammen Monate ab — weit mehr als die 24h/7d-Queries.
REQUEST_LOG_MAX_BYTES = int(os.getenv("REQUEST_LOG_MAX_BYTES", str(100 * 1024 * 1024)))
# Lese-Budget pro Datei für query(): skaliert mit dem Zeitfenster, hart gedeckelt.
# Einträge sind append-ordered — der Tail genügt, solange er bis zum Cutoff
# zurückreicht; ob das der Fall war, meldet query() als coverage_complete.
_SCAN_BYTES_PER_HOUR = int(os.getenv("REQUEST_LOG_SCAN_BYTES_PER_HOUR", str(2 * 1024 * 1024)))
_SCAN_BYTES_FLOOR = 16 * 1024 * 1024
_SCAN_BYTES_CAP = 128 * 1024 * 1024


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
        tools_enabled, worker, client_ip,
        [reason, source]  — only present on non-2xx; drives the contract tab.
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
        reason: Optional[str] = None,
        source: Optional[str] = None,
    ) -> None:
        """Append a request log entry to disk."""
        entry: Dict[str, Any] = {
            "ts": round(time.time(), 3),
            "method": method,
            "endpoint": endpoint,
            "status": status_code,
            "duration_s": round(duration_s, 4),
            "tools": tools_enabled,
            "worker": self._worker_id,
            "client": client_ip,
        }
        if reason:
            entry["reason"] = reason
        if source:
            entry["source"] = source
        try:
            with open(self._jsonl_path, "a") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
            self._maybe_rotate()
        except OSError as e:
            logger.warning(f"Cannot write request log: {e}")

    def _maybe_rotate(self) -> None:
        """Rotate the active file to .1 once it exceeds REQUEST_LOG_MAX_BYTES.

        Mehrere Uvicorn-Prozesse desselben Workers appenden auf dieselbe Datei;
        das flock auf der Lock-Datei stellt sicher, dass genau EIN Prozess
        rotiert (der Verlierer überspringt — beim nächsten Append ist die Datei
        wieder klein). Offene Append-Handles anderer Prozesse folgen dem
        umbenannten File harmlos zu Ende (open-per-append macht das Fenster
        winzig).
        """
        try:
            if os.path.getsize(self._jsonl_path) <= REQUEST_LOG_MAX_BYTES:
                return
        except OSError:
            return
        lock_path = self._jsonl_path + ".rotate.lock"
        try:
            with open(lock_path, "w") as lock_f:
                try:
                    fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return  # anderer Prozess rotiert gerade
                # Re-check unter Lock: der Gewinner eines früheren Rennens
                # kann schon rotiert haben.
                if os.path.getsize(self._jsonl_path) <= REQUEST_LOG_MAX_BYTES:
                    return
                os.replace(self._jsonl_path, self._jsonl_path + ".1")
                logger.info(
                    f"request_log rotated: {os.path.basename(self._jsonl_path)} "
                    f"→ .1 (>{REQUEST_LOG_MAX_BYTES} bytes)"
                )
        except OSError as e:
            logger.warning(f"request_log rotation failed: {e}")

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
        scan_budget = min(
            _SCAN_BYTES_CAP,
            max(_SCAN_BYTES_FLOOR, hours * _SCAN_BYTES_PER_HOUR),
        ) if hours > 0 else _SCAN_BYTES_CAP

        all_entries: List[dict] = []
        endpoint_stats: Dict[str, dict] = defaultdict(
            lambda: {"count": 0, "total_duration": 0.0, "errors": 0,
                     "min_duration": float("inf"), "max_duration": 0.0}
        )

        def _consume(entry: dict) -> None:
            ep = entry.get("endpoint", "unknown")
            if endpoint_filter and endpoint_filter not in ep:
                return
            if status_filter == "success" and entry.get("status", 0) >= 400:
                return
            if status_filter == "error" and entry.get("status", 0) < 400:
                return
            all_entries.append(entry)
            s = endpoint_stats[ep]
            s["count"] += 1
            dur = entry.get("duration_s", 0)
            s["total_duration"] += dur
            s["min_duration"] = min(s["min_duration"], dur)
            s["max_duration"] = max(s["max_duration"], dur)
            if entry.get("status", 200) >= 400:
                s["errors"] += 1

        # Nur den Datei-Tail lesen (Einträge sind append-ordered): reicht der
        # Tail nicht bis zum Cutoff zurück, die Alt-Generation (.1) dazunehmen;
        # reicht auch das nicht, wird das EHRLICH als coverage_complete=False
        # gemeldet statt still zu fehlen (vorher: Full-Scan über alle
        # Generationen — 3 GB pro Aufruf, sättigte den metrics-reader).
        coverage_complete = True
        for filepath in sorted(glob.glob(pattern)):
            covered = self._scan_tail(filepath, cutoff, scan_budget, _consume)
            if not covered:
                rotated = filepath + ".1"
                if os.path.exists(rotated):
                    covered = self._scan_tail(rotated, cutoff, scan_budget, _consume)
            if not covered:
                coverage_complete = False
        if not coverage_complete:
            logger.warning(
                f"request_log query({hours}h): scan budget {scan_budget} bytes "
                f"did not reach the cutoff — results are a truncated window"
            )

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
            "coverage_complete": coverage_complete,
        }

    @staticmethod
    def _scan_tail(
        filepath: str,
        cutoff: float,
        max_bytes: int,
        consume: Callable[[dict], None],
    ) -> bool:
        """Parse at most the last max_bytes of a JSONL file, feeding entries
        newer than cutoff into consume().

        Returns True when the read window verifiably covers the cutoff: either
        the file was read from byte 0, or the first (=oldest) parsed entry is
        older than the cutoff. Returns False when the window might miss older
        in-range entries — the caller decides how to escalate (Alt-Generation
        lesen bzw. coverage_complete=False melden).
        """
        try:
            size = os.path.getsize(filepath)
            with open(filepath, "rb") as f:
                from_start = size <= max_bytes
                if not from_start:
                    f.seek(size - max_bytes)
                    f.readline()  # partielle Zeile nach dem Seek verwerfen
                oldest_ts: Optional[float] = None
                for raw in f:
                    try:
                        entry = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    ts = entry.get("ts", 0)
                    if oldest_ts is None:
                        oldest_ts = ts
                    if ts < cutoff:
                        continue
                    try:
                        consume(entry)
                    except KeyError:
                        continue
                if from_start:
                    return True
                # Tail-Fenster: abgedeckt, wenn der älteste gelesene Eintrag
                # VOR dem Cutoff liegt (dann fehlt nichts Neueres).
                return oldest_ts is not None and oldest_ts < cutoff
        except OSError:
            # Fehlende/unlesbare Datei fehlt nicht "unabgedeckt" — es gibt sie
            # schlicht nicht (Glob-Kandidat verschwunden = rotiert worden).
            return True


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

    # Tail budget for latest_for_account(). The file grows ~5 MB/day at the
    # 5-minute scrape cadence; 4 MB of tail therefore covers well over half a
    # day of snapshots — far more than any sane freshness window. Bounded on
    # purpose: this runs on the admission hot path (behind a TTL) and the file
    # is tens of MB, so a full parse per worker per TTL is not acceptable.
    _TAIL_SCAN_BYTES = int(os.getenv("CC_USAGE_TAIL_SCAN_BYTES", str(4 * 1024 * 1024)))

    def latest_for_account(
        self, account: str, max_age_s: float
    ) -> tuple[Optional[dict], Optional[float], str]:
        """
        Newest snapshot entry for ONE account, scanning the file tail backwards.

        Returns (account_entry, snapshot_ts, reason).
        `account_entry` is None whenever no usable value exists; `reason` then
        says WHY in a form fit for a log line and an operator alarm. It is
        never "" on the None path — a caller must always be able to state the
        cause, because "no data" silently rendered as 0 is exactly the failure
        this method exists to make impossible.

        Why per-account and not "the newest snapshot": several producers write
        into this one file (the dev-server scraper posts the four dev accounts,
        the partner-server scraper posts the partner accounts). Taking the last
        line and looking for yourself in it means every foreign snapshot blanks
        your own reading for one cadence. Scanning back to the newest snapshot
        that actually CONTAINS this account is both correct and producer-count
        agnostic.
        """
        if not os.path.exists(self.JSONL_PATH):
            return None, None, f"snapshot file missing ({self.JSONL_PATH})"

        cutoff = time.time() - max_age_s
        try:
            size = os.path.getsize(self.JSONL_PATH)
            with open(self.JSONL_PATH, "rb") as f:
                if size > self._TAIL_SCAN_BYTES:
                    f.seek(size - self._TAIL_SCAN_BYTES)
                    f.readline()  # discard the partial first line
                chunk = f.read()
        except OSError as e:
            return None, None, f"snapshot file unreadable: {e}"

        newest_ts_seen: Optional[float] = None
        for raw in reversed(chunk.splitlines()):
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            ts = float(entry.get("ts", 0) or 0)
            if newest_ts_seen is None:
                newest_ts_seen = ts
            if ts < cutoff:
                # Snapshots are append-ordered: everything further back is older
                # still, so there is nothing left to find within the window.
                break
            for acc in entry.get("accounts", []) or []:
                if acc.get("account") == account:
                    return acc, ts, ""

        if newest_ts_seen is None:
            return None, None, "snapshot file contains no readable snapshot"
        age = time.time() - newest_ts_seen
        return None, None, (
            f"no snapshot for account {account!r} within {int(max_age_s)}s "
            f"(newest snapshot in file is {int(age)}s old)"
        )

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
