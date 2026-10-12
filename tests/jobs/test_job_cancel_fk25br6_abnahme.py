"""FK25B2 R6 — the DELETE answer for a cancelled job, every branch.

Part 1 is Codex' FK25BR R6 counterprobes
(/root/projekte/local-storage/fk25br6-20261012/test_r6_consistency.py,
against a6d4a16), scenarios unchanged, assertions turned from the observed
defect into the target. Real Postgres, real store, real route.

Part 2 is the full matrix of routes._cancelled_answer: current account ×
earlier-attempt trace → HTTP status / code / ledger, for the DELETE that
cancels a pending job and for a repeated DELETE on a cancelled one; every
cell asserted twice (repeat identical). The table is in FK25B-ergebnis.md (R6).
"""
import logging

import pytest

from src.jobs import registry, store, store_client
from tests.jobs import test_job_cancel_br10 as b
from tests.jobs import test_job_cancel_fk25b as f
from tests.jobs import test_job_cancel_fk25br4_abnahme as a

pg_store = b.pg_store
mem = f.mem


async def twice():
    x = await f._delete(f.JOB, f.OWNER_HEADERS)
    y = await f._delete(f.JOB, f.OWNER_HEADERS)
    assert x.status_code == y.status_code and f.ohne_zeit(x) == f.ohne_zeit(y)
    return x


def _unknown(r, ledger=None):
    err = r.json()["error"]
    assert (r.status_code, err["code"], err["status"]) == (
        502, "job_cancel_ledger_unknown", "cancelled",
    ), r.text
    if ledger is not None:
        assert err["ledger"] == ledger
    return err


async def crashed_then_claimed(pg_store):
    await b._insert(pg_store, f.JOB, "pending", attribution=f.OWNER_ATTR)
    assert await store.mark_running(f.JOB)
    await a._altern(pg_store, f.JOB)
    claimed = await store.claim_stale_job(60, 5)
    assert claimed["attempts"] == 2 and claimed["status"] == "running"


# ---------------------------------------------------------------------------
# Part 1 — Codex' nine probes as targets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("wish_before_defer", [False, True])
async def test_crash_then_park_unknown_answers_502(
    pg_store, monkeypatch, wish_before_defer, caplog
):
    """Codex saw 200 (fruehere_versuche / vom_lauf), the second without an
    ERROR. Now 502 job_cancel_ledger_unknown both ways, ERROR both ways. With
    the wish before the park, the 'vom_lauf' account covers attempt 2; only
    the crashed attempt 1 is history."""
    a._real_store(monkeypatch)
    await crashed_then_claimed(pg_store)
    if wish_before_defer:
        await store.cancel_job(f.JOB)
    await store.defer_job(f.JOB, 30, "synthetic second attempt parks")
    with caplog.at_level(logging.WARNING):
        r = await twice()
    err = _unknown(r, ledger="vom_lauf" if wish_before_defer else "fruehere_versuche")
    v = err["fruehere_versuche"]
    erwartet = (1, 0, 1) if wish_before_defer else (2, 1, 1)
    assert (v["anzahl"], v["geparkt"], v["abgebrochen_ohne_bericht"]) == erwartet
    assert v["buchung"] == "unbekannt" and v["call_uids"] is None and v["hinweis"]
    assert any(x.levelname == "ERROR" and "earlier attempts" in x.message for x in caplog.records)


@pytest.mark.asyncio
async def test_same_unknown_without_requeue_answers_502(pg_store, monkeypatch):
    """Unchanged (Codex: already right): watchdog closes the running attempt."""
    a._real_store(monkeypatch)
    await b._insert(pg_store, f.JOB, "pending", attribution=f.OWNER_ATTR)
    await store.mark_running(f.JOB)
    await store.cancel_job(f.JOB)
    await a._altern(pg_store, f.JOB)
    await store.claim_stale_job(60, 5)
    err = _unknown(await twice(), ledger="unbekannt")
    assert "fruehere_versuche" not in err  # the one attempt is the account's own


@pytest.mark.asyncio
@pytest.mark.parametrize("counter", [None, "unreadable"])
@pytest.mark.parametrize("report", [None, "vom_lauf"])
async def test_unreadable_counter_responses(pg_store, monkeypatch, counter, report):
    """Codex saw 200 (no report: without call_uids; vom_lauf: no hint at all).
    Now 502 unknown, history 'unbekannt' with call_uids null and the hint."""
    a._real_store(monkeypatch)
    await b._insert(pg_store, f.JOB, "pending", attribution=f.OWNER_ATTR)
    if report:
        await store.update_progress(f.JOB, {"abbruch": {"ledger": report}})

    async def unreadable(job_id):
        job = await store.get_job(job_id)
        job["attempts"] = counter
        return job

    monkeypatch.setattr(store_client, "get_job", unreadable)
    err = _unknown(await twice(), ledger=report or "fruehere_versuche")
    v = err["fruehere_versuche"]
    assert v["anzahl"] is None and v["buchung"] == "unbekannt"
    assert v["call_uids"] is None and "not readable" in v["hinweis"]


