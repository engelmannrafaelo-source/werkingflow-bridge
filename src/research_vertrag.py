"""The execution contract of one /v1/research run (P6-VERTRAG, 06.11.2026).

Befund (koordination/tagesauftrag-20261106/BRIDGE-P6-VERTRAGSBEFUND-20261106.json):
the research-cloud path ran with fixed ceilings (max_tokens 32000, 8
continuations, 900 s per transport) that no request field reached, the pool
path with its own deadline (research_laufzeit, 3300 s) and the request's
max_turns, and a durable job's idempotency digest covered the client body
only — the same body after a changed server configuration ran under other
limits without anyone noticing.

So a research run has ONE contract:

* readable before admission — ``GET /v1/research/vertrag`` names the
  ceilings, the per-path capabilities (what binds, what a path cannot
  enforce, whether a limit holds per run / per round / per API request);
* decided at admission from the server's ceilings and the caller's wish
  (``vertrag``): ``effective = min(ceiling, wish)`` — a caller only ever
  lowers, a wish above the ceiling is clamped and shown as
  ``begrenzt_durch: server``;
* checked against the path the request was routed to, BEFORE pool admission,
  backend and budget: a wish for a limit the path cannot enforce is refused
  (``vertrag_nicht_durchsetzbar``), never executed as an apparent promise;
  a request without a wish (legacy) runs as before;
* effective on the path: the executors read the limits from here, lower
  them further only by what the path itself knows (depth budget,
  promotional cap) and say so (``begrenzt_durch: depth|promotional``) —
  the response field ``vertrag`` shows the exact values that bound;
* bound to its version. ``version`` = protocol ``research-vertrag/1`` +
  sha256 over the canonical ceilings AND capabilities: any change of a
  number or of what a path can do is a new version. A wish naming another
  version runs only as a FULLY SPECIFIED contract (same protocol, all nine
  limits explicit, each within the current ceiling) — then exactly those
  limits bind and the response names both versions; otherwise it is refused
  (``vertrag_version_abweichend``). Nothing is ever silently reinterpreted;
* persisted with a durable job as a snapshot (jobs/routes.py writes it into
  the job's server-side attribution, never into the client body, so the
  idempotency digest stays the client's body and a replay finds its job
  after any server change); the job's self-call — first dispatch and reclaim
  after a restart alike — sends the snapshot as its wish.

The time ceiling sits inside the chain of src/research_laufzeit.py: the
contract's gesamtzeit_s can never exceed GESAMTFRIST_S (checked at import,
like pruefe_staffelung — a misconfigured env fails the worker's start).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Dict, List, Literal, Optional, Set, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

import src.research_laufzeit as _rl
from src.research_laufzeit import PRUEFER_MIN_REST_S, RUECKRUNDE_MIN_REST_S

logger = logging.getLogger(__name__)

SCHEMA = "research-vertrag/1"
FEHLER_VERSION_ABWEICHEND = "vertrag_version_abweichend"
FEHLER_NICHT_DURCHSETZBAR = "vertrag_nicht_durchsetzbar"
FEHLER_VERTRAG_UNGUELTIG = "vertrag_ungueltig"

Pfad = Literal["pool", "cloud"]
# "unwirksam": the routed path cannot enforce this limit (nicht_durchsetzbar)
# — the value is shown, nothing binds (R2-5).
Grund = Literal["server", "client", "depth", "promotional", "snapshot", "unwirksam"]

# The nine limits: (name, env, default, geltung). Defaults = the values live
# on 06.11.2026, so a caller without a wish runs as before — except
# fortsetzungen, which now counts the WHOLE run (all rounds): 16 is today's
# maximum (8 per round, main round + return round), the per-round cap of 8
# stays as a capability.
GRENZEN: Tuple[Tuple[str, str, Optional[float], str], ...] = (
    ("ausgabe_tokens", "RESEARCH_VERTRAG_AUSGABE_TOKENS_MAX", 32000,
     "je Modellantwort (Messages-API max_tokens); cloud"),
    ("iterationen", "RESEARCH_VERTRAG_ITERATIONEN_MAX", 40,
     "Hauptlauf (CLI-Turns, max_turns); Reparatur- und Rückrunde haben eigene feste Deckel <= 40; pool"),
    ("fortsetzungen", "RESEARCH_VERTRAG_FORTSETZUNGEN_MAX", 16,
     "gesamter Lauf, alle Runden (Messages-API-Aufrufe); je Runde zusätzlich fortsetzungen_je_runde; cloud"),
    ("suchen", "RESEARCH_VERTRAG_SUCHEN_MAX", 20,
     "je Messages-API-Anfrage (max_uses des Servertools web_search); Summe im Ergebnis web_searches; cloud"),
    ("abrufe", "RESEARCH_VERTRAG_ABRUFE_MAX", 15,
     "je Messages-API-Anfrage (max_uses des Servertools web_fetch); Summe im Ergebnis web_fetches; cloud"),
    ("perplexity_suchen", "RESEARCH_VERTRAG_PERPLEXITY_MAX", 8,
     "gesamter Lauf (perplexity_search, inkl. Rückrunde); pool und cloud"),
    ("dokument_abrufe", "RESEARCH_VERTRAG_DOKUMENTE_MAX", 10,
     "gesamter Lauf (fetch_document, inkl. Rückrunde); pool und cloud"),
    # Default None = the run deadline of the time chain (research_laufzeit
    # GESAMTFRIST_S, env RESEARCH_GESAMTFRIST_S) — read at call time, so a
    # shorter chain never makes the worker's start fail (R1 W4).
    ("gesamtzeit_s", "RESEARCH_VERTRAG_GESAMTZEIT_S_MAX", None,
     "gesamter Lauf: Modellaufrufe, Werkzeuge, Bibliothek, Prüfer, Rückrunde, Nachmessung; eine Frist, "
     "keine neue bei Fortsetzung/Rückrunde/Cross-Worker-Retry; pool und cloud"),
    ("transportzeit_s", "RESEARCH_VERTRAG_TRANSPORTZEIT_S_MAX", 900,
     "je Messages-API-Aufruf (HTTP), nie länger als die Restfrist; cloud"),
)
NAMEN: Tuple[str, ...] = tuple(n for n, _, _, _ in GRENZEN)
GANZZAHLIG = frozenset(n for n in NAMEN if not n.endswith("_s"))
GELTUNG: Dict[str, str] = {n: g for n, _, _, g in GRENZEN}

FORTSETZUNGEN_JE_RUNDE = 8
PROMOTIONAL_CAP = {"suchen": 3, "abrufe": 3}

# What binds on which path, and what a path cannot enforce.
WIRKSAM_JE_PFAD: Dict[str, List[str]] = {
    "pool": ["iterationen", "perplexity_suchen", "dokument_abrufe", "gesamtzeit_s"],
    "cloud": [
        "ausgabe_tokens", "fortsetzungen", "suchen", "abrufe", "perplexity_suchen",
        "dokument_abrufe", "gesamtzeit_s", "transportzeit_s",
    ],
}
NICHT_DURCHSETZBAR_JE_PFAD: Dict[str, List[str]] = {
    # The CLI runs its own WebSearch/WebFetch without a per-run cap, takes no
    # output-token limit (claude_cli.run_completion) and has no continuation
    # count or HTTP transport of its own; its one bound is gesamtzeit_s.
    "pool": ["ausgabe_tokens", "fortsetzungen", "suchen", "abrufe", "transportzeit_s"],
    # The cloud path has no CLI turns.
    "cloud": ["iterationen"],
}

WUNSCHFELDER = ["version"] + list(NAMEN) + ["verlangt"]


def _lies(env: str, default: Optional[float], ganzzahlig: bool) -> Union[int, float]:
    roh = os.getenv(env)
    if roh is None or roh == "":
        if default is None:  # gesamtzeit_s: the time chain's run deadline
            return float(_rl.GESAMTFRIST_S)
        return int(default) if ganzzahlig else float(default)
    try:
        wert = int(roh) if ganzzahlig else float(roh)
    except ValueError as e:
        raise ValueError(f"{env}={roh!r} is not a number") from e
    if wert <= 0:
        raise ValueError(f"{env}={roh!r} must be > 0")
    return wert


def obergrenzen() -> Dict[str, Union[int, float]]:
    """The server's ceilings, from env (defaults = live values 06.11.2026),
    clamped into the time chain: gesamtzeit_s <= GESAMTFRIST_S,
    transportzeit_s <= gesamtzeit_s, fortsetzungen >= one round. A value
    outside is clamped and logged, never a start failure (R2-4) — a host
    with a shorter chain (RESEARCH_GESAMTFRIST_S, MAX_TIMEOUT) still
    starts; a non-number or a value <= 0 stays a loud error."""
    g = {name: _lies(env, default, name in GANZZAHLIG) for name, env, default, _ in GRENZEN}
    kette = float(_rl.GESAMTFRIST_S)
    if g["gesamtzeit_s"] > kette:
        logger.warning(
            f"research-vertrag: gesamtzeit_s={g['gesamtzeit_s']:.0f} geklemmt auf "
            f"RESEARCH_GESAMTFRIST_S={kette:.0f}"
        )
        g["gesamtzeit_s"] = kette
    if g["transportzeit_s"] > g["gesamtzeit_s"]:
        logger.warning(
            f"research-vertrag: transportzeit_s={g['transportzeit_s']:.0f} geklemmt auf "
            f"gesamtzeit_s={g['gesamtzeit_s']:.0f}"
        )
        g["transportzeit_s"] = float(g["gesamtzeit_s"])
    if g["fortsetzungen"] < FORTSETZUNGEN_JE_RUNDE:
        logger.warning(
            f"research-vertrag: fortsetzungen={g['fortsetzungen']} geklemmt auf "
            f"fortsetzungen_je_runde={FORTSETZUNGEN_JE_RUNDE}"
        )
        g["fortsetzungen"] = FORTSETZUNGEN_JE_RUNDE
    return g


def pruefe_obergrenzen(grenzen: Optional[Dict[str, Union[int, float]]] = None) -> None:
    """Raise ValueError unless ``grenzen`` fit the time chain. What
    obergrenzen() returns always does (it clamps); the check guards a dict
    handed in from elsewhere, and the import-time call below is the
    self-test that the clamping holds."""
    g = grenzen or obergrenzen()
    fehler = []
    if not g["gesamtzeit_s"] <= _rl.GESAMTFRIST_S:
        fehler.append(
            f"gesamtzeit_s={g['gesamtzeit_s']:.0f} must be <= RESEARCH_GESAMTFRIST_S={_rl.GESAMTFRIST_S:.0f}"
        )
    if not g["transportzeit_s"] <= g["gesamtzeit_s"]:
        fehler.append(
            f"transportzeit_s={g['transportzeit_s']:.0f} must be <= gesamtzeit_s={g['gesamtzeit_s']:.0f}"
        )
    if not g["fortsetzungen"] >= FORTSETZUNGEN_JE_RUNDE:
        fehler.append(
            f"fortsetzungen={g['fortsetzungen']} must be >= fortsetzungen_je_runde={FORTSETZUNGEN_JE_RUNDE}"
        )
    if fehler:
        raise ValueError("research contract ceilings out of order: " + "; ".join(fehler))


pruefe_obergrenzen()


def capabilities() -> Dict[str, Any]:
    """What the paths can do — part of the version, so a change of semantics
    is a new version, not just a change of numbers."""
    from src.research_cloud.prompt import _FETCH_MAX_USES, _SEARCH_MAX_USES

    return {
        "pfade": {
            pfad: {
                "wirksam": list(WIRKSAM_JE_PFAD[pfad]),
                "nicht_durchsetzbar": list(NICHT_DURCHSETZBAR_JE_PFAD[pfad]),
            }
            for pfad in ("pool", "cloud")
        },
        "geltung": dict(GELTUNG),
        "fortsetzungen_je_runde": FORTSETZUNGEN_JE_RUNDE,
        "depth_budget": {"suchen": dict(_SEARCH_MAX_USES), "abrufe": dict(_FETCH_MAX_USES)},
        "promotional_cap": dict(PROMOTIONAL_CAP),
        "zeit_minima": {
            "pruefer_min_rest_s": PRUEFER_MIN_REST_S,
            "rueckrunde_min_rest_s": RUECKRUNDE_MIN_REST_S,
        },
        "max_tokens_feld": "unwirksam auf beiden Pfaden; verbindlich ist vertrag.ausgabe_tokens",
        "max_turns_feld": "senkt iterationen (pool), erhoeht nie",
    }


def version(grenzen: Optional[Dict[str, Union[int, float]]] = None) -> str:
    """``research-vertrag/1+<sha256 over ceilings and capabilities>[:12]``."""
    g = grenzen or obergrenzen()
    kanonisch = json.dumps(
        {"grenzen": {k: g[k] for k in NAMEN}, "capabilities": capabilities()},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return f"{SCHEMA}+{hashlib.sha256(kanonisch.encode('utf-8')).hexdigest()[:12]}"


def protokoll(version_text: str) -> str:
    return version_text.split("+", 1)[0]


class ResearchVertragWunsch(BaseModel):
    """What a caller may ask for: the version it read, and lower limits.
    Unknown fields are refused (a typo must not pass as 'no wish').

    A limit set below the ceiling is a DEMAND: the routed path must enforce
    it or the request is refused. ``verlangt`` narrows that: when present,
    only the limits named there are demands; the others are soft — they
    bind where the path can enforce them and are shown as
    ``nicht_durchsetzbar`` where it cannot, without a refusal. The server's
    own job snapshot always carries ``verlangt`` (the demands admitted at
    submit), so a self-call never turns a mirrored default into a demand
    (R1 B1). ``max_turns`` is never a demand."""

    model_config = ConfigDict(extra="forbid")

    version: Optional[str] = Field(
        default=None,
        description=(
            "The contract version the caller read (GET /v1/research/vertrag). Another "
            "version than the current one is accepted only as a fully specified contract "
            "(same protocol, all nine limits set, each within the current ceiling) — a "
            "durable job's persisted snapshot; otherwise refused before any run "
            "(fehler_code vertrag_version_abweichend)."
        ),
    )
    ausgabe_tokens: Optional[int] = Field(default=None, ge=1, description=GELTUNG["ausgabe_tokens"])
    iterationen: Optional[int] = Field(default=None, ge=1, description=GELTUNG["iterationen"])
    fortsetzungen: Optional[int] = Field(default=None, ge=1, description=GELTUNG["fortsetzungen"])
    suchen: Optional[int] = Field(default=None, ge=1, description=GELTUNG["suchen"])
    abrufe: Optional[int] = Field(default=None, ge=1, description=GELTUNG["abrufe"])
    perplexity_suchen: Optional[int] = Field(default=None, ge=1, description=GELTUNG["perplexity_suchen"])
    dokument_abrufe: Optional[int] = Field(default=None, ge=1, description=GELTUNG["dokument_abrufe"])
    gesamtzeit_s: Optional[float] = Field(default=None, gt=0, description=GELTUNG["gesamtzeit_s"])
    transportzeit_s: Optional[float] = Field(default=None, gt=0, description=GELTUNG["transportzeit_s"])
    verlangt: Optional[List[str]] = Field(
        default=None,
        description=(
            "Which of the set limits are demands (must be enforceable on the routed path). "
            "Absent: every limit set below the ceiling. Written by the server into a job's snapshot."
        ),
    )

    @field_validator("verlangt")
    @classmethod
    def _nur_grenzen(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return None
        fremd = [n for n in v if n not in NAMEN]
        if fremd:
            raise ValueError(f"verlangt names no limit: {', '.join(fremd)}")
        return [n for n in NAMEN if n in v]

    def gesetzt(self) -> Set[str]:
        return {n for n in NAMEN if n in self.model_fields_set and getattr(self, n) is not None}

    def forderungen(self, grenzen: Dict[str, Union[int, float]]) -> List[str]:
        """The demands of this wish under ``grenzen``: set, below the
        ceiling, and (when ``verlangt`` is given) named there."""
        kandidaten = self.gesetzt() if self.verlangt is None else (self.gesetzt() & set(self.verlangt))
        return [n for n in NAMEN if n in kandidaten and getattr(self, n) < grenzen[n]]


class VertragVersionAbweichend(ValueError):
    """The caller named a contract version that is not the current one and
    did not send a fully specified contract within the current ceilings."""

    fehler_code = FEHLER_VERSION_ABWEICHEND
    retryable = False

    def __init__(self, gesendet: str, aktuell: str, warum: str):
        super().__init__(
            f"research contract version {gesendet!r} is not the current {aktuell!r} and "
            f"cannot be taken over unchanged ({warum}) — refused before any run. Read "
            "GET /v1/research/vertrag and resend."
        )
        self.gesendet = gesendet
        self.aktuell = aktuell


class VertragNichtDurchsetzbar(ValueError):
    """The caller asked for a limit the routed path cannot enforce."""

    fehler_code = FEHLER_NICHT_DURCHSETZBAR
    retryable = False

    def __init__(self, pfad: str, grenzen: List[str]):
        super().__init__(
            f"research contract: the request asks for {', '.join(grenzen)}, which the "
            f"{pfad} path cannot enforce (GET /v1/research/vertrag: capabilities.pfade.{pfad}."
            "nicht_durchsetzbar) — refused before admission; no other path is chosen for it."
        )
        self.pfad = pfad
        self.grenzen = list(grenzen)


class ResearchVertrag(BaseModel):
    """The effective contract of one run — what actually binds."""

    schema_version: str = SCHEMA
    version: str
    server_version: str
    quelle: Literal["anfrage", "snapshot"] = "anfrage"
    grenzen: Dict[str, Union[int, float]]
    begrenzt_durch: Dict[str, Grund]
    geltung: Dict[str, str] = Field(default_factory=lambda: dict(GELTUNG))
    pfad: Optional[Pfad] = None
    nicht_durchsetzbar: List[str] = Field(default_factory=list)
    # Limits the caller asked for below the ceiling (checked against the path).
    verlangt: List[str] = Field(default_factory=list)

    def grenze(self, name: str) -> Union[int, float]:
        return self.grenzen[name]

    def fuer_pfad(self, pfad: Pfad) -> "ResearchVertrag":
        """The path's view. Raises VertragNichtDurchsetzbar when the caller
        asked for a limit this path cannot enforce. Limits the path cannot
        enforce are marked ``begrenzt_durch: unwirksam`` — their value is
        shown (a soft wish, the ceiling), nothing binds (R2-5)."""
        unerfuellbar = [n for n in self.verlangt if n in NICHT_DURCHSETZBAR_JE_PFAD[pfad]]
        if unerfuellbar:
            raise VertragNichtDurchsetzbar(pfad, unerfuellbar)
        begrenzt = dict(self.begrenzt_durch)
        for n in NICHT_DURCHSETZBAR_JE_PFAD[pfad]:
            begrenzt[n] = "unwirksam"
        return self.model_copy(update={
            "pfad": pfad, "nicht_durchsetzbar": list(NICHT_DURCHSETZBAR_JE_PFAD[pfad]),
            "begrenzt_durch": begrenzt,
        })

    def senken(self, name: str, wert: Union[int, float], grund: Grund) -> "ResearchVertrag":
        """Lower one limit by what the path knows (depth budget, promotional
        cap). Never raises a limit; a value at or above the current one
        changes nothing."""
        if wert >= self.grenzen[name]:
            return self
        grenzen = dict(self.grenzen)
        grenzen[name] = int(wert) if name in GANZZAHLIG else float(wert)
        begrenzt = dict(self.begrenzt_durch)
        begrenzt[name] = grund
        return self.model_copy(update={"grenzen": grenzen, "begrenzt_durch": begrenzt})

    def als_wunsch(self) -> Dict[str, Any]:
        """The contract as the wish that reproduces it exactly: version,
        every effective limit, and which of them were demands. A durable
        job's snapshot."""
        return {"version": self.version, **{k: self.grenzen[k] for k in NAMEN}, "verlangt": list(self.verlangt)}

    def ausgabe(self) -> Dict[str, Any]:
        return self.model_dump()


