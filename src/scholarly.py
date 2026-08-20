"""OA-Scholarly-Schicht für /v1/research (Weg A: deterministische Pre-Retrieval-Injektion).

Holt LEGALE Open-Access-Fachinhalte (OpenAlex-Abstracts + CORE-Volltext) zur Recherche-Query und
formatiert sie als Kontext-Block, der VOR den SuperClaude-Research-Prompt gehängt wird. Damit stützt
sich der Agent auf peer-reviewte Primärliteratur (inkl. der darin zitierten Normwerte VDI/DIN/ÖNORM/EN)
statt nur auf Snippets/Shop-Seiten. Kein Sci-Hub, kein Paywall-Bypass.

Design (bewusst dependency-frei + schnell, für die geteilte Bridge):
  - OpenAlex: Discovery + `abstract_inverted_index` → rekonstruierter Abstract (immer da, schnell)
  - CORE:     `fullText` direkt aus dem JSON (57M Repository-/Dissertations-Volltexte), best-effort
              (keyless rate-limitet → 429 wird fail-soft übersprungen)
  - KEINE PDF-Downloads/-Extraktion (Worker haben keine PDF-Lib; Downloads waren der Timeout-Treiber)
  - Alle Netz-Calls parallel + hart budgetiert; komplett fail-soft → bei jedem Fehler leerer String,
    Research läuft unverändert weiter.

Aktivierung nur wenn BRIDGE_SCHOLARLY_ENABLED=true UND request.research_mode=="academic".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger("scholarly")

MAILTO = os.getenv("SCHOLARLY_MAILTO", "office@werking.tools")
UA = {"User-Agent": f"werkingflow-scholarly (mailto:{MAILTO})"}
_HTTP_TIMEOUT = 15         # OpenAlex: kleine JSON-Antworten, 15s ist reichlich
# CORE liefert `fullText` INLINE: gemessen 1.2-2.3 MB und 7-12s je Query (20.08.2026, 8x parallel
# ab der dev-Bridge), Ausreisser bis 12s. Mit 15s lag der Abbruch mitten in der Normalverteilung —
# jeder CORE-Ausschlag wurde zum ReadTimeout, ohne dass CORE defekt war. Bleibt im 90s-Gesamtbudget.
_CORE_HTTP_TIMEOUT = 30
_MAX_ENTRIES = 12          # so viele Quellen maximal in den Kontext-Block
_EXCERPT_CHARS = 1600      # Zeichen je Quelle (CORE-Volltext gekürzt)

_PLANNER_SYS = (
    "Du bist ein Recherche-Query-Planner für die wissenschaftliche Literatur-API OpenAlex. "
    "Aus einem Energie-Ingenieur-Research-Prompt erzeugst du kurze SUCHBEGRIFFE (Keyword-Phrasen, "
    "keine Fragen/Sätze). Regeln: (a) mische ENGLISCH (internationale Journals) und DEUTSCH "
    "(Dissertationen/Forschungsberichte, wo ÖNORM/VDI/OIB zitiert werden); (b) für jede im Prompt "
    "genannte Norm eine eigene norm-verankerte Query; (c) für jedes technische Thema eine Konzept-Query. "
    "Antworte AUSSCHLIESSLICH als JSON-Array von Strings (8-14 Queries), nichts sonst."
)


def scholarly_enabled(research_mode: Optional[str]) -> bool:
    """Master-Kill-Switch (env) UND per-call opt-in. Default: aus."""
    return (
        os.getenv("BRIDGE_SCHOLARLY_ENABLED", "false").lower() == "true"
        and (research_mode or "standard") == "academic"
    )


def _get(url: str, **kw) -> requests.Response:
    kw.setdefault("headers", {}).update(UA)
    kw.setdefault("timeout", _HTTP_TIMEOUT)
    return requests.get(url, **kw)


def _reconstruct_abstract(inv: Optional[Dict[str, List[int]]]) -> str:
    """OpenAlex liefert Abstracts als inverted index {wort: [positionen]} → Fließtext."""
    if not inv:
        return ""
    positions: List[tuple] = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)


def _openalex(query: str, n: int) -> List[Dict[str, Any]]:
    r = _get("https://api.openalex.org/works",
             params={"search": query, "per-page": n, "mailto": MAILTO,
                     "select": "title,publication_year,doi,open_access,abstract_inverted_index"})
    if r.status_code != 200:
        return []
    out = []
    for w in r.json().get("results", []):
        oa = w.get("open_access") or {}
        abstract = _reconstruct_abstract(w.get("abstract_inverted_index"))
        out.append({
            "title": (w.get("title") or "")[:200],
            "year": w.get("publication_year"),
            "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
            "url": oa.get("oa_url"),
            "text": abstract,
            "kind": "abstract",
            "source": "OpenAlex",
        })
    return out


def _core(query: str, n: int) -> List[Dict[str, Any]]:
    # CORE keyless (freier Rate-Limit, 429 → fail-soft []). Trailing-Slash nötig. fullText kommt
    # direkt im JSON — das ist der eigentliche Mehrwert (echter Volltext ohne PDF-Download).
    key = os.getenv("CORE_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        r = _get("https://api.core.ac.uk/v3/search/works/", headers=headers,
                 params={"q": query, "limit": n}, timeout=_CORE_HTTP_TIMEOUT)
        if r.status_code != 200:
            if r.status_code == 429:
                # Keyless-Rate-Limit: Schicht degradiert STILL auf OpenAlex-Abstracts —
                # unter Last sichtbar machen (CORE_API_KEY hebt das Limit).
                logger.warning("research-cloud: CORE rate-limited (429, keyless) — Volltext-Anteil degradiert auf Abstracts")
            return []
        out = []
        for w in r.json().get("results", []):
            ft = w.get("fullText") or ""
            out.append({
                "title": (w.get("title") or "")[:200],
                "year": w.get("yearPublished"),
                "doi": w.get("doi"),
                "url": w.get("downloadUrl") or (w.get("links") or [{}])[0].get("url") if w.get("links") else w.get("downloadUrl"),
                "text": ft,
                "kind": "fulltext" if ft else "meta",
                "source": "CORE",
            })
        return out
    except (requests.RequestException, ValueError) as e:
        # Ebene beachten: _core() laeuft EINMAL PRO GEPLANTER SUCHQUERY (8-14 parallel je Lauf).
        # Eine ausgefallene Teil-Query heisst NICHT, dass der Lauf Volltexte verliert — die
        # anderen Queries liefern weiter. Deshalb INFO und bewusst OHNE Alarm-Marker: gemeldet
        # wird die Lauf-Ebene (siehe _retrieve_and_format), nicht jede einzelne Query. Sichtbar
        # bleibt es trotzdem — nur eben nicht als Alarm (Messung 20.08.2026: 354 solcher Zeilen
        # in 3 Wochen, davon fuehrten die wenigsten zu einem Lauf ohne Volltext).
        logger.info(f"CORE-Teilquery ohne Ergebnis ({type(e).__name__}: {e}) — restliche Queries laufen weiter")
        return []


def _retrieve_and_format(queries: List[str], per_query: int) -> str:
    """Parallel je Query OpenAlex+CORE, dedupe, Kontext-Block bauen. Keine PDF-Downloads."""
    papers: List[Dict[str, Any]] = []
    seen = set()

    def fetch(q: str) -> List[Dict[str, Any]]:
        res = []
        try:
            res += _openalex(q, per_query)
        except requests.RequestException:
            pass
        res += _core(q, per_query)
        return res

    # Alle Query-Fetches parallel (Discovery ist schnell; kein Download)
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(queries)))) as pool:
        futs = [pool.submit(fetch, q) for q in queries]
        for f in as_completed(futs):
            try:
                hits = f.result()
            except Exception:
                continue
            for p in hits:
                key = (p.get("doi") or "").lower() or p.get("title", "").lower()[:60]
                if key and key not in seen and p.get("text"):
                    seen.add(key)
                    papers.append(p)

    if not papers:
        logger.warning("OA-Retrieval ohne verwertbare Quellen (0 Treffer mit Text) — Kontext-Block entfällt")
        return ""

    # Volltext (CORE) zuerst, dann Abstracts — nach Textlänge
    papers.sort(key=lambda p: (p["kind"] != "fulltext", -len(p.get("text", ""))))
    fulltext_n = sum(1 for p in papers[:_MAX_ENTRIES] if p["kind"] == "fulltext")
    _msg = f"OA-Kontext: {min(len(papers), _MAX_ENTRIES)} Quellen, davon {fulltext_n} Volltext"
    if fulltext_n == 0:
        # DAS ist die echte Degradierung: der Lauf stuetzt sich nur noch auf Abstracts.
        # Genau diese Zeile ueberwacht research-cloud-monitor.py (Marker "davon 0 Volltext").
        logger.warning(f"research-cloud: {_msg} — Recherche degradiert auf Abstracts")
    else:
        logger.info(_msg)
    blocks = []
    for p in papers[:_MAX_ENTRIES]:
        excerpt = re.sub(r"\s+", " ", p["text"]).strip()[:_EXCERPT_CHARS]
        doi = f" · doi:{p['doi']}" if p.get("doi") else ""
        url = f"\nURL: {p['url']}" if p.get("url") else ""
        tag = "Volltext" if p["kind"] == "fulltext" else "Abstract"
        blocks.append(f"### {p['title']} ({p.get('year')}){doi}\nQuelle ({p['source']}, {tag}):{url}\n\n{excerpt}")

    header = (
        "## VERIFIZIERTE OA-LITERATUR (legal via OpenAlex/CORE)\n"
        "Nutze diese peer-reviewten Open-Access-Auszüge als PRIMÄRE, zitierbare Quellen (mit URL/DOI). "
        "Darin zitierte Normwerte (VDI/DIN/ÖNORM/EN) dürfen mit Quellenangabe übernommen werden. "
        "Ergänze offene Web-Suche nur für Herstellerdaten/Tarife/Förderungen, die hier fehlen.\n"
    )
    return header + "\n\n".join(blocks)


async def _plan_queries(query: str, attribution: Optional[Dict[str, Any]], model: Optional[str]) -> List[str]:
    """Destilliert die (evtl. sehr lange) Research-Query in Suchbegriffe. Fail-soft → [query[:180]]."""
    fallback = [query.strip()[:180]] if query.strip() else []
    try:
        from src.jobs.executors import _self_post_json
        body = {
            "model": model or "claude-sonnet-4-5-20250929",
            "max_tokens": 1000,
            "messages": [
                {"role": "system", "content": _PLANNER_SYS},
                {"role": "user", "content": query[:9000]},
            ],
        }
        data = await _self_post_json("/v1/chat/completions", body, attribution, 60.0)
        content = data["choices"][0]["message"]["content"]
        m = re.search(r"\[.*\]", content, re.DOTALL)
        queries = [q.strip() for q in json.loads(m.group(0)) if isinstance(q, str) and q.strip()]
        return queries[:14] or fallback
    except Exception as e:
        logger.warning(f"scholarly planner fiel auf raw-query zurück: {type(e).__name__}: {e}")
        return fallback


async def build_oa_context(
    query: str,
    *,
    attribution: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    per_query: int = 12,
    overall_timeout: float = 90.0,
) -> str:
    """Liefert den OA-Literatur-Kontext-Block (Markdown) oder '' (fail-soft/timeout)."""
    try:
        async def _run() -> str:
            queries = await _plan_queries(query, attribution, model)
            if not queries:
                return ""
            return await asyncio.to_thread(_retrieve_and_format, queries, per_query)

        return await asyncio.wait_for(_run(), timeout=overall_timeout)
    except Exception as e:  # inkl. TimeoutError — Research darf NIE an dieser Schicht scheitern
        logger.warning(f"OA-Schicht übersprungen (fail-soft): {type(e).__name__}: {e}")
        return ""
