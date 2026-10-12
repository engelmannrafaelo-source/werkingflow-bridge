"""FK25B2 R7 — a stop that began booking and then lost its endpoint.

Source: the R7 reviewer's repro
/root/projekte/local-storage/fk25br7-20261012/test_r7_luecke.py (against
69097cc), scenarios unchanged, assertions turned from the observed defect
(409 job_terminal, GET error retryable) into the target. Real Postgres, real
registry run body, real store, real DELETE and GET routes; only the
executor's end is synthetic (the 'offen' marker, then the JobFailure a broken
self-call raises).

Fix (store variant of the verdict): mark_error on a running row with the
owner's wish and ledger 'offen' closes it as cancelled/unbekannt, like the
watchdog — Table C then answers 502 job_cancel_ledger_unknown.
"""
import asyncio

import httpx
import pytest

from src.error_contract import JobFailure
from src.jobs import registry, routes, selbstaufruf, store
from tests.jobs import test_job_cancel_br10 as b
from tests.jobs import test_job_cancel_fk25b as f
from tests.jobs import test_job_cancel_fk25br4_abnahme as a

pg_store = b.pg_store
mem = f.mem


def _endpoint_dies_mid_booking(gate=None, vermerk=True):
    async def executor(payload, attribution, report_progress):
        if gate is not None:
            await gate.wait()
        if vermerk:
            # what selbstaufruf does right before booking the stop
            await selbstaufruf._buchung_offen_vermerken(
                f.JOB, "job_cancelled", {"uid-offen-1": {}})
        # endpoint worker gone -> executors._post_self -> JobFailure
        raise JobFailure("self-call /v1/chat/completions: connection failed "
                         "(RemoteProtocolError: Server disconnected)", retryable=True)
    return executor


async def _get(job_id):
    transport = httpx.ASGITransport(app=f._router_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge") as c:
        return await c.get(f"/v1/jobs/{job_id}", headers=f.OWNER_HEADERS)


def _unknown_offen(r):
    err = r.json()["error"]
    assert (r.status_code, err["code"], err["status"], err["ledger"]) == (
        502, "job_cancel_ledger_unknown", "cancelled", "unbekannt",
    ), r.text
    assert "uid-offen-1" in err["message"]
    assert "fruehere_versuche" not in err  # the one attempt is the account's own
    return err


async def _zeile_und_get():
    row = await store.get_job(f.JOB)
    assert row["status"] == "cancelled"
    abbruch = row["progress"]["abbruch"]
    assert (abbruch["ledger"], abbruch["geschlossen_von"], abbruch["offene_abzuege"]) == (
        "unbekannt", "mark_error", ["uid-offen-1"],
    )
    assert "connection failed" in row["error"]["message"]  # kept for diagnosis
    g = (await _get(f.JOB)).json()
    assert (g["status"], g["error"]) == ("cancelled", None)  # never a retryable error


@pytest.mark.asyncio
async def test_repeated_delete_after_offen_then_mark_error(pg_store, monkeypatch):
    """Reviewer saw: row 'error' (retryable), DELETE 409 job_terminal ×2.
    Now: row cancelled/unbekannt, DELETE 502 unknown ×2, GET cancelled."""
    a._real_store(monkeypatch)
    monkeypatch.setattr(registry, "get_executor", lambda kind: _endpoint_dies_mid_booking())
    await b._insert(pg_store, f.JOB, "pending", attribution=f.OWNER_ATTR)
    assert await store.mark_running(f.JOB)
    await store.cancel_job(f.JOB)  # owner's wish recorded while running
    await registry._run_body(f.JOB, "chat", {}, f.OWNER_ATTR)
    await _zeile_und_get()
    x = await f._delete(f.JOB, f.OWNER_HEADERS)
    y = await f._delete(f.JOB, f.OWNER_HEADERS)
    assert f.ohne_zeit(x) == f.ohne_zeit(y)
    _unknown_offen(x)


@pytest.mark.asyncio
async def test_stopping_delete_sees_offen_then_mark_error(pg_store, monkeypatch):
    """Reviewer saw: the stopping DELETE 409 'finished before the cancel took
    effect', repeat 409. Now both 502 unknown."""
    a._real_store(monkeypatch)
    monkeypatch.setattr(routes, "JOB_CANCEL_WAIT_S", 5.0)
    monkeypatch.setattr(routes, "JOB_CANCEL_WAIT_TICK_S", 0.05)
    gate = asyncio.Event()
    monkeypatch.setattr(registry, "get_executor", lambda kind: _endpoint_dies_mid_booking(gate))

    def wake(job_id):
        gate.set()
        return True

    monkeypatch.setattr(routes, "abbruch_melden", wake)
    await b._insert(pg_store, f.JOB, "pending", attribution=f.OWNER_ATTR)
    assert await store.mark_running(f.JOB)
    lauf = asyncio.create_task(registry._run_body(f.JOB, "chat", {}, f.OWNER_ATTR))
    await asyncio.sleep(0.1)
    x = await f._delete(f.JOB, f.OWNER_HEADERS)
    await lauf
    y = await f._delete(f.JOB, f.OWNER_HEADERS)
    await _zeile_und_get()
    assert f.ohne_zeit(x) == f.ohne_zeit(y)
    _unknown_offen(x)


@pytest.mark.asyncio
async def test_endpoint_dies_before_offen_stays_terminal_error(pg_store, monkeypatch):
    """Named limit: the endpoint dies between stop and 'offen' — no stop is
    on record; the runner's error is an ordinary error, DELETE 409."""
    a._real_store(monkeypatch)
    monkeypatch.setattr(
        registry, "get_executor", lambda kind: _endpoint_dies_mid_booking(vermerk=False)
    )
    await b._insert(pg_store, f.JOB, "pending", attribution=f.OWNER_ATTR)
    assert await store.mark_running(f.JOB)
    await store.cancel_job(f.JOB)
    await registry._run_body(f.JOB, "chat", {}, f.OWNER_ATTR)
    assert (await store.get_job(f.JOB))["status"] == "error"
    r = await f._delete(f.JOB, f.OWNER_HEADERS)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "job_terminal")


