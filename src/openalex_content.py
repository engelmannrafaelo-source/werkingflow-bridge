"""OpenAlex-Content-API: echter Volltext (GROBID-XML) als Quelle für die OA-Scholarly-Schicht.

Ergänzt (nicht ersetzt) `src/scholarly.py`'s CORE-Anbindung um eine zweite Volltextquelle:
`content.openalex.org/works/{id}.grobid-xml` liefert GROBID-geparstes TEI-XML für Werke, die
OpenAlex bereits indiziert hat — Auth per `?api_key=`, ohne Key 401.

Format-Entscheidung (gemessen 03.09.2026, Testwerk W2032996814 + W3038067977):
  - GROBID-XML: 8.8 KB gzip, stdlib-Parse (gzip + xml.etree), sauberer Klartext.
  - PDF: 1.8 MB, bräuchte eine neue PDF-Lib im Worker-Image (das Worker-Image installiert
    poetry OHNE die `pdf`-Extras — siehe docker/Dockerfile.worker — Docling ist dort nicht
    vorhanden). GROBID-XML ist die dependency-freie Wahl und war Rafaels Vermutung ("weniger
    Bytes, Struktur") — deshalb ist NUR dieses Format implementiert. Ein PDF-Pfad würde eine
    neue Dependency ins schlanke Worker-Image ziehen; das ist eine bewusste Auslassung, keine
    vergessene Funktion.

Credits: JEDER Abruf (unabhängig vom Format) kostet 100 Credits (gemessen: x-ratelimit-limit
10000, x-ratelimit-limit-usd 1, x-ratelimit-cost-usd 0.01 pro Abruf) — macht ~100 Abrufe/Tag
für den GESAMTEN geteilten `OPENALEX_API_KEY` (Vision/Research/Bridge teilen sich Keys, siehe
Memory `reference_anthropic_api_key_inventory` fürs Analogon bei Anthropic-Keys). Deshalb NIE
ungedeckelt pro Treffer abrufen — `fetch_fulltexts()` nimmt einen harten `max_fetches`-Deckel,
Default konservativ auf `DEFAULT_MAX_FETCHES`.

Sprache: GROBID extrahiert PDFs unabhängig von der Sprache — bei nicht-englischen/-deutschen
Volltexten (z.B. koreanisches Paper mit GROBID-Fehlparse der Autorennamen, live beobachtet)
landet sonst Zeichensalat im Report. `_detect_language()` ist eine dependency-freie
Stopwort-/Skript-Heuristik (kein langdetect im Worker-Image); erkennt sie weder Englisch noch
Deutsch, fällt die Rückgabe auf den (stets englischen) OpenAlex-Abstract zurück und markiert
`degraded_to_abstract=True` — nie stiller Zeichensalat, nie ein Platzhaltertext.

Fail-soft wie der Rest der OA-Schicht: jeder Fehler (kein Key, 404 kein Volltext indiziert,
Netzfehler, Parse-Fehler) → None, niemals eine Exception nach außen, niemals Platzhaltertext
als Inhalt (das war der CORE-Fehler, den diese Schicht nicht wiederholen soll).
"""
from __future__ import annotations

import gzip
import logging
import os
import re
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

import requests

logger = logging.getLogger("openalex_content")

MAILTO = os.getenv("SCHOLARLY_MAILTO", "office@werking.tools")
UA = {"User-Agent": f"werkingflow-scholarly (mailto:{MAILTO})"}

_CONTENT_BASE = "https://content.openalex.org/works"
_HTTP_TIMEOUT = 30  # GROBID-XML ist klein (KB-Bereich gzip) — 30s ist reichlich, nicht der CORE-PDF-Fall
_TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}

# 100 Credits/Abruf, ~10000 Credits (= $1) Tagesbudget des GETEILTEN Keys -> ~100 Abrufe/Tag
# für alle Prozesse zusammen. Bewusst klein: ein einzelner Research-Lauf plant 8-14 Queries,
# ein ungedeckelter Abruf pro Treffer würde das Tagesbudget in einem Lauf verbrennen.
DEFAULT_MAX_FETCHES = 3

