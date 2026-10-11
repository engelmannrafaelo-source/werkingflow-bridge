"""Research transport; editorial policy is supplied by the caller.

Omitting research_instructions selects the frozen pre-migration contract.
"""

from typing import Optional, Tuple

import re
from src.research_budgets import search_budget_for_depth

_VALID_DEPTHS = ("quick", "standard", "deep", "exhaustive")
_DEFAULT_DEPTH = "standard"

# max_turns per depth — hard regulator on the CLI side. Sized as
# search budget + fetch budget + headroom for planning/writing turns.
_TURN_CAP = {"quick": 20, "standard": 30, "deep": 45, "exhaustive": 60}
_TURN_FLOOR = 20  # below this even a quick pass can't reliably finish + write

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


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    from src.compat import research_protocol as legacy
    return getattr(legacy, name)


def build_research_execution_prompt(
    prompt: str,
    max_turns: int,
    library_dir: Optional[str] = None,
    antwort_im_text: bool = False,
    research_instructions: Optional[str] = None,
) -> Tuple[str, int, str]:
    if research_instructions is None:
        from src.compat import research_protocol as legacy
        return legacy.build_research_execution_prompt(
            prompt,
            max_turns,
            library_dir,
            antwort_im_text,
        )
    if not research_instructions.strip():
        raise ValueError("research_instructions must not be blank")
    if not isinstance(prompt, str) or not _COMMAND_RE.match(prompt.strip()):
        raise ValueError("not a /sc:research prompt")
    depth = parse_depth(prompt)
    query = _FLAG_RE.sub(
        "", _COMMAND_RE.sub("", prompt.strip(), count=1)
    ).strip()
    if not query:
        raise ValueError("research prompt contains no query after flag stripping")
    searches, fetches = search_budget_for_depth(depth)
    turns = max(min(max_turns, _TURN_CAP[depth]), _TURN_FLOOR)
    output = (
        "Return the result in your final assistant message, in the caller's format."
        if antwort_im_text
        else "Write the result to OUTPUT_FILE_PATH, in the caller's format."
    )
    access = (
        f"\nLibrary files are available under {library_dir}/." if library_dir else ""
    )
    return (
        f"{research_instructions}\n\nQUERY: {query}\n\n"
        f"Tool ceilings: {searches} WebSearch calls, {fetches} WebFetch calls."
        f"{access}\n{output}",
        turns,
        depth,
    )