@pytest.mark.asyncio
async def test_later_booked_stop_keeps_previous_crash(pg_store, monkeypatch):
    """Codex saw 200 {ledger: gebucht}; attempt 1's crash vanished. Now 502
    unknown, ledger gebucht (attempt 2's stop) plus attempt 1 as history."""
    a._real_store(monkeypatch)
    await crashed_then_claimed(pg_store)
    await store.cancel_job(f.JOB)
    await registry._als_abgebrochen_schliessen(f.JOB, "chat", {
        "ledger": "gebucht", "ledger_outcomes": ["written"], "offene_abzuege": [],
        "grund": "job_cancelled", "job_id": f.JOB})
    job = await store.get_job(f.JOB)
    assert job["attempts"] == 2 and job["defer_count"] == 0
    err = _unknown(await twice(), ledger="gebucht")
    v = err["fruehere_versuche"]
    assert (v["anzahl"], v["geparkt"], v["abgebrochen_ohne_bericht"], v["buchung"]) == (
        1, 0, 1, "unbekannt",
    )


@pytest.mark.asyncio
async def test_single_booked_stop_stays_200(pg_store, monkeypatch):
    """Point 3: one attempt, stopped and booked — the account covers it; 200."""
    a._real_store(monkeypatch)
    await b._insert(pg_store, f.JOB, "pending", attribution=f.OWNER_ATTR)
    assert await store.mark_running(f.JOB)
    await store.cancel_job(f.JOB)
    await registry._als_abgebrochen_schliessen(f.JOB, "chat", {
        "ledger": "gebucht", "ledger_outcomes": ["written"], "offene_abzuege": [],
        "grund": "job_cancelled", "job_id": f.JOB})
    r = await twice()
    assert (r.status_code, r.json()) == (
        200, {"job_id": f.JOB, "status": "cancelled", "ledger": "gebucht"},
    )


@pytest.mark.asyncio
async def test_never_claimed(pg_store, monkeypatch):
    a._real_store(monkeypatch)
    await b._insert(pg_store, f.JOB, "pending", attribution=f.OWNER_ATTR)
    r = await twice()
    assert r.status_code == 200 and r.json() == {"job_id": f.JOB, "status": "cancelled"}


# ---------------------------------------------------------------------------
# Part 2 — the matrix
# ---------------------------------------------------------------------------

BERICHTE = [None, "gebucht", "vom_lauf", "rueckstau", "fehlgeschlagen", "unbekannt",
            "offen", "kaputt"]
SPUREN = ["keine", "nur_geparkt", "ungeklaert", "unlesbar"]

# report → (status, code) with no earlier attempts
BASIS = {
    None: (200, None), "gebucht": (200, None), "vom_lauf": (200, None),
    "rueckstau": (202, None), "fehlgeschlagen": (502, "job_cancel_ledger_failed"),
    "unbekannt": (502, "job_cancel_ledger_unknown"),
    "offen": (502, "job_cancel_ledger_unknown"),
    "kaputt": (502, "job_cancel_ledger_unknown"),
}


def _erwartet(bericht, spur):
    status, code = BASIS[bericht]
    if spur in ("ungeklaert", "unlesbar") and status < 300:
        status, code = 502, "job_cancel_ledger_unknown"
    return status, code


def _zaehler(bericht, spur):
    gedeckt = 0 if bericht is None else 1
    gedeckt_park = 1 if bericht == "vom_lauf" else 0
    if spur == "keine":
        return gedeckt, gedeckt_park
    if spur == "nur_geparkt":
        return gedeckt + 1, gedeckt_park + 1
    if spur == "ungeklaert":
        return gedeckt + 1, gedeckt_park
    return "?", gedeckt_park


# A pending row carries no stop account: the 'pending' way (the DELETE that
# itself cancels) only exists for bericht None.
FAELLE = [("wiederholt", b_, s) for b_ in BERICHTE for s in SPUREN] + [
    ("pending", None, s) for s in SPUREN
]