def _wunsch_aus(request_body: Any) -> Optional[ResearchVertragWunsch]:
    roh = getattr(request_body, "vertrag", None)
    if roh is None:
        return None
    if isinstance(roh, ResearchVertragWunsch):
        return roh
    if isinstance(roh, dict):
        return ResearchVertragWunsch.model_validate(roh)
    # A ResearchRequest carries None or the validated model here — nothing
    # else can reach this point from the route. Anything else is a test
    # double's auto-attribute (MagicMock bodies in the existing harnesses),
    # not a wish; treated like max_turns below: only a real value counts.
    return None


def _wert(name: str, wert: Union[int, float]) -> Union[int, float]:
    return int(wert) if name in GANZZAHLIG else float(wert)


def vertrag_erstellen(
    request_body: Any, *, grenzen: Optional[Dict[str, Union[int, float]]] = None
) -> ResearchVertrag:
    """The effective contract for ``request_body`` (a ResearchRequest or any
    object with ``vertrag``/``max_turns``): min(ceiling, wish) per limit.
    Raises VertragVersionAbweichend when the wish names another version
    that cannot be taken over unchanged."""
    g = grenzen or obergrenzen()
    aktuell = version(g)
    wunsch = _wunsch_aus(request_body)

    if wunsch is not None and wunsch.version is not None and wunsch.version != aktuell:
        # Take-over of a fully specified contract (a persisted snapshot):
        # exactly its limits, unchanged, as long as every one still fits.
        if protokoll(wunsch.version) != SCHEMA:
            raise VertragVersionAbweichend(wunsch.version, aktuell, f"protocol is not {SCHEMA}")
        fehlend = [n for n in NAMEN if n not in wunsch.gesetzt()]
        if fehlend:
            raise VertragVersionAbweichend(
                wunsch.version, aktuell, f"not fully specified: {', '.join(fehlend)} missing"
            )
        zu_hoch = [n for n in NAMEN if getattr(wunsch, n) > g[n]]
        if zu_hoch:
            raise VertragVersionAbweichend(
                wunsch.version, aktuell, f"above the current ceiling: {', '.join(zu_hoch)}"
            )
        effektiv = {n: _wert(n, getattr(wunsch, n)) for n in NAMEN}
        begrenzt: Dict[str, Grund] = {n: "snapshot" for n in NAMEN}
        # max_turns lowers the turns here as everywhere (R1 W2) — a legacy
        # field, never a demand.
        max_turns = _max_turns(request_body)
        if max_turns is not None and max_turns < effektiv["iterationen"]:
            effektiv["iterationen"] = max_turns
            begrenzt["iterationen"] = "client"
        # Demands: the ones the snapshot names. A hand-written contract
        # without `verlangt` is read conservatively — every limit below the
        # CURRENT ceiling is a demand (refuse rather than run silently).
        return ResearchVertrag(
            version=wunsch.version, server_version=aktuell, quelle="snapshot",
            grenzen=effektiv, begrenzt_durch=begrenzt, verlangt=wunsch.forderungen(g),
        )

    wuensche: Dict[str, Optional[Union[int, float]]] = {
        name: (getattr(wunsch, name) if wunsch is not None else None) for name in NAMEN
    }
    # The existing field max_turns lowers the pool's turns like a wish (it
    # bound there before this contract existed; a default of 30 stays 30).
    # It is not a contract demand: a legacy caller is routed as before, and
    # a self-call's mirrored default never refuses the cloud path (R1 B1).
    max_turns = _max_turns(request_body)
    if max_turns is not None:
        w = wuensche["iterationen"]
        wuensche["iterationen"] = max_turns if w is None else min(w, max_turns)

    effektiv: Dict[str, Union[int, float]] = {}
    begrenzt: Dict[str, Grund] = {}
    for name in NAMEN:
        decke = g[name]
        w = wuensche[name]
        if w is not None and w < decke:
            effektiv[name] = _wert(name, w)
            begrenzt[name] = "client"
        else:
            effektiv[name] = _wert(name, decke)
            begrenzt[name] = "server"
    return ResearchVertrag(
        version=aktuell, server_version=aktuell, grenzen=effektiv, begrenzt_durch=begrenzt,
        verlangt=wunsch.forderungen(g) if wunsch is not None else [],
    )


