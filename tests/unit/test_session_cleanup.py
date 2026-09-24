"""Session-dir retention sweep (src/session_cleanup.py).

Regression: the old inline sweep in main.cleanup_old_sessions only looked at
INSTANCES_DIR/<instance>/<session>, but workers write INSTANCES_DIR/<session>.
On prod nothing was ever deleted (measured 2026-09-24).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.session_cleanup import (
    cleanup_disabled,
    collect_session_dirs,
    sweep_expired_sessions,
)

NOW = datetime(2026, 9, 24, 12, 0, 0)
CUTOFF = NOW - timedelta(hours=24)


def _session(parent: Path, when: datetime | None, *, key: str = "completed_at") -> Path:
    d = parent / f"{when or NOW:%Y-%m-%d-%H%M}_{uuid.uuid4()}"
    d.mkdir(parents=True)
    (d / "prompt.txt").write_text("x")
    meta = {"created_at": (when or NOW).isoformat()}
    if when is not None:
        meta[key] = when.isoformat()
    else:
        meta = {}
    (d / "metadata.json").write_text(json.dumps(meta))
    return d


def test_top_level_layout_is_swept(tmp_path: Path) -> None:
    """The actual worker layout: /app/instances/<session> — the missed case."""
    old = _session(tmp_path, NOW - timedelta(days=30))
    fresh = _session(tmp_path, NOW - timedelta(hours=1))

    result = sweep_expired_sessions(tmp_path, CUTOFF)

    assert result.cleaned == 1
    assert not old.exists()
    assert fresh.exists()


def test_nested_instance_layout_still_swept(tmp_path: Path) -> None:
    inst = tmp_path / "worker-coach"
    old = _session(inst, NOW - timedelta(days=2))
    fresh = _session(inst, NOW)
    legacy = _session(inst / "temp" / "sessions", NOW - timedelta(days=3))

    result = sweep_expired_sessions(tmp_path, CUTOFF)

    assert result.cleaned == 2
    assert not old.exists() and not legacy.exists()
    assert fresh.exists() and inst.exists()


def test_session_dir_is_leaf_not_scanned_as_instance(tmp_path: Path) -> None:
    top = _session(tmp_path, NOW)
    # a pattern-shaped subdir inside a session must not be treated as a session
    inner = _session(top, NOW - timedelta(days=5))
    assert inner not in collect_session_dirs(tmp_path)


def test_foreign_dirs_and_missing_metadata_untouched(tmp_path: Path) -> None:
    jobs = tmp_path / "_async_jobs"
    jobs.mkdir()
    (jobs / "job.json").write_text("{}")
    no_meta = tmp_path / f"2020-01-01-0000_{uuid.uuid4()}"
    no_meta.mkdir()
    no_ts = _session(tmp_path, None)

    result = sweep_expired_sessions(tmp_path, CUTOFF)

    assert result.cleaned == 0
    assert jobs.exists() and no_meta.exists() and no_ts.exists()
    assert result.skipped == 2


def test_created_at_fallback_and_tz_aware_timestamp(tmp_path: Path) -> None:
    d = tmp_path / f"2026-09-01-0000_{uuid.uuid4()}"
    d.mkdir()
    (d / "metadata.json").write_text(json.dumps({"created_at": "2026-09-01T00:00:00+00:00"}))

    assert sweep_expired_sessions(tmp_path, CUTOFF).cleaned == 1
    assert not d.exists()


def test_symlink_escape_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    target = _session(outside, NOW - timedelta(days=9))
    root = tmp_path / "instances"
    root.mkdir()
    link = root / target.name
    link.symlink_to(target, target_is_directory=True)

    result = sweep_expired_sessions(root, CUTOFF)

    assert result.cleaned == 0
    assert target.exists()


@pytest.mark.parametrize("val,expected", [("true", True), ("1", True), ("", False), ("false", False)])
def test_kill_switch(monkeypatch: pytest.MonkeyPatch, val: str, expected: bool) -> None:
    monkeypatch.setenv("SESSION_CLEANUP_DISABLED", val)
    assert cleanup_disabled() is expected
