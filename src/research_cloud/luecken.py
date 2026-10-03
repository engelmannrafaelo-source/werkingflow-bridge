"""Lücken-Nachkontrolle: before a research report is handed back, the bridge
checks every gap the report itself declares against the run's tool log and,
if gaps went unsearched while budget is left, sends the model back ONCE.

Rafael 03.10.2026 (Entscheidung e-perplexity-luecken-kontrolle, Antwort a).
Measured on Dev (4d9dfba, pool path, three runs of the same request): the
tools work — one run found a manufacturer brochure via perplexity_search and
read the value at the source with fetch_document — but whether the model uses
them for the open points is chance (datasheet 1 of 3, missing standard 1 of 3).
The rules in the system prompt lose against the caller's own brief ("write the
output immediately", "no filler queries"). More prompt text does not change
that; a check after the report does, because it no longer competes with the
caller's instructions.

Why a separate structured model call and not a machine-readable gap block the
report has to carry:
- the block would be one more system-prompt rule — exactly the kind that lost
  against the caller's brief in the measurement;
- the report is the /v1/research contract; a block in it would have to be
  stripped again, and a stripped block that the model forgot looks the same as
  "no gaps";
- matching a gap ("value X of type Y not confirmed") to a tool call ("datasheet
  Y") is a semantic question, not a pattern over customer language.
The checker gets only the report and the tool log, answers in a fixed JSON
schema (cloud: forced tool call; pool: JSON validated against the same model),
and is a cheap model. Its own failure is named (``pruef_fehler`` in
provider_meta + an ERROR log line) and never counts as "no gaps".

Exactly one return round: the decision below is taken once per run; the
caller never asks again after the return round. The second check after it
only measures (``luecken_nach_rueckrunde``) and never triggers anything.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from os import environ
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional

import httpx
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

FLAG = "RESEARCH_LUECKEN_NACHKONTROLLE"
PRUEF_MODELL = "claude-haiku-4-5-20251001"
PRUEF_WERKZEUG = "luecken_melden"
# The checker reads the whole report; beyond this the tail is cut and the cut
# is said in the request (a report of this size is far beyond what was measured).
MAX_BERICHT_ZEICHEN = 150_000
MAX_PROTOKOLL_EINTRAEGE = 200

_AN = ("1", "true", "yes", "on")
_AUS = ("0", "false", "no", "off")


class LueckenKonfigFehler(Exception):
    """The switch is set to something that cannot work — refuse the run."""


class LueckenPruefFehler(Exception):
    """The checker call failed or answered outside the schema."""


def nachkontrolle_aktiv(perplexity_an: bool) -> bool:
    """Own flag, default = the Perplexity flag (the return round needs its tools).

    Unset -> follows Perplexity (on wherever Perplexity is on, off in Prod as
    long as Perplexity is off there). Explicit off -> off. Explicit on while
    Perplexity is off -> configuration error: the return round could not search.
    """
    roh = (environ.get(FLAG) or "").strip().lower()
    if not roh:
        return perplexity_an
    if roh in _AUS:
        return False
    if roh in _AN:
        if not perplexity_an:
            raise LueckenKonfigFehler(
                f"{FLAG} ist an, aber RESEARCH_PERPLEXITY_ENABLED nicht — die Rückrunde "
                "hätte keine Werkzeuge zum Suchen. Beide einschalten oder die Nachkontrolle aus."
            )
        return True
    raise LueckenKonfigFehler(f"{FLAG}={roh!r} ist kein gültiger Wert (an: {_AN}, aus: {_AUS})")


# ---------------------------------------------------------------------------
# Tool log
# ---------------------------------------------------------------------------

@dataclass
class WerkzeugAufruf:
    werkzeug: str  # perplexity_search | fetch_document | web_search | web_fetch
    eingabe: str


def _eingabe_text(werkzeug: str, eingabe: Dict[str, Any]) -> str:
    if werkzeug == "perplexity_search":
        return str(eingabe.get("frage") or "")
    if werkzeug == "web_search":
        return str(eingabe.get("query") or "")
    if werkzeug in ("fetch_document", "web_fetch"):
        teile = [str(eingabe.get("url") or "")]
        for schluessel in ("suchbegriff", "seiten"):
            if eingabe.get(schluessel):
                teile.append(f"{schluessel}={eingabe[schluessel]}")
        return " ".join(teile)
    return json.dumps(eingabe, ensure_ascii=False)[:300]


_CLOUD_NAMEN = {
    "perplexity_search": "perplexity_search",
    "fetch_document": "fetch_document",
    "web_search": "web_search",
    "web_fetch": "web_fetch",
}
_POOL_NAMEN = {
    "mcp__perplexity__perplexity_search": "perplexity_search",
    "mcp__dokument__fetch_document": "fetch_document",
    "WebSearch": "web_search",
    "WebFetch": "web_fetch",
}


def protokoll_aus_cloud_nachrichten(nachrichten: List[Dict[str, Any]]) -> List[WerkzeugAufruf]:
    """Tool calls of a Messages-API conversation (client and server tools)."""
    protokoll: List[WerkzeugAufruf] = []
    for nachricht in nachrichten:
        if nachricht.get("role") != "assistant" or not isinstance(nachricht.get("content"), list):
            continue
        for block in nachricht["content"]:
            if not isinstance(block, dict) or block.get("type") not in ("tool_use", "server_tool_use"):
                continue
            werkzeug = _CLOUD_NAMEN.get(block.get("name"))
            if werkzeug:
                protokoll.append(WerkzeugAufruf(werkzeug, _eingabe_text(werkzeug, block.get("input") or {})))
    return protokoll


def protokoll_aus_pool_chunks(chunks: List[Any]) -> List[WerkzeugAufruf]:
    """Tool calls of a CLI run (the attr-dicts run_completion yields)."""
    protokoll: List[WerkzeugAufruf] = []
    for chunk in chunks:
        if not isinstance(chunk, dict) or not isinstance(chunk.get("content"), list):
            continue
        for block in chunk["content"]:
            name = getattr(block, "name", None)
            if name is None and isinstance(block, dict):
                name = block.get("name")
            werkzeug = _POOL_NAMEN.get(name)
            if not werkzeug:
                continue
            eingabe = getattr(block, "input", None)
            if eingabe is None and isinstance(block, dict):
                eingabe = block.get("input")
            protokoll.append(WerkzeugAufruf(werkzeug, _eingabe_text(werkzeug, eingabe or {})))
    return protokoll


# ---------------------------------------------------------------------------
# Checker request / answer
# ---------------------------------------------------------------------------

class Luecke(BaseModel):
    punkt: str = Field(min_length=1)
    art: Literal["norm", "produkt", "kennwert", "sonstiges"]
    gesucht: bool
    gelesen: bool


class LueckenBefund(BaseModel):
    luecken: List[Luecke]


LUECKEN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "luecken": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "punkt": {
                        "type": "string",
                        "description": "Der offene Punkt so konkret, wie der Bericht ihn nennt "
                        "(Normnummer, Typbezeichnung, Kennwert mit Bezug).",
                    },
                    "art": {"type": "string", "enum": ["norm", "produkt", "kennwert", "sonstiges"]},
                    "gesucht": {
                        "type": "boolean",
                        "description": "true, wenn im Werkzeugprotokoll ein perplexity_search-Aufruf "
                        "genau diesen Punkt sucht.",
                    },
                    "gelesen": {
                        "type": "boolean",
                        "description": "true, wenn im Werkzeugprotokoll ein fetch_document- oder "
                        "web_fetch-Aufruf eine Quelle zu diesem Punkt liest.",
                    },
                },
                "required": ["punkt", "art", "gesucht", "gelesen"],
            },
        }
    },
    "required": ["luecken"],
}

PRUEF_SYSTEM = """Du prüfst einen fertigen Recherchebericht, bevor er abgegeben wird. Du recherchierst nicht selbst.

