"""Die platform-api-Seite der Geldzeile (ADR-0009 Schritt 2c).

Der Worker haelt seit Schritt 2c keine DB-Verbindung mehr; die beiden INSERTs
liegen in src/activity/ledger_db.py. Die Invarianten, die vorher im Writer
festgenagelt waren, gelten weiter — nur eben hier:

1. Die Audit-Zeile haengt an "die Geldzeile wurde in DIESEM Versuch erzeugt".
   `activities` hat keinen Unique-Schluessel: ein bedingungsloser Nachlauf
   wuerde den Audit-Trail bei jedem Wiederholungsversuch verdoppeln.
2. Beide Zeilen tragen dieselbe Ursprungszeit — sonst driften die zwei Sichten
   auf denselben Call auseinander, und ein Call vom Monatsletzten landet in der
   einen Sicht im naechsten Monat.
3. Audit und Abrechnung teilen kein Schicksal. Bis 2026-08-01 sassen sie in
   einem try-Block, ein abgelehnter Audit-INSERT (app_id="bridge-jobs" gegen
   das ENUM) riss die Geldzeile mit — jeder un-attribuierte durable Job buchte
   gar nichts.
4. Geld zuerst: die Geldzeile wird VOR der Audit-Zeile geschrieben.
"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.activity import ledger_db

RECORDED_AT = datetime.fromtimestamp(1_700_000_000.0, tz=timezone.utc)
USER_ID = str(uuid.uuid4())
TENANT_ID = str(uuid.uuid4())

# Positionsbindung des usage_events-INSERTs (siehe ledger_db.insert_ai_call):
# $17 = recorded_at, $18 = idempotency_key — sie haengen bewusst HINTER der
# Metadata, damit $1..$16 unveraendert bleiben. $19/$20 = status/error_code
# (Migration 059) haengen aus demselben Grund GANZ HINTEN an.
USAGE_RECORDED_AT = 16
USAGE_IDEMPOTENCY_KEY = 17
USAGE_STATUS = 18
USAGE_ERROR_CODE = 19
AUDIT_RECORDED_AT = 6


def _pool(*, created=True, audit_raises=False):
    conn = AsyncMock()
    conn.order = []

    async def _fetchrow(sql, *args):
        conn.order.append("usage_events")
        return MagicMock() if created else None

    async def _execute(sql, *args):
        conn.order.append("activities")
        if audit_raises:
            raise RuntimeError('invalid input value for enum app_id: "bridge-jobs"')
        return None

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.execute = AsyncMock(side_effect=_execute)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


async def _insert(pool, **over):
    kwargs = dict(
        idempotency_key="call-uid-1",
        recorded_at=RECORDED_AT,
        actor_user_id=USER_ID,
        tenant_id=TENANT_ID,
        app="werking-report",
        app_env="prod",
        model="claude-sonnet-5",
        provider="anthropic",
        region=None,
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        billing_mode="flat_rate_estimated",
        real_cost_eur=0.0,
        hypothetical_cost_eur=0.01,
        pricing_version="test-v1",
        provider_metadata={"feature": "call"},
        audit_event_type="ai-call:call",
        audit_payload={"feature": "call", "promptTokens": 1000},
    )
    kwargs.update(over)
    with patch.object(ledger_db, "get_pool", return_value=pool):
        return await ledger_db.insert_ai_call(**kwargs)


@pytest.mark.asyncio
async def test_created_row_also_writes_the_audit_row():
    pool, conn = _pool(created=True)
    result = await _insert(pool)

    assert result.created is True
    assert result.audit_written is True
    assert conn.order == ["usage_events", "activities"], (
        "Geld zuerst — die Reihenfolge ist die Zusage, wenn die Verbindung "
        "zwischen zwei Statements stirbt"
    )


@pytest.mark.asyncio
async def test_duplicate_does_not_write_a_second_audit_row():
    """Invariante 1 — `activities` hat keinen Unique-Schluessel."""
    pool, conn = _pool(created=False)
    result = await _insert(pool)

    assert result.created is False
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_both_rows_carry_the_calls_origin_time():
    """Invariante 2."""
    pool, conn = _pool(created=True)
    await _insert(pool)

    usage_args = conn.fetchrow.call_args_list[0].args[1:]
    audit_args = conn.execute.call_args_list[0].args[1:]

    assert usage_args[USAGE_RECORDED_AT] == RECORDED_AT
    assert audit_args[AUDIT_RECORDED_AT] == RECORDED_AT


@pytest.mark.asyncio
async def test_audit_failure_does_not_cost_the_ledger_row():
    """Invariante 3 — die Regression von 2026-08-01."""
    pool, conn = _pool(created=True, audit_raises=True)
    result = await _insert(pool)

    assert result.created is True, (
        "ein gescheiterter Audit-INSERT darf die Abrechnungszeile nicht mitreissen"
    )
    assert result.audit_written is False
    assert result.audit_error is not None


@pytest.mark.asyncio
async def test_idempotency_key_and_metadata_reach_the_row():
    pool, conn = _pool(created=True)
    await _insert(pool, provider_metadata={"app_id_raw": "bridge-jobs"})

    usage_args = conn.fetchrow.call_args_list[0].args[1:]
    assert usage_args[USAGE_IDEMPOTENCY_KEY] == "call-uid-1"
    assert json.loads(usage_args[15])["app_id_raw"] == "bridge-jobs"


@pytest.mark.asyncio
async def test_status_and_error_code_extracted_from_provider_metadata_into_columns():
    """Migration 059 — status/error_code become dedicated, indexed columns
    instead of living only inside provider_metadata JSONB. ai_call_writer.py
    already puts both into provider_metadata for every call; insert_ai_call
    must extract them so a 429 mid-call error is queryable via a real column,
    not just via ->>'status' extraction."""
    pool, conn = _pool(created=True)
    await _insert(
        pool,
        provider_metadata={
            "status": "error",
            "error_code": "429",
            "error_message": "rate limited",
        },
    )

    usage_args = conn.fetchrow.call_args_list[0].args[1:]
    assert usage_args[USAGE_STATUS] == "error"
    assert usage_args[USAGE_ERROR_CODE] == "429"


@pytest.mark.asyncio
async def test_missing_status_in_metadata_defaults_to_success():
    """provider_metadata without a 'status' key (legacy callers, or a call
    site that never sets it) must not leave the column NULL — the ledger
    only ever records completed calls unless told otherwise."""
    pool, conn = _pool(created=True)
    await _insert(pool, provider_metadata={"feature": "call"})

    usage_args = conn.fetchrow.call_args_list[0].args[1:]
    assert usage_args[USAGE_STATUS] == "success"
    assert usage_args[USAGE_ERROR_CODE] is None


@pytest.mark.asyncio
async def test_ledger_failure_propagates():
    """Der Endpunkt muss daraus ein 5xx machen koennen. Wuerde diese Funktion
    den Fehler schlucken und "geschrieben" melden, quittierte der Worker seinen
    Puffer-Eintrag — aus einer wiederholbaren Luecke wuerde nicht abgerechnete
    Nutzung."""
    pool, conn = _pool(created=True)
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("connection reset by peer"))

    with pytest.raises(RuntimeError):
        await _insert(pool)
