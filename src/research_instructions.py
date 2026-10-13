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


def caller_literatur_planer(request) -> Optional[str]:
    """The caller's instruction for planning literature search terms (BRAUS
    F19); only together with research_instructions (ResearchRequest checks)."""
    value = vars(request).get("literatur_planer")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("literatur_planer must be a nonblank string")
    return value


def research_privacy_options(request) -> dict:
    """Only the legacy request may use the frozen app exceptions."""
    terms = vars(request).get("privacy_allowlist")
    if terms is not None:
        if not isinstance(terms, list) or any(not isinstance(t, str) or not t.strip() for t in terms):
            raise ValueError("privacy_allowlist must contain nonblank strings")
        return {"privacy_allowlist": terms}
    return {"privacy_allowlist": []} if caller_instructions(request) is not None else {}