Aufgabe: Finde jede Stelle, an der der Bericht selbst einen Punkt als offen meldet — z. B. „nicht auffindbar“,
„nicht bestätigt“, „nicht verifiziert“, „nicht gesucht“, „nicht geprüft“, „Lücke“, „keine Angabe gefunden“,
„konnte nicht ermittelt werden“ oder gleichbedeutend in jeder Sprache. Je offenem Punkt ein Eintrag:
- punkt: so konkret, wie der Bericht ihn nennt (Normnummer, Produkt-/Typbezeichnung, Kennwert mit Bezug);
- art: norm (Norm, Richtlinie, Verordnung, Gesetz), produkt (Produkt, Typ, Datenblatt), kennwert (eine Zahl
  oder Eigenschaft), sonstiges (alles andere, z. B. Preise auf Anfrage, interne Angaben des Auftraggebers);
- gesucht: true nur, wenn das Werkzeugprotokoll einen perplexity_search-Aufruf enthält, der genau diesen Punkt
  sucht (sinngemäß, auch in anderer Formulierung oder Sprache);
- gelesen: true nur, wenn das Protokoll einen fetch_document- oder web_fetch-Aufruf auf eine Quelle zu genau
  diesem Punkt enthält.

Melde nur, was der Bericht als offen kennzeichnet — nicht, was du selbst für unvollständig hältst. Ein Punkt,
den der Bericht belegt beantwortet, ist keine Lücke. Gibt es keinen offenen Punkt, ist die Liste leer."""


def baue_pruefanfrage(bericht: str, protokoll: List[WerkzeugAufruf]) -> str:
    gekuerzt = ""
    if len(bericht) > MAX_BERICHT_ZEICHEN:
        bericht = bericht[:MAX_BERICHT_ZEICHEN]
        gekuerzt = f"\n[Bericht nach {MAX_BERICHT_ZEICHEN} Zeichen gekürzt]"
    zeilen = [f"{i}. {a.werkzeug}: {a.eingabe}" for i, a in enumerate(protokoll[:MAX_PROTOKOLL_EINTRAEGE], 1)]
    if len(protokoll) > MAX_PROTOKOLL_EINTRAEGE:
        zeilen.append(f"[... {len(protokoll) - MAX_PROTOKOLL_EINTRAEGE} weitere Aufrufe gekürzt]")
    protokoll_text = "\n".join(zeilen) if zeilen else "(keine Such- oder Lese-Aufrufe)"
    return (
        f"## Werkzeugprotokoll des Laufs\n{protokoll_text}\n\n"
        f"## Bericht\n<bericht>\n{bericht}{gekuerzt}\n</bericht>"
    )


_JSON_ZAUN = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


def lies_befund(daten: Any) -> LueckenBefund:
    """Validate the checker's answer. A dict (forced tool call) or text that is
    one JSON object (optionally fenced). Anything else is a named error."""
    if isinstance(daten, str):
        text = daten.strip()
        zaun = _JSON_ZAUN.search(text)
        if zaun:
            text = zaun.group(1)
        elif not text.startswith("{"):
            anfang, ende = text.find("{"), text.rfind("}")
            if anfang < 0 or ende <= anfang:
                raise LueckenPruefFehler(f"Prüfantwort enthält kein JSON-Objekt: {daten[:200]!r}")
            text = text[anfang:ende + 1]
        try:
            daten = json.loads(text)
        except json.JSONDecodeError as e:
            raise LueckenPruefFehler(f"Prüfantwort ist kein gültiges JSON ({e}): {text[:200]!r}") from e
    if not isinstance(daten, dict):
        raise LueckenPruefFehler(f"Prüfantwort ist kein Objekt: {type(daten).__name__}")
    try:
        return LueckenBefund.model_validate(daten)
    except ValidationError as e:
        raise LueckenPruefFehler(f"Prüfantwort verletzt das Schema: {e}") from e


def offene_luecken(befund: LueckenBefund) -> List[Luecke]:
    """Gaps the return round is for: norm/produkt/kennwert without a
    perplexity_search for them. The system prompt names Perplexity as the
    first search for exactly these three kinds; a gap reported with only a
    web search behind it has not had that search."""
    return [l for l in befund.luecken if l.art != "sonstiges" and not l.gesucht]


def baue_rueckrunden_auftrag(
    offene: List[Luecke],
    *,
    perplexity_werkzeug: str,
    dokument_werkzeug: str,
    perplexity_rest: int,
    dokument_rest: int,
    ziel: str,
) -> str:
    """The one continuation message. ``ziel`` says where the full report goes
    (a file to overwrite, or the answer itself)."""
    punkte = "\n".join(f"- [{l.art}] {l.punkt}" for l in offene)
    return f"""Nachkontrolle der Bridge vor der Abgabe: Dein Bericht meldet diese Punkte als offen, und das
