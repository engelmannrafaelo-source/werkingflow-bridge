"""OA-Scholarly-Schicht für /v1/research (Weg A: deterministische Pre-Retrieval-Injektion).

Holt LEGALE Open-Access-Volltexte (OpenAlex + Unpaywall + optional CORE) zur Recherche-Query und
formatiert sie als Kontext-Block, der VOR den SuperClaude-Research-Prompt gehängt wird. Damit zitiert
der Agent echten Paper-Volltext (inkl. der darin zitierten Normwerte VDI/DIN/ÖNORM/EN) statt nur
Abstracts/Shop-Seiten. Kein Sci-Hub, kein Paywall-Bypass — was nicht OA ist, bleibt außen vor
(dafür ist WebSearch weiter da).

Sicherheit: komplett fail-soft. Jeder Fehler → leerer String → Research läuft unverändert weiter.
Aktivierung nur wenn BRIDGE_SCHOLARLY_ENABLED=true UND request.research_mode=="academic"
(Default aus → keine Verhaltensänderung für bestehende Caller).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger("scholarly")

MAILTO = os.getenv("SCHOLARLY_MAILTO", "office@werking.tools")
UA = {"User-Agent": f"werkingflow-scholarly (mailto:{MAILTO})"}
_HTTP_TIMEOUT = 25

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


# ---------- HTTP-Helfer (sync; im Threadpool aufgerufen) ----------

def _get(url: str, **kw) -> requests.Response:
    kw.setdefault("headers", {}).update(UA)
    kw.setdefault("timeout", _HTTP_TIMEOUT)
    return requests.get(url, **kw)


def _openalex(query: str, n: int) -> List[Dict[str, Any]]:
    r = _get("https://api.openalex.org/works",
             params={"search": query, "per-page": n, "mailto": MAILTO})
    r.raise_for_status()
    out = []
    for w in r.json().get("results", []):
        oa = w.get("open_access") or {}
        out.append({
            "title": (w.get("title") or "")[:200],
            "year": w.get("publication_year"),
            "doi": (w.get("doi") or "").replace("https://doi.org/", "") or None,
            "oa_url": oa.get("oa_url"),
        })
    return out


def _unpaywall_oa(doi: str) -> Optional[str]:
    try:
        r = _get(f"https://api.unpaywall.org/v2/{quote(doi)}", params={"email": MAILTO})
        if r.status_code != 200:
            return None
        loc = (r.json() or {}).get("best_oa_location") or {}
        return loc.get("url_for_pdf") or loc.get("url")
    except requests.RequestException:
        return None


def _core(query: str, n: int) -> List[Dict[str, Any]]:
    # CORE ist auch OHNE Key nutzbar (freier Rate-Limit ~5 Req/10s) — Key nur für höheren Durchsatz.
    # Trailing-Slash-URL ist nötig (sonst 301). Fail-soft.
    key = os.getenv("CORE_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        r = _get("https://api.core.ac.uk/v3/search/works/",
                 headers=headers, params={"q": query, "limit": n})
        if r.status_code != 200:
            return []
        return [{"title": (w.get("title") or "")[:200], "year": w.get("yearPublished"),
                 "doi": w.get("doi"), "oa_url": w.get("downloadUrl")}
                for w in r.json().get("results", [])]
    except (requests.RequestException, ValueError):
        return []


def _pdf_to_text(content: bytes, max_chars: int) -> str:
    """Reine-Python-Extraktion (pypdf). Kein System-Poppler nötig. Fail-soft → ''."""
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        parts, total = [], 0
        for page in reader.pages:
            t = page.extract_text() or ""
            parts.append(t)
            total += len(t)
            if total >= max_chars:
                break
        return "".join(parts)[:max_chars]
    except Exception:
        return ""


def _fetch_fulltext(url: str, max_chars: int = 4000) -> str:
    try:
        r = _get(url, timeout=40, allow_redirects=True)
        if r.status_code != 200 or (r.content[:5] != b"%PDF-"
                                    and "pdf" not in r.headers.get("content-type", "").lower()):
            return ""
        return _pdf_to_text(r.content, max_chars)
    except requests.RequestException:
        return ""


def _retrieve_and_format(queries: List[str], max_results: int, max_fulltext: int) -> str:
    """Sync: OpenAlex(+CORE)-Discovery, Unpaywall-OA-Recovery, Volltext für Top-N, Markdown-Block."""
    papers: List[Dict[str, Any]] = []
    seen = set()
    for q in queries:
        try:
            hits = _openalex(q, max_results) + _core(q, max_results)
        except requests.RequestException:
            continue
        for p in hits:
            key = p.get("doi") or p.get("oa_url") or p.get("title")
            if key and key not in seen:
                seen.add(key)
                papers.append(p)

    for p in papers:
        if not p.get("oa_url") and p.get("doi"):
            u = _unpaywall_oa(p["doi"])
            if u:
                p["oa_url"] = u

    blocks, got = [], 0
    for p in papers:
        if got >= max_fulltext:
            break
        if not p.get("oa_url"):
            continue
        txt = _fetch_fulltext(p["oa_url"])
        if not txt:
            continue
        got += 1
        excerpt = re.sub(r"[ \t]+", " ", txt).strip()[:1600]
        doi = f" · doi:{p['doi']}" if p.get("doi") else ""
        blocks.append(f"### {p['title']} ({p.get('year')}){doi}\nQuelle (OA-Volltext): {p['oa_url']}\n\n{excerpt}")

    if not blocks:
        return ""
    header = (
        "## VERIFIZIERTE OA-LITERATUR (echter Volltext, legal via OpenAlex/Unpaywall)\n"
        "Nutze diese Volltext-Auszüge als PRIMÄRE, zitierbare Quellen (mit URL). Sie stammen aus "
        "peer-reviewten Open-Access-Papern; darin zitierte Normwerte (VDI/DIN/ÖNORM/EN) dürfen mit "
        "Quellenangabe übernommen werden. Ergänze offene Web-Suche nur für Herstellerdaten/Tarife/"
        "Förderungen, die hier fehlen.\n"
    )
    return header + "\n\n".join(blocks)


# ---------- LLM-Query-Planner (async, Augenprinzip statt Heuristik) ----------

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
        data = await _self_post_json("/v1/chat/completions", body, attribution, 90.0)
        content = data["choices"][0]["message"]["content"]
        m = re.search(r"\[.*\]", content, re.DOTALL)
        queries = [q.strip() for q in json.loads(m.group(0)) if isinstance(q, str) and q.strip()]
        return queries[:14] or fallback
    except Exception as e:
        logger.warning(f"scholarly planner fiel auf raw-query zurück: {e}")
        return fallback


# ---------- Öffentliche Einstiegsfunktion ----------

async def build_oa_context(
    query: str,
    *,
    attribution: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    max_results: int = 20,
    max_fulltext: int = 6,
    overall_timeout: float = 180.0,
) -> str:
    """Liefert den OA-Literatur-Kontext-Block (Markdown) oder '' (fail-soft/timeout)."""
    try:
        async def _run() -> str:
            queries = await _plan_queries(query, attribution, model)
            if not queries:
                return ""
            return await asyncio.to_thread(_retrieve_and_format, queries, max_results, max_fulltext)

        return await asyncio.wait_for(_run(), timeout=overall_timeout)
    except Exception as e:  # inkl. TimeoutError — Research darf NIE an dieser Schicht scheitern
        logger.warning(f"OA-Schicht übersprungen (fail-soft): {e}")
        return ""