def _max_turns(request_body: Any) -> Optional[int]:
    """The legacy field as a number, or None (unset, or a test double)."""
    max_turns = getattr(request_body, "max_turns", None)
    if isinstance(max_turns, int) and not isinstance(max_turns, bool) and max_turns >= 1:
        return max_turns
    return None


def _state(request: Any) -> Any:
    return getattr(request, "state", None) if request is not None else None


def _merken(request: Any, vertrag: ResearchVertrag) -> None:
    state = _state(request)
    if state is not None:
        try:
            state.research_vertrag = vertrag
        except Exception:
            pass


def vertrag_annehmen(request: Any, request_body: Any) -> ResearchVertrag:
    """Admission step 1 (before routing): decide the contract once and
    remember it on the request."""
    vertrag = vertrag_erstellen(request_body)
    _merken(request, vertrag)
    return vertrag


def vertrag_fuer(request: Any, request_body: Any, pfad: Optional[Pfad] = None) -> ResearchVertrag:
    """The contract of this request (request.state.research_vertrag), or —
    without a request/state, e.g. a direct call in tests — a fresh one.
    With ``pfad``: the path's view (admission step 2 when not yet taken:
    raises VertragNichtDurchsetzbar)."""
    state = _state(request)
    vertrag = getattr(state, "research_vertrag", None) if state is not None else None
    if not isinstance(vertrag, ResearchVertrag):
        vertrag = vertrag_erstellen(request_body)
        _merken(request, vertrag)
    if pfad is None or vertrag.pfad == pfad:
        return vertrag
    vertrag = vertrag.fuer_pfad(pfad)
    _merken(request, vertrag)
    return vertrag


