"""FK25B2 R3 — Codex' FK25BR R3 counterprobes, taken over as the acceptance.

Source: /root/projekte/local-storage/fk25br3-20261012/test_r3_counterprobes.py
(Codex, 12.10.2026, against 1245e64). Scenarios unchanged; the assertions
are the corrected outcomes, the docstrings name what Codex saw. Added: a
concurrent DELETE while the first one waits, and the stop's own booking
whose deduction is lost without spool. All synthetic.
"""
import asyncio
import threading
import uuid
from types import SimpleNamespace

import pytest

from src.activity import ai_call_writer, ledger_spool
from src.jobs import registry, selbstaufruf, teilverbrauch
from tests.jobs import test_job_cancel_fk25b as f

welt = f.welt
mem = f.mem
REAL_WRITER = ai_call_writer.persist_ai_call_activity


# ---------------------------------------------------------------------------
# P1 — every DELETE on a stopped job answers from the stored account
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("spooled", [False, True])
async def test_repeat_delete_keeps_ledger_state(welt, monkeypatch, spooled):
    """Codex saw the second DELETE answer 200 without a ledger claim
    (REPEAT_LOSES_LEDGER 502→200, 202→200). Now it repeats the first answer,
    and nothing is run or booked again."""
    from src import main

    started = threading.Event()
    runs = []

    async def work(*args):
        runs.append(1)
        started.set()
        await asyncio.sleep(30)

    booked = []

    async def book(**kw):
        booked.append(kw)
        if spooled:
            ai_call_writer._buchung_gesichert()
        return "failed"

    monkeypatch.setattr(main, "_chat_completions_lauf", work)
    monkeypatch.setattr(ai_call_writer, "persist_ai_call_activity", book)
    welt.mem.add(f.JOB, "running")
    task = asyncio.create_task(registry._run_body(f.JOB, "chat", f.BODY, f.OWNER_ATTR))
    try:
        assert await f._bis(started.is_set)
        first = await f._delete(f.JOB, f.OWNER_HEADERS)
        await asyncio.wait_for(task, 3)
        second = await f._delete(f.JOB, f.OWNER_HEADERS)
        for r in (first, second):
            if spooled:
                assert (r.status_code, r.json()["status"], r.json()["ledger"]) == (
                    202, "cancelled", "rueckstau",
                ), r.text
            else:
                err = r.json()["error"]
                assert (r.status_code, err["code"], err["status"]) == (
                    502, "job_cancel_ledger_failed", "cancelled",
                ), r.text
        assert f.ohne_zeit(first) == f.ohne_zeit(second)
        assert len(runs) == 1 and len(booked) == 1
        assert welt.mem.rows[f.JOB]["progress"]["abbruch"]["ledger"] == (
            "rueckstau" if spooled else "fehlgeschlagen"
        )
    finally:
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger_ok", [True, False])
async def test_concurrent_delete_while_stopping_answers_alike(welt, monkeypatch, ledger_ok):
    """Two DELETEs at once, the stop finishing between them: both answers
    agree with the stored account; one run, one booking."""
    from src import main

    started = threading.Event()
    release = asyncio.Event()

    async def work(*args):
        started.set()
        await asyncio.sleep(30)

    booked = []

    async def book(**kw):
        booked.append(kw)
        await release.wait()
        return "written" if ledger_ok else "failed"

    monkeypatch.setattr(main, "_chat_completions_lauf", work)
    monkeypatch.setattr(ai_call_writer, "persist_ai_call_activity", book)
    welt.mem.add(f.JOB, "running")
    task = asyncio.create_task(registry._run_body(f.JOB, "chat", f.BODY, f.OWNER_ATTR))
    try:
        assert await f._bis(started.is_set)
        first = asyncio.create_task(f._delete(f.JOB, f.OWNER_HEADERS))
        assert await f._bis(lambda: bool(booked))
        second = asyncio.create_task(f._delete(f.JOB, f.OWNER_HEADERS))
        await asyncio.sleep(0.3)
        release.set()
        r1, r2 = await asyncio.gather(first, second)
        await asyncio.wait_for(task, 3)
        expected = 200 if ledger_ok else 502
        assert (r1.status_code, r2.status_code) == (expected, expected), (r1.text, r2.text)
        assert f.ohne_zeit(r1) == f.ohne_zeit(r2)
        assert len(booked) == 1
    finally:
        release.set()
        if not task.done():
            task.cancel()


@pytest.mark.asyncio
async def test_delete_on_pending_cancelled_job_stays_plain_200(mem):
    """Nothing ran: a repeated DELETE on a job cancelled while pending stays
    200 without a ledger claim."""
    mem.add(f.JOB, "pending")
    r1 = await f._delete(f.JOB, f.OWNER_HEADERS)
    r2 = await f._delete(f.JOB, f.OWNER_HEADERS)
    for r in (r1, r2):
        assert (r.status_code, r.json()) == (200, {"job_id": f.JOB, "status": "cancelled"})