@pytest.mark.asyncio
@pytest.mark.parametrize("weg,bericht,spur", FAELLE)
async def test_matrix(mem, weg, bericht, spur):
    attempts, defer_count = _zaehler(bericht, spur)
    progress = None
    if bericht == "kaputt":
        progress = {"abbruch": "not-a-dict"}
    elif bericht is not None:
        progress = {"abbruch": {"ledger": bericht, "ledger_outcomes": ["x"],
                                "offene_abzuege": ["uid-1"]}}
    mem.add(f.JOB, "pending" if weg == "pending" else "cancelled",
            progress=progress, attempts=attempts, defer_count=defer_count)
    r = await twice()
    status, code = _erwartet(bericht, spur)
    assert r.status_code == status, (bericht, spur, r.text)
    body = r.json()
    if code is not None:
        teil = body["error"]
        assert (teil["code"], teil["status"]) == (code, "cancelled")
    else:
        teil = body
        assert teil["status"] == "cancelled"
    erwartetes_ledger = (
        "unlesbar" if bericht == "kaputt" else bericht
    ) if bericht is not None else ("fruehere_versuche" if spur != "keine" else None)
    assert teil.get("ledger") == erwartetes_ledger, (bericht, spur, teil)
    v = teil.get("fruehere_versuche")
    if spur == "keine":
        assert v is None
        if bericht is None:
            assert body == {"job_id": f.JOB, "status": "cancelled"}
    else:
        assert v is not None and v["call_uids"] is None and v["hinweis"]
        assert v["buchung"] == ("vom_lauf" if spur == "nur_geparkt" else "unbekannt")
        if spur != "unlesbar":
            assert (v["anzahl"], v["abgebrochen_ohne_bericht"]) == (
                1, 0 if spur == "nur_geparkt" else 1,
            )


# The third way into the same answer: the stopping DELETE itself
# (_stop_running sees the row turn 'cancelled' and answers from it). The stop
# is simulated by the local wake-up: it closes the row with the account, as
# registry._als_abgebrochen_schliessen / the watchdog / defer_job would.
FAELLE_STOPP = [(b_, s) for b_ in BERICHTE if b_ is not None for s in SPUREN]


@pytest.mark.asyncio
@pytest.mark.parametrize("bericht,spur", FAELLE_STOPP)
async def test_matrix_im_stopp(mem, monkeypatch, bericht, spur):
    from src.jobs import routes

    attempts, defer_count = _zaehler(bericht, spur)
    mem.add(f.JOB, "running", attempts=attempts, defer_count=defer_count)
    progress = (
        {"abbruch": "not-a-dict"} if bericht == "kaputt"
        else {"abbruch": {"ledger": bericht, "ledger_outcomes": ["x"]}}
    )

    def stopp(job_id):
        mem.rows[job_id].update(status="cancelled", progress=progress)
        return True

    monkeypatch.setattr(routes, "abbruch_melden", stopp)
    r = await twice()  # first: the stopping DELETE; second: the repeat
    status, code = _erwartet(bericht, spur)
    assert r.status_code == status, (bericht, spur, r.text)
    teil = r.json()["error"] if code else r.json()
    if code:
        assert teil["code"] == code
    assert teil["status"] == "cancelled"
    assert teil.get("ledger") == ("unlesbar" if bericht == "kaputt" else bericht)
    assert ("fruehere_versuche" in teil) == (spur != "keine")


# ---------------------------------------------------------------------------
# Part 3 — the DELETE route's other branches, each once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_off_is_503(mem, monkeypatch):
    from fastapi import HTTPException

    from src.jobs import routes

    def aus():
        raise HTTPException(status_code=503, detail="Generic async jobs disabled")

    monkeypatch.setattr(routes, "_require_enabled", aus)
    mem.add(f.JOB, "pending")
    r = await f._delete(f.JOB, f.OWNER_HEADERS)
    assert r.status_code == 503 and mem.rows[f.JOB]["status"] == "pending"


@pytest.mark.asyncio
async def test_home_unconfigured_is_503(mem, monkeypatch):
    monkeypatch.delenv("BRIDGE_ORIGIN_ID", raising=False)
    mem.add(f.JOB, "pending")
    r = await f._delete(f.JOB, f.OWNER_HEADERS)
    assert r.status_code == 503 and mem.rows[f.JOB]["status"] == "pending"


@pytest.mark.asyncio
async def test_row_vanishes_while_stopping_is_404(mem, monkeypatch):
    from src.jobs import routes

    mem.add(f.JOB, "running")

    def weg(job_id):
        del mem.rows[job_id]
        return True

    monkeypatch.setattr(routes, "abbruch_melden", weg)
    r = await f._delete(f.JOB, f.OWNER_HEADERS)
    assert (r.status_code, r.json()["error"]["code"]) == (404, "job_not_found")


@pytest.mark.asyncio
async def test_unknown_store_status_is_loud(mem):
    mem.add(f.JOB, "rätselhaft")
    with pytest.raises(RuntimeError, match="unknown status"):
        await f._delete(f.JOB, f.OWNER_HEADERS)
