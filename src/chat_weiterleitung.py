"""Chat call relayed to another worker: one deadline, no orphan, honest 429
(CHATFWD, 12.10.2026).

Befund (koordination/BRREF-ergebnis.md, Kapazitaetsdiagnose 17:31-17:35Z):
Energy's methodology prompt (~164k tokens, Sonnet) hit worker2 and worker4,
whose accounts were at the weekly limit / out of token budget. Both relayed it
to worker1 (``main._cross_worker_retry``) with a fixed 30 s httpx timeout.
worker1 needed 64-119 s, so worker2/worker4 gave up after 30 s and answered
429 — while worker1 computed the answer to the end and paid for it. Those
orphans held worker1's token budget, so Energy's NEXT attempt queued on
worker1 for 5 s and got 429 too, and every retry made a new orphan: Energy
competed with itself. The relay target was chosen with a 500-token free-budget
check against a 164k prompt, and the client-facing 429 said "All Anthropic
accounts at weekly limit" although worker1's account was not.

Fixed here, one place for the relay rules:

* ONE deadline per chat call (``frist_bis``): set when the call reaches the
  first worker (``CHAT_GESAMTFRIST_S``), carried on every relay hop in
  ``FRIST_HEADER`` — a peer never starts a fresh clock behind a waiter that
  started earlier (same pattern as research_laufzeit.FRIST_HEADER).
* The relaying worker waits for the peer until that deadline plus a short
  grace (``relay_timeout_s``), never a fixed 30 s that is shorter than the run.
* The peer accepts a relayed call only while the remaining time is at least
  ``CHAT_RELAY_MIN_REST_S`` (``pruefe_annahme``), and runs it under
  ``unter_aufsicht``: when the deadline passes or the caller's connection is
  gone, the run is cancelled — the CLI dies with it (claude_cli bounded close
  kills the CLI's process group on cancellation, BRLZR R2).
* The relaying worker gives up its relay the moment ITS caller is gone
  (``relay_unter_aufsicht``) — closing the connection is what lets the peer see
  the caller leave.
* A relay target must carry the prompt (``pool_lage``): free budget
  (effective cap − in flight) at least the request's token estimate.
* When no worker can take the call, the 429 names the real reason from the
  pool state and a retry_after that matches it (``ehrliche_lage``).

Time chain (inner < outer; checked by ``pruefe_staffelung`` at import):

    CLI run on the peer       ≤ remaining deadline − CHAT_RELAY_MARGIN_S
    deadline                  CHAT_GESAMTFRIST_S                3300 s
  < relaying worker's wait    deadline + CHAT_RELAY_GRACE_S
  < nginx read timeout, chat/research location                  3450 s
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable, Dict, Iterable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Absolute deadline (unix seconds) of one chat call, forwarded on every relay hop.
FRIST_HEADER = "x-bridge-chat-frist-bis"

CHAT_GESAMTFRIST_S = float(os.getenv("CHAT_GESAMTFRIST_S", "3300"))
# The relaying worker waits this long past the deadline, so the peer's own
# answer (also its deadline error) arrives before the waiter gives up.
CHAT_RELAY_GRACE_S = float(os.getenv("CHAT_RELAY_GRACE_S", "20"))
# The peer stops its run this long BEFORE the deadline: cancelling the CLI and
# answering takes time, and the answer must reach the waiter.
CHAT_RELAY_MARGIN_S = float(os.getenv("CHAT_RELAY_MARGIN_S", "10"))
# Remaining time a peer needs to accept a relayed call at all. A floor, not a
# per-prompt prediction: measured 12.10.2026 a 164k-token single-turn Sonnet
# call took 64-119 s.
CHAT_RELAY_MIN_REST_S = float(os.getenv("CHAT_RELAY_MIN_REST_S", "180"))
# Honest retry_after when an account has room but its budget is held by
# running calls: in-flight budget drains as those calls end.
CHAT_KAPAZITAET_RETRY_S = int(os.getenv("CHAT_KAPAZITAET_RETRY_S", "60"))
# How often the supervisor asks whether the caller is still there.
AUFSICHT_TAKT_S = float(os.getenv("CHAT_AUFSICHT_TAKT_S", "0.5"))
# docker/nginx.conf proxy_read_timeout of the chat/research location (static
# file, checked in tests against research_laufzeit.NGINX_RESEARCH_READ_TIMEOUT_S).
NGINX_CHAT_READ_TIMEOUT_S = 3450.0

# Reasons, stable identifiers in the error envelope.
REASON_RELAY_FRIST_ZU_KURZ = "relay_deadline_too_short"
REASON_RELAY_FRIST_ABGELAUFEN = "relay_deadline_expired"
REASON_POOL_KAPAZITAET_BELEGT = "pool_capacity_busy"
REASON_POOL_KONTEN_LIMIT = "pool_accounts_limited"
REASON_POOL_ZU_GROSS = "pool_request_exceeds_every_cap"
REASON_POOL_UNBEKANNT = "pool_state_unknown"


def pruefe_staffelung() -> None:
    fehler = []
    if not CHAT_RELAY_MARGIN_S < CHAT_RELAY_MIN_REST_S:
        fehler.append(
            f"CHAT_RELAY_MARGIN_S={CHAT_RELAY_MARGIN_S:.0f} must be < "
            f"CHAT_RELAY_MIN_REST_S={CHAT_RELAY_MIN_REST_S:.0f}"
        )
    aussen = CHAT_GESAMTFRIST_S + CHAT_RELAY_GRACE_S
    if not aussen < NGINX_CHAT_READ_TIMEOUT_S:
        fehler.append(
            f"CHAT_GESAMTFRIST_S+CHAT_RELAY_GRACE_S={aussen:.0f} must be < "
            f"nginx chat read timeout {NGINX_CHAT_READ_TIMEOUT_S:.0f}"
        )
    if fehler:
        raise ValueError("chat relay time chain out of order: " + "; ".join(fehler))


pruefe_staffelung()


class AufruferWeg(Exception):
    """The caller's connection closed while its call was running."""


