"""Caller-owned research policy and neutral tool documentation."""

from typing import Optional

TOOL_DESCRIPTIONS = {
    "perplexity_search": "Search the web for a question; returns text and source URLs. Queries are anonymized.",
    "fetch_document": "Fetch a public document URL; returns text with page numbers. Optional seiten or suchbegriff selects pages.",
}


def caller_instructions(request) -> Optional[str]:
    value = vars(request).get("research_instructions")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("research_instructions must be a string")
    if not value.strip():
        raise ValueError("research_instructions must not be blank")
    return value
