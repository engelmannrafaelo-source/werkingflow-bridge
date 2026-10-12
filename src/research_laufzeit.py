"""The overall deadline of one /v1/research run (BRREF-LAUFZEIT, 12.10.2026).

Befund (koordination/BRREF-LAUFZEIT-befund.md): a deep pool research is up to
five CLI calls — main run, repair round, gap checker, return round, re-check —
each with its own MAX_TIMEOUT (2400 s), while the durable job waited 2400 s
for the WHOLE call. Energy run b (job_dev_2095ec95…) took 2392 s; 8 s more and
the job would have ended as an error while the run went on and was paid for.
And execution_time_seconds reported 282 s — only the main run.

So a research run has ONE deadline (``RESEARCH_GESAMTFRIST_S``, default
3300 s, below Energy's 3600 s poll limit), set at the start:

* every CLI call gets the remaining time as its timeout (claude_cli
  run_completion ``timeout_s``) — when it fires, the CLI process is
  terminated, nothing runs on after the deadline;
* the optional rounds (gap check, return round, re-check) only start when
  the remaining time is enough for them; otherwise they are skipped, named
  in ``nachkontrolle`` — never started to overrun;
* a mandatory round (main run, repair round) that hits the deadline ends the
  run with ``LaufzeitErschoepft`` (fehler_code ``laufzeit_erschoepft``);
* ``details()`` is the run time of the whole call, split per round.

THE time chain, inner < outer, single source of truth here (P6TZ2): every
waiter must wait longer than what it waits for, or it gives up while the run
it started goes on and is paid for —

    CLI round   ≤ remaining time of the deadline (and ≤ MAX_TIMEOUT)
    deadline    RESEARCH_GESAMTFRIST_S          3300 s
  < cross-worker research retry                  3400 s  (main._cross_worker_retry)
  < nginx read timeout, research location        3450 s  (docker/nginx.conf)
  < durable job self-call                        3500 s  (jobs/executors.py)
  < Energy's job poller                          3600 s  (outside the bridge)

The nginx step (BRLZR P1): a job whose self-call meets a capacity 429 is
re-dispatched THROUGH the local LB (executors._post_with_capacity_redispatch),
so the research then runs behind nginx's proxy_read_timeout. With 2500 s there
nginx gave up on a 2700 s run, answered 503, the job was parked and could run
again while the first run still worked. That location now waits longer than
the worker can take (its deadline, or its own cross-worker retry) and shorter
than the job self-call waiting on it. nginx.conf cannot read this module, so
``NGINX_RESEARCH_READ_TIMEOUT_S`` is the number the file must carry, and
tests/research_cloud/test_research_laufzeit.py reads the file and compares.

``pruefe_staffelung`` checks the order at import — a misconfigured env fails
the worker's start, loudly, instead of cutting runs at random. A run retried
on another worker keeps the ORIGINAL deadline (header FRIST_HEADER), so the
peer does not start a fresh 3300 s behind a waiter that started earlier.
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

GESAMTFRIST_S = float(os.getenv("RESEARCH_GESAMTFRIST_S", "3300"))
# Remaining time one gap check (one turn of the cheap model) needs. Measured
# 12.10.2026: 75–150 s per check on a deep report.
PRUEFER_MIN_REST_S = float(os.getenv("RESEARCH_PRUEFER_MIN_REST_S", "240"))
# Remaining time a return round needs on top of the re-check after it.
RUECKRUNDE_MIN_REST_S = float(os.getenv("RESEARCH_RUECKRUNDE_MIN_REST_S", "600"))

CROSS_WORKER_TIMEOUT_S = float(os.getenv("RESEARCH_CROSS_WORKER_TIMEOUT_S", "3400"))
# docker/nginx.conf proxy_read_timeout of the chat/research location and of
# @production_streaming — a constant, because the file is static (test-checked).
NGINX_RESEARCH_READ_TIMEOUT_S = 3450.0
JOB_SELBSTAUFRUF_TIMEOUT_S = float(os.getenv("BRIDGE_RESEARCH_JOB_TIMEOUT_S", "3500"))
# Outside the bridge (werking-energy's job poller) — here only for the order.
ENERGY_POLLER_S = 3600.0
# The CLI's own ceiling per call (claude_cli, MAX_TIMEOUT in ms).
MAX_TIMEOUT_S = int(os.getenv("MAX_TIMEOUT", "2400000")) / 1000

# Absolute deadline (unix seconds) of a research run, forwarded on a
# cross-worker retry so the peer works to the same end.
FRIST_HEADER = "x-bridge-research-frist-bis"

LAUFZEIT_ERSCHOEPFT = "laufzeit_erschoepft"


def staffelung() -> Dict[str, float]:
    return {
        "max_timeout_s": MAX_TIMEOUT_S,
        "gesamtfrist_s": GESAMTFRIST_S,
        "cross_worker_s": CROSS_WORKER_TIMEOUT_S,
        "nginx_research_s": NGINX_RESEARCH_READ_TIMEOUT_S,
        "job_selbstaufruf_s": JOB_SELBSTAUFRUF_TIMEOUT_S,
        "energy_poller_s": ENERGY_POLLER_S,
    }


def pruefe_staffelung(werte: Optional[Dict[str, float]] = None) -> None:
    """Raise ValueError unless MAX_TIMEOUT ≤ deadline < cross-worker < nginx
    research location < job self-call < Energy poller."""
    w = werte or staffelung()
    kette = [
        "gesamtfrist_s", "cross_worker_s", "nginx_research_s",
        "job_selbstaufruf_s", "energy_poller_s",
    ]
    fehler = [
        f"{a}={w[a]:.0f} must be < {b}={w[b]:.0f}"
        for a, b in zip(kette, kette[1:]) if not w[a] < w[b]
    ]
    if not w["max_timeout_s"] <= w["gesamtfrist_s"]:
        fehler.append(
            f"max_timeout_s={w['max_timeout_s']:.0f} must be <= gesamtfrist_s={w['gesamtfrist_s']:.0f}"
        )
    if fehler:
        raise ValueError("research time chain out of order (inner < outer): " + "; ".join(fehler))


pruefe_staffelung()


def frist_bis(request: Any) -> float:
    """Absolute deadline of this research request: the one a cross-worker
    retry forwarded (never later than a fresh one), else now + deadline.
    Remembered on request.state for a later retry from this worker."""
    jetzt = time.time()
    bis = jetzt + GESAMTFRIST_S
    roh = None
    try:
        roh = request.headers.get(FRIST_HEADER) if request is not None else None
    except Exception:
        roh = None
    if isinstance(roh, str):
        try:
            bis = min(bis, float(roh))
        except ValueError:
            logger.error(f"research: malformed {FRIST_HEADER}={roh!r} — using a fresh deadline")
    state = getattr(request, "state", None) if request is not None else None
    if state is not None and not isinstance(getattr(state, "research_frist_bis", None), float):
        try:
            state.research_frist_bis = bis
        except Exception:
            pass
    return bis


class LaufzeitErschoepft(RuntimeError):
    """The research run reached its overall deadline in a mandatory round.
    Not retryable: the same request is likely to need the same time again;
    a smaller one may not."""

    fehler_code = LAUFZEIT_ERSCHOEPFT
    retryable = False


class Laufzeit:
    """Deadline and per-round run time of one research run."""

    def __init__(self, frist_s: Optional[float] = None, *, uhr: Callable[[], float] = time.monotonic):
        self.frist_s = GESAMTFRIST_S if frist_s is None else float(frist_s)
        self._uhr = uhr
        self._start = uhr()
        self.runden: Dict[str, float] = {}
        self._laufend: Dict[str, float] = {}

    @classmethod
    def fuer_anfrage(cls, request: Any, **kw: Any) -> "Laufzeit":
        """Deadline of a /v1/research request (frist_bis), as remaining time."""
        return cls(max(0.0, frist_bis(request) - time.time()), **kw)

    def beginne(self, runde: str) -> None:
        self._laufend[runde] = self._uhr()

    def beende(self, runde: str) -> None:
        start = self._laufend.pop(runde, None)
        if start is not None:
            self.runden[runde] = round(self.runden.get(runde, 0.0) + (self._uhr() - start), 2)

    def beende_alle(self) -> None:
        for runde in list(self._laufend):
            self.beende(runde)

    def vergangen(self) -> float:
        return self._uhr() - self._start

    def rest(self) -> float:
        return self.frist_s - self.vergangen()

    def reicht(self, sekunden: float) -> bool:
        return self.rest() >= sekunden

    def frist_fuer(self, runde: str, *, reserve_s: float = 0.0) -> float:
        """Timeout for the next CLI call of ``runde``: the remaining time,
        less ``reserve_s`` kept for what must still follow. Raises
        LaufzeitErschoepft when nothing is left."""
        verfuegbar = self.rest() - reserve_s
        if verfuegbar <= 0:
            raise LaufzeitErschoepft(
                f"research run reached its overall deadline of {self.frist_s:.0f} s before "
                f"{runde} (elapsed {self.vergangen():.0f} s)"
            )
        return verfuegbar

    @contextmanager
    def messen(self, runde: str) -> Iterator[None]:
        self.beginne(runde)
        try:
            yield
        finally:
            self.beende(runde)

    def details(self) -> Dict[str, object]:
        return {
            "gesamt_s": round(self.vergangen(), 2),
            "frist_s": self.frist_s,
            "runden": dict(self.runden),
        }


def als_laufzeitfehler(exc: BaseException, laufzeit: Optional[Laufzeit]) -> BaseException:
    """A CLI timeout (asyncio.TimeoutError = TimeoutError) that ended a
    mandatory round becomes LaufzeitErschoepft, the timeout as its cause.
    Everything else stays as it is."""
    if isinstance(exc, LaufzeitErschoepft) or not isinstance(exc, TimeoutError):
        return exc
    wo = (
        f"after {laufzeit.vergangen():.0f} s of its overall deadline of {laufzeit.frist_s:.0f} s"
        if laufzeit is not None else "at the CLI's own MAX_TIMEOUT"
    )
    fehler = LaufzeitErschoepft(
        f"research run stopped {wo}: a mandatory round did not finish in time — the agent "
        "was terminated, no partial report is handed out"
    )
    fehler.__cause__ = exc
    return fehler
