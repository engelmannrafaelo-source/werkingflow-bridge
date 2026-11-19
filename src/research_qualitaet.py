"""Research quality: real tool counts, lower bounds for deep/exhaustive, a visible status.

Befund 19.11.2026 (Codex 01a1f332, Mac-Lauf 81e4cae3): ``depth=deep`` answered
``success`` with 0 WebFetch in three of five runs; the pool response carried no
search/fetch counters at all; the protocol named only ceilings. This module
measures what a run actually did — from the CLI's ToolUse blocks and the
matching ToolResult flags — and judges it against lower bounds.

Measurement (DQ-1, Kanzler 19.11.: variant (a) plus a content signal):
* ``suchen`` / ``fetches_versucht``: ToolUse blocks (WebSearch, WebFetch,
  fetch_document), never the model's own report.
* a fetch counts as **read** (``fetches_ok``) only if its ToolResult is not
  ``is_error`` AND carries a content signal judged here in the worker: enough
  text, no redirect notice, no error/not-found page. The content itself is
  never kept or passed on — only the verdict (see ``ergebnis_eintrag``).
* ``domains``: distinct hosts of the read fetches, taken from the ToolUse
  ``input.url`` (a redirect counts as the URL that was asked for).
* ``hops``: rounds "search(es) -> at least one read fetch"; a search issued
  AFTER a read fetch opens the next round. Only rounds with a read fetch count.
Unknown stays ``None`` with a reason — never 0, and under the quality duty never
a pass (DQ-5).

Lower bounds live HERE, outside ``research_vertrag.obergrenzen()`` and
``capabilities()``: they must not change the contract version (DQ, Kanzler).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

#: Lower bounds per depth (Kanzler 19.11.2026). quick/standard: none.
UNTERGRENZEN: Dict[str, Dict[str, int]] = {
    "deep": {"fetches_ok": 5, "domains": 3, "hops": 2},
    "exhaustive": {"fetches_ok": 8, "domains": 5, "hops": 3},
}

#: Content signal for a read page: shorter extractions are no read source.
MIN_ZEICHEN_GELESEN = 300
_UMLEITUNG = ("redirect detected", "redirected to", "the url redirected", "weiterleitung")
_FEHLSEITE = ("404 not found", "page not found", "error 404", "403 forbidden", "access denied",
              "failed to fetch", "unable to fetch", "could not fetch")

_SUCHE = {"WebSearch"}
_FETCH = {"WebFetch"}


def _ist_fetch(name: Optional[str]) -> bool:
    return bool(name) and (name in _FETCH or str(name).endswith("fetch_document"))


def _ist_suche(name: Optional[str]) -> bool:
    return bool(name) and (name in _SUCHE or str(name).endswith("perplexity_search"))


def _attr(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _ergebnis_text(inhalt: Any) -> str:
    if isinstance(inhalt, str):
        return inhalt
    teile: List[str] = []
    for b in inhalt or []:
        t = _attr(b, "text")
        if isinstance(t, str):
            teile.append(t)
    return "\n".join(teile)


def ergebnis_eintrag(block: Any) -> Optional[Dict[str, Any]]:
    """Verdict of ONE ToolResult block: {tool_use_id, is_error, gelesen}. The text is
    read here and dropped — nothing of the content leaves this function."""
    tid = _attr(block, "tool_use_id")
    if not tid:
        return None
    is_error = bool(_attr(block, "is_error"))
    text = _ergebnis_text(_attr(block, "content"))
    kopf = text[:600].lower()
    gelesen = (
        not is_error
        and len(text.strip()) >= MIN_ZEICHEN_GELESEN
        and not any(m in kopf for m in _UMLEITUNG)
        and not any(m in kopf for m in _FEHLSEITE)
    )
    return {"tool_use_id": str(tid), "is_error": is_error, "gelesen": gelesen}


def ergebnisse_aus_turn(message: Any) -> List[Dict[str, Any]]:
    """All ToolResult verdicts of one user turn (SDK UserMessage or dict shape)."""
    inhalt = _attr(message, "content")
    if not isinstance(inhalt, list):
        return []
    out = []
    for block in inhalt:
        if type(block).__name__ == "ToolResultBlock" or _attr(block, "type") == "tool_result":
            e = ergebnis_eintrag(block)
            if e is not None:
                out.append(e)
    return out


def _host(url: Any) -> Optional[str]:
    try:
        h = urlsplit(str(url or "")).hostname
    except ValueError:
        return None
    if not h:
        return None
    return h[4:] if h.startswith("www.") else h


@dataclass
class Messung:
    suchen: int = 0
    fetches_versucht: int = 0
    fetches_ok: Optional[int] = 0
    quellen_gelesen: Optional[int] = 0
    domains: Optional[int] = 0
    hops: Optional[int] = 0
    gruende_unbekannt: List[str] = field(default_factory=list)

    def als_dict(self) -> Dict[str, Any]:
        return {
            "suchen": self.suchen, "fetches_versucht": self.fetches_versucht,
            "fetches_ok": self.fetches_ok, "quellen_gelesen": self.quellen_gelesen,
            "domains": self.domains, "hops": self.hops,
        }


def _tool_uses(chunks: Iterable[Any]):
    for chunk in chunks:
        inhalt = _attr(chunk, "content")
        if not isinstance(inhalt, list):
            continue
        for block in inhalt:
            name = _attr(block, "name")
            if name is None:
                continue
            yield block


def messen(chunks: Iterable[Any], ergebnisse: Optional[List[Dict[str, Any]]]) -> Messung:
    """Measure one run (all rounds) from its yielded chunks and its own ToolResult list.

    ``ergebnisse`` is None when the run collected no results (other path): then
    the read-dependent values are unknown (None + reason), the attempts still count."""
    m = Messung()
    urteil = {e["tool_use_id"]: e for e in (ergebnisse or [])}
    gelesen_urls: List[str] = []
    runden_mit_lesen = 0
    runde_gelesen = False
    for block in _tool_uses(chunks):
        name = _attr(block, "name")
        eingabe = _attr(block, "input") or {}
        if _ist_suche(name):
            m.suchen += 1
            if runde_gelesen:          # a search after a read fetch opens the next round
                runden_mit_lesen += 1
                runde_gelesen = False
        elif _ist_fetch(name):
            m.fetches_versucht += 1
            e = urteil.get(str(_attr(block, "id") or ""))
            if e is not None and e["gelesen"]:
                url = eingabe.get("url") if isinstance(eingabe, dict) else None
                gelesen_urls.append(str(url or ""))
                runde_gelesen = True
    if runde_gelesen:
        runden_mit_lesen += 1
    if ergebnisse is None:
        m.fetches_ok = m.quellen_gelesen = m.domains = m.hops = None
        m.gruende_unbekannt.append("keine Werkzeugergebnisse auf diesem Weg gesammelt")
        return m
    m.fetches_ok = len(gelesen_urls)
    m.quellen_gelesen = len({u for u in gelesen_urls if u})
    m.domains = len({h for h in (_host(u) for u in gelesen_urls) if h})
    m.hops = runden_mit_lesen
    return m


def bewerten(messung: Messung, depth: Optional[str], *, pflicht: bool, pfad: str) -> Dict[str, Any]:
    """The ``qualitaet`` block. status: nicht_anwendbar | erfuellt | teilweise | unbekannt."""
    soll = UNTERGRENZEN.get(depth or "")
    block: Dict[str, Any] = {
        "pfad": pfad, "tiefe": depth, "pflicht": bool(pflicht),
        "erreicht": messung.als_dict(), "soll": dict(soll) if soll else None,
        "messart": "ToolUse + ToolResult (is_error + Inhaltssignal), Domains/Hops aus angefragten URLs",
    }
    if not soll:
        block.update(status="nicht_anwendbar", grund=None)
        return block
    fehlend = []
    unbekannt = []
    for k, mindest in soll.items():
        wert = getattr(messung, k)
        if wert is None:
            unbekannt.append(k)
        elif wert < mindest:
            fehlend.append(f"{k} {wert} < {mindest}")
    if unbekannt:
        block.update(status="unbekannt", grund=(
            f"nicht messbar auf Pfad {pfad}: {', '.join(unbekannt)}"
            + (f" ({'; '.join(messung.gruende_unbekannt)})" if messung.gruende_unbekannt else "")
            + (f"; verfehlt: {', '.join(fehlend)}" if fehlend else "")))
    elif fehlend:
        block.update(status="teilweise", grund="Untergrenze verfehlt: " + ", ".join(fehlend))
    else:
        block.update(status="erfuellt", grund=None)
    return block


def top_status(qualitaet: Dict[str, Any]) -> str:
    """Top-level ResearchResponse.status for a successful run (variant B): only under the
    quality duty does a missed or unmeasurable bound turn ``success`` into ``teilweise``."""
    if qualitaet.get("pflicht") and qualitaet.get("status") in ("teilweise", "unbekannt"):
        return "teilweise"
    return "success"


def untergrenzen_fuer(depth: Optional[str], pflicht: bool) -> Optional[Dict[str, int]]:
    """Bounds that bind the PROMPT: only under the quality duty (DQ-2/3: one condition
    for prompt text, follow-up round and status)."""
    if not pflicht:
        return None
    soll = UNTERGRENZEN.get(depth or "")
    return dict(soll) if soll else None


#: Apps that get the curated library (Kanzler 19.11.2026: only werking-energy).
BIBLIOTHEK_APPS = frozenset({"werking-energy"})


def bibliothek_fuer_app(app_id: Optional[str]) -> bool:
    """Stage 1 of the library gate (D4): app assignment. Stage 2 (relevance threshold)
    is OFF for werking-energy until its owner decides (DQ-2) and therefore not built."""
    return (app_id or "").strip().lower() in BIBLIOTHEK_APPS
