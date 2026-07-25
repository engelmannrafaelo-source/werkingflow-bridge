"""Per-user RESEARCH-scoped provider pin — sibling of user_provider_override.py.

DESIGN.md (research-cloud-overflow) asks for a pin distinct from the global
Bedrock compliance pin: "Neuer, research-gescopter Wert (z. B.
{"research_provider": "anthropic-api"}), damit Chat/andere Endpoints
unberührt bleiben". Rather than adding a new DB column, this reads a second
key on the SAME ``users.provider_config`` JSONB the Bedrock pin already uses
— ``research_provider`` — and reuses that module's cached DB read verbatim
(no duplicate query/cache; same column, different key).

Supported value: ``"cloud"`` (pin this user's /v1/research calls to the
research-cloud path). Any other non-null value is a config error — fail loud
rather than silently ignoring a typo in an admin-set pin.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

SUPPORTED_RESEARCH_PROVIDERS = {"cloud"}


class ResearchProviderOverrideError(RuntimeError):
    """users.provider_config.research_provider carries an unsupported value."""


async def get_user_research_pin(raw_user_id: Any) -> Optional[str]:
    """Return "cloud" if this user is pinned to the research-cloud path, else
    None. Raises ResearchProviderOverrideError on an unsupported value, and
    propagates UserProviderOverrideError if the underlying provider_config
    lookup itself fails (DB error) — callers should treat both as "cannot
    verify the pin" rather than defaulting to unpinned.
    """
    from src.routing.user_provider_override import get_user_provider_config

    config = await get_user_provider_config(raw_user_id)
    if not config:
        return None

    value = config.get("research_provider")
    if value is None:
        return None
    if value not in SUPPORTED_RESEARCH_PROVIDERS:
        raise ResearchProviderOverrideError(
            f"provider_config.research_provider={value!r} is not supported "
            f"(supported: {sorted(SUPPORTED_RESEARCH_PROVIDERS)})"
        )
    return value
