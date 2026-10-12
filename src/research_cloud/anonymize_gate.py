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

from typing import Optional

from fastapi import Request

PRIVACY_GATE_FAILED = "privacy_gate_failed"
PRIVACY_SERVICE_UNREACHABLE = "privacy_service_unreachable"


def privacy_urteil(exc: BaseException) -> dict:
    """fehler_code / retryable of a failed anonymize gate.

    privacy_service_unreachable: no privacy endpoint answered (primary and
    fallback, src.privacy_client.PrivacyServiceUnavailable anywhere in the
    cause chain) — the same request can succeed once the service is back:
    retryable. privacy_gate_failed: the service answered but did not attest
    a verified anonymization (or returned empty text) — not retryable, the
    gate refused this text."""
    from src.privacy_client import PrivacyServiceUnavailable

    seen = set()
    e: Optional[BaseException] = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, PrivacyServiceUnavailable):
            return {"fehler_code": PRIVACY_SERVICE_UNREACHABLE, "retryable": True}
        e = e.__cause__ or e.__context__
    return {"fehler_code": PRIVACY_GATE_FAILED, "retryable": False}


class CloudAnonymizeError(Exception):
    """Raised when the query cannot be verifiably anonymized before a
    research-cloud call. The caller must abort the cloud call entirely.
    ``fehler_code`` / ``retryable``: privacy_urteil of the cause."""

    fehler_code = PRIVACY_GATE_FAILED
    retryable = False


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
        fehler = CloudAnonymizeError(
            f"anonymize gate raised before attesting success — refusing research-cloud call: {e}"
        )
        urteil = privacy_urteil(e)
        fehler.fehler_code, fehler.retryable = urteil["fehler_code"], urteil["retryable"]
        raise fehler from e

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