def vertrag_merken(request: Any, vertrag: ResearchVertrag) -> ResearchVertrag:
    """An executor lowered a limit (depth/promotional): keep the exact
    contract on the request so the response shows what bound."""
    _merken(request, vertrag)
    return vertrag


def beschreibung() -> Dict[str, Any]:
    """GET /v1/research/vertrag — readable before admission."""
    g = obergrenzen()
    return {
        "schema_version": SCHEMA,
        "version": version(g),
        "grenzen": g,
        "capabilities": capabilities(),
        "wunschfeld": "vertrag",
        "wunschfelder": list(WUNSCHFELDER),
        "regel": (
            "effektiv = min(grenze, wunsch); ein Wunsch senkt nur, nie erhoeht; ein Wunsch fuer eine "
            "Grenze, die der entschiedene Pfad nicht durchsetzen kann, wird vor Admission abgelehnt "
            f"({FEHLER_NICHT_DURCHSETZBAR}, HTTP 422); eine fremde Version laeuft nur als vollstaendig "
            f"bestimmter Vertrag innerhalb der aktuellen Grenzen, sonst {FEHLER_VERSION_ABWEICHEND} (HTTP 422)"
        ),
        "antwortfeld": (
            "vertrag (version, server_version, quelle, grenzen, begrenzt_durch, geltung, pfad, "
            "nicht_durchsetzbar)"
        ),
        "forderungen": (
            "eine unter der Obergrenze gesetzte Grenze ist eine Forderung an den Pfad; `verlangt` "
            "benennt die Forderungen ausdruecklich (dann sind die uebrigen gesetzten Grenzen weich); "
            "max_turns ist nie eine Forderung"
        ),
        "zeitkette": {"gesamtfrist_s": _rl.GESAMTFRIST_S},
    }


