"""
_get_executor() thread-pool sizing (src/privacy/anonymizer.py).

The pool was hardcoded to 2 workers ("Presidio is CPU-bound, more workers
don't help") — true on the CPU-only hosts this ran on so far, but wrong once
Flair inference moves to a GPU host: the admission-control semaphore in
app.py (_SMART_ANONYMIZE_MAX_CONCURRENT) is already env-configurable, and
raising it without also raising this executor's worker count would just move
the bottleneck here instead of removing it. SMART_ANONYMIZE_EXECUTOR_WORKERS
makes this configurable; the default (2, unset) must stay identical to the
prior hardcoded behaviour.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("presidio_analyzer")

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.privacy import anonymizer  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_executor_singleton():
    """Every test starts with a fresh executor so the module-level singleton
    from a previous test doesn't leak its worker count into this one."""
    anonymizer._executor = None
    yield
    if anonymizer._executor is not None:
        anonymizer._executor.shutdown(wait=False)
    anonymizer._executor = None


def test_default_worker_count_matches_prior_hardcoded_behavior(monkeypatch):
    monkeypatch.setattr(anonymizer, "_EXECUTOR_WORKERS", 2)
    pool = anonymizer._get_executor()
    assert pool._max_workers == 2


def test_worker_count_configurable_via_env(monkeypatch):
    monkeypatch.setattr(anonymizer, "_EXECUTOR_WORKERS", 8)
    pool = anonymizer._get_executor()
    assert pool._max_workers == 8


def test_env_var_parsed_at_module_load(monkeypatch):
    """SMART_ANONYMIZE_EXECUTOR_WORKERS is read once at import time — this
    pins that the parsing itself (int(os.getenv(..., "2"))) is correct,
    independent of the singleton-caching behaviour covered above."""
    assert anonymizer._EXECUTOR_WORKERS == int(
        __import__("os").environ.get("SMART_ANONYMIZE_EXECUTOR_WORKERS", "2")
    )
