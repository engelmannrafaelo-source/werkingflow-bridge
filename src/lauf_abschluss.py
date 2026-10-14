"""How a model run ended — and whether its result may be handed out (BRREF).

Befund P6CUT, dev/worker1, 12.10.2026 09:58Z (CLI 2.1.285): Anthropic's safety
classifier stopped an answer — stop_reason "refusal", stop_details {type:
refusal, category: reasoning_extraction}. The CLI then appended a meta user
turn ("…stopped by a safety classifier…") and ran a SECOND model call that
answered with a short apology; the result reported success. The chat route
returned the cut first message with finish_reason "stop". Codex BRREFR R1/R2
then found the same stop slipping through every other output path that
derived the end on its own: tool-leak retry, research repair round, return
round, research-cloud, doc-agent, the research download.

So there is one place for it, and every output path goes past it:

* ``LaufAbschluss`` — how one model run ended: the OpenAI ``finish_reason``,
  the raw stop reason, a transport break (stream ended without the CLI's
  result) and an unfinished CLI result. Built from CLI chunks
  (``claude_cli.lauf_abschluss``) or from a Messages API stop_reason
  (``abschluss_aus_stop_reason``, research-cloud).
* ``annehmen(abschluss)`` — THE acceptance point. Returns the abschluss when
  its result may be handed out; otherwise raises ``TransportAbbruch``,
  ``ModellAbbruch`` or ``LaufUnfertig``. Each path maps the exception to its
  own protocol (chat: 503 / finish_reason content_filter; research and
  doc-agent: status=error; a return round: keep the report from before).
* ``vermerken`` / ``vermerkt`` — the verdict travels with a stored result: a
  research session directory carries it in ``ABSCHLUSS_DATEI``, and the
  download serves a report only under a vermerk that says it may.

Rule for the finish reason:
  refusal (stop_reason or stop_details.type)  → "content_filter"
  max_tokens / CLI output-limit continuation  → "length"
  end_turn / stop_sequence / none reported    → "stop" (CLI path)
  anything else                               → passed on unchanged
After a refusal the answer ends with the refused model call (all its content
blocks, same message_id); the CLI's follow-up call is never part of it.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

NACH_MODELLABBRUCH_KEY = "nach_modellabbruch"
MODELL_ABBRUCH_STOP_REASONS = ("refusal",)
# claude_cli.ANTWORT_ENDE_FORTGESETZT — repeated here so this module stays
# importable without the CLI stack (research-cloud uses it too).
ANTWORT_ENDE_FORTGESETZT = "max_tokens_fortgesetzt"

STOP_REASON_FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    ANTWORT_ENDE_FORTGESETZT: "length",
    "model_context_window_exceeded": "length",
    "refusal": "content_filter",
}
VOLLSTAENDIGE_FINISH_REASONS = ("stop", "tool_calls")

# The vermerk in a research session directory (next to claudedocs/).
ABSCHLUSS_DATEI = "bridge-laufabschluss.json"


def _is_result_chunk(chunk: Any) -> bool:
    """claude_cli.is_result_chunk (kept identical; this module must not import
    the CLI stack)."""
    if not isinstance(chunk, dict):
        return False
    if chunk.get("type") == "result":
        return True
    return "type" not in chunk and "is_error" in chunk and "num_turns" in chunk


def _is_assistant_chunk(chunk: Any) -> bool:
    """claude_cli._is_assistant_chunk (kept identical)."""
    return (
        isinstance(chunk, dict)
        and not _is_result_chunk(chunk)
        and chunk.get("type") in (None, "assistant")
        and isinstance(chunk.get("content"), list)
    )


def ist_modell_abbruch(chunk: dict) -> bool:
    details = chunk.get("stop_details")
    return chunk.get("stop_reason") in MODELL_ABBRUCH_STOP_REASONS or (
        isinstance(details, dict) and details.get("type") in MODELL_ABBRUCH_STOP_REASONS
    )


class AbbruchFilter:
    """Chunk by chunk: does this chunk belong to the answer?

    Non-assistant chunks (system, result, markers) always pass — they are no
    content. Before a model stop every assistant chunk passes. The refused
    call is recognised by its own stop_reason / stop_details; its further
    content blocks (same message_id) still pass. After it, every assistant
    chunk of another call is the CLI's follow-up and is held back. Second net:
    a chunk run_completion marked NACH_MODELLABBRUCH_KEY (the CLI's meta turn
    came, the stop fields got lost) — the stop is then recorded there."""

    def __init__(self) -> None:
        self.abbruch: Optional[dict] = None
        self._gesperrte_id: Optional[str] = None
        self._folge_ids: set = set()

    def nimmt(self, chunk: Any) -> bool:
        if not _is_assistant_chunk(chunk):
            return True
        msg_id = chunk.get("message_id")
        if self.abbruch is not None:
            if msg_id is not None and msg_id == self._gesperrte_id:
                return True
            self._folge_ids.add(msg_id or f"anon-{len(self._folge_ids)}")
            self.abbruch["folgezuege"] = len(self._folge_ids)
            return False
        if ist_modell_abbruch(chunk):
            details = chunk.get("stop_details") if isinstance(chunk.get("stop_details"), dict) else {}
            self.abbruch = {
                "stop_reason": chunk.get("stop_reason") or details.get("type") or "refusal",
                "category": details.get("category"),
                "folgezuege": 0,
            }
            self._gesperrte_id = msg_id
            return True
        if chunk.get(NACH_MODELLABBRUCH_KEY):
            self.abbruch = {"stop_reason": "refusal", "category": None, "folgezuege": 0}
            self._folge_ids.add(msg_id or "anon-0")
            self.abbruch["folgezuege"] = 1
            return False
        return True


@dataclass
class LaufAbschluss:
    """How one model run ended — the single source for every output path.

    finish_reason: OpenAI value (rule in the module docstring).
    stop_reason: raw value the model reported (None: none reported).
    category / folgezuege: only after a model stop.
    transport: subtype of the marker run_completion yields when the stream
      ended WITHOUT the CLI's result (claude_cli.find_truncation_marker).
    unfertig: description of a CLI result that did not end in success
      (claude_cli.find_unfinished_result) — error_max_turns, an error subtype,
      no result at all. Matters where a run writes its output in parts.
    antwort_chunks: the run's chunks without the CLI's follow-up call.
    zuege: usage per model call, in order (one entry per message_id).
    fortsetzungen: seams in the handed-out answer — the CLI continued it past
      its output limit this often and the parts were assembled (BRIDGETEXT,
      claude_cli.antwort_teile). 0 everywhere else."""

    finish_reason: str
    stop_reason: Optional[str]
    category: Optional[str] = None
    folgezuege: int = 0
    transport: Optional[str] = None
    unfertig: Optional[str] = None
    antwort_chunks: list = field(default_factory=list)
    zuege: list = field(default_factory=list)
    fortsetzungen: int = 0

    @property
    def modell_abbruch(self) -> bool:
        return self.finish_reason == "content_filter"

    @property
    def vollstaendig(self) -> bool:
        """True only for a run that is finished in every respect: it reached
        its completion marker (no transport break), the model did not stop
        it, the CLI result is not unfinished (error_max_turns, error subtype,
        none at all), and the finish reason is one of a complete answer.
        Anything else — including a reason nobody reported — is not."""
        return (
            self.transport is None
            and not self.modell_abbruch
            and self.unfertig is None
            and self.finish_reason in VOLLSTAENDIGE_FINISH_REASONS
        )

    def details(self) -> dict:
        """The public view (x_bridge_finish, job results, the vermerk)."""
        d: dict = {"stop_reason": self.stop_reason, "vollstaendig": self.vollstaendig}
        if self.modell_abbruch:
            d.update(category=self.category, folgezuege=self.folgezuege)
        if self.transport:
            d["transport"] = self.transport
        if self.unfertig:
            d["unfertig"] = self.unfertig
        if self.fortsetzungen:
            d["fortsetzungen"] = self.fortsetzungen
        return d


def abschluss_aus_stop_reason(
    stop_reason: Optional[str], *, category: Optional[str] = None,
) -> LaufAbschluss:
    """LaufAbschluss of a Messages API response (research-cloud). No stop
    reason reported: "unknown" — never assumed complete."""
    if not stop_reason:
        return LaufAbschluss(finish_reason="unknown", stop_reason=None)
    return LaufAbschluss(
        finish_reason=STOP_REASON_FINISH.get(stop_reason, stop_reason),
        stop_reason=stop_reason,
        category=category if stop_reason in MODELL_ABBRUCH_STOP_REASONS else None,
    )


# ── the acceptance point ─────────────────────────────────────────────────────

class LaufNichtAbgeschlossen(RuntimeError):
    """The run's result may not be handed out. ``grund`` names why,
    ``abschluss`` carries the whole verdict."""

    grund = "nicht_abgeschlossen"
    retryable: Optional[bool] = None

    def __init__(self, message: str, abschluss: LaufAbschluss):
        super().__init__(message)
        self.abschluss = abschluss


class TransportAbbruch(LaufNichtAbgeschlossen):
    """The stream ended without the CLI's result: what arrived is cut.
    Retryable — the same request may get through next time."""

    grund = "transport"
    retryable = True


class ModellAbbruch(LaufNichtAbgeschlossen):
    """The model stopped the answer (safety classifier). Not retryable: the
    same request is likely to meet the same classifier."""

    grund = "modell_abbruch"
    fehler_code = "modell_abbruch"
    retryable = False


class LaufUnfertig(LaufNichtAbgeschlossen):
    """The run is not complete (abschluss.vollstaendig is false) for a reason
    other than a transport break or a model stop: error_max_turns, an error
    result, the output limit, a finish reason nobody reported."""

    grund = "unfertig"


def annehmen(abschluss: LaufAbschluss, *, ganz_fertig: bool = False) -> LaufAbschluss:
    """THE acceptance point: return ``abschluss`` if its result may be handed
    out, raise otherwise. Order: transport break first (nothing arrived
    whole, not even the stop), then a model stop, then — with
    ``ganz_fertig`` — anything that is not ``vollstaendig``.

    ganz_fertig=True: the result is final as it stands — a research report,
    a revised report, a file the run wrote. Only a complete run may be handed
    out. ganz_fertig=False: the protocol can carry an incomplete result
    honestly — chat answers it with finish_reason "length" (or "unknown")
    and x_bridge_finish.vollstaendig false; a caller must not read it as
    complete."""
    if abschluss.transport:
        raise TransportAbbruch(
            f"model run ended without completion marker ({abschluss.transport}) — the result is cut",
            abschluss,
        )
    if abschluss.modell_abbruch:
        raise ModellAbbruch(
            f"model run stopped by the model (stop_reason={abschluss.stop_reason}, "
            f"category={abschluss.category}, folgezuege={abschluss.folgezuege}) — "
            "not a finished result",
            abschluss,
        )
    if ganz_fertig and not abschluss.vollstaendig:
        raise LaufUnfertig(
            f"model run did not end complete (finish_reason={abschluss.finish_reason}, "
            f"stop_reason={abschluss.stop_reason}, unfertig={abschluss.unfertig}) — "
            "not a finished result",
            abschluss,
        )
    return abschluss


# ── the verdict travels with a stored result ────────────────────────────────

def vermerken(verzeichnis: Optional[Path], *, auslieferbar: bool, grund: Optional[str] = None,
              abschluss: Optional[LaufAbschluss] = None) -> None:
    """Write the verdict next to a stored result. A failed write is logged
    as error — the download then refuses the report (no vermerk), loud."""
    if verzeichnis is None:
        return
    inhalt = {
        "auslieferbar": bool(auslieferbar),
        "grund": grund,
        "abschluss": abschluss.details() if abschluss is not None else None,
    }
    try:
        (Path(verzeichnis) / ABSCHLUSS_DATEI).write_text(json.dumps(inhalt), encoding="utf-8")
    except OSError as e:
        logger.error(f"Laufabschluss nicht vermerkt in {verzeichnis}: {e}")


def vermerkt(verzeichnis: Path) -> Optional[dict]:
    """The stored verdict, or None when there is none (run still going, ended
    before it was written, or written before BRREF)."""
    pfad = Path(verzeichnis) / ABSCHLUSS_DATEI
    try:
        return json.loads(pfad.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        logger.error(f"Laufabschluss in {pfad} nicht lesbar: {e}")
        return None


def abschluss_eines_ergebnisses(result: Any) -> Optional[dict]:
    """The verdict a stored job result carries, for the poll (GET /v1/jobs).

    ``vollstaendig`` is always a boolean and true ONLY when the result itself
    says so (x_bridge_finish.vollstaendig is True). Results written before
    that field existed — chat results with x_bridge_finish from BRREF R1/R2,
    or without any, when "stop" was hard-wired — cannot vouch for it: false,
    with ``vollstaendig_unbekannt`` and a reason, never true by inference.
    Results that are not chat-shaped (research, doc-agent reach a done job
    only on success and carry no verdict here): None."""
    if not isinstance(result, dict):
        return None
    finish = None
    choices = result.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish = choices[0].get("finish_reason")
    gemeldet = result.get("x_bridge_finish")
    if choices is None and not isinstance(gemeldet, dict):
        return None
    abschluss = dict(gemeldet) if isinstance(gemeldet, dict) else {}
    abschluss["finish_reason"] = finish
    if abschluss.get("vollstaendig") is not True and abschluss.get("vollstaendig") is not False:
        abschluss["vollstaendig"] = False
        abschluss["vollstaendig_unbekannt"] = True
        abschluss["grund"] = (
            "x_bridge_finish_ohne_vollstaendig" if isinstance(gemeldet, dict)
            else "kein_x_bridge_finish"
        )
    return abschluss