# ── durable jobs (src/jobs) ────────────────────────────────────────────────
# The snapshot lives in the job's server-side attribution under this key —
# like funding_source/bridge_origin/eigentuemer: decided at submit, never
# taken from the body, persisted with the row, handed to the executor on
# first dispatch and on reclaim after a restart.
JOB_VERTRAG_KEY = "research_vertrag"


def snapshot_fuer_job(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The contract a research job is admitted with, as the wish that
    reproduces it (version + nine limits). Raises VertragVersionAbweichend,
    or pydantic ValidationError / ValueError for an unusable wish."""
    payload = payload or {}
    roh = payload.get("vertrag")
    if roh is not None and not isinstance(roh, dict):
        raise ValueError(f"payload.vertrag must be an object, got {type(roh).__name__}")
    wunsch = ResearchVertragWunsch.model_validate(roh) if roh is not None else None
    # The self-call validates the payload as a ResearchRequest; its defaults
    # (max_turns 30) bind there. The snapshot mirrors exactly that (R1 B1):
    # what it says is what the run will bind.
    from src.models import ResearchRequest  # at call time: models imports this module

    class _Rumpf:
        vertrag = wunsch
        max_turns = payload.get("max_turns", ResearchRequest.model_fields["max_turns"].default)

    return vertrag_erstellen(_Rumpf()).als_wunsch()


def selbstaufruf_rumpf(payload: Dict[str, Any], attribution: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The research self-call body: the job's payload with the persisted
    snapshot as its wish. A job submitted before this contract existed has
    no snapshot and runs as a legacy request (no wish)."""
    snapshot = (attribution or {}).get(JOB_VERTRAG_KEY)
    if isinstance(snapshot, dict) and snapshot.get("version"):
        return {**payload, "vertrag": dict(snapshot)}
    return dict(payload)
