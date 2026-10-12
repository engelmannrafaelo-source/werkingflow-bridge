"""What a stopped job self-call has already used (FK25B2, Befund 5).

The CLI books a run's usage only when the run ends (its result message). A
run stopped by a job cancel never gets there, so FK25B wrote 0 tokens and
status 'error' — "costs nothing" in the ledger vocabulary, while the model
calls that had already completed were paid for.

Each completed model call does report its usage on the way, though: every
assistant message carries the usage of its whole model call
(sdk_parser._retain_cli_end_fields, ``message.usage``/``message.id``; the
same data claude_cli._usage_je_zug sums for a finished run). This module
collects those reports for the duration of one supervised job self-call, so
the cancel's ledger row can book what was measured.

What stays unknown, and is said so in the row: the model call in flight when
the run was stopped (its usage arrives only with its answer), and anything a
non-CLI provider (Bedrock, research cloud) used on a request that was cut.

A run that completes (result message seen) books its own usage as before.
Its model calls stay in the collector until that booking is durably taken
over by the ledger writer — the write-ahead spool has the record, or the
writer settled it definitively (``buchung_gesichert``, called by
ai_call_writer). Only then are they dropped, so a research job whose earlier
rounds were booked is not booked twice, and a run stopped between its result
and its own booking still has its measured calls booked by the stop
(FK25BR R2, P2: they used to be dropped at the result message already).

The writer reports to whichever booking is listening in the current
context: the run's own (the collector), or a stop's own booking
(``eigene_buchung``), which needs to know whether its rows are durable.

Ledger row and budget deduction are two steps (FK25BR R3): the writer
reports the row the moment it is written (``zeile_geschrieben``) — before
its next await, the deduction, where a job cancel can land. The collector
then drops the calls (no second row under a new identity), but keeps the
deduction as OPEN (``abzug_beginnt``) until the writer reports it settled
(``abzug_endet``). A stop finds it open and re-sends it under the call's
own key, which platform-api applies at most once (migration 061) — so an
interrupted deduction is neither lost nor counted as done.

Request-scoped through a ContextVar: the supervised task and every task it
creates (the SDK's reader) see the same collector; nothing else does.
"""
from __future__ import annotations

import contextvars
from typing import Any, Dict, List, Optional

_USAGE_FELDER = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_read_input_tokens", "cache_read_tokens"),
    ("cache_creation_input_tokens", "cache_creation_tokens"),
)


class Teilverbrauch:
    """Usage of the completed, not yet booked model calls of one self-call."""

    def __init__(self) -> None:
        # message id → {model, input_tokens, ...}; a model call reports its
        # usage once per content block, the last report wins.
        self._zuege: Dict[str, Dict[str, Any]] = {}
        # Calls of runs that ended with their result: booked by that run,
        # once the writer has them durably (buchung_gesichert).
        self._abgeschlossen: Dict[str, Dict[str, Any]] = {}
        # call_uid → {"args": deduction arguments, "gespoolt": bool}: rows
        # written whose budget deduction has not been settled.
        self._offene_abzuege: Dict[str, Dict[str, Any]] = {}
        self._anonym = 0

    def zug(self, message_id: Optional[str], model: Optional[str], usage: Dict[str, Any]) -> None:
        if not message_id:
            self._anonym += 1
            message_id = f"anon-{self._anonym}"
        eintrag: Dict[str, Any] = {"model": model}
        for quelle, ziel in _USAGE_FELDER:
            try:
                eintrag[ziel] = int(usage.get(quelle) or 0)
            except (TypeError, ValueError):
                eintrag[ziel] = 0
        self._zuege[message_id] = eintrag

    def lauf_beendet(self) -> None:
        """A CLI run ended with its result: it will book these calls itself —
        kept until the writer has that booking."""
        self._abgeschlossen.update(self._zuege)
        self._zuege.clear()

    def lauf_gebucht(self) -> None:
        """The writer has the finished runs' booking durably."""
        self._abgeschlossen.clear()

    def zuege(self) -> List[Dict[str, Any]]:
        """Measured calls nobody has durably booked yet."""
        return list(self._abgeschlossen.values()) + list(self._zuege.values())

    def offene_abzuege(self) -> Dict[str, Dict[str, Any]]:
        """Deductions of written rows that were not settled."""
        return dict(self._offene_abzuege)