class FristAbgelaufen(Exception):
    """The chat call's deadline passed while its run was still going."""


def eingehende_frist(request: Any) -> Optional[float]:
    """Deadline this call arrived with on a relay hop, else None."""
    roh = request.headers.get(FRIST_HEADER)
    if roh is None:
        return None
    try:
        return float(roh)
    except ValueError:
        # Loud: a peer sent a deadline we cannot read. Treat as expired rather
        # than inventing a fresh one — a fresh clock is exactly the orphan.
        logger.error(f"chat relay: malformed {FRIST_HEADER}={roh!r} — treated as expired")
        return 0.0


def frist_bis(request: Any) -> float:
    """Absolute deadline of this chat call: the forwarded one, else first
    worker's now + CHAT_GESAMTFRIST_S. Remembered on request.state."""
    state = request.state
    bis = getattr(state, "chat_frist_bis", None)
    if isinstance(bis, float):
        return bis
    eingang = eingehende_frist(request)
    bis = eingang if eingang is not None else time.time() + CHAT_GESAMTFRIST_S
    state.chat_frist_bis = bis
    return bis


def relay_timeout_s(bis: float) -> float:
    """How long the relaying worker waits for the peer."""
    return max(0.0, bis - time.time()) + CHAT_RELAY_GRACE_S


def rest_s(bis: float) -> float:
    return bis - time.time()


def frist_zu_kurz_error(rest: float):
    from src.middleware.bridge_error import (
        SOURCE_BRIDGE_INTERNAL,
        TYPE_QUEUE_TIMEOUT,
        bridge_error,
    )

    return bridge_error(
        source=SOURCE_BRIDGE_INTERNAL,
        error_type=TYPE_QUEUE_TIMEOUT,
        reason=REASON_RELAY_FRIST_ZU_KURZ,
        message=(
            f"Relayed chat call not accepted: {max(0.0, rest):.0f}s left of the "
            f"caller's deadline, a run needs at least {CHAT_RELAY_MIN_REST_S:.0f}s."
        ),
        status_code=503,
        retry_after_s=CHAT_KAPAZITAET_RETRY_S,
        extra={"rest_s": round(max(0.0, rest), 1), "min_rest_s": CHAT_RELAY_MIN_REST_S},
    )


def frist_abgelaufen_error(bis: float):
    from src.middleware.bridge_error import (
        SOURCE_BRIDGE_INTERNAL,
        TYPE_UPSTREAM_TIMEOUT,
        bridge_error,
    )

    return bridge_error(
        source=SOURCE_BRIDGE_INTERNAL,
        error_type=TYPE_UPSTREAM_TIMEOUT,
        reason=REASON_RELAY_FRIST_ABGELAUFEN,
        message="Relayed chat call stopped at the caller's deadline; the run was cancelled.",
        status_code=504,
        retry_after_s=CHAT_KAPAZITAET_RETRY_S,
        extra={"frist_bis": bis},
    )


async def _beaufsichtigt(
    arbeit: Awaitable[T], *, bis: Optional[float], wer: str
) -> T:
    """Run ``arbeit``; cancel it when the caller is gone or ``bis`` passes."""
    from src.activity.delivery import caller_gone

    task = asyncio.ensure_future(arbeit)
    try:
        while True:
            warte = AUFSICHT_TAKT_S
            if bis is not None:
                warte = max(0.0, min(warte, bis - time.time()))
            done, _ = await asyncio.wait({task}, timeout=warte)
            if done:
                return task.result()
            grund: Optional[Exception] = None
            if await caller_gone():
                grund = AufruferWeg(f"{wer}: caller gone")
            elif bis is not None and time.time() >= bis:
                grund = FristAbgelaufen(f"{wer}: deadline passed")
            if grund is not None:
                logger.warning(f"🛑 {grund} — cancelling the run")
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:  # noqa: BLE001 — the run's own end, logged
                    logger.info(f"{wer}: run ended with {exc!r} while being cancelled")
                raise grund
    finally:
        if not task.done():
            # Our own caller (the handler) was cancelled: take the run with us.
            task.cancel()


