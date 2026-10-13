"""OA-Scholarly-Schicht für /v1/research (Weg A: deterministische Pre-Retrieval-Injektion).

Holt LEGALE Open-Access-Fachinhalte (OpenAlex-Abstracts + Volltext aus CORE UND der
OpenAlex-Content-API) zur Recherche-Query und formatiert sie als Kontext-Block, der VOR den
SuperClaude-Research-Prompt gehängt wird. Damit stützt sich der Agent auf peer-reviewte
Primärliteratur (inkl. der darin zitierten Normwerte VDI/DIN/ÖNORM/EN) statt nur auf
Snippets/Shop-Seiten. Kein Sci-Hub, kein Paywall-Bypass.

Design (bewusst dependency-frei + schnell, für die geteilte Bridge):
  - OpenAlex Search: Discovery + `abstract_inverted_index` → rekonstruierter Abstract (immer da, schnell)
  - OpenAlex Content-API (`src/openalex_content.py`): GROBID-XML-Volltext für die relevantesten
    Abstract-only-Treffer, hart credit-gedeckelt (siehe dortiger Modul-Docstring), fremdsprachige
    Volltexte werden erkannt und NICHT verwendet (Rückfall auf den Abstract)
  - CORE:     `fullText` direkt aus dem JSON (57M Repository-/Dissertations-Volltexte), best-effort
              (keyless rate-limitet → 429 wird fail-soft übersprungen); der Sentinel-Platzhalter
              "Not available for public API users." zählt NICHT als Volltext (siehe `_core()`)
  - KEIN PDF-Download/-Extraktion für CORE (Worker haben keine PDF-Lib; Downloads waren der
    Timeout-Treiber) — die Content-API liefert stattdessen bereits geparstes XML, kein PDF nötig
  - Alle Netz-Calls parallel + hart budgetiert; komplett fail-soft → bei jedem Fehler leerer String,
    Research läuft unverändert weiter.

Aktivierung nur wenn BRIDGE_SCHOLARLY_ENABLED=true UND request.research_mode=="academic".
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

# The mechanism lives in src/literatur.py; this module keeps only the frozen
# Energy planner and header for legacy callers (BRAUS F19).
from src.literatur import (  # noqa: F401  (re-exported for legacy imports)
    _CORE_HTTP_TIMEOUT,
    _HTTP_TIMEOUT,
    plan_queries,
    quellen_bloecke,
    scholarly_enabled,
    scholarly_profile,
)

logger = logging.getLogger("scholarly")

_PLANNER_SYS = (
    "Du bist ein Recherche-Query-Planner für die wissenschaftliche Literatur-API OpenAlex. "
    "Aus einem Energie-Ingenieur-Research-Prompt erzeugst du kurze SUCHBEGRIFFE (Keyword-Phrasen, "
    "keine Fragen/Sätze). Regeln: (a) mische ENGLISCH (internationale Journals) und DEUTSCH "
    "(Dissertationen/Forschungsberichte, wo ÖNORM/VDI/OIB zitiert werden); "
    "(b) NATIONALE Regelwerke (ÖNORM, TRVB, OIB-Richtlinie, DIN) sind KEINE Fachliteratur und in "
    "OpenAlex/CORE nicht indexiert — suche NIEMALS nach ihrer Bezeichnung. Nimm stattdessen die "
    "EUROPÄISCHE Entsprechung (EN/ISO) als EIGENE, KURZE Query; sie ist der staerkste Treffer-Anker. "
    "Ergänze getrennt davon eine Query auf die PHYSIKALISCHE GRÖSSE bzw. das Verfahren. "
    "(b1) HALTE QUERIES KURZ, zwei bis fünf Begriffe. Mehr Begriffe stapeln heisst NICHT genauer: "
    "gemessen 03.09.2026 lieferte \'EN 12101 smoke control\' 247 thematisch exakte Treffer, waehrend "
    "das ueberladene \'EN 12101-6 pressure differential system\' als Top-Treffer eine Arbeit ueber "
    "Bisphenol A und das Nervensystem zurueckgab. "
    "(b2) Frage NICHT auf Deutsch nach Fachbegriffen — \'Druckbelüftung Sicherheitstreppenraum\' und "
    "\'Druckbelüftungsanlage Treppenraum\' ergaben beide 0 Treffer. Deutsch lohnt nur fuer "
    "Dissertations-/Berichtstitel, nicht fuer Anlagentechnik. "
    "Beispiel fuer TRVB 112 (Druckbelüftung): \'EN 12101 smoke control\' UND "
    "\'stairwell pressurization door opening force\' als ZWEI getrennte kurze Queries; "
    "(c) für jedes technische Thema eine Konzept-Query. "
    "Antworte AUSSCHLIESSLICH als JSON-Array von Strings (8-14 Queries), nichts sonst."
)


def _retrieve_and_format(queries: List[str], per_query: int) -> str:
    """Legacy block: the Energy header above the neutral source blocks."""
    bloecke, n = quellen_bloecke(queries, per_query)
    if not n:
        return ""
    header = (
        "## VERIFIZIERTE OA-LITERATUR (legal via OpenAlex/CORE)\n"
        "Nutze diese peer-reviewten Open-Access-Auszüge für physikalische Zusammenhänge, Verfahren, "
        "Messmethoden und Normkontext — mit Quellenangabe (URL/DOI).\n"
        "**GRENZWERTE AUS DIESER LITERATUR HERAUSARBEITEN — das ist der Zweck dieser Schicht.** Die "
        "Regelwerke selbst (TRVB, OIB, ÖNORM) sind kostenpflichtig und liegen hier nicht vor; die "
        "Sekundärliteratur ist der vorgesehene Weg, um die einschlägigen Zahlenwerte (Druckdifferenzen, "
        "Kräfte, U-Werte, Fristen, Bemessungsansätze) zu ermitteln. Gib sie aus, mit Quelle (URL/DOI).\n"
        "**Dabei IMMER dazuschreiben, auf welches Regelwerk und welches Land sich ein Wert bezieht.** "
        "Grenzwerte unterscheiden sich je Rechtsraum — eine koreanische Arbeit nennt für dieselbe "
        "Türöffnungskraft 110 N (NFSC 501A), während im österreichischen Raum TRVB/EN 12101-6 mit 100 N "
        "gelten. Ein Wert ohne Regelwerks- und Länderbezug ist unbrauchbar; ein Wert MIT diesem Bezug "
        "ist verwertbar, auch wenn er aus Sekundärliteratur stammt.\n"
        "Die Herkunft (Land der Institutionen, Jahr, Sprache) steht bei jeder Quelle — sie entscheidet "
        "mit, ob eine Arbeit auf den österreichischen Rechtsraum übertragbar ist.\n"
        "Ergänze offene Web-Suche nur für Herstellerdaten/Tarife/Förderungen, die hier fehlen.\n"
    )
    return header + bloecke


async def _plan_queries(query: str, attribution: Optional[Dict[str, Any]], model: Optional[str]) -> List[str]:
    """Destilliert die (evtl. sehr lange) Research-Query in Suchbegriffe. Fail-soft → [query[:180]]."""
    return await plan_queries(query, _PLANNER_SYS, attribution, model)


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