# Stopwort-Heuristik statt einer neuen Dependency (kein langdetect/fasttext im Worker-Image).
_EN_STOP = {"the", "and", "of", "to", "in", "is", "for", "that", "with", "this", "are",
            "on", "as", "by", "was", "were", "be", "or", "from", "an", "which"}
_DE_STOP = {"der", "die", "und", "das", "nicht", "mit", "ist", "auf", "den", "von",
            "für", "eine", "ein", "wird", "werden", "sich", "im", "des", "dass"}
_MIN_STOP_HITS = 3        # unter dieser Trefferzahl ist die Stichprobe zu klein für eine Aussage
_NON_LATIN_SAMPLE = 2000  # Zeichen, an denen der Nicht-Lateinisch-Anteil gemessen wird
_NON_LATIN_THRESHOLD = 0.15


def _detect_language(text: str) -> str:
    """Grobe EN/DE-Erkennung. Rückgabe 'en'|'de'|'other' — 'other' fasst jede dritte Sprache
    (CJK, Kyrillisch, Arabisch, aber auch z.B. Französisch/Spanisch) zusammen, weil für BEIDE
    Fälle dieselbe Konsequenz gilt: für den deutschsprachigen Ingenieur-Report nicht verwertbar
    -> auf den Abstract zurückfallen."""
    if not text:
        return "other"
    sample = text[:_NON_LATIN_SAMPLE]
    non_latin = sum(1 for c in sample if not c.isspace() and ord(c) > 0x2000)
    if non_latin / max(1, len(sample.replace(" ", "") or " ")) > _NON_LATIN_THRESHOLD:
        return "other"

    words = re.findall(r"[a-zA-ZäöüÄÖÜß]+", text.lower())[:500]
    en_hits = sum(1 for w in words if w in _EN_STOP)
    de_hits = sum(1 for w in words if w in _DE_STOP)
    if de_hits > en_hits and de_hits >= _MIN_STOP_HITS:
        return "de"
    if en_hits >= _MIN_STOP_HITS:
        return "en"
    return "other"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _clean(el: ET.Element) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _extract_tei_text(xml_bytes: bytes) -> Dict[str, str]:
    """TEI-XML (GROBID) -> {'abstract','body'} Klartext, Tags/Referenzen entfernt.

    GROBID-Antworten sind NICHT strukturell einheitlich (gemessen 03.09.2026, zwei echte
    Werke): meistens `<text><body><div>...</div></body><back>...</back></text>`, manchmal
    liegen die `<div>`-Absätze aber DIREKT unter `<text>` ohne `<body>`-Wrapper. Ein reines
    `.//tei:body`-XPath liefert im zweiten Fall still leer -> Text fiele fälschlich auf den
    Abstract zurück, obwohl 200 KB echter Artikeltext da wären. Deshalb: `<body>` bevorzugen,
    sonst alle Direktkinder von `<text>` außer `<front>`/`<back>` (Titelblatt/Referenzen).
    """
    root = ET.fromstring(xml_bytes)

    abstract_el = root.find(".//tei:abstract", _TEI_NS)
    abstract = _clean(abstract_el) if abstract_el is not None else ""

    text_el = root.find(".//tei:text", _TEI_NS)
    body = ""
    if text_el is not None:
        body_el = text_el.find("tei:body", _TEI_NS)
        if body_el is not None:
            body = _clean(body_el)
        else:
            body = re.sub(r"\s+", " ", " ".join(
                _clean(child) for child in text_el if _local_name(child.tag) not in ("front", "back")
            )).strip()

    return {"abstract": abstract, "body": body}


