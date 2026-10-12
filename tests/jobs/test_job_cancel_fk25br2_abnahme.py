"""FK25B2 R2 — Codex' FK25BR R2 counterprobes, taken over as the acceptance.

Source: /root/projekte/local-storage/fk25br2-20261012/test_r2_counterprobes.py
(Codex, 12.10.2026, against 000c960). The scenarios are Codex' own; where a
probe proved a defect, the assertion is the corrected outcome and the
docstring names what it saw before. Added next to them: the spooled
("rueckstau") variant and the no-double-booking guard that the P2 fix needs.
All synthetic: no provider, no real ledger.
"""
import asyncio
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.activity import ai_call_writer, ledger_spool
from src.jobs import registry, selbstaufruf, teilverbrauch
from tests.jobs import test_job_cancel_fk25b as f

mem = f.mem
welt = f.welt
REAL_WRITER = ai_call_writer.persist_ai_call_activity


# ---------------------------------------------------------------------------
# P1 — a failed booking is never confirmed as booked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_ledger_does_not_confirm_cancel(welt, monkeypatch):
    """Codex saw DELETE 200 'cancelled' while the writer answered 'failed'
    (FALSE_BOOKED_HTTP). Now: the run is stopped and the row cancelled, but
    DELETE says 502 job_cancel_ledger_failed and the row carries the account."""
    from src import main

    started = threading.Event()

    async def work(*a):
        started.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(main, "_chat_completions_lauf", work)
    failed = AsyncMock(return_value="failed")
    monkeypatch.setattr(ai_call_writer, "persist_ai_call_activity", failed)
    welt.mem.add(f.JOB, "running")
    task = asyncio.create_task(registry._run_body(f.JOB, "chat", f.BODY, f.OWNER_ATTR))
    try:
        assert await f._bis(started.is_set)
        r = await f._delete(f.JOB, f.OWNER_HEADERS)
        await asyncio.wait_for(task, 3)
        assert failed.await_count == 1
        err = r.json()["error"]
        assert (r.status_code, err["code"], err["status"]) == (
            502, "job_cancel_ledger_failed", "cancelled",
        ), r.text
        assert welt.mem.rows[f.JOB]["status"] == "cancelled"
        abbruch = welt.mem.rows[f.JOB]["progress"]["abbruch"]
        assert abbruch["ledger"] == "fehlgeschlagen"
        assert abbruch["ledger_outcomes"] == ["failed"]
    finally:
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
async def test_real_writer_failure_and_no_durable_spool_is_failed(monkeypatch, ledger_seam):
    """Codex' real-writer probe: ledger down, spool write refused. Codex saw
    ledger 'gebucht' (REAL_WRITER_FAILED_WITHOUT_SPOOL). Now 'fehlgeschlagen'."""
    ledger_seam.context = {"tenantId": str(uuid.uuid4()), "billingMode": "pay_per_token"}
    ledger_seam.explode_on = "ledger"
    monkeypatch.setattr(ledger_spool, "append_call", lambda *a, **kw: False)
    monkeypatch.setattr(ledger_spool, "spool_enabled", lambda: True)
    outcomes = []

    async def track(**kw):
        outcome = await REAL_WRITER(**kw)
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(ai_call_writer, "persist_ai_call_activity", track)
    req = SimpleNamespace(state=SimpleNamespace(ledger_provider="anthropic"))
    result = await selbstaufruf._im_ledger_vermerken(
        req, f.JOB,
        {"app_id": "werking-energy", "user_id": str(uuid.uuid4()), "app_env": "dev"},
        "claude-sonnet-5-5", "/v1/chat/completions", 10, "wunsch",
        [{"model": "claude-sonnet-5-5", "input_tokens": 111, "output_tokens": 22}],
    )
    assert outcomes == ["failed"] and not ledger_seam.ledger_calls
    assert result["ledger"] == "fehlgeschlagen"
    assert result["ledger_outcomes"] == ["failed"]