Werkzeugprotokoll zeigt zu keinem davon eine Suche mit `{perplexity_werkzeug}`:

{punkte}

Prüfe genau diese Punkte jetzt nach — nur sie, keine neue Recherche zu anderen Themen:
1. Je Punkt eine Frage an `{perplexity_werkzeug}` (noch {perplexity_rest} Aufrufe frei). Eine Typ- oder
   Produktbezeichnung darf gesucht werden. Ordnest du dabei einen Typ einem Hersteller zu, kennzeichnest du das
   im Bericht ausdrücklich als Interpretation — nicht raten, nicht als Tatsache führen.
2. Findest du eine lesbare Quelle (Datenblatt, Broschüre, Normseite), liest du die tragende Angabe dort nach:
   Dokumente mit `{dokument_werkzeug}` (noch {dokument_rest} Aufrufe frei), Webseiten per Web-Abruf.
3. Ergänze den Bericht an genau diesen Stellen: belegter Wert mit Quelle und Seite, oder die Lücke bleibt —
   dann mit dem Vermerk, wonach gesucht wurde und was gefunden bzw. nicht gefunden wurde. Alles andere im
   Bericht bleibt unverändert.

{ziel}

Dies ist die einzige Nachkontrolle; danach wird der Bericht abgegeben."""


# ---------------------------------------------------------------------------
# Checker implementations
# ---------------------------------------------------------------------------

@dataclass
class PruefAntwort:
    daten: Any
    input_tokens: int = 0
    output_tokens: int = 0
    kosten_usd: Optional[float] = None


Pruefer = Callable[[str, str], Awaitable[PruefAntwort]]


def pruef_kosten_usd(input_tokens: int, output_tokens: int) -> Optional[float]:
    try:
        from src.pricing import cost_usd
        return cost_usd(PRUEF_MODELL, input_tokens, output_tokens)
    except KeyError as e:
        logger.error(f"luecken: kein Preis für {PRUEF_MODELL} — Prüfkosten fehlen in der Buchung: {e}")
        return None


def messages_api_pruefer(
    api_key: Optional[str],
    client: Optional[httpx.AsyncClient] = None,
    *,
    url: str = "https://api.anthropic.com/v1/messages",
    timeout_seconds: float = 120.0,
) -> Pruefer:
    """Cloud path: a forced tool call, so the answer IS the schema."""

    async def pruefe(system: str, anfrage: str) -> PruefAntwort:
        if not api_key:
            raise LueckenPruefFehler("kein API-Schlüssel für den Prüfaufruf (RESEARCH_CLOUD_API_KEY)")
        body = {
            "model": PRUEF_MODELL,
            "max_tokens": 4000,
            "system": system,
            "tools": [{
                "name": PRUEF_WERKZEUG,
                "description": "Meldet die offenen Punkte des Berichts.",
                "input_schema": LUECKEN_SCHEMA,
            }],
            "tool_choice": {"type": "tool", "name": PRUEF_WERKZEUG},
            "messages": [{"role": "user", "content": anfrage}],
        }
        headers = {"Content-Type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"}
        eigener = client is None
        c = client or httpx.AsyncClient(timeout=timeout_seconds)
        try:
            antwort = await c.post(url, headers=headers, json=body)
        except httpx.HTTPError as e:
            raise LueckenPruefFehler(f"Prüfaufruf nicht erreichbar: {type(e).__name__}: {e}") from e
        finally:
            if eigener:
                await c.aclose()
        if antwort.status_code != 200:
            raise LueckenPruefFehler(f"Prüfaufruf HTTP {antwort.status_code}: {antwort.text[:300]}")
        roh = antwort.json()
        usage = roh.get("usage") or {}
        ein, aus = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        bloecke = [b for b in roh.get("content") or [] if b.get("type") == "tool_use" and b.get("name") == PRUEF_WERKZEUG]
        if not bloecke:
            raise LueckenPruefFehler(f"Prüfantwort ohne {PRUEF_WERKZEUG}-Aufruf (stop_reason={roh.get('stop_reason')})")
        return PruefAntwort(bloecke[0].get("input"), ein, aus, pruef_kosten_usd(ein, aus))

    return pruefe


POOL_JSON_HINWEIS = (
    "\n\nAntworte ausschließlich mit EINEM JSON-Objekt nach diesem Schema, ohne Text davor oder danach:\n"
    + json.dumps(LUECKEN_SCHEMA, ensure_ascii=False)
)

# (system, user prompt) -> (answer text, input_tokens, output_tokens)
TextLauf = Callable[[str, str], Awaitable["tuple[str, int, int]"]]


def pool_pruefer(lauf: TextLauf) -> Pruefer:
    """Pool path: the same checker over the CLI (no API key on the pool path).
    There is no forced tool call there, so the JSON is asked for in the prompt
    and validated against the same model (lies_befund) — outside the schema is
    a named error, never "no gaps"."""

    async def pruefe(system: str, anfrage: str) -> PruefAntwort:
        text, ein, aus = await lauf(system, anfrage + POOL_JSON_HINWEIS)
        if not (text or "").strip():
            raise LueckenPruefFehler("Prüflauf im Pool lieferte keinen Text")
        return PruefAntwort(text, ein, aus, pruef_kosten_usd(ein, aus))

    return pruefe


# ---------------------------------------------------------------------------
# Per-run state
# ---------------------------------------------------------------------------

@dataclass
class Nachkontrolle:
    """One per research run. Holds the decision and everything provider_meta shows."""

    pruefer: Pruefer
    luecken_gemeldet: Optional[int] = None
    luecken_ohne_suche: Optional[int] = None
    rueckrunde: bool = False
    rueckrunde_grund: Optional[str] = None
    rueckrunde_fehler: Optional[str] = None
    luecken_nach_rueckrunde: Optional[int] = None
    luecken_ohne_suche_nach_rueckrunde: Optional[int] = None
    pruef_fehler: List[str] = field(default_factory=list)
    pruef_input_tokens: int = 0
    pruef_output_tokens: int = 0
    pruef_kosten_usd: float = 0.0
    pruef_kosten_fehlen: int = 0
    rueckrunde_usage: Dict[str, Any] = field(default_factory=dict)
    _entschieden: bool = False

    async def _pruefe(self, bericht: str, protokoll: List[WerkzeugAufruf], phase: str) -> Optional[LueckenBefund]:
        try:
            antwort = await self.pruefer(PRUEF_SYSTEM, baue_pruefanfrage(bericht, protokoll))
            self.pruef_input_tokens += antwort.input_tokens
            self.pruef_output_tokens += antwort.output_tokens
            if antwort.kosten_usd is None:
                self.pruef_kosten_fehlen += 1
            else:
                self.pruef_kosten_usd += antwort.kosten_usd
            return lies_befund(antwort.daten)
        except Exception as e:  # named, logged, never read as "no gaps"
            meldung = f"{phase}: {type(e).__name__}: {e}"
            self.pruef_fehler.append(meldung[:500])
            logger.error(f"research Lücken-Nachkontrolle: Prüfung fehlgeschlagen ({meldung})")
            return None

    async def entscheide(
        self, bericht: str, protokoll: List[WerkzeugAufruf], *, perplexity_rest: int
    ) -> Optional[List[Luecke]]:
        """Return the gaps for the return round, or None. Callable once per run."""
        if self._entschieden:
            raise RuntimeError("Lücken-Nachkontrolle: zweite Entscheidung im selben Lauf — es gibt nur eine Rückrunde")
        self._entschieden = True
        if not (bericht or "").strip():
            self.rueckrunde_grund = "kein_bericht"
            return None
        befund = await self._pruefe(bericht, protokoll, "vorher")
        if befund is None:
            self.rueckrunde_grund = "pruefung_fehlgeschlagen"
            return None
        offene = offene_luecken(befund)
        self.luecken_gemeldet = len(befund.luecken)
        self.luecken_ohne_suche = len(offene)
        if not offene:
            self.rueckrunde_grund = "keine_offenen_luecken"
            return None
        if perplexity_rest <= 0:
            self.rueckrunde_grund = "budget_erschoepft"
            logger.warning(
                f"research Lücken-Nachkontrolle: {len(offene)} Lücke(n) ohne Suche, aber das "
                "perplexity_search-Budget ist erschöpft — keine Rückrunde"
            )
            return None
        self.rueckrunde = True
        self.rueckrunde_grund = "luecken_ohne_suche"
        logger.info(
            f"research Lücken-Nachkontrolle: Rückrunde für {len(offene)} von {len(befund.luecken)} Lücke(n)"
        )
        return offene

    async def nachmessen(self, bericht: str, protokoll: List[WerkzeugAufruf]) -> None:
        """After the return round: count again. Measures only, triggers nothing."""
        befund = await self._pruefe(bericht, protokoll, "nachher")
        if befund is not None:
            self.luecken_nach_rueckrunde = len(befund.luecken)
            self.luecken_ohne_suche_nach_rueckrunde = len(offene_luecken(befund))

    def as_meta(self) -> Dict[str, Any]:
        meta: Dict[str, Any] = {
            "luecken_nachkontrolle": True,
            "luecken_gemeldet": self.luecken_gemeldet,
            "luecken_ohne_suche": self.luecken_ohne_suche,
            "rueckrunde": self.rueckrunde,
            "rueckrunde_grund": self.rueckrunde_grund,
            "luecken_pruef_input_tokens": self.pruef_input_tokens,
            "luecken_pruef_output_tokens": self.pruef_output_tokens,
            "luecken_pruef_kosten_usd": round(self.pruef_kosten_usd, 6),
        }
        if self.pruef_kosten_fehlen:
            meta["luecken_pruef_kosten_fehlen"] = self.pruef_kosten_fehlen
        if self.pruef_fehler:
            meta["luecken_pruef_fehler"] = self.pruef_fehler
        if self.rueckrunde:
            meta["luecken_nach_rueckrunde"] = self.luecken_nach_rueckrunde
            meta["luecken_ohne_suche_nach_rueckrunde"] = self.luecken_ohne_suche_nach_rueckrunde
            meta["rueckrunde_usage"] = self.rueckrunde_usage
        if self.rueckrunde_fehler:
            meta["rueckrunde_fehler"] = self.rueckrunde_fehler
        return meta
