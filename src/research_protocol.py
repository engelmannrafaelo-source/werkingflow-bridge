"""Depth-aware execution protocol for the /v1/research CLI path.

The CLI path can't hand the executor a per-tool ``max_uses`` like the cloud
path does, so the search/fetch budget lands as prompt instruction and
``max_turns`` acts as the hard regulator. The budget table itself is shared
with the cloud path (``research_cloud.prompt.search_budget_for_depth``) —
one SSoT, both paths.

History: until 2026-08-04 the CLI path replaced the whole research prompt
with a hardcoded "2-3 TARGETED searches only" protocol and discarded the
``--depth`` flag, so every depth tier delivered the same abbreviated pass.
Multi-question briefs (energy Phase 6, categories A-F) came back with whole
categories dropped — declared honestly by the agent, caught loudly by nobody.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from src.research_cloud.prompt import search_budget_for_depth

_VALID_DEPTHS = ("quick", "standard", "deep", "exhaustive")
_DEFAULT_DEPTH = "standard"

# max_turns per depth — hard regulator on the CLI side. Sized as
# search budget + fetch budget + headroom for planning/writing turns.
_TURN_CAP = {"quick": 20, "standard": 30, "deep": 45, "exhaustive": 60}
_TURN_FLOOR = 20  # below this even a quick pass can't reliably finish + write

# Output sizing per depth (words). Scales the report with the budget so a
# deep pass isn't forced back into quick-sized summaries.
_FINDING_WORDS = {"quick": 100, "standard": 150, "deep": 250, "exhaustive": 300}
_ANALYSIS_WORDS = {"quick": 150, "standard": 300, "deep": 600, "exhaustive": 900}

# Flags main.py appends after the quoted query — stripped from the query
# text; their information either lands elsewhere (--depth) or is SuperClaude
# vocabulary the direct protocol replaces (--strategy, --max-hops, ...).
_FLAG_RE = re.compile(
    r"\s*--(?:depth|strategy|max-hops|confidence|parallel|sources)\s+\S+"
)

_COMMAND_RE = re.compile(r"^/(?:sc:)?research\s+", re.DOTALL)

_DEPTH_RE = re.compile(r"--depth\s+(quick|standard|deep|exhaustive)\b")


def parse_depth(prompt: str) -> str:
    """Extract the requested depth from a ``/sc:research`` prompt.

    Missing flag -> default depth (mirrors ResearchRequest.depth default).
    An unknown depth value can't reach us: the API layer validates the
    Literal before the prompt is built.
    """
    match = _DEPTH_RE.search(prompt)
    return match.group(1) if match else _DEFAULT_DEPTH


def build_research_execution_prompt(
    prompt: str, max_turns: int, library_dir: Optional[str] = None
) -> Tuple[str, int, str]:
    """Transform a ``/sc:research "…" --flags`` prompt into the direct
    execution protocol, honouring the requested depth.

    Returns ``(execution_prompt, max_turns, depth)``.

    ``library_dir`` names the workdir folder that holds the curated library
    (``research_library_pool.LIBRARY_WORKDIR_NAME``) when this run has one.
    Then the protocol reads the library FIRST and the web only for gaps and
    currency (Rafael, 29.09.2026 — Befund c76ab7fb: 17 WebSearch / 7 WebFetch
    against 2 library reads with 204 documents in the folder, because step 1
    of this protocol named only the web). Without a library the protocol is
    byte-identical to the pre-change text (snapshot-tested).

    Everything that isn't the command token or a known flag stays part of
    the query — including an injected OA literature block, which must reach
    the executor verbatim.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("research prompt must be a non-empty string")
    if not _COMMAND_RE.match(prompt.strip()):
        raise ValueError(
            "not a /sc:research prompt — caller must gate on the command prefix"
        )

    depth = parse_depth(prompt)
    search_budget, fetch_budget = search_budget_for_depth(depth)

    research_query = _COMMAND_RE.sub("", prompt.strip(), count=1)
    research_query = _FLAG_RE.sub("", research_query).strip()
    if not research_query:
        raise ValueError("research prompt contains no query after flag stripping")

    turn_cap = _TURN_CAP[depth]
    effective_turns = max(min(max_turns, turn_cap), _TURN_FLOOR)

    if library_dir:
        return (
            _library_first_protocol(
                research_query, depth, search_budget, fetch_budget, library_dir
            ),
            effective_turns,
            depth,
        )

    execution_prompt = f"""Research this query and write output IMMEDIATELY:

QUERY: {research_query}

PROTOCOL (depth: {depth} — budgets are ceilings, not targets):
1. Use WebSearch (up to {search_budget} searches) and WebFetch (up to {fetch_budget} page fetches) — targeted, no filler queries
2. Address EVERY question/category the query contains. If the budget cannot cover all of them in depth, reduce per-item depth instead of dropping items, and list anything you could not source under an explicit "Offene Lücken / Open gaps" heading — never drop a sub-question silently
3. Extract key findings with sources (keep each finding under {_FINDING_WORDS[depth]} words)
4. Write the report to claudedocs/research_output.md IMMEDIATELY after your searches are done
5. DO NOT conduct additional searches after writing the file

OUTPUT STRUCTURE:
# Research Report

## Summary
[2-4 sentences]

## Key Findings
- [Finding with source, one bullet per finding]

## Analysis
[Max {_ANALYSIS_WORDS[depth]} words]

## Offene Lücken
[Only if something asked in the query could not be covered or sourced — name it explicitly. Omit the section if there are none.]

## Sources
[List URLs]

CRITICAL: The file claudedocs/research_output.md MUST exist when you finish. Use the Write tool.
"""
    return execution_prompt, effective_turns, depth