@pytest.mark.asyncio
async def test_real_writer_failure_with_durable_spool_is_backlog(monkeypatch, ledger_seam):
    """Same failure, but the spool has the record: 'rueckstau' — owed, not
    'gebucht'."""
    ledger_seam.context = {"tenantId": str(uuid.uuid4()), "billingMode": "pay_per_token"}
    ledger_seam.explode_on = "ledger"
    monkeypatch.setattr(ledger_spool, "append_call", lambda *a, **kw: True)
    monkeypatch.setattr(ledger_spool, "spool_enabled", lambda: True)
    req = SimpleNamespace(state=SimpleNamespace(ledger_provider="anthropic"))
    result = await selbstaufruf._im_ledger_vermerken(
        req, f.JOB,
        {"app_id": "werking-energy", "user_id": str(uuid.uuid4()), "app_env": "dev"},
        "claude-sonnet-5-5", "/v1/chat/completions", 10, "wunsch",
        [{"model": "claude-sonnet-5-5", "input_tokens": 111, "output_tokens": 22}],
    )
    assert result["ledger"] == "rueckstau" and result["ledger_outcomes"] == ["failed"]


@pytest.mark.asyncio
async def test_spooled_booking_answers_202_cancelled(welt, monkeypatch):
    """DELETE for a stop whose booking is owed in the spool: 202, status
    cancelled, ledger rueckstau — stopped, not yet billed."""
    from src import main

    started = threading.Event()

    async def work(*a):
        started.set()
        await asyncio.sleep(30)

    async def spooled_only(**kw):
        ai_call_writer._buchung_gesichert()  # what the writer does after the fsync
        return "failed"

    monkeypatch.setattr(main, "_chat_completions_lauf", work)
    monkeypatch.setattr(ai_call_writer, "persist_ai_call_activity", spooled_only)
    welt.mem.add(f.JOB, "running")
    task = asyncio.create_task(registry._run_body(f.JOB, "chat", f.BODY, f.OWNER_ATTR))
    try:
        assert await f._bis(started.is_set)
        r = await f._delete(f.JOB, f.OWNER_HEADERS)
        await asyncio.wait_for(task, 3)
        body = r.json()
        assert (r.status_code, body["status"], body["ledger"]) == (202, "cancelled", "rueckstau")
        assert welt.mem.rows[f.JOB]["status"] == "cancelled"
    finally:
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
async def test_pending_ledger_keeps_delete_at_202(welt, monkeypatch):
    """Unchanged: while the writer is still busy, DELETE says 202 running."""
    from src import main
    from src.jobs import routes

    started = threading.Event()
    booking = threading.Event()
    release = threading.Event()

    async def work(*a):
        started.set()
        await asyncio.sleep(30)

    async def slow_book(**kw):
        booking.set()
        while not release.is_set():
            await asyncio.sleep(.01)
        return "written"

    monkeypatch.setattr(main, "_chat_completions_lauf", work)
    monkeypatch.setattr(ai_call_writer, "persist_ai_call_activity", slow_book)
    monkeypatch.setattr(routes, "JOB_CANCEL_WAIT_S", .15)
    welt.mem.add(f.JOB, "running")
    task = asyncio.create_task(registry._run_body(f.JOB, "chat", f.BODY, f.OWNER_ATTR))
    try:
        assert await f._bis(started.is_set)
        r = await f._delete(f.JOB, f.OWNER_HEADERS)
        assert r.status_code == 202 and booking.is_set()
        assert welt.mem.rows[f.JOB]["status"] == "running"
        release.set()
        await asyncio.wait_for(task, 3)
        assert welt.mem.rows[f.JOB]["status"] == "cancelled"
        assert welt.mem.rows[f.JOB]["progress"]["abbruch"]["ledger"] == "gebucht"
    finally:
        release.set()
        if not task.done():
            task.cancel()