async def unter_aufsicht(arbeit: Awaitable[T], bis: float) -> T:
    """Peer side: a relayed chat call runs until its deadline (minus the
    margin) at most, and not a moment after its caller left."""
    return await _beaufsichtigt(arbeit, bis=bis - CHAT_RELAY_MARGIN_S, wer="relayed chat")


async def relay_unter_aufsicht(arbeit: Awaitable[T]) -> T:
    """Relaying side: abandon the relay (closing the connection to the peer)
    as soon as this worker's own caller is gone."""
    return await _beaufsichtigt(arbeit, bis=None, wer="chat relay")


def pool_lage(
    accounts: Dict[str, Dict[str, Any]],
    *,
    benoetigt_tokens: int,
    ausgeschlossen: Iterable[str],
) -> Dict[str, Any]:
    """Which worker can carry a call of ``benoetigt_tokens`` — and if none,
    why, with a retry_after that matches the pool.

    A target must be available, out of cooldown, and have free budget
    (effective cap − in flight) of at least the request's estimate. Workers in
    ``ausgeschlossen`` (this one, earlier hops) are reported but not chosen.
    """
    ausgeschlossen = set(ausgeschlossen)
    benoetigt = max(int(benoetigt_tokens or 0), 1)
    worker_lage: list[Dict[str, Any]] = []
    ziele: list[tuple[str, int]] = []
    for konto, info in accounts.items():
        worker = info.get("worker")
        if not worker:
            continue
        cap = int(info.get("effective_cap_tokens") or 0)
        frei = cap - int(info.get("current_in_flight_tokens") or 0)
        cooldown = int(info.get("cooldown_remaining_s") or 0)
        verfuegbar = bool(info.get("available")) and cooldown <= 0
        if not verfuegbar:
            zustand = "konto_limit"
        elif cap < benoetigt:
            zustand = "zu_gross"
        elif frei < benoetigt:
            zustand = "kapazitaet_belegt"
        else:
            zustand = "frei"
        worker_lage.append({
            "worker": worker,
            "zustand": zustand,
            "frei_tokens": max(frei, 0),
            "cap_tokens": cap,
            "cooldown_s": cooldown,
            "weekly_percent": info.get("weekly_percent"),
            "ausgeschlossen": worker in ausgeschlossen,
        })
        if zustand == "frei" and worker not in ausgeschlossen:
            ziele.append((worker, frei))

    ziel = max(ziele, key=lambda x: x[1])[0] if ziele else None
    zustaende = {w["zustand"] for w in worker_lage}
    if not worker_lage:
        grund, retry = REASON_POOL_UNBEKANNT, CHAT_KAPAZITAET_RETRY_S
    elif "frei" in zustaende or "kapazitaet_belegt" in zustaende:
        # Some account could take it as soon as running calls end (or right
        # now, on a worker this call already tried).
        grund, retry = REASON_POOL_KAPAZITAET_BELEGT, CHAT_KAPAZITAET_RETRY_S
    elif "konto_limit" in zustaende:
        limits = [w["cooldown_s"] for w in worker_lage if w["zustand"] == "konto_limit"]
        bekannte = [c for c in limits if c > 0]
        grund = REASON_POOL_KONTEN_LIMIT
        retry = min(bekannte) if bekannte else CHAT_KAPAZITAET_RETRY_S
    else:
        grund, retry = REASON_POOL_ZU_GROSS, None
    return {
        "ziel": ziel,
        "grund": grund,
        "retry_after_s": retry,
        "benoetigt_tokens": benoetigt,
        "worker": sorted(worker_lage, key=lambda w: w["worker"]),
    }


_GRUND_TEXT = {
    REASON_POOL_KAPAZITAET_BELEGT: (
        "no worker has free token budget for this call right now; "
        "budget frees as running calls end"
    ),
    REASON_POOL_KONTEN_LIMIT: "every account that could take this call is at its usage limit",
    REASON_POOL_ZU_GROSS: "the call is larger than every worker's token budget",
    REASON_POOL_UNBEKANNT: "the pool state could not be read",
}


def ehrliche_lage(request: Any, eigener_grund: str) -> Optional[Dict[str, Any]]:
    """Fields for the client-facing error when a relay was looked for and not
    possible: what this worker hit, what the pool looks like, and the
    retry_after that fits. None when no pool lookup happened."""
    lage = getattr(request.state, "chat_pool_lage", None)
    if not isinstance(lage, dict):
        return None
    return {
        "reason": lage["grund"],
        "message": f"{eigener_grund}; {_GRUND_TEXT[lage['grund']]}",
        "retry_after_s": lage["retry_after_s"],
        "pool": {
            "benoetigt_tokens": lage["benoetigt_tokens"],
            "worker": lage["worker"],
        },
    }