def fetch_fulltext(work_id: str, api_key: Optional[str], *,
                    timeout: float = _HTTP_TIMEOUT) -> Optional[Dict[str, Any]]:
    """Volltext EINES Werks über die OpenAlex-Content-API (GROBID-XML). None = kein Volltext
    verfügbar oder Fehler — fail-soft, wirft nie. `work_id` akzeptiert 'W123' und die volle
    'https://openalex.org/W123'-Form (wie sie aus der Search-API kommt)."""
    if not api_key:
        logger.warning("openalex-content: kein OPENALEX_API_KEY gesetzt — Content-API übersprungen")
        return None

    wid = work_id.rstrip("/").rsplit("/", 1)[-1]
    url = f"{_CONTENT_BASE}/{wid}.grobid-xml"
    try:
        r = requests.get(url, params={"api_key": api_key}, headers=UA, timeout=timeout)
    except requests.RequestException as e:
        logger.info(f"openalex-content: {wid} Netzfehler ({type(e).__name__}) — übersprungen")
        return None

    if r.status_code == 404:
        logger.info(f"openalex-content: {wid} kein Volltext im Content-Index (404)")
        return None
    if r.status_code == 401:
        logger.warning("openalex-content: 401 — OPENALEX_API_KEY ungültig/abgelaufen, Content-API übersprungen")
        return None
    if r.status_code != 200:
        logger.warning(f"openalex-content: {wid} unerwarteter Status {r.status_code} — übersprungen")
        return None

    credits_used = r.headers.get("x-ratelimit-credits-used")
    remaining = r.headers.get("x-ratelimit-remaining")

    try:
        xml_bytes = gzip.decompress(r.content)
        parts = _extract_tei_text(xml_bytes)
    except (OSError, ET.ParseError) as e:
        logger.warning(f"openalex-content: {wid} GROBID-XML-Parse-Fehler ({type(e).__name__}: {e}) — übersprungen")
        return None

    body, abstract = parts["body"], parts["abstract"]
    lang = _detect_language(body or abstract)
    use_body = bool(body) and lang in ("en", "de")
    text = body if use_body else abstract
    # degraded = wir HATTEN einen Volltext, verwerfen ihn aber wegen der Sprache (nicht: es gab
    # gar keinen). Beide Fälle landen auf demselben `text`, aber nur der erste ist eine echte
    # Degradierung — unterscheidbar für die spätere scholarly.py-Verdrahtung ("kind": fulltext/abstract).
    degraded = bool(body) and not use_body

    if not text:
        logger.info(f"openalex-content: {wid} kein extrahierbarer Text (weder Volltext noch Abstract) — übersprungen")
        return None

    logger.info(
        f"openalex-content: {wid} {'Volltext' if use_body else 'nur Abstract'} geholt "
        f"(lang={lang}, degraded={degraded}, {credits_used or '?'} Credits verbraucht, "
        f"{remaining or '?'} verbleibend)"
    )
    return {
        "text": text,
        "abstract": abstract,
        "kind": "fulltext" if use_body else "abstract",
        "language": lang,
        "degraded_to_abstract": degraded,
        "credits_used": int(credits_used) if credits_used and credits_used.isdigit() else None,
    }


def fetch_fulltexts(work_ids: List[str], api_key: Optional[str], *,
                     max_fetches: int = DEFAULT_MAX_FETCHES) -> Dict[str, Dict[str, Any]]:
    """Holt Volltexte für höchstens `max_fetches` Werke (Credit-Deckel des geteilten Keys!).
    Reihenfolge von `work_ids` = Priorität des Aufrufers (z.B. Relevanz-Rang zuerst).
    Bricht NICHT bei einem Fehlschlag ab — ruft weiter bis der Deckel erreicht ist."""
    out: Dict[str, Dict[str, Any]] = {}
    if not api_key or max_fetches <= 0:
        return out
    for wid in work_ids[:max_fetches]:
        res = fetch_fulltext(wid, api_key)
        if res:
            out[wid] = res
    return out