def _library_first_protocol(
    research_query: str,
    depth: str,
    search_budget: int,
    fetch_budget: int,
    library_dir: str,
) -> str:
    """Protocol for a run with a curated library in the workdir.

    The order is unconditional — library first, web second — and not left to
    the model's reading of catalogue titles ("covers an entry the question?").
    Same order and same source split as the appended catalogue block
    (``research_cloud.prompt._POOL_LIBRARY_HEADER``) and the cloud path.
    """
    return f"""Research this query and write output IMMEDIATELY:

QUERY: {research_query}

PROTOCOL (depth: {depth} — budgets are ceilings, not targets):
1. Durchsuche {library_dir}/ (Grep nach Begriffen, Normnummern, Werten) und lies alle passenden Dokumente.
2. WebSearch/WebFetch nur für das, was dort fehlt, und für Aktualität/Fassungsstand (up to {search_budget} searches and up to {fetch_budget} page fetches — targeted, no filler queries)
3. Address EVERY question/category the query contains. If the budget cannot cover all of them in depth, reduce per-item depth instead of dropping items, and list anything you could not source under an explicit "Offene Lücken / Open gaps" heading — never drop a sub-question silently
4. Extract key findings with sources (keep each finding under {_FINDING_WORDS[depth]} words). Every number states its origin: (Bibliothek: <id>) or (Web: <URL>)
5. Write the report to claudedocs/research_output.md IMMEDIATELY after your research is done
6. DO NOT conduct additional searches after writing the file

OUTPUT STRUCTURE:
# Research Report

## Summary
[2-4 sentences]

## Key Findings
- [Finding with source, one bullet per finding — every number marked (Bibliothek: <id>) or (Web: <URL>)]

## Analysis
[Max {_ANALYSIS_WORDS[depth]} words]

## Offene Lücken
[Only if something asked in the query could not be covered or sourced — name it explicitly. Omit the section if there are none.]

## Sources
[Split by origin, one line per source:
Bibliothek: <id>
Web: <URL>]

CRITICAL: The file claudedocs/research_output.md MUST exist when you finish. Use the Write tool.
"""
