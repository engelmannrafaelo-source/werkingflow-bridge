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
import math
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
REASON_RELAY_FRIST_UNGUELTIG = "relay_deadline_invalid"
REASON_RELAY_FRIST_ABGELAUFEN = "relay_deadline_expired"
REASON_POOL_KAPAZITAET_BELEGT = "pool_capacity_busy"
REASON_POOL_KONTEN_LIMIT = "pool_accounts_limited"
REASON_POOL_ZU_GROSS = "pool_request_exceeds_every_cap"
REASON_POOL_UNBEKANNT = "pool_state_unknown"
# The relay reached the peer, then its answer was lost: the peer may be running
# the call. Answered 424 (never retried by nginx), main._cross_worker_retry.
REASON_RELAY_AUSGANG_UNBEKANNT = "relay_outcome_unknown"
# Capacity rejection (throttle/queue_timeout) before anything ran, with a peer
# that has room: no relay, the 429 carries this marker and the LB moves the
# call (main.bridge_error_handler, 20.10.2026). Response header of the same name.
LB_FAILOVER_HEADER = "x-bridge-failover"
LB_FAILOVER_WERT = "lb-no-relay"


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


class UngueltigeFrist(ValueError):
    """The deadline header is not a finite number."""


def eingehende_frist(request: Any) -> Optional[float]:
    """Deadline this call arrived with on a relay hop, else None.

    Trust boundary (CHATFWDR P1): only a worker sets this header, on its own
    direct worker-to-worker relay; the LB clears any inbound copy
    (docker/nginx.conf more_clear_input_headers), like X-Pool-Exhausted. What
    reaches a worker is therefore a peer's — and even so it can only SHORTEN
    the call's deadline (frist_bis caps it at this worker's own). inf, nan
    and anything unreadable are refused (UngueltigeFrist → 400), never
    turned into a deadline.
    """
    roh = request.headers.get(FRIST_HEADER)
    if roh is None:
        return None
    try:
        wert = float(roh)
    except ValueError:
        raise UngueltigeFrist(f"{FRIST_HEADER}={roh[:40]!r} is not a number") from None
    if not math.isfinite(wert):
        raise UngueltigeFrist(f"{FRIST_HEADER}={roh[:40]!r} is not finite")
    return wert


def frist_bis(request: Any) -> float:
    """Absolute deadline of this chat call: this worker's own (arrival +
    CHAT_GESAMTFRIST_S), shortened to a forwarded one if that is earlier —
    never extended by it. Remembered on request.state. Raises UngueltigeFrist."""
    state = request.state
    bis = getattr(state, "chat_frist_bis", None)
    if isinstance(bis, float):
        return bis
    eigene = time.time() + CHAT_GESAMTFRIST_S
    eingang = eingehende_frist(request)
    bis = eigene if eingang is None else min(eingang, eigene)
    state.chat_frist_bis = bis
    return bis


def frist_ungueltig_error(exc: UngueltigeFrist):
    from src.middleware.bridge_error import (
        SOURCE_BRIDGE_INTERNAL,
        TYPE_INTERNAL,
        bridge_error,
    )

    return bridge_error(
        source=SOURCE_BRIDGE_INTERNAL,
        error_type=TYPE_INTERNAL,
        reason=REASON_RELAY_FRIST_UNGUELTIG,
        message=f"Invalid relay deadline: {exc}.",
        status_code=400,
        retryable_override=False,
    )


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


def _zahl(info: Dict[str, Any], feld: str) -> float:
    try:
        wert = float(info.get(feld) or 0)
    except (TypeError, ValueError):
        return 0.0
    return wert if math.isfinite(wert) else 0.0


