"""The endpoint side of a job's self-call (FK25B, FK25B2).

A job runs its model work as a self-call to /v1/chat/completions, /v1/research
or /v1/doc-agent (src/jobs/executors.py), marked with JOB_LAUF_HEADER. When
its owner cancels the job, THIS side stops the run — not the runner — because
only here is it known whether the run had already finished:

  * the supervised call watches for the job's cancel wish: a direct wake-up
    when the DELETE reached this worker (abbruch_melden), otherwise the store
    every JOB_CANCEL_POLL_S;
  * on the wish it cancels the run and WAITS for it to end — the CLI with its
    children through the bounded close (claude_cli, BRLZR R2) — and books the
    stop in the ledger;
  * only then it answers: 409 with ABBRUCH_HEADER = "bestaetigt" and the
    account of the stop, including what the ledger writer answered
    (``ledger``: gebucht / rueckstau / fehlgeschlagen). The runner stores
    that account on the row and closes it as 'cancelled' (registry). DELETE
    answers 200 only for 'gebucht' — process gone AND booked (FK25BR Befund
    2, R2 P1); a booking that is merely owed in the spool or lost is said
    so (routes._stop_running);
  * if the run returns a result while being stopped (it finished in the same
    instant), that result is the answer — the runner stores it as 'done'
    and the DELETE gets 409 job_terminal. A finished, paid result is never
    thrown away (FK25BR Befund 3).

Before FK25B2 the runner cancelled its own HTTP request and closed the row at
once; the endpoint only learned of it from the closed socket, its CLI was
still alive and its ledger row unwritten when DELETE said 200, and a result
completed in that instant was lost.

When the runner itself goes away (worker shutdown, self-call timeout) the
closed connection still stops the run (error code job_selbstaufruf_getrennt);
there is nobody left to hand a result to.

The ledger row of a stop books what was measured (src/jobs/teilverbrauch.py):
the completed model calls since the last booked run, at their real price, as
status 'undelivered' (paid, nothing delivered). Not measurable, and named so
in provider_meta: the model call in flight at the stop, and a non-CLI
provider's cut request. With nothing measured the row stays 'error', 0
tokens, and says whether a provider had been reached at all (FK25BR Befund 5).

The header is not a trust boundary: a caller who sets it gets their own call
supervised (it ends when they hang up, or when the job it names is
cancelled — which only that job's owner can do) and booked under their own
attribution. Nothing else.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Dict, List, Optional, Set, TypeVar

from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Marks a job's self-call; carries the job id (executors._build_headers).
JOB_LAUF_HEADER = "X-Bridge-Job-Lauf"
# The endpoint's confirmation that it stopped a job's run (value BESTAETIGT).
ABBRUCH_HEADER = "X-Bridge-Job-Abgebrochen"
BESTAETIGT = "bestaetigt"
STATUS_ABGEBROCHEN = 409
# The runner hung up: nobody reads this, only the access log does (nginx's
# "client closed request").
STATUS_CALLER_GONE = 499

ERROR_CODE_JOB_CANCELLED = "job_cancelled"
ERROR_CODE_SELBSTAUFRUF_GETRENNT = "job_selbstaufruf_getrennt"

# How often a supervised self-call asks the store for its job's cancel wish.
JOB_CANCEL_POLL_S = float(os.getenv("BRIDGE_JOB_CANCEL_POLL_S", "2"))

_GRUND_WUNSCH = "wunsch"
_GRUND_GETRENNT = "getrennt"

# job id → events of the supervised self-calls of that job on THIS worker.
_LAUFENDE: Dict[str, Set[asyncio.Event]] = {}


class JobLaufAbgebrochen(Exception):
    """The endpoint confirmed it stopped the job's run (executors raise it
    for a response carrying ABBRUCH_HEADER)."""

    def __init__(self, bericht: Dict[str, Any]):
        super().__init__(f"job run stopped by the endpoint: {bericht}")
        self.bericht = bericht


def job_lauf(request: Any) -> Optional[str]:
    """Job id of a marked self-call, else None."""
    wert = request.headers.get(JOB_LAUF_HEADER)
    return wert or None


def abbruch_melden(job_id: str) -> bool:
    """Wake the supervised self-calls of ``job_id`` on THIS worker. True when
    there was one."""
    events = _LAUFENDE.get(job_id)
    if not events:
        return False
    for event in events:
        event.set()
    return True


def ledger_provider_merken(request: Any, provider: str) -> None:
    """Called by the endpoints once they know where the call goes."""
    request.state.ledger_provider = provider


async def _wunsch_im_store(job_id: str) -> bool:
    """Raises store_client.CancelRouteMissing behind an old platform-api."""
    from src.jobs import store_client

    state = await store_client.cancel_state(job_id)
    return bool(state and state.get("cancel_requested"))


async def unter_job_aufsicht(
    request: Any,
    arbeit: Awaitable[T],
    *,
    attribution: Dict[str, Any],
    model: Optional[str],
    endpunkt: str,
):
    """Run ``arbeit``; if this is a job self-call, stop it on the job's cancel
    wish or when the runner hangs up (see the module docstring)."""
    job_id = job_lauf(request)
    if job_id is None:
        return await arbeit
    from src import chat_weiterleitung as cw
    from src.activity.delivery import caller_gone
    from src.jobs import store_client, teilverbrauch

    sammler, token = teilverbrauch.beginnen()
    try:
        task = asyncio.ensure_future(arbeit)  # copies the collector into its context
    finally:
        teilverbrauch.beenden(token)
    event = asyncio.Event()
    _LAUFENDE.setdefault(job_id, set()).add(event)
    beginn = time.time()
    naechste_abfrage = time.monotonic() + JOB_CANCEL_POLL_S
    store_abfragbar = True
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=cw.AUFSICHT_TAKT_S)
            if done:
                return task.result()
            grund: Optional[str] = None
            if event.is_set():
                grund = _GRUND_WUNSCH
            elif await caller_gone():
                grund = _GRUND_GETRENNT
            elif store_abfragbar and time.monotonic() >= naechste_abfrage:
                naechste_abfrage = time.monotonic() + JOB_CANCEL_POLL_S
                try:
                    if await _wunsch_im_store(job_id):
                        grund = _GRUND_WUNSCH
                except store_client.CancelRouteMissing as e:
                    # The DELETE route answers this loudly (502
                    # job_store_incompatible); here only a wake-up on this
                    # worker can still stop the run.
                    logger.error(
                        f"🛑 job self-call {endpunkt} for {job_id}: cannot watch for a "
                        f"cancel — {e}"
                    )
                    store_abfragbar = False
                except Exception as e:  # noqa: BLE001 — a failed poll must not end the run
                    logger.warning(f"⚠️ job self-call {job_id}: cancel poll failed: {e}")
            if grund is None:
                continue
            stopp = time.monotonic()
            logger.warning(
                f"🛑 job self-call {endpunkt} for {job_id}: "
                f"{'cancelled by its owner' if grund == _GRUND_WUNSCH else 'runner gone'}"
                f" — stopping the run"
            )
            task.cancel()
            try:
                result = await task  # includes the CLI's bounded close
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001 — the run's own end, while being stopped
                logger.info(f"job self-call {job_id}: run ended with {e!r} while being stopped")
            else:
                # Finished in the same instant: the work is done and paid for
                # (booked by the run itself) — it is the answer.
                logger.warning(
                    f"job self-call {job_id}: run finished before the stop took "
                    f"effect — answering its result"
                )
                return result
            bericht = await _im_ledger_vermerken(
                request, job_id, attribution, model, endpunkt,
                time.time() - beginn, grund, sammler.zuege(),
            )
            bericht["stopp_s"] = round(time.monotonic() - stopp, 2)
            if grund == _GRUND_GETRENNT:
                return JSONResponse(
                    status_code=STATUS_CALLER_GONE,
                    content={"error": {"message": "job self-call: runner gone, run stopped"}},
                )
            return JSONResponse(
                status_code=STATUS_ABGEBROCHEN,
                headers={ABBRUCH_HEADER: BESTAETIGT},
                content={
                    "error": {
                        "message": f"job {job_id}: run stopped on its owner's cancel",
                        "reason": ERROR_CODE_JOB_CANCELLED,
                        "retryable": False,
                    },
                    "job_abbruch": bericht,
                },
            )
    finally:
        if not task.done():
            # Our own handler was cancelled: take the run with us.
            task.cancel()
        events = _LAUFENDE.get(job_id)
        if events is not None:
            events.discard(event)
            if not events:
                del _LAUFENDE[job_id]


# What the writer's answers mean for the stop's booking (ledger_spool outcomes).
LEDGER_GEBUCHT = "gebucht"            # every row settled (written/duplicate/skipped)
LEDGER_RUECKSTAU = "rueckstau"        # a row is owed, but on disk: the flusher books it
LEDGER_FEHLGESCHLAGEN = "fehlgeschlagen"  # a row is neither booked nor spooled: lost


async def _buchen(**felder: Any) -> "tuple[str, str]":
    """One row through the writer; returns (writer outcome, ledger state).

    The writer never raises for an Exception and answers with a ledger_spool
    outcome; 'failed' (or 'deduction_owed') is part of its contract, so its
    return is the booking's result — not the fact that it returned."""
    from src.activity import ai_call_writer, ledger_spool
    from src.jobs import teilverbrauch

    buchung, token = teilverbrauch.eigene_buchung()
    try:
        outcome = await ai_call_writer.persist_ai_call_activity(**felder)
    finally:
        teilverbrauch.eigene_buchung_beenden(token)
    if ledger_spool.is_definitive(outcome):
        return outcome, LEDGER_GEBUCHT
    if buchung.gesichert:
        return outcome, LEDGER_RUECKSTAU
    return outcome, LEDGER_FEHLGESCHLAGEN


