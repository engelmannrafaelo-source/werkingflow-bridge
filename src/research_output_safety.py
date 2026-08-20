"""
Guards POST /v1/research's optional `output_path` field against arbitrary
file write / path traversal (security-audit-live-findings-20260818.md
L10c/B.4).

`output_path` is documented as "Host filesystem path where research report
should be saved. If not provided, saves to /tmp/" (src/models.py
ResearchRequest) — a same-host convenience for a CLI running alongside the
Bridge (DESIGN.md example: "/Users/rafael/research/ai_2025.md"). But
/v1/research is reachable over the network by any app/attacker holding a
Bridge credential, and both write sites (the CLI-subprocess copy in main.py
and the research-cloud open() write) passed the value straight to the
filesystem with no containment check — an attacker could overwrite any file
the Bridge process user can write to.

No verified real caller relies on this field outside the documented /tmp/
default: the canonical client (orchestrator/bin/bridge-research.py) writes
the response's `content` field to a *local* path itself instead of trusting
a server-side write, and no app in the monorepo sends output_path at all.
Containing writes to a single allowed directory therefore changes behaviour
for nobody real.

Fail-closed on the boundary (reject anything outside the allowed dir) but
fail-soft on the overall request: research output is always available in the
response body, so a rejected output_path degrades to "wasn't additionally
saved server-side" — it never fails the research call itself.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _allowed_output_dir() -> Path:
    """Read live (not cached at import) so tests can point it at a tmp_path
    via monkeypatch without reimporting the module."""
    return Path(os.getenv("BRIDGE_RESEARCH_OUTPUT_DIR", "/tmp")).resolve()


def _is_contained(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def safe_output_path(requested: Optional[str]) -> Optional[Path]:
    """Resolve a caller-supplied output_path against the allowed directory.

    Returns the resolved Path when it is safely contained (symlinks and `..`
    segments resolved before the containment check — a raw string-prefix
    check would miss both). Returns None when unset, unresolvable, or outside
    the allowed directory; callers must treat None as "do not write" and log
    a loud warning is already emitted here.
    """
    if not requested or not requested.strip():
        return None

    base = _allowed_output_dir()
    try:
        candidate = Path(requested)
        if not candidate.is_absolute():
            candidate = base / candidate
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as e:
        logger.warning(
            "research output_path %r could not be resolved (%s) — write skipped",
            requested, e,
        )
        return None

    if not _is_contained(resolved, base):
        logger.warning(
            "research output_path %r resolves outside the allowed directory %s "
            "(resolved=%s) — rejecting write (possible path-traversal/arbitrary-"
            "write attempt). The research content is still returned in the "
            "response body.",
            requested, base, resolved,
        )
        return None
    return resolved


def default_output_path(filename: str) -> Path:
    """The pre-existing '/tmp/<filename>' fallback, rooted at the allowed dir.

    Path(filename).name strips any directory component from the input, so a
    caller cannot smuggle traversal through the fallback filename either.
    """
    return _allowed_output_dir() / Path(filename).name
