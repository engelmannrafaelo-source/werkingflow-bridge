"""Research-cloud system prompt + depth -> search-budget mapping.

``/sc:research`` (SuperClaude) only exists inside the Claude Code CLI — the
cloud path needs an equivalent system prompt with the same report shape
(executive summary, source list, uncertainty markers) plus a way to map
``ResearchRequest.depth`` onto the executor's tool-call budget (``max_uses``),
since there's no CLI ``--depth`` flag to hand this to.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from src.research_cloud.library import entry_has_fulltext

from src.compat.research_content import SYSTEM_PROMPT_TEMPLATE  # compatibility only

# The library is shown as a CATALOGUE, not announced as a tool.
#
# Until 2026-09-05 this was a two-line note ("eine kuratierte Bibliothek steht
# zur Verfügung, sieh dir ihr Verzeichnis an").
#
# Two measurements, and they do NOT say the same thing — keep them apart:
#
#   * A/B-Lauf 2026-09-04, over the APP path (anonymised query), four questions:
#     library armed, both tools offered, 0 calls in 4 of 4.
#   * Controlled A/B 2026-09-05, executor called directly (NOT anonymised), same
#     question in both arms (OIB-RL 2.1 Brandabschnittsflächen, depth=quick):
#     old wording 2 library calls + 2 searches + 1 fetch, 376 s, 35.407 output
#     tokens; catalogue 1 library call, 0 searches, 0 fetches, 68 s, 6.845 output
#     tokens, answer of the same length.
#
# So the old wording does not make the library unreachable — it makes reaching it
# a detour: the model searches the web first and arrives at the full text late and
# expensively, if at all. The catalogue removes the detour, and that is what is
# measured here: 5,5x faster and 5x fewer output tokens for an equally long answer
# that cites the amtssignierte LGBl-Kundmachung.
#
# The 0-of-4 above is NOT reproduced by this change and must not be claimed as its
# baseline. Its arm differs in a second variable: the app path anonymises the query,
# which turns "in Österreich" into ANON_LOCATION_001 while jurisdiction is exactly
# what distinguishes this library. That hypothesis is open and belongs to the
# privacy path, not here (specs/research-library/ENTWURF-…-20260904.md, H2).
#
# Why a catalogue at all: to judge the library relevant the model must read the
# index; to read the index it must already believe the library is relevant. A web
# search returns usable snippets on the first call. The fix is not more
# instruction — it removes the first step.
#
# This does not classify the question and carries no keyword routing (the
# DESIGN.md guardrail against topic patterns); it shows the holdings and lets
# the model match. ~17 KB for 94 entries, in the cached system block.
from src.compat.research_content import _LIBRARY_HEADER  # compatibility only


# Der Weg ueber den Worker-Pool (Claude-Code-CLI, ~9 von 10 Produktions-
# recherchen) hat keinen Werkzeug-Loop, in dem library_get beantwortet werden
# koennte. Dort liegen die Volltexte stattdessen als Dateien im Arbeits-
# verzeichnis (src/research_library_pool.py), und das Verzeichnis sagt, wie
# man sie oeffnet. Inhaltlich dieselbe Rangfolge wie oben — nur die
# Zugriffsart unterscheidet sich (Rafael-Entscheidung 07.09.2026, Karte k20).
#
# Rangfolge seit 29.09.2026 UNBEDINGT (Befund c76ab7fb): vorher hiess es
# "Deckt ein Eintrag der Bibliothek die Frage ab, ...", d. h. das Modell
# entschied anhand von Titelzeilen, ob die Bibliothek zaehlt — gemessen
# 1.499 Websuchen gegen 352 gelesene Bibliotheksdokumente. Jetzt: erst
# Bibliothek, dann Web nur fuer Luecken und Aktualitaet; Quellenliste nach
# Herkunft getrennt. Dieselbe Reihenfolge steht im CLI-Protokoll
# (src/research_protocol.py) und im Cloud-Block oben.
from src.compat.research_content import _POOL_LIBRARY_HEADER  # compatibility only


def _catalogue_lines(
    documents: List[Dict[str, Any]], unavailable_ids: Optional[Set[str]] = None
) -> List[str]:
    """One line per entry: id, title and jurisdiction — the three fields a model
    needs to decide "is this my question's legal space and subject". Publisher,
    edition and notes stay behind library_index; putting all eight fields here
    would triple the size for information that does not drive the choice.

    ``unavailable_ids`` marks entries the *caller* cannot serve even though the
    index claims a full text (pool path: no file in the mirror). They stay in
    the catalogue — the holdings list is never trimmed — but must not be
    advertised as loadable.
    """
    lines: List[str] = []
    for entry in documents:
        doc_id = entry.get("id", "")
        title = (entry.get("title") or "").strip()
        jurisdiction = (entry.get("jurisdiction") or "").strip()
        has_text = entry_has_fulltext(entry) and not (
            unavailable_ids and doc_id in unavailable_ids
        )
        suffix = "" if has_text else " — KEIN VOLLTEXT"
        place = f" [{jurisdiction}]" if jurisdiction else ""
        lines.append(f"- `{doc_id}` — {title}{place}{suffix}")
    return lines


def build_library_catalogue(
    index: Optional[Dict[str, Any]], research_instructions: Optional[str] = None,
) -> str:
    """Render the library index as the prompt section above (cloud path)."""
    if not index:
        return ""
    documents: List[Dict[str, Any]] = index.get("documents") or []
    if not documents:
        return ""
    header = (_LIBRARY_HEADER.format(count=len(documents)) if research_instructions is None
              else "\n\nLibrary catalogue (library_get accepts id):\n")
    return header + "\n".join(_catalogue_lines(documents))


def build_pool_library_catalogue(
    index: Optional[Dict[str, Any]],
    *,
    dir_name: str,
    unavailable_ids: Optional[Set[str]] = None,
    research_instructions: Optional[str] = None,
) -> str:
    """Same holdings, addressed as files — the block appended to the Claude
    Code system prompt on the worker-pool path.

    Returns "" when there is nothing to show; the caller must then run without
    a library block rather than with an empty heading.
    """
    if not index:
        return ""
    documents: List[Dict[str, Any]] = index.get("documents") or []
    if not documents:
        return ""
    header = (_POOL_LIBRARY_HEADER.format(count=len(documents), dir_name=dir_name)
              if research_instructions is None else
              f"\n\nLibrary files: {dir_name}/<id>.md. Catalogue:\n")
    return header + "\n".join(_catalogue_lines(documents, unavailable_ids))


from src.compat.research_content import _DEPTH_INSTRUCTION  # compatibility only

# max_uses per depth — the executor's cost regulator (DESIGN.md: "max_uses
# als Kostenregler pro Tier"). Deliberately mirrors the depth timing bands
# already documented on ResearchRequest.depth (quick 1-2min/1 hop ...
# exhaustive 8-15min/5 hops) as a search-count proxy, not a literal hop count.
_SEARCH_MAX_USES = {"quick": 5, "standard": 10, "deep": 15, "exhaustive": 20}
_FETCH_MAX_USES = {"quick": 3, "standard": 6, "deep": 10, "exhaustive": 15}

_DEFAULT_DEPTH = "standard"


# perplexity_search (RESEARCH_PERPLEXITY_ENABLED, Rafael 02.10.2026). The tool
# description carries the same rules; the prompt states the ORDER, because the
# order is what the library block above already defines and Perplexity must
# slot in behind it, not in front of it.
from src.compat.research_content import _PERPLEXITY_SECTION  # compatibility only


def build_system_prompt(
    depth: Optional[str],
    library_index: Optional[Dict[str, Any]] = None,
    perplexity: bool = False,
    research_instructions: Optional[str] = None,
) -> str:
    """Build the research system prompt.

    ``library_index`` is the loaded index for this run (library.load_library_for_run)
    or None when the library is off. Passing the index rather than a bool is the
    point of the 2026-09-05 change: the catalogue is only honest if it is the
    same index the tool calls are served from.
    """
    if research_instructions is not None:
        if not research_instructions.strip():
            raise ValueError("research_instructions must not be blank")
        return research_instructions + build_library_catalogue(library_index, research_instructions)
    depth_key = depth if depth in _DEPTH_INSTRUCTION else _DEFAULT_DEPTH
    prompt = SYSTEM_PROMPT_TEMPLATE.format(depth_instruction=_DEPTH_INSTRUCTION[depth_key])
    prompt += build_library_catalogue(library_index)
    if perplexity:
        prompt += _PERPLEXITY_SECTION
    return prompt


def search_budget_for_depth(depth: Optional[str]) -> Tuple[int, int]:
    """Return (web_search_max_uses, web_fetch_max_uses) for a research depth."""
    depth_key = depth if depth in _SEARCH_MAX_USES else _DEFAULT_DEPTH
    return _SEARCH_MAX_USES[depth_key], _FETCH_MAX_USES[depth_key]