_aktuell: "contextvars.ContextVar[Optional[Teilverbrauch]]" = contextvars.ContextVar(
    "bridge_job_teilverbrauch", default=None
)


def beginnen() -> "tuple[Teilverbrauch, contextvars.Token]":
    sammler = Teilverbrauch()
    return sammler, _aktuell.set(sammler)


def beenden(token: "contextvars.Token") -> None:
    _aktuell.reset(token)


class EigeneBuchung:
    """What the writer reported for a stop's own booking."""

    def __init__(self) -> None:
        self.gesichert = False  # the write-ahead spool has the record
        # A deduction of this booking ended unsettled — the writer's outcome
        # may still read 'written' then (spool off: the loss is only logged).
        self.abzug_offen = False


_eigene: "contextvars.ContextVar[Optional[EigeneBuchung]]" = contextvars.ContextVar(
    "bridge_job_eigene_buchung", default=None
)


def eigene_buchung() -> "tuple[EigeneBuchung, contextvars.Token]":
    buchung = EigeneBuchung()
    return buchung, _eigene.set(buchung)


def eigene_buchung_beenden(token: "contextvars.Token") -> None:
    _eigene.reset(token)


def buchung_gesichert() -> None:
    """Called by ai_call_writer once the write-ahead spool has the booking.
    A no-op outside a supervised job self-call."""
    eigene = _eigene.get()
    if eigene is not None:
        eigene.gesichert = True
        return
    sammler = _aktuell.get()
    if sammler is not None:
        sammler.lauf_gebucht()


def buchung_erledigt() -> None:
    """Called by ai_call_writer when it settled a booking definitively. For a
    run's own booking the calls are booked; a stop's own booking reads the
    writer's outcome itself (``gesichert`` means spooled, nothing else)."""
    if _eigene.get() is not None:
        return
    sammler = _aktuell.get()
    if sammler is not None:
        sammler.lauf_gebucht()


def zeile_geschrieben() -> None:
    """Called by ai_call_writer right after the usage row is written (or
    found already written), before the deduction's await. For a run's own
    booking: its measured calls are booked now."""
    if _eigene.get() is not None:
        return
    sammler = _aktuell.get()
    if sammler is not None:
        sammler.lauf_gebucht()


def abzug_beginnt(call_uid: str, args: Dict[str, Any], gespoolt: bool) -> None:
    """Called by ai_call_writer before it awaits a deduction."""
    if _eigene.get() is not None:
        return
    sammler = _aktuell.get()
    if sammler is not None:
        sammler._offene_abzuege[call_uid] = {"args": args, "gespoolt": gespoolt}


def abzug_endet(call_uid: str, settled: bool) -> None:
    """Called by ai_call_writer when the deduction answered. Not called when
    the await was cancelled — the deduction then stays open."""
    eigene = _eigene.get()
    if eigene is not None:
        if not settled:
            eigene.abzug_offen = True
        return
    sammler = _aktuell.get()
    if sammler is not None and settled:
        sammler._offene_abzuege.pop(call_uid, None)


def aus_cli_nachricht(data: Dict[str, Any]) -> None:
    """Called by the SDK parser for every CLI message; a no-op outside a
    supervised job self-call."""
    sammler = _aktuell.get()
    if sammler is None:
        return
    typ = data.get("type")
    if typ == "result":
        sammler.lauf_beendet()
        return
    if typ != "assistant":
        return
    message = data.get("message")
    if isinstance(message, dict) and isinstance(message.get("usage"), dict):
        sammler.zug(message.get("id"), message.get("model"), message["usage"])