# ---------------------------------------------------------------------------
# P2 — measured usage survives until the run's own booking is durable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_keeps_measured_usage_until_booked(welt, monkeypatch):
    """Codex' probe: one completed call (111/22), then the result, then the
    stop before the run's own booking. Codex saw one row with 0/0, error,
    verbrauch unbekannt (KNOWN_USAGE_CLEARED). Now the stop books 111/22."""
    from src import main

    ready = threading.Event()

    async def work(*a):
        selbstaufruf.ledger_provider_merken(a[1], "anthropic")
        teilverbrauch.aus_cli_nachricht({"type": "assistant", "message": {
            "id": "completed-call", "model": "claude-sonnet-5-5",
            "usage": {"input_tokens": 111, "output_tokens": 22}}})
        teilverbrauch.aus_cli_nachricht({"type": "result", "subtype": "success"})
        ready.set()
        await asyncio.sleep(30)  # run has result; its endpoint has not booked yet

    monkeypatch.setattr(main, "_chat_completions_lauf", work)
    welt.mem.add(f.JOB, "running")
    task = asyncio.create_task(registry._run_body(f.JOB, "chat", f.BODY, f.OWNER_ATTR))
    try:
        assert await f._bis(ready.is_set)
        r = await f._delete(f.JOB, f.OWNER_HEADERS)
        await asyncio.wait_for(task, 3)
        assert r.status_code == 200
        assert len(welt.ledger) == 1
        row = welt.ledger[0]
        assert (row["input_tokens"], row["output_tokens"], row["status"]) == (
            111, 22, "undelivered",
        )
        assert row["provider_meta"]["verbrauch"] == "teilweise_gemessen"
    finally:
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
async def test_durably_booked_run_is_not_booked_again(welt, monkeypatch):
    """The other side of P2: once the run's own booking is durable (the
    writer's hook, after the spool fsync), the stop does not book those calls
    a second time."""
    from src import main

    ready = threading.Event()

    async def work(*a):
        selbstaufruf.ledger_provider_merken(a[1], "anthropic")
        teilverbrauch.aus_cli_nachricht({"type": "assistant", "message": {
            "id": "completed-call", "model": "claude-sonnet-5-5",
            "usage": {"input_tokens": 111, "output_tokens": 22}}})
        teilverbrauch.aus_cli_nachricht({"type": "result", "subtype": "success"})
        ai_call_writer._buchung_gesichert()  # the run's own booking reached the spool
        ready.set()
        await asyncio.sleep(30)

    monkeypatch.setattr(main, "_chat_completions_lauf", work)
    welt.mem.add(f.JOB, "running")
    task = asyncio.create_task(registry._run_body(f.JOB, "chat", f.BODY, f.OWNER_ATTR))
    try:
        assert await f._bis(ready.is_set)
        r = await f._delete(f.JOB, f.OWNER_HEADERS)
        await asyncio.wait_for(task, 3)
        assert r.status_code == 200
        (row,) = welt.ledger
        assert (row["input_tokens"], row["output_tokens"], row["status"]) == (0, 0, "error")
    finally:
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
async def test_real_writer_reports_durable_booking_to_the_run(monkeypatch, ledger_seam):
    """The hook itself, through the real writer: a spooled booking inside a
    supervised self-call drops the finished run's calls; outside one it is a
    no-op; a booking that is neither spooled nor settled keeps them."""
    ledger_seam.context = {"tenantId": str(uuid.uuid4()), "billingMode": "pay_per_token"}
    kw = dict(
        app_id="werking-energy", user_id=str(uuid.uuid4()), agent_id=None,
        workflow_id=None, model="claude-sonnet-5-5", input_tokens=111,
        output_tokens=22, status="success", duration_ms=1, provider="anthropic",
    )
    usage = {"type": "assistant", "message": {
        "id": "m1", "model": "claude-sonnet-5-5",
        "usage": {"input_tokens": 111, "output_tokens": 22}}}

    async def run(spool_ok: bool, ledger_ok: bool):
        monkeypatch.setattr(ledger_spool, "spool_enabled", lambda: True)
        monkeypatch.setattr(ledger_spool, "append_call", lambda *a, **k: spool_ok)
        monkeypatch.setattr(ledger_spool, "ack", lambda *a, **k: None)
        ledger_seam.explode_on = None if ledger_ok else "ledger"
        sammler, token = teilverbrauch.beginnen()
        try:
            teilverbrauch.aus_cli_nachricht(usage)
            teilverbrauch.aus_cli_nachricht({"type": "result"})
            assert len(sammler.zuege()) == 1
            await REAL_WRITER(**kw)
        finally:
            teilverbrauch.beenden(token)
        return sammler.zuege()

    assert await run(spool_ok=True, ledger_ok=False) == []
    assert await run(spool_ok=False, ledger_ok=True) == []
    assert len(await run(spool_ok=False, ledger_ok=False)) == 1
    await REAL_WRITER(**kw)  # outside a self-call: nothing to report to