@pytest.mark.asyncio
@pytest.mark.parametrize("fall", ["ohne_wunsch", "fertiger_bericht"])
async def test_mark_error_other_rows_unchanged(pg_store, monkeypatch, fall):
    """Only 'offen' + wish + running is taken over; an error without a wish,
    or after a finished account, stays an ordinary error."""
    a._real_store(monkeypatch)
    await b._insert(pg_store, f.JOB, "running", attribution=f.OWNER_ATTR)
    if fall == "fertiger_bericht":
        await store.cancel_job(f.JOB)
        await store.update_progress(f.JOB, {"abbruch": {"ledger": "gebucht"}})
    else:
        await store.update_progress(f.JOB, {"abbruch": {"ledger": "offen"}})
    await store.mark_error(f.JOB, "boom", code="EXECUTOR_ERROR", retryable=True)
    row = await store.get_job(f.JOB)
    assert row["status"] == "error"
    assert row["progress"]["abbruch"]["ledger"] == ("gebucht" if fall == "fertiger_bericht"
                                                    else "offen")


@pytest.mark.asyncio
async def test_stop_running_sees_pending_again_is_loud(mem, monkeypatch):
    """R7 P2: _stop_running finds the row 'pending' again (unreachable with
    the BR10b store: defer_job closes a wished row as cancelled) — fail loud."""
    mem.add(f.JOB, "running")

    def zurueck(job_id):
        mem.rows[job_id]["status"] = "pending"
        return True

    monkeypatch.setattr(routes, "abbruch_melden", zurueck)
    with pytest.raises(RuntimeError, match="unexpected status 'pending'"):
        await f._delete(f.JOB, f.OWNER_HEADERS)


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger", ["unbekannt", "gebucht", None])
async def test_w1_mark_error_after_watchdog_keeps_cancelled(pg_store, monkeypatch, caplog, ledger):
    """FK25BR R8 P2-a, the reviewer's W1 (test_r8_wettlauf.py,
    test_w1_watchdog_zuerst_dann_mark_error; it printed 'error' + 409): the
    watchdog closed the row while the runner still lived; the runner's late
    mark_error must not turn 'cancelled'/'unbekannt' into a retryable error.
    Also for a row closed by a stop ('gebucht') or a pending cancel (no account)."""
    a._real_store(monkeypatch)
    if ledger == "unbekannt":
        await b._insert(pg_store, f.JOB, "running", attribution=f.OWNER_ATTR)
        await store.cancel_job(f.JOB)
        await selbstaufruf._buchung_offen_vermerken(f.JOB, "job_cancelled", {"uid-offen-1": {}})
        await a._altern(pg_store, f.JOB)
        await store.claim_stale_job(1, 5)
    elif ledger == "gebucht":
        await b._insert(pg_store, f.JOB, "running", attribution=f.OWNER_ATTR)
        await store.cancel_job(f.JOB)
        await registry._als_abgebrochen_schliessen(f.JOB, "chat", {"ledger": "gebucht"})
    else:
        await b._insert(pg_store, f.JOB, "pending", attribution=f.OWNER_ATTR)
        await store.cancel_job(f.JOB)
    vorher = await store.get_job(f.JOB)
    assert vorher["status"] == "cancelled"
    await store.mark_error(f.JOB, "late runner error", code="EXECUTOR_ERROR", retryable=True)
    nachher = await store.get_job(f.JOB)
    assert (nachher["status"], nachher["progress"], nachher["error"]) == (
        "cancelled", vorher["progress"], vorher["error"],
    )
    assert any(r.levelname == "ERROR" and "mark_error" in r.message and "ignored" in r.message
               for r in caplog.records)
    r = await f._delete(f.JOB, f.OWNER_HEADERS)
    if ledger == "unbekannt":
        assert (r.status_code, r.json()["error"]["code"]) == (502, "job_cancel_ledger_unknown")
    else:
        assert r.status_code == 200
    g = (await _get(f.JOB)).json()
    assert (g["status"], g["error"]) == ("cancelled", None)


@pytest.mark.asyncio
async def test_mark_error_on_vanished_row_still_raises(pg_store, monkeypatch):
    """The 'cancelled' carve-out does not hide a row that is really gone."""
    a._real_store(monkeypatch)
    with pytest.raises(store.JobRowMissing):
        await store.mark_error("job_dev_" + "f" * 32, "boom", code="EXECUTOR_ERROR")
