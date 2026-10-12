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

A run that completes (result message seen) books its own usage as before; its
model calls are dropped from the collector then, so a research job whose
earlier rounds were booked is not booked twice.

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

    def lauf_gebucht(self) -> None:
        """A CLI run ended with its result: it books these calls itself."""
        self._zuege.clear()

    def zuege(self) -> List[Dict[str, Any]]:
        return list(self._zuege.values())


_aktuell: "contextvars.ContextVar[Optional[Teilverbrauch]]" = contextvars.ContextVar(
    "bridge_job_teilverbrauch", default=None
)


def beginnen() -> "tuple[Teilverbrauch, contextvars.Token]":
    sammler = Teilverbrauch()
    return sammler, _aktuell.set(sammler)


def beenden(token: "contextvars.Token") -> None:
    _aktuell.reset(token)


def aus_cli_nachricht(data: Dict[str, Any]) -> None:
    """Called by the SDK parser for every CLI message; a no-op outside a
    supervised job self-call."""
    sammler = _aktuell.get()
    if sammler is None:
        return
    typ = data.get("type")
    if typ == "result":
        sammler.lauf_gebucht()
        return
    if typ != "assistant":
        return
    message = data.get("message")
    if isinstance(message, dict) and isinstance(message.get("usage"), dict):
        sammler.zug(message.get("id"), message.get("model"), message["usage"])