_LEDGER_RANG = (LEDGER_GEBUCHT, LEDGER_RUECKSTAU, LEDGER_FEHLGESCHLAGEN)


def _summe_je_modell(zuege: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    summe: Dict[str, Dict[str, int]] = {}
    for zug in zuege:
        modell = zug.get("model") or "unknown"
        s = summe.setdefault(modell, {
            "zuege": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
        })
        s["zuege"] += 1
        for feld in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
            s[feld] += int(zug.get(feld) or 0)
    return summe


async def _im_ledger_vermerken(
    request: Any,
    job_id: str,
    attribution: Dict[str, Any],
    model: Optional[str],
    endpunkt: str,
    dauer_s: float,
    grund: str,
    zuege: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Book the stop; returns the account the confirmation carries —
    ``ledger`` is the worst state of its rows, ``ledger_outcomes`` what the
    writer answered for each."""
    from src.activity.delivery import STATUS_UNDELIVERED
    from src.activity.providers import PROVIDER_UNROUTED

    if grund == _GRUND_WUNSCH:
        code = ERROR_CODE_JOB_CANCELLED
        warum = "Job von seinem Eigentümer abgebrochen"
    else:
        code = ERROR_CODE_SELBSTAUFRUF_GETRENNT
        warum = "Job-Lauf hat den Selbstaufruf getrennt (kein Abbruch durch den Eigentümer)"
    provider = getattr(request.state, "ledger_provider", None) or PROVIDER_UNROUTED
    geroutet = provider != PROVIDER_UNROUTED
    summe = _summe_je_modell(zuege)
    if summe:
        verbrauch = "teilweise_gemessen"
    elif geroutet:
        verbrauch = "unbekannt"
    else:
        verbrauch = "keiner"
    meta = {
        "job_id": job_id,
        "endpunkt": endpunkt,
        "verbrauch": verbrauch,
        "gemessene_zuege": sum(s["zuege"] for s in summe.values()),
        # Its usage arrives only with its answer; a non-CLI provider's cut
        # request is not measured at all.
        "offener_zug_unbekannt": geroutet,
    }
    luecke = (
        "Verbrauch des beim Abbruch laufenden Modellaufrufs nicht messbar."
        if geroutet else "Kein Anbieter erreicht."
    )
    logger.warning(
        f"🛑 job self-call {endpunkt} for {job_id} stopped after {dauer_s:.1f}s "
        f"({code}, provider={provider}, verbrauch={verbrauch}, "
        f"zuege={meta['gemessene_zuege']})"
    )
    gemeinsam = dict(
        provider=provider,
        app_id=attribution.get("app_id"),
        user_id=attribution.get("user_id"),
        agent_id=attribution.get("agent_id"),
        workflow_id=attribution.get("workflow_id"),
        duration_ms=int(dauer_s * 1000),
        error_code=code,
        app_env=attribution.get("app_env"),
    )
    ergebnisse: List["tuple[str, str]"] = []
    if not summe:
        ergebnisse.append(await _buchen(
            **gemeinsam,
            provider_meta=meta,
            model=model or "unknown",
            input_tokens=0,
            output_tokens=0,
            status="error",
            error_message=f"{warum}; Lauf nach {dauer_s:.0f}s beendet. {luecke}",
        ))
    for modell, s in summe.items():
        # 'undelivered': the model calls ran and are paid for, their answer
        # reached nobody — the ledger prices and deducts them like any call.
        ergebnisse.append(await _buchen(
            **gemeinsam,
            provider_meta={**meta, "zuege_dieses_modells": s["zuege"]},
            model=modell,
            input_tokens=s["input_tokens"],
            output_tokens=s["output_tokens"],
            cache_read_tokens=s["cache_read_tokens"],
            cache_creation_tokens=s["cache_creation_tokens"],
            status=STATUS_UNDELIVERED,
            error_message=(
                f"{warum}; Lauf nach {dauer_s:.0f}s beendet. Gebucht: {s['zuege']} "
                f"abgeschlossene Modellaufrufe. {luecke}"
            ),
        ))
    ledger = max((z for _, z in ergebnisse), key=_LEDGER_RANG.index)
    outcomes = [o for o, _ in ergebnisse]
    if ledger == LEDGER_FEHLGESCHLAGEN:
        logger.error(
            f"🛑 job self-call {endpunkt} for {job_id}: run stopped, but its ledger "
            f"booking FAILED and is not spooled — this usage is not metered "
            f"(writer: {outcomes}, provider={provider}, verbrauch={verbrauch})"
        )
    elif ledger == LEDGER_RUECKSTAU:
        logger.error(
            f"🛑 job self-call {endpunkt} for {job_id}: run stopped, ledger booking "
            f"not settled — owed in the ledger spool, the flusher books it "
            f"(writer: {outcomes})"
        )
    return {
        "job_id": job_id,
        "grund": code,
        "endpunkt": endpunkt,
        "provider": provider,
        "dauer_s": round(dauer_s, 2),
        "verbrauch": verbrauch,
        "gemessene_zuege": meta["gemessene_zuege"],
        "offener_zug_unbekannt": geroutet,
        "ledger": ledger,
        "ledger_outcomes": outcomes,
    }
