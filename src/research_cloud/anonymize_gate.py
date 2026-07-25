"""Fail-closed anonymize gate for the research-cloud path.

Per DESIGN.md ("Anonymize-Pflicht fail-loud: JA — Executor verweigert
Cloud-Call statt unanonymisiert zu senden") and the 2026-07-25 privacy commits
(fe38f77..ccb27d3: fail-closed anonymization + value-free attestation), the
cloud executor MUST NOT build its own anonymize logic — it reuses the same
central fail-closed path + attestation persistence that
``/v1/privacy/smart-anonymize`` uses (``src.main._smart_anonymize_core``).

The only difference from the HTTP endpoint's contract: that endpoint always
returns 200 with a ``status`` field (proxy semantics for external callers).
This gate raises on anything but a verified success, because a preflight
anonymize failure must abort the cloud call outright — no silent fallback to
sending the raw query, and no silent fallback to the worker pool either
(silent fallback is only legitimate at the cap-vs-execution check, not here).
"""
from __future__ import annotations

from fastapi import Request


class CloudAnonymizeError(Exception):
    """Raised when the query cannot be verifiably anonymized before a
    research-cloud call. The caller must abort the cloud call entirely."""


async def anonymize_query_for_cloud(request: Request, query: str, language: str = "de") -> str:
    """Anonymize ``query`` via the central fail-closed path and return the
    anonymized text. Raises CloudAnonymizeError (never returns unanonymized
    text) if the detector is disabled, the call fails, or the result doesn't
    attest a genuine successful run.
    """
    from src.main import _smart_anonymize_core  # lazy: avoid import cycle with the app module

    try:
        result = await _smart_anonymize_core(request, text=query, language=language)
    except Exception as e:
        raise CloudAnonymizeError(
            f"anonymize gate raised before attesting success — refusing research-cloud call: {e}"
        ) from e

    if result.status != "success" or not result.anonymization_performed:
        raise CloudAnonymizeError(
            f"anonymize gate did not attest success (status={result.status!r}, "
            f"anonymization_performed={result.anonymization_performed!r}, error={result.error!r}) "
            "— refusing to forward the query to the research-cloud path"
        )

    anonymized = result.smart_anonymized_text
    if anonymized is None or (query and not anonymized):
        raise CloudAnonymizeError(
            "anonymize gate returned empty text for a non-empty query — "
            "refusing to forward raw content to the research-cloud path"
        )
    return anonymized