def _worker_zustand(info: Dict[str, Any], benoetigt: int) -> tuple[str, Optional[int], str]:
    """(zustand, retry_after_s, retry_quelle) of one worker for a call of
    ``benoetigt`` tokens, from the budget and limit FIELDS of the pool state —
    not from ``available``, which is false for a full in-flight budget
    (headroom 0) just as for an account limit, nor from the aggregated
    ``cooldown_remaining_s`` alone, which also carries adaptive and temporary
    holds (CHATFWDR P2).

    * zu_gross          — the call exceeds what the worker admits even without
                          the weekly squeeze: effective_cap_tokens is
                          cap × safety margin × budget_multiplier
                          (AdaptiveLoadLimiter._effective_cap), so the
                          unsqueezed admit limit is effective / multiplier. NOT
                          adaptive_cap_tokens — that is the raw cap BEFORE the
                          safety margin, which no account reset frees
                          (CHATFWDR R2).
    * konto_limit       — capacity lock (Anthropic reset_at) or a measured
                          session ≥ 95 % / weekly ≥ 100 %, or the weekly squeeze
                          (effective cap below the call, unsqueezed cap above).
                          retry = the lock / session reset when known.
    * gedrosselt        — a temporary hold (cooldown) without an account limit.
    * kapazitaet_belegt — the budget is held by running calls.
    * frei              — takes the call now.
    """
    cap = int(_zahl(info, "effective_cap_tokens"))
    mult = _zahl(info, "budget_multiplier") if info.get("budget_multiplier") is not None else 1.0
    # multiplier 0 (weekly ≥ 97 %): the squeeze hides the unsqueezed limit;
    # that worker is limited by its account, whatever the call's size.
    voller_cap = int(cap / mult) if mult > 0 else None
    frei = cap - int(_zahl(info, "current_in_flight_tokens"))
    cooldown = int(_zahl(info, "cooldown_remaining_s"))
    lock = int(_zahl(info, "capacity_lock_remaining_s"))
    gemessen = bool(info.get("usage_known"))
    session = info.get("session_percent")
    weekly = info.get("weekly_percent")
    session_voll = gemessen and isinstance(session, (int, float)) and session >= 95.0
    weekly_voll = gemessen and isinstance(weekly, (int, float)) and weekly >= 100.0

    if voller_cap is not None and voller_cap < benoetigt:
        return "zu_gross", None, "keine"
    if lock > 0:
        return "konto_limit", lock, "kontosperre"
    if session_voll:
        reset = int(_zahl(info, "session_reset_in_s"))
        if reset > 0:
            return "konto_limit", reset, "session_reset"
        return "konto_limit", None, "unbekannt"
    if weekly_voll or cap < benoetigt:
        return "konto_limit", None, "unbekannt"
    if cooldown > 0:
        return "gedrosselt", cooldown, "cooldown"
    if frei < benoetigt:
        return "kapazitaet_belegt", CHAT_KAPAZITAET_RETRY_S, "schaetzung"
    return "frei", CHAT_KAPAZITAET_RETRY_S, "schaetzung"


def pool_lage(
    accounts: Dict[str, Dict[str, Any]],
    *,
    benoetigt_tokens: int,
    ausgeschlossen: Iterable[str],
) -> Dict[str, Any]:
    """Which worker can carry a call of ``benoetigt_tokens`` — and if none,
    why, with a retry_after that matches the pool.

    Workers in ``ausgeschlossen`` (this one, earlier hops) are reported but
    not chosen. Only workers whose cap can hold the call decide the reason and
    the retry: a too-small worker's early reset does not help this call.
    Account keys are not reported — only worker ids.
    """
    ausgeschlossen = set(ausgeschlossen)
    benoetigt = max(int(benoetigt_tokens or 0), 1)
    worker_lage: list[Dict[str, Any]] = []
    ziele: list[tuple[str, int]] = []
    for info in accounts.values():
        worker = info.get("worker")
        if not worker:
            continue
        zustand, retry, quelle = _worker_zustand(info, benoetigt)
        frei = int(_zahl(info, "effective_cap_tokens") - _zahl(info, "current_in_flight_tokens"))
        worker_lage.append({
            "worker": worker,
            "zustand": zustand,
            "frei_tokens": max(frei, 0),
            "cap_tokens": int(_zahl(info, "effective_cap_tokens")),
            "retry_after_s": retry,
            "retry_quelle": quelle,
            "weekly_percent": info.get("weekly_percent"),
            "ausgeschlossen": worker in ausgeschlossen,
        })
        if zustand == "frei" and worker not in ausgeschlossen:
            ziele.append((worker, frei))

    ziel = max(ziele, key=lambda x: x[1])[0] if ziele else None

    def frueheste(zustaende: set) -> Optional[int]:
        werte = [
            w["retry_after_s"] for w in worker_lage
            if w["zustand"] in zustaende and w["retry_after_s"] is not None
        ]
        return min(werte) if werte else None

    vorhanden = {w["zustand"] for w in worker_lage}
    if not worker_lage:
        grund, retry = REASON_POOL_UNBEKANNT, CHAT_KAPAZITAET_RETRY_S
    elif vorhanden & {"frei", "kapazitaet_belegt", "gedrosselt"}:
        # Some account with room under its limit: it takes the call once
        # running calls end or its temporary hold lapses.
        grund = REASON_POOL_KAPAZITAET_BELEGT
        retry = frueheste({"frei", "kapazitaet_belegt", "gedrosselt"})
    elif "konto_limit" in vorhanden:
        grund = REASON_POOL_KONTEN_LIMIT
        # Earliest KNOWN reset among the limited workers that could carry the
        # call. None known (weekly limit without a lock): no invented number —
        # the capacity estimate, marked as such in pool.worker[].retry_quelle.
        retry = frueheste({"konto_limit"})
        if retry is None:
            retry = CHAT_KAPAZITAET_RETRY_S
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
