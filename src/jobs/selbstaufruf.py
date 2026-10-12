"""The endpoint side of a job's self-call (FK25B).

A job runs its model work as a self-call to /v1/chat/completions, /v1/research
or /v1/doc-agent (src/jobs/executors.py), marked with JOB_LAUF_HEADER. When
its owner cancels the job, the runner cancels the executor and the self-call's
connection closes (src/jobs/registry.py). Without supervision the endpoint did
not notice and ran the CLI to the end — paid for, result discarded, capacity
held (12.10.2026). Here the endpoint runs a marked call under the same
supervision as a relayed chat call (chat_weiterleitung._beaufsichtigt): the
caller's connection gone → the run is cancelled, and the CLI with its
children ends through the bounded close (claude_cli, BRLZR R2).

The ledger notes the stop: one row, status 'error', error_code
``job_cancelled`` (the owner cancelled the job) or ``job_selbstaufruf_getrennt``
(the runner hung up for another reason: self-call timeout, worker shutdown).
Tokens are 0 because they are not known: the CLI reports usage only when a
run ends, and this one did not end. Rounds of the same run that completed
earlier (research) are booked by their own rows as before. Provider: what the
endpoint had routed to when it stopped (request.state.ledger_provider), or
``unrouted`` when it stopped before routing — then nothing was sent anywhere.

The header is not a trust boundary: a caller who sets it gets their own call
supervised (it ends when they hang up) and, if the value names a job, that
job's cancel state read for their own ledger row's error code. Nothing else.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Dict, Optional, TypeVar

from fastapi.responses import JSONResponse

from src.jobs.registry import JOB_LAUF_HEADER

logger = logging.getLogger(__name__)

T = TypeVar("T")

ERROR_CODE_JOB_CANCELLED = "job_cancelled"
ERROR_CODE_SELBSTAUFRUF_GETRENNT = "job_selbstaufruf_getrennt"
# Not a status anyone reads (the runner has hung up), only what the access
# log shows: nginx's "client closed request".
STATUS_CALLER_GONE = 499


def job_lauf(request: Any) -> Optional[str]:
    """Job id of a marked self-call, else None."""
    wert = request.headers.get(JOB_LAUF_HEADER)
    return wert or None


def ledger_provider_merken(request: Any, provider: str) -> None:
    """Called by the endpoints once they know where the call goes."""
    request.state.ledger_provider = provider


async def unter_job_aufsicht(
    request: Any,
    arbeit: Awaitable[T],
    *,
    attribution: Dict[str, Any],
    model: Optional[str],
    endpunkt: str,
):
    """Run ``arbeit``; if this is a job self-call, cancel it as soon as the
    runner hangs up, note it in the ledger and answer 499."""
    job_id = job_lauf(request)
    if job_id is None:
        return await arbeit
    from src import chat_weiterleitung as cw

    beginn = time.time()
    try:
        return await cw._beaufsichtigt(arbeit, bis=None, wer=f"job self-call {job_id}")
    except cw.AufruferWeg:
        await _im_ledger_vermerken(
            request, job_id, attribution, model, endpunkt, time.time() - beginn
        )
        return JSONResponse(
            status_code=STATUS_CALLER_GONE,
            content={"error": {"message": "job self-call: runner gone, run stopped"}},
        )


async def _abbruch_gewuenscht(job_id: str) -> Optional[bool]:
    from src.jobs import store_client
    from src.jobs.job_id import JobIdMalformed, parse_home

    try:
        parse_home(job_id)
    except JobIdMalformed:
        return None
    try:
        state = await store_client.cancel_state(job_id)
    except Exception as e:  # noqa: BLE001 — only decides the error code
        logger.warning(f"job self-call {job_id}: cancel state unreadable: {e}")
        return None
    return bool(state and state.get("cancel_requested"))


async def _im_ledger_vermerken(
    request: Any,
    job_id: str,
    attribution: Dict[str, Any],
    model: Optional[str],
    endpunkt: str,
    dauer_s: float,
) -> None:
    from src.activity.ai_call_writer import persist_ai_call_activity
    from src.activity.providers import PROVIDER_UNROUTED

    gewuenscht = await _abbruch_gewuenscht(job_id)
    if gewuenscht:
        code = ERROR_CODE_JOB_CANCELLED
        warum = "Job von seinem Eigentümer abgebrochen"
    else:
        code = ERROR_CODE_SELBSTAUFRUF_GETRENNT
        warum = "Job-Lauf hat den Selbstaufruf getrennt (kein Abbruchwunsch gespeichert)"
    provider = getattr(request.state, "ledger_provider", None) or PROVIDER_UNROUTED
    logger.warning(
        f"🛑 job self-call {endpunkt} for {job_id} stopped after {dauer_s:.1f}s "
        f"({code}, provider={provider})"
    )
    await persist_ai_call_activity(
        provider=provider,
        provider_meta={"job_id": job_id, "endpunkt": endpunkt, "usage_bekannt": False},
        app_id=attribution.get("app_id"),
        user_id=attribution.get("user_id"),
        agent_id=attribution.get("agent_id"),
        workflow_id=attribution.get("workflow_id"),
        model=model or "unknown",
        input_tokens=0,
        output_tokens=0,
        status="error",
        duration_ms=int(dauer_s * 1000),
        error_code=code,
        error_message=(
            f"{warum}; Lauf nach {dauer_s:.0f}s beendet. Verbrauch bis zum "
            f"Abbruch nicht gemessen (die CLI meldet ihn erst am Laufende)."
        ),
        app_env=attribution.get("app_env"),
    )
