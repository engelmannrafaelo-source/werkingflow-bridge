"""Retention sweep for per-session directories under INSTANCES_DIR.

Every CLI session writes ``prompt.txt``, ``messages.jsonl``, ``final_response.json``
and ``metadata.json`` into a directory named ``YYYY-MM-DD-HHMM_<uuid>``. The old
sweep in ``main.cleanup_old_sessions`` only looked one level deeper
(``INSTANCES_DIR/<instance>/<session>``), but ``ClaudeCLI`` creates the session
directory directly in its cwd, which in the worker containers IS
``INSTANCES_DIR`` (``/app/instances/<session>``). Result, measured on prod
2026-09-24: not a single session directory was ever removed (20k+ on
prod-workers since 2026-08-31, 5.7k on server2 since 2026-04-16) — full prompts
kept indefinitely instead of 24 h.

This module collects candidates on BOTH layouts:
  * ``INSTANCES_DIR/<session>``                      (actual layout)
  * ``INSTANCES_DIR/<instance>/<session>``            (documented layout)
  * ``INSTANCES_DIR/<instance>/temp/sessions/<any>``  (legacy progress tracking)

Anything that does not match those shapes (e.g. ``_async_jobs``) is never touched.

Kill switch: ``SESSION_CLEANUP_DISABLED=true`` makes the sweep a loud no-op
(WARNING every cycle). Exists so the backlog can be preserved as evidence on a
host until it has been secured — deploying this fix without it deletes every
session directory older than the retention window on the first cycle.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

SESSION_DIR_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{4}_[a-f0-9-]{36}$")


def cleanup_disabled() -> bool:
    return os.getenv("SESSION_CLEANUP_DISABLED", "").strip().lower() in ("1", "true", "yes")


@dataclass
class SweepResult:
    cleaned: int = 0
    failed: int = 0
    skipped: int = 0
    cleaned_paths: List[Path] = field(default_factory=list)


def collect_session_dirs(instances_dir: Path) -> List[Path]:
    """All candidate session directories on every supported layout."""
    candidates: List[Path] = []
    try:
        top = [d for d in instances_dir.iterdir() if d.is_dir()]
    except OSError:
        logger.error(f"❌ Failed to list instances directory: {instances_dir}", exc_info=True)
        return candidates

    for entry in top:
        if SESSION_DIR_PATTERN.match(entry.name):
            # Actual layout: session dir sits directly under INSTANCES_DIR.
            # It is a leaf — never descend into it looking for more sessions.
            candidates.append(entry)
            continue

        # Documented layout: entry is an instance dir.
        legacy = entry / "temp" / "sessions"
        if legacy.is_dir():
            try:
                candidates.extend(p for p in legacy.iterdir() if p.is_dir())
            except OSError:
                logger.error(f"❌ Failed to list {legacy}", exc_info=True)
        try:
            candidates.extend(
                p for p in entry.iterdir() if p.is_dir() and SESSION_DIR_PATTERN.match(p.name)
            )
        except OSError:
            logger.error(f"❌ Failed to list {entry}", exc_info=True)
    return candidates


def _session_timestamp(session_dir: Path) -> datetime | None:
    metadata_file = session_dir / "metadata.json"
    if not metadata_file.exists():
        return None
    metadata = json.loads(metadata_file.read_text())
    ts = metadata.get("completed_at") or metadata.get("created_at")
    if not ts:
        return None
    parsed = datetime.fromisoformat(ts)
    # Sessions write naive local time (datetime.now()); compare like with like.
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def sweep_expired_sessions(instances_dir: Path, cutoff: datetime) -> SweepResult:
    """Delete session dirs whose metadata timestamp is older than ``cutoff``.

    Dirs without metadata or without a timestamp are kept (counted as skipped):
    a session still being written must never be removed.
    """
    result = SweepResult()
    if not instances_dir.exists():
        logger.warning(f"⚠️  Instances directory not found: {instances_dir}")
        return result

    real_root = Path(os.path.realpath(instances_dir))
    for session_dir in collect_session_dirs(instances_dir):
        try:
            ts = _session_timestamp(session_dir)
        except (json.JSONDecodeError, ValueError, TypeError, OSError):
            logger.error(f"❌ Unreadable metadata in session: {session_dir}", exc_info=True)
            result.skipped += 1
            continue
        if ts is None:
            result.skipped += 1
            continue
        if ts >= cutoff:
            continue

        real_session = Path(os.path.realpath(session_dir))
        if real_root not in real_session.parents:
            logger.error(f"❌ Security: session path escapes instances dir (symlink?): {session_dir}")
            result.skipped += 1
            continue
        try:
            shutil.rmtree(session_dir)
            result.cleaned += 1
            result.cleaned_paths.append(session_dir)
        except OSError:
            result.failed += 1
            logger.error(f"❌ Failed to delete session directory: {session_dir}", exc_info=True)
    return result
