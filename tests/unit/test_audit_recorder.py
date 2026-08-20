"""Tests for the platform-api-side audit_log INSERT (ADR-0009 Schritt 2a, C4).

This used to be src/audit/recorder.py's own DB write; that logic now lives in
src/audit/db_writer.py (used by POST /v1/internal/audit-events, run on
platform-api). recorder.py itself is now an HTTP client of that endpoint —
see test_audit_recorder_http.py for its (very different) contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.audit import db_writer


class _FakeConn:
    def __init__(self, sink):
        self.sink = sink

    async def execute(self, sql, *args):
        self.sink["sql"] = sql
        self.sink["args"] = args


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, sink):
        self.conn = _FakeConn(sink)

    def acquire(self):
        return _FakeAcquire(self.conn)


@pytest.mark.asyncio
async def test_insert_is_noop_when_db_disabled(monkeypatch):
    monkeypatch.setattr(db_writer, "is_db_enabled", lambda: False)

    def _boom():
        raise AssertionError("get_pool must not be called when DB is disabled")

    monkeypatch.setattr(db_writer, "get_pool", _boom)
    # Must not raise.
    await db_writer.insert_audit_event("pii.pseudonymized")


@pytest.mark.asyncio
async def test_insert_writes_value_free_attestation(monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(db_writer, "is_db_enabled", lambda: True)
    monkeypatch.setattr(db_writer, "get_pool", lambda: _FakePool(sink))

    await db_writer.insert_audit_event(
        "pii.pseudonymized",
        actor_user_id="11111111-1111-1111-1111-111111111111",
        actor_label="engelmann",
        target_kind="anonymization",
        target_id="wf-1",
        metadata={"total_entities": 4, "entity_counts_by_type": {"PERSON": 1}},
    )

    assert "INSERT INTO audit_log" in sink["sql"]
    args = sink["args"]
    assert str(args[0]) == "11111111-1111-1111-1111-111111111111"
    assert args[2] == "pii.pseudonymized"
    meta = json.loads(args[5])
    assert meta["total_entities"] == 4
    assert meta["entity_counts_by_type"] == {"PERSON": 1}


@pytest.mark.asyncio
async def test_non_uuid_actor_is_preserved_not_dropped(monkeypatch):
    sink: dict = {}
    monkeypatch.setattr(db_writer, "is_db_enabled", lambda: True)
    monkeypatch.setattr(db_writer, "get_pool", lambda: _FakePool(sink))

    await db_writer.insert_audit_event(
        "pii.pseudonymized",
        actor_user_id="david@engelmann.example",  # not a UUID
    )

    args = sink["args"]
    assert args[0] is None  # not a valid UUID -> None
    meta = json.loads(args[5])
    assert meta["actor_user_id_raw"] == "david@engelmann.example"
