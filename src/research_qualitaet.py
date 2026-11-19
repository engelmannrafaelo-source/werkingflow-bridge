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
* ``domains``: distinct REGISTRABLE domains (eTLD+1) of the read fetches, taken
  from the ToolUse ``input.url`` (a redirect counts as the URL that was asked for);
  subdomains of one site count once (DQ-R2, Review e8511fe6).
* ``hops``: rounds "search(es) -> at least one read fetch"; a search issued
  AFTER a read fetch opens the next round. Only rounds with a read fetch count.
* ``leseurteil`` (DQ-R1): counters only — not-read fetches per reason
  (is_error | umleitung | fehlseite | kurz), fetches without a verdict, and a length
  class per non-error result. They make the read verdict traceable and calibrate
  threshold and markers in the live acceptance; no content leaves the worker.
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
#: Provisional (DQ-R1): WebFetch returns the model's SUMMARY of the page, which can be
#: short on a real read; 300 is confirmed only after the live acceptance R15-R17 reports
#: the length classes below.
MIN_ZEICHEN_GELESEN = 300
#: WebFetch's own redirect notice only (DQ-R1: "weiterleitung" hit real German content
#: like "Anrufweiterleitung einrichten"; "redirected to" hits ordinary prose). Checked
#: at the START of the result, where the tool puts its notice.
_UMLEITUNG = ("redirect detected",)
_UMLEITUNG_KOPF = 120
#: Length classes for calibration (counters only), upper bounds exclusive.
LAENGEN_KLASSEN = ((150, "unter_150"), (300, "150_299"), (1000, "300_999"))
LAENGE_GROSS = "ab_1000"
GRUENDE_NICHT_GELESEN = ("is_error", "umleitung", "fehlseite", "kurz")
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


def _laengen_klasse(n: int) -> str:
    for grenze, name in LAENGEN_KLASSEN:
        if n < grenze:
            return name
    return LAENGE_GROSS


def ergebnis_eintrag(block: Any) -> Optional[Dict[str, Any]]:
    """Verdict of ONE ToolResult block: {tool_use_id, is_error, gelesen, grund, laenge}.
    grund (DQ-R1): None when read, else the FIRST failing check in the order
    is_error, umleitung, fehlseite, kurz. laenge is a class, never the text. The text
    is read here and dropped — nothing of the content leaves this function."""
    tid = _attr(block, "tool_use_id")
    if not tid:
        return None
    is_error = bool(_attr(block, "is_error"))
    text = _ergebnis_text(_attr(block, "content"))
    rumpf = text.strip()
    kopf = rumpf[:600].lower()
    grund: Optional[str]
    if is_error:
        grund = "is_error"
    elif any(m in kopf[:_UMLEITUNG_KOPF] for m in _UMLEITUNG):
        grund = "umleitung"
    elif any(m in kopf for m in _FEHLSEITE):
        grund = "fehlseite"
    elif len(rumpf) < MIN_ZEICHEN_GELESEN:
        grund = "kurz"
    else:
        grund = None
    return {"tool_use_id": str(tid), "is_error": is_error, "gelesen": grund is None,
            "grund": grund, "laenge": None if is_error else _laengen_klasse(len(rumpf))}


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


#: Second-level public suffixes (DQ-R2): without a public-suffix list the registrable
#: domain is the last two labels, or the last three under one of these.
_SUFFIX_ZWEISTUFIG = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "ltd.uk", "plc.uk", "me.uk", "net.uk", "nhs.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au",
    "co.at", "or.at", "ac.at", "gv.at",
    "co.nz", "org.nz", "govt.nz", "ac.nz",
    "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp",
    "com.br", "org.br", "gov.br", "com.cn", "org.cn", "gov.cn", "edu.cn",
    "co.za", "org.za", "gov.za", "co.in", "org.in", "gov.in", "ac.in",
    "com.tr", "gov.tr", "com.mx", "gob.mx", "com.ar", "com.sg", "gov.sg", "com.hk",
    "co.kr", "or.kr", "go.kr", "com.tw", "gov.tw", "co.il", "gov.il", "ac.il",
    "eu.int", "europa.eu",
})


def _host(url: Any) -> Optional[str]:
    try:
        h = urlsplit(str(url or "")).hostname
    except ValueError:
        return None
    if not h:
        return None
    return h.rstrip(".").lower() or None


def _domain(url: Any) -> Optional[str]:
    """Registrable domain (eTLD+1) of a URL; IP addresses stay whole (DQ-R2)."""
    h = _host(url)
    if not h:
        return None
    if ":" in h or h.replace(".", "").isdigit():
        return h
    labels = h.split(".")
    if len(labels) <= 2:
        return h
    n = 3 if ".".join(labels[-2:]) in _SUFFIX_ZWEISTUFIG else 2
    return ".".join(labels[-n:])


@dataclass
class Messung:
    suchen: int = 0
    fetches_versucht: int = 0
    fetches_ok: Optional[int] = 0
    quellen_gelesen: Optional[int] = 0
    domains: Optional[int] = 0
    hops: Optional[int] = 0
    gruende_unbekannt: List[str] = field(default_factory=list)
    #: DQ-R1 counters (no content): not-read fetches per reason, fetches whose result
    #: never arrived, length class per non-error result.
    nicht_gelesen: Dict[str, int] = field(default_factory=dict)
    ohne_urteil: int = 0
    laengen: Dict[str, int] = field(default_factory=dict)

    def leseurteil(self) -> Dict[str, Any]:
        return {
            "nicht_gelesen": {g: self.nicht_gelesen.get(g, 0) for g in GRUENDE_NICHT_GELESEN},
            "ohne_urteil": self.ohne_urteil,
            "laengen": dict(self.laengen),
            "schwelle_zeichen": MIN_ZEICHEN_GELESEN,
        }

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
            if e is None:
                m.ohne_urteil += 1
            else:
                if e.get("laenge"):
                    m.laengen[e["laenge"]] = m.laengen.get(e["laenge"], 0) + 1
                if not e["gelesen"] and e.get("grund"):
                    m.nicht_gelesen[e["grund"]] = m.nicht_gelesen.get(e["grund"], 0) + 1
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
    m.domains = len({d for d in (_domain(u) for u in gelesen_urls) if d})
    m.hops = runden_mit_lesen
    return m


def bewerten(messung: Messung, depth: Optional[str], *, pflicht: bool, pfad: str) -> Dict[str, Any]:
    """The ``qualitaet`` block. status: nicht_anwendbar | erfuellt | teilweise | unbekannt."""
    soll = UNTERGRENZEN.get(depth or "")
    block: Dict[str, Any] = {
        "pfad": pfad, "tiefe": depth, "pflicht": bool(pflicht),
        "erreicht": messung.als_dict(), "soll": dict(soll) if soll else None,
        "messart": ("ToolUse + ToolResult (is_error + Inhaltssignal), Domains = registrierbare "
                    "Domains (eTLD+1) und Hops aus angefragten URLs"),
        "leseurteil": None if messung.fetches_ok is None else messung.leseurteil(),
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
