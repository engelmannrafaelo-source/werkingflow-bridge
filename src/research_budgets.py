"""Shared resource ceilings for research transports; no editorial policy."""
from typing import Optional, Tuple

_SEARCH_MAX_USES = {"quick": 5, "standard": 10, "deep": 15, "exhaustive": 20}
_FETCH_MAX_USES = {"quick": 3, "standard": 6, "deep": 10, "exhaustive": 15}

_DEFAULT_DEPTH = "standard"


def search_budget_for_depth(depth: Optional[str]) -> Tuple[int, int]:
    """Return (web_search_max_uses, web_fetch_max_uses) for a research depth."""
    depth_key = depth if depth in _SEARCH_MAX_USES else _DEFAULT_DEPTH
    return _SEARCH_MAX_USES[depth_key], _FETCH_MAX_USES[depth_key]
