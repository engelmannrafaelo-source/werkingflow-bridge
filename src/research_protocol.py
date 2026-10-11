"""Research transport; editorial policy is supplied by the caller.

Omitting research_instructions selects the frozen pre-migration contract.
"""

from typing import Optional, Tuple

from src.compat import research_protocol as legacy
from src.research_cloud.prompt import search_budget_for_depth

parse_depth = legacy.parse_depth


def __getattr__(name):
    return getattr(legacy, name)


def build_research_execution_prompt(
    prompt: str,
    max_turns: int,
    library_dir: Optional[str] = None,
    antwort_im_text: bool = False,
    research_instructions: Optional[str] = None,
) -> Tuple[str, int, str]:
    if research_instructions is None:
        return legacy.build_research_execution_prompt(
            prompt,
            max_turns,
            library_dir,
            antwort_im_text,
        )
    if not research_instructions.strip():
        raise ValueError("research_instructions must not be blank")
    if not isinstance(prompt, str) or not legacy._COMMAND_RE.match(prompt.strip()):
        raise ValueError("not a /sc:research prompt")
    depth = parse_depth(prompt)
    query = legacy._FLAG_RE.sub(
        "", legacy._COMMAND_RE.sub("", prompt.strip(), count=1)
    ).strip()
    if not query:
        raise ValueError("research prompt contains no query after flag stripping")
    searches, fetches = search_budget_for_depth(depth)
    turns = max(min(max_turns, legacy._TURN_CAP[depth]), legacy._TURN_FLOOR)
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
