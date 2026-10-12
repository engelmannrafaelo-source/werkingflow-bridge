"""FK25B2 R4 — Codex' FK25BR R4 counterprobe, turned into the acceptance.

Source: /root/projekte/local-storage/fk25br4-20261012/test_r4_lost_report.py
(Codex, 12.10.2026, against 67f688e). Scenario unchanged: a real Postgres
job, the stop interrupted inside the awaited deduction resume, the real
watchdog closes the row, two DELETEs through the real route. Codex' probe
asserted the defect (LOST_RECOVERY_REPORT 200/200, no account); here it
asserts the corrected outcome. Added: the watchdog's 'pending' branch and
defer_job's branch, which close rows that did not / did run.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.activity import ai_call_writer
from src.jobs import registry, routes, selbstaufruf, store, store_client
from tests.jobs import test_job_cancel_br10 as br10
from tests.jobs import test_job_cancel_fk25b as f

pg_store = br10.pg_store


def _real_store(monkeypatch):
    from src.main import extract_attribution_context

    monkeypatch.setenv("BRIDGE_ORIGIN_ID", f.HOME)
    monkeypatch.setattr(routes, "verify_api_key", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(routes, "_attribution_extractor", extract_attribution_context)
    for name in ("get_job", "cancel_job", "cancel_state", "finish_cancelled", "heartbeat",
                 "update_progress", "mark_done", "mark_error", "claim_stale_job",
                 "find_abandoned", "defer_job"):
        monkeypatch.setattr(store_client, name, getattr(store, name))


async def _altern(pg, job_id):
    async with pg.acquire() as c:
        await c.execute(
            "UPDATE ai_jobs SET heartbeat_at=NOW()-interval '1 hour',"
            "updated_at=NOW()-interval '1 hour' WHERE job_id=$1", job_id,
        )


@pytest.mark.asyncio
async def test_cancel_during_recovery_watchdog_then_delete_is_not_plain_success(
    pg_store, monkeypatch
):
    """Codex' scenario. Codex saw: job cancelled by the watchdog without an
    account, DELETE 200/200 {job_id,status} — 'nothing ran'. Now: the stop
    recorded 'booking in progress' with the open deduction's id before it
    booked; the watchdog turns it into 'unbekannt'; both DELETEs answer 502
    job_cancel_ledger_unknown naming that id. No replay, no extra booking,
    no second deduction."""
    _real_store(monkeypatch)
    entered = asyncio.Event()
    deductions = []
    bookings = []

    async def interrupted_deduction(*args, **kwargs):
        deductions.append(kwargs["call_uid"])
        entered.set()
        await asyncio.Event().wait()

    async def book(**kwargs):
        bookings.append(kwargs)
        return "written"

    monkeypatch.setattr(ai_call_writer, "_deduct_call_cost", interrupted_deduction)
    monkeypatch.setattr(ai_call_writer, "persist_ai_call_activity", book)

    async def executor(*args):
        report = await selbstaufruf._im_ledger_vermerken(
            SimpleNamespace(state=SimpleNamespace(ledger_provider="anthropic")),
            f.JOB, f.OWNER_ATTR, "m", "/v1/chat/completions", 1, "wunsch", [],
            abzuege={"already-written-call": {"gespoolt": False, "args": {
                "user_id": "synthetic", "app_id": "werking-energy", "cost_eur": 1,
                "workflow_id": "synthetic", "tenant_id": "synthetic", "call_ts": 1}}})
        raise selbstaufruf.JobLaufAbgebrochen(report)

    monkeypatch.setitem(registry._EXECUTORS, "chat", executor)
    await br10._insert(pg_store, f.JOB, "running", attribution=f.OWNER_ATTR)
    await store.cancel_job(f.JOB)
    task = asyncio.create_task(registry._run_body(f.JOB, "chat", {}, f.OWNER_ATTR))
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()  # worker/handler shutdown interrupts the awaited recovery
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert deductions == ["already-written-call"] and bookings == []
    job = await store.get_job(f.JOB)
    assert job["status"] == "running"
    assert job["progress"]["abbruch"]["ledger"] == "offen"
    assert job["progress"]["abbruch"]["offene_abzuege"] == ["already-written-call"]

    await _altern(pg_store, f.JOB)
    assert await store.claim_stale_job(60, 3) is None
    job = await store.get_job(f.JOB)
    assert job["status"] == "cancelled"
    assert job["progress"]["abbruch"]["ledger"] == "unbekannt"
    assert job["progress"]["abbruch"]["geschlossen_von"] == "watchdog"
    assert job["progress"]["abbruch"]["offene_abzuege"] == ["already-written-call"]

    responses = [await f._delete(f.JOB, f.OWNER_HEADERS) for _ in range(2)]
    for r in responses:
        err = r.json()["error"]
        assert (r.status_code, err["code"], err["status"]) == (
            502, "job_cancel_ledger_unknown", "cancelled",
        ), r.text
        assert "already-written-call" in err["message"]
    assert f.ohne_zeit(responses[0]) == f.ohne_zeit(responses[1])
    assert deductions == ["already-written-call"] and bookings == []


@pytest.mark.asyncio
async def test_watchdog_running_without_any_marker_is_unknown(pg_store, monkeypatch):
    """The worker died mid-run, before any stop began (no marker at all):
    it ran, its booking is unknown — 502, not 200."""
    _real_store(monkeypatch)
    await br10._insert(pg_store, f.JOB, "running", attribution=f.OWNER_ATTR)
    await store.cancel_job(f.JOB)
    await _altern(pg_store, f.JOB)
    await store.claim_stale_job(60, 3)
    r = await f._delete(f.JOB, f.OWNER_HEADERS)
    assert (r.status_code, r.json()["error"]["code"]) == (502, "job_cancel_ledger_unknown")


@pytest.mark.asyncio
async def test_watchdog_keeps_a_written_account(pg_store, monkeypatch):
    """A stop that wrote its account but died before finish_cancelled: the
    watchdog closes the row and keeps that account (no 'unbekannt' over it)."""
    _real_store(monkeypatch)
    await br10._insert(pg_store, f.JOB, "running", attribution=f.OWNER_ATTR)
    await store.cancel_job(f.JOB)
    await store.update_progress(f.JOB, {"abbruch": {"ledger": "gebucht"}})
    await _altern(pg_store, f.JOB)
    await store.claim_stale_job(60, 3)
    r = await f._delete(f.JOB, f.OWNER_HEADERS)
    assert (r.status_code, r.json()) == (
        200, {"job_id": f.JOB, "status": "cancelled", "ledger": "gebucht"},
    )


@pytest.mark.asyncio
async def test_watchdog_pending_branch_stays_plain_200(pg_store, monkeypatch):
    """Never ran: a 'pending' row with a wish, closed by the watchdog —
    plain 200, no ledger claim."""
    _real_store(monkeypatch)
    await br10._insert(pg_store, f.JOB, "pending", attribution=f.OWNER_ATTR)
    async with pg_store.acquire() as c:
        await c.execute(
            "UPDATE ai_jobs SET cancel_requested_at=NOW() WHERE job_id=$1", f.JOB
        )
    await store.claim_stale_job(60, 3)
    assert (await store.get_job(f.JOB))["status"] == "cancelled"
    r = await f._delete(f.JOB, f.OWNER_HEADERS)
    assert (r.status_code, r.json()) == (200, {"job_id": f.JOB, "status": "cancelled"})


@pytest.mark.asyncio
async def test_defer_job_cancelled_row_says_it_ran(pg_store, monkeypatch):
    """A running row parked by its own run with the wish recorded: it ran
    and booked itself — 200 with ledger 'vom_lauf', not the bare 'nothing
    ran' answer."""
    _real_store(monkeypatch)
    await br10._insert(pg_store, f.JOB, "running", attribution=f.OWNER_ATTR)
    await store.cancel_job(f.JOB)
    await store.defer_job(f.JOB, 30, "429 no account capacity")
    assert (await store.get_job(f.JOB))["status"] == "cancelled"
    r = await f._delete(f.JOB, f.OWNER_HEADERS)
    assert (r.status_code, r.json()) == (
        200, {"job_id": f.JOB, "status": "cancelled", "ledger": "vom_lauf"},
    )