# ---------------------------------------------------------------------------
# P1 — a row written before the stop is not booked again
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_written_ledger_then_cancelled_deduction_not_rebooked(monkeypatch, ledger_seam):
    """Codex' probe: spool refused, row written, the run cancelled while its
    deduction awaits. Codex saw a second 111/22 row ('undelivered') under a
    new key and a second deduction under a new call_uid
    (DOUBLE_LEDGER_AFTER_CANCEL). Now: the collector learned of the row at
    the write; the stop books no tokens again and re-sends the open deduction
    under the SAME call_uid."""
    ledger_seam.context = {"tenantId": str(uuid.uuid4()), "billingMode": "pay_per_token"}
    monkeypatch.setattr(ai_call_writer, "persist_ai_call_activity", REAL_WRITER)
    monkeypatch.setattr(ledger_spool, "spool_enabled", lambda: True)
    monkeypatch.setattr(ledger_spool, "append_call", lambda *args, **kw: False)
    entered = asyncio.Event()
    deductions = []

    async def deduction(*args, **kwargs):
        deductions.append(kwargs["call_uid"])
        if len(deductions) == 1:
            entered.set()
            await asyncio.Event().wait()
        return True

    monkeypatch.setattr(ai_call_writer, "_deduct_call_cost", deduction)
    attr = {"app_id": "werking-energy", "user_id": str(uuid.uuid4()), "app_env": "dev"}
    collector, token = teilverbrauch.beginnen()
    task = None
    try:
        teilverbrauch.aus_cli_nachricht({"type": "assistant", "message": {
            "id": "finished-one", "model": "claude-sonnet-5-5",
            "usage": {"input_tokens": 111, "output_tokens": 22}}})
        teilverbrauch.aus_cli_nachricht({"type": "result"})
        task = asyncio.create_task(REAL_WRITER(
            **attr, agent_id=None, workflow_id=None, model="claude-sonnet-5-5",
            input_tokens=111, output_tokens=22, status="success", duration_ms=10,
            provider="anthropic",
        ))
        await asyncio.wait_for(entered.wait(), 2)
        assert len(ledger_seam.ledger_calls) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert collector.zuege() == []
        assert list(collector.offene_abzuege()) == [deductions[0]]
        req = SimpleNamespace(state=SimpleNamespace(ledger_provider="anthropic"))
        report = await selbstaufruf._im_ledger_vermerken(
            req, f.JOB, attr, "claude-sonnet-5-5", "/v1/chat/completions", 10,
            "wunsch", collector.zuege(),
        )
        tokens = [(r["input_tokens"], r["output_tokens"]) for r in ledger_seam.ledger_calls]
        assert tokens == [(111, 22), (0, 0)]  # the stop's row: no tokens again
        assert ledger_seam.ledger_calls[1]["provider_metadata"]["status"] == "error"
        assert deductions == [deductions[0], deductions[0]]  # resumed, same key
        assert report["ledger"] == "gebucht"
        assert report["nachgeholte_abzuege"] == 1 and report["offene_abzuege"] == []
    finally:
        teilverbrauch.beenden(token)
        if task is not None and not task.done():
            task.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("gespoolt,erwartet", [(False, "fehlgeschlagen"), (True, "rueckstau")])
async def test_resumed_deduction_unanswered_is_not_booked(monkeypatch, gespoolt, erwartet):
    """The open deduction cannot be settled at the stop either: never
    'gebucht' — owed (spooled: its replay re-sends it) or lost."""
    async def no_answer(*args, **kwargs):
        return False

    async def book(**kw):
        return "written"

    monkeypatch.setattr(ai_call_writer, "_deduct_call_cost", no_answer)
    monkeypatch.setattr(ai_call_writer, "persist_ai_call_activity", book)
    req = SimpleNamespace(state=SimpleNamespace(ledger_provider="anthropic"))
    report = await selbstaufruf._im_ledger_vermerken(
        req, f.JOB, {"app_id": "werking-energy"}, "m", "/v1/chat/completions", 1,
        "wunsch", [],
        abzuege={"uid-1": {"gespoolt": gespoolt, "args": {
            "user_id": "u", "app_id": "werking-energy", "cost_eur": 0.1,
            "workflow_id": None, "tenant_id": None, "call_ts": 1.0}}},
    )
    assert report["ledger"] == erwartet
    assert report["offene_abzuege"] == ["uid-1"]


@pytest.mark.asyncio
async def test_stop_booking_with_lost_deduction_is_failed(monkeypatch, ledger_seam):
    """The stop's own row: written, but its deduction unanswered and not
    spooled — the writer answers 'written' and only logs the loss. The stop
    does not call that 'gebucht'."""
    ledger_seam.context = {"tenantId": str(uuid.uuid4()), "billingMode": "pay_per_token"}
    monkeypatch.setattr(ai_call_writer, "persist_ai_call_activity", REAL_WRITER)
    monkeypatch.setattr(ledger_spool, "spool_enabled", lambda: False)

    async def no_answer(*args, **kwargs):
        return False

    monkeypatch.setattr(ai_call_writer, "_deduct_call_cost", no_answer)
    req = SimpleNamespace(state=SimpleNamespace(ledger_provider="anthropic"))
    report = await selbstaufruf._im_ledger_vermerken(
        req, f.JOB, {"app_id": "werking-energy", "user_id": str(uuid.uuid4())},
        "claude-sonnet-5-5", "/v1/chat/completions", 10, "wunsch",
        [{"model": "claude-sonnet-5-5", "input_tokens": 40_000, "output_tokens": 2_000}],
        abzuege={},
    )
    assert report["ledger"] == "fehlgeschlagen"
    assert report["ledger_outcomes"] == ["written+deduction_lost"]
