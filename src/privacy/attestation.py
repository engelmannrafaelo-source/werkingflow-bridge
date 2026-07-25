"""
PII pseudonymization attestation — value-free proof of what was anonymized.

The attestation is the DSGVO Rechenschaftspflicht (accountability) record: it
proves *that*, *how much*, and *of which types* PII was pseudonymized in a run —
WITHOUT ever storing the original PII values. The plaintext values live only in
the encrypted, per-run mapping; the attestation is safe to persist in a queryable
audit log and show to a customer, auditor, or DPA.

Hard rule: an attestation must NEVER contain an original PII value — only entity
TYPES and COUNTS. `assert_value_free()` and the unit tests enforce this so a
future change cannot accidentally turn the accountability record into a new
plaintext PII store (which would defeat the entire pipeline).
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

# Fields on a detected-entity that carry the plaintext PII value. These must be
# read to build counts but NEVER copied into an attestation.
_VALUE_FIELDS = frozenset({"original", "original_text", "value", "text"})


def build_attestation(
    detected_entities: List[Dict[str, Any]],
    *,
    status: str = "success",
    anonymization_performed: bool = True,
    mapping_size: Optional[int] = None,
    mode: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a value-free attestation summary.

    Reads only the entity ``type`` from each detected entity; any value field
    (``original`` etc.) is deliberately ignored. When per-type detail is not
    available (e.g. basic mode returns no typed entities), ``mapping_size`` is
    used for the total so the count is still attested honestly.

    Returns a dict of types + counts only — provably free of PII values
    (guaranteed by ``assert_value_free`` on the result).
    """
    counts = Counter(
        (e.get("type") or e.get("entity_type") or "UNKNOWN")
        for e in detected_entities
    )
    total = sum(counts.values())
    if not counts and mapping_size:
        total = mapping_size

    attestation: Dict[str, Any] = {
        "status": status,
        "anonymization_performed": anonymization_performed,
        "entity_counts_by_type": dict(sorted(counts.items())),
        "total_entities": total,
    }
    if mode is not None:
        attestation["mode"] = mode
    if model is not None:
        attestation["model"] = model

    # Safety net: never emit an attestation that carries a plaintext value.
    assert_value_free(attestation)
    return attestation


def assert_value_free(attestation: Dict[str, Any]) -> None:
    """Fail loud if an attestation carries any plaintext-value field.

    An attestation is meant to be persisted in an audit log and shown to
    auditors; it must never re-introduce the PII it attests to.
    """
    def _walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in _VALUE_FIELDS:
                    raise ValueError(
                        f"attestation contains forbidden value field {key!r} — "
                        "attestations must be value-free (types + counts only)"
                    )
                _walk(value)
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                _walk(item)

    _walk(attestation)
