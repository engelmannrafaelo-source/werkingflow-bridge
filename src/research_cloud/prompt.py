"""Research-cloud system prompt + depth -> search-budget mapping.

``/sc:research`` (SuperClaude) only exists inside the Claude Code CLI — the
cloud path needs an equivalent system prompt with the same report shape
(executive summary, source list, uncertainty markers) plus a way to map
``ResearchRequest.depth`` onto the executor's tool-call budget (``max_uses``),
since there's no CLI ``--depth`` flag to hand this to.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from src.research_cloud.library import entry_has_fulltext

SYSTEM_PROMPT_TEMPLATE = """Du bist ein gründlicher Recherche-Analyst. Recherchiere die Anfrage {depth_instruction} \
mit den dir zur Verfügung stehenden Werkzeugen, priorisiere Primärquellen (offizielle Dokumentation, Studien, Normen, Herstellerangaben) vor \
Sekundärquellen, und prüfe wichtige Fakten gegen mindestens zwei unabhängige Quellen bevor du sie übernimmst.

Schreibe einen strukturierten Markdown-Report:
- Executive Summary (2-4 Sätze)
- Detailbefunde mit konkreten Zahlen/Fakten und Quellenverweisen
- Quellenliste mit URLs
- Kennzeichne Unsicherheiten explizit ("nicht verifiziert", "widersprüchliche Quellen gefunden" etc.)

Erfinde keine Zahlen, Quellen oder Fakten. Wenn eine Information nicht auffindbar ist, sag das offen."""

# The library is shown as a CATALOGUE, not announced as a tool.
#
# Until 2026-09-05 this was a two-line note ("eine kuratierte Bibliothek steht
# zur Verfügung, sieh dir ihr Verzeichnis an"). Measured result of that wording:
# the library was armed, both tools were offered, and the model called them in
# 0 of 4 questions (A/B-Lauf 2026-09-04) — it answered from the open web while
# the OIB-Richtlinie it needed sat in the bucket as full text.
#
# The reason is a chicken-and-egg, not laziness: to judge the library relevant
# the model must read the index; to read the index it must already believe the
# library is relevant. A web search, meanwhile, returns usable snippets on the
# first call. So the fix is not more instruction — it is removing the first
# step: the catalogue is IN the prompt, before the model chooses anything.
#
# This does not classify the question and carries no keyword routing (the
# DESIGN.md guardrail against topic patterns); it shows the holdings and lets
# the model match. ~17 KB for 94 entries, in the cached system block.
_LIBRARY_HEADER = """

## Kuratierte Dokumentbibliothek (Volltexte)

Für diese Recherche steht dir eine kuratierte, private Bibliothek mit Volltexten zur Verfügung.
Ihr vollständiges Verzeichnis steht unten — du musst es nicht erst abrufen.

Rangfolge der Quellen: Deckt ein Eintrag der Bibliothek die Frage ab, ist sein Volltext die
maßgebliche Quelle — lade ihn mit `library_get` (Parameter `id`) und zitiere daraus, statt dich
auf Zusammenfassungen Dritter aus der Websuche zu stützen. Die Websuche ergänzt: Aktualität,
Fassungsstände und alles, was die Bibliothek nicht führt.

Einträge, die unten mit KEIN VOLLTEXT gekennzeichnet sind, sind reine Katalogverweise; für sie
schlägt `library_get` fehl — nutze dort die genannte Quelle über die Websuche.

`library_index` liefert dasselbe Verzeichnis mit allen Metadaten (Herausgeber, Fassung,
Quell-URL, Anmerkungen). Rufe es nur, wenn du diese Zusatzangaben brauchst.

### Verzeichnis ({count} Einträge)

"""


def build_library_catalogue(index: Optional[Dict[str, Any]]) -> str:
    """Render the library index as the prompt section above.

    One line per entry: id, title and jurisdiction — the three fields a model
    needs to decide "is this my question's legal space and subject". Publisher,
    edition and notes stay behind library_index; putting all eight fields here
    would triple the size for information that does not drive the choice.
    """
    if not index:
        return ""
    documents: List[Dict[str, Any]] = index.get("documents") or []
    if not documents:
        return ""
    lines: List[str] = []
    for entry in documents:
        doc_id = entry.get("id", "")
        title = (entry.get("title") or "").strip()
        jurisdiction = (entry.get("jurisdiction") or "").strip()
        suffix = "" if entry_has_fulltext(entry) else " — KEIN VOLLTEXT"
        place = f" [{jurisdiction}]" if jurisdiction else ""
        lines.append(f"- `{doc_id}` — {title}{place}{suffix}")
    return _LIBRARY_HEADER.format(count=len(documents)) + "\n".join(lines)


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


def build_system_prompt(
    depth: Optional[str], library_index: Optional[Dict[str, Any]] = None
) -> str:
    """Build the research system prompt.

    ``library_index`` is the loaded index for this run (library.load_library_for_run)
    or None when the library is off. Passing the index rather than a bool is the
    point of the 2026-09-05 change: the catalogue is only honest if it is the
    same index the tool calls are served from.
    """
    depth_key = depth if depth in _DEPTH_INSTRUCTION else _DEFAULT_DEPTH
    prompt = SYSTEM_PROMPT_TEMPLATE.format(depth_instruction=_DEPTH_INSTRUCTION[depth_key])
    prompt += build_library_catalogue(library_index)
    return prompt


def search_budget_for_depth(depth: Optional[str]) -> Tuple[int, int]:
    """Return (web_search_max_uses, web_fetch_max_uses) for a research depth."""
    depth_key = depth if depth in _SEARCH_MAX_USES else _DEFAULT_DEPTH
    return _SEARCH_MAX_USES[depth_key], _FETCH_MAX_USES[depth_key]
