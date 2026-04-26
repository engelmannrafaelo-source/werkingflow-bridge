"""Detect responses where Claude announced a tool use that never produced content.

When tools are disallowed (default OpenAI-compat behavior on /v1/chat/completions)
and ``max_turns=1``, Claude can still emit pre-tool-use intro text such as
``"I'll write the research prompt directly to the output file."`` before
attempting a blocked tool call. The blocked attempt ends the turn — only the
intro text reaches the client, which then receives a 50–100 char response
despite a multi-thousand-char prompt.

This module recognises that anomaly so the caller can retry once with stronger
guard-rails (``max_turns=2`` plus a prepended system reminder).
"""

from __future__ import annotations

import re
from typing import Optional


# Response opens with an agentic-intro phrase ("I'll write…", "Let me create…").
_OPENS_WITH_INTRO = re.compile(
    r"^\s*(I'?ll|Let me|I will|I'?m going to|I am going to)\s+"
    r"(write|create|generate|save|output|produce|build|prepare|draft|put)\b",
    re.IGNORECASE,
)

DEFAULT_MIN_VALID_CHARS = 200
DEFAULT_MIN_PROMPT_CHARS = 2000

TOOL_LEAK_GUARD_REMINDER = (
    "ABSOLUTE RULE: You CANNOT use tools — they are disabled. "
    "Output the requested content directly as your text reply. "
    "Do NOT begin with 'I'll write', 'Let me create', or any similar tool-use intro. "
    "Begin your response with the requested format (e.g. a markdown header) and nothing else.\n\n"
)


def looks_like_tool_leak(
    content: Optional[str],
    prompt_chars: int = 0,
    min_valid_chars: int = DEFAULT_MIN_VALID_CHARS,
    min_prompt_chars: int = DEFAULT_MIN_PROMPT_CHARS,
) -> bool:
    """Return True iff the response looks like a tool-use intro instead of content.

    Heuristics (all must hold):
    - Response is non-empty (an empty response is handled by a separate code path).
    - Stripped response is shorter than ``min_valid_chars``.
    - The user prompt was substantial (> ``min_prompt_chars``); a short prompt
      legitimately produces a short response.
    - Response opens with an agentic-intro phrase.
    """
    if not content:
        return False
    stripped = content.strip()
    if len(stripped) >= min_valid_chars:
        return False
    if prompt_chars < min_prompt_chars:
        return False
    return bool(_OPENS_WITH_INTRO.match(stripped))


def hardened_system_prompt(original: Optional[str]) -> str:
    """Prepend a hard tool-leak guard to the existing system prompt."""
    base = original or ""
    return TOOL_LEAK_GUARD_REMINDER + base
