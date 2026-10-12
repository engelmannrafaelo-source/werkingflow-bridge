"""FK25B2 R5 — a pending job is not proof it never ran (FK25BR R5).

Codex' probe /root/projekte/local-storage/fk25br5-20261012/test_r5_windows.py
::test_defer_delete_order (against 2d5cecb) showed: running → defer_job
(pending) → DELETE answered a plain 200, the answer the contract keeps for
jobs that never ran. Turned into the acceptance (scenario unchanged), plus
the crash-requeue chain (watchdog claims a dead run back to pending) and the
never-ran control. Real Postgres, real route, real store.
"""
import pytest

from src.jobs import store
from tests.jobs import test_job_cancel_br10 as br10
from tests.jobs import test_job_cancel_fk25b as f
from tests.jobs import test_job_cancel_fk25br4_abnahme as a

pg_store = br10.pg_store


async def _zweimal(job_id, status=200):
    first = await f._delete(job_id, f.OWNER_HEADERS)
    second = await f._delete(job_id, f.OWNER_HEADERS)
    assert first.status_code == second.status_code == status, first.text
    assert first.json() == second.json()
    body = first.json()
    return body["error"] if status == 502 else body


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["delete_first", "defer_first"])
async def test_defer_delete_order(pg_store, monkeypatch, order):
    """Codex' scenario. defer_first: Codex saw a plain 200 without ledger.
    Now 200 with ledger 'fruehere_versuche' — one parked attempt that booked
    itself. delete_first unchanged ('vom_lauf')."""
    a._real_store(monkeypatch)
    await br10._insert(pg_store, f.JOB, "running", attribution=f.OWNER_ATTR)
    if order == "delete_first":
        await store.cancel_job(f.JOB)
    await store.defer_job(f.JOB, 30, "synthetic capacity wait")
    body = await _zweimal(f.JOB)
    job = await store.get_job(f.JOB)
    assert job["defer_count"] == 1 and job["status"] == "cancelled"
    if order == "delete_first":
        assert body["ledger"] == "vom_lauf" and "fruehere_versuche" not in body
    else:
        assert body["ledger"] == "fruehere_versuche"
        v = body["fruehere_versuche"]
        assert (v["anzahl"], v["geparkt"], v["abgebrochen_ohne_bericht"], v["buchung"]) == (
            1, 1, 0, "vom_lauf",
        )
        assert v["call_uids"] is None and "not recorded" in v["hinweis"]


@pytest.mark.asyncio
async def test_crash_requeue_then_delete_names_unknown_attempt(pg_store, monkeypatch):
    """The crash chain: a run started (attempts 1), its worker died, the
    watchdog requeued it to pending (claim bumps attempts, a failed second
    start leaves it pending again — set by test SQL, a state simulation, as
    Codex R6 notes), then DELETE. Not a plain 200: the dead attempts are
    named, their booking 'unbekannt' — since R6 as 502
    job_cancel_ledger_unknown."""
    a._real_store(monkeypatch)
    await br10._insert(pg_store, f.JOB, "pending", attribution=f.OWNER_ATTR)
    assert await store.mark_running(f.JOB)  # attempt 1 starts
    await a._altern(pg_store, f.JOB)        # its worker dies
    claimed = await store.claim_stale_job(60, 5)  # watchdog requeue: attempt 2
    assert claimed is not None and claimed["job_id"] == f.JOB
    async with pg_store.acquire() as c:     # attempt 2's worker dies before it ran:
        await c.execute(                    # the row is pending again (requeue state)
            "UPDATE ai_jobs SET status='pending' WHERE job_id=$1", f.JOB
        )
    body = await _zweimal(f.JOB, 502)
    assert body["code"] == "job_cancel_ledger_unknown"
    assert body["ledger"] == "fruehere_versuche"
    v = body["fruehere_versuche"]
    assert (v["anzahl"], v["geparkt"], v["abgebrochen_ohne_bericht"], v["buchung"]) == (
        2, 0, 2, "unbekannt",
    )
    assert (await store.get_job(f.JOB))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_crash_then_park_mixes_both(pg_store, monkeypatch):
    """One crashed attempt, then one that parked: 'unbekannt' wins (502 since
    R6), both counted."""
    a._real_store(monkeypatch)
    await br10._insert(pg_store, f.JOB, "pending", attribution=f.OWNER_ATTR)
    assert await store.mark_running(f.JOB)
    await a._altern(pg_store, f.JOB)
    assert await store.claim_stale_job(60, 5) is not None
    await store.defer_job(f.JOB, 30, "synthetic capacity wait")
    body = await _zweimal(f.JOB, 502)
    v = body["fruehere_versuche"]
    assert (v["anzahl"], v["geparkt"], v["abgebrochen_ohne_bericht"], v["buchung"]) == (
        2, 1, 1, "unbekannt",
    )


@pytest.mark.asyncio
async def test_never_ran_stays_plain_200(pg_store, monkeypatch):
    """No trace of any attempt: plain 200, also on repeat."""
    a._real_store(monkeypatch)
    await br10._insert(pg_store, f.JOB, "pending", attribution=f.OWNER_ATTR)
    assert await _zweimal(f.JOB) == {"job_id": f.JOB, "status": "cancelled"}
