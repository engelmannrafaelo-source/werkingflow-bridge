"""Research-cloud system prompt + depth -> search-budget mapping.

``/sc:research`` (SuperClaude) only exists inside the Claude Code CLI — the
cloud path needs an equivalent system prompt with the same report shape
(executive summary, source list, uncertainty markers) plus a way to map
``ResearchRequest.depth`` onto the executor's tool-call budget (``max_uses``),
since there's no CLI ``--depth`` flag to hand this to.
"""
from __future__ import annotations

from typing import Optional, Tuple

SYSTEM_PROMPT_TEMPLATE = """Du bist ein gründlicher Recherche-Analyst. Recherchiere die Anfrage {depth_instruction} \
per Websuche, priorisiere Primärquellen (offizielle Dokumentation, Studien, Normen, Herstellerangaben) vor \
Sekundärquellen, und prüfe wichtige Fakten gegen mindestens zwei unabhängige Quellen bevor du sie übernimmst.

Schreibe einen strukturierten Markdown-Report:
- Executive Summary (2-4 Sätze)
- Detailbefunde mit konkreten Zahlen/Fakten und Quellenverweisen
- Quellenliste mit URLs
- Kennzeichne Unsicherheiten explizit ("nicht verifiziert", "widersprüchliche Quellen gefunden" etc.)

Erfinde keine Zahlen, Quellen oder Fakten. Wenn eine Information nicht auffindbar ist, sag das offen."""

_DEPTH_INSTRUCTION = {
    "quick": "knapp und fokussiert (1-2 zentrale Quellen reichen)",
    "standard": "in üblicher Tiefe (mehrere Quellen, 2-3 Recherche-Hops)",
    "deep": "gründlich (3-4 Recherche-Hops, mehrere unabhängige Quellen je Kernaussage)",
    "exhaustive": "erschöpfend (5+ Recherche-Hops, so vollständig wie möglich)",
}

# max_uses per depth — the executor's cost regulator (DESIGN.md: "max_uses
# als Kostenregler pro Tier"). Deliberately mirrors the depth timing bands
# already documented on ResearchRequest.depth (quick 1-2min/1 hop ...
# exhaustive 8-15min/5 hops) as a search-count proxy, not a literal hop count.
_SEARCH_MAX_USES = {"quick": 5, "standard": 10, "deep": 15, "exhaustive": 20}
_FETCH_MAX_USES = {"quick": 3, "standard": 6, "deep": 10, "exhaustive": 15}

_DEFAULT_DEPTH = "standard"


def build_system_prompt(depth: Optional[str]) -> str:
    depth_key = depth if depth in _DEPTH_INSTRUCTION else _DEFAULT_DEPTH
    return SYSTEM_PROMPT_TEMPLATE.format(depth_instruction=_DEPTH_INSTRUCTION[depth_key])


def search_budget_for_depth(depth: Optional[str]) -> Tuple[int, int]:
    """Return (web_search_max_uses, web_fetch_max_uses) for a research depth."""
    depth_key = depth if depth in _SEARCH_MAX_USES else _DEFAULT_DEPTH
    return _SEARCH_MAX_USES[depth_key], _FETCH_MAX_USES[depth_key]
