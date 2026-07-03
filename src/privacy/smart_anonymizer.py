"""
Smart Anonymizer — local deterministic pseudonymization

Single-stage pipeline: Presidio (pattern recognizers) + local Flair NER
(see flair_recognizer.py) detect PII; every detection is replaced by a
placeholder and recorded in the mapping. Fully LOCAL — no cleartext ever
leaves the privacy service.

The former stage 2 (cloud-Haiku "refinement" that could RESTORE placeholders
for readability) was REMOVED entirely (decision Rafael, 2026-07-03): it was
blind (saw only placeholders + types, never values), restored ~1 in 22
entities, cost 25-52s per call, and had not proven itself. The response shape
is unchanged — `restored_entities` is now always empty and `smart_*` fields
mirror the raw detection result.
"""

import logging
import re
from typing import Any, Dict, Optional

from .anonymizer import PresidioAnonymizer

logger = logging.getLogger(__name__)


# ── Hard-PII classes (GDPR fail-safe) ────────────────────────────────────────
# Used by the post-pipeline leak assertion: original values of these entity
# types must NEVER appear as plaintext in the anonymized output.
_NEVER_RESTORE_TYPES = frozenset({
    "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE",
    "CREDIT_CARD", "IP_ADDRESS", "US_SSN", "CRYPTO", "MEDICAL_LICENSE",
})


# ── Post-pipeline consistency invariants ─────────────────────────────────────

def _assert_mapping_text_consistent(mapping: Dict[str, str], text: str) -> None:
    """Invariant 1: every mapping key must appear in the anonymized text.

    A violation means a placeholder was registered as "kept" but is absent from
    the output — de-anonymization would silently fail to restore it, and the
    consumer adapter rightfully rejects it. Fail loud here rather than return
    broken data. Root cause is typically overlapping Presidio spans that
    corrupted the right-to-left replacement loop (now fixed in anonymizer.py,
    but this assertion is the safety-net for future regressions).
    """
    missing = [ph for ph in mapping if ph not in text]
    if missing:
        raise ValueError(
            f"smart_anonymize consistency violation: {len(missing)} mapping "
            f"placeholder(s) absent from smart_anonymized_text: {missing[:5]}"
        )


def _assert_no_hard_pii_leak(
    mapping: Dict[str, str],
    text: str,
    type_by_placeholder: Dict[str, str],
) -> None:
    """Invariant 2: original values of hard-PII entities must NOT appear as
    plaintext in the anonymized text.

    If a PERSON / EMAIL_ADDRESS / PHONE_NUMBER etc. value is found verbatim in
    the output, the anonymization pipeline has a data leak — fail loud.
    Values ≤ 3 chars are skipped to avoid false positives on coincidental
    single-word matches (e.g. the string "at" appearing naturally).
    """
    leaks = []
    for placeholder, original in mapping.items():
        entity_type = type_by_placeholder.get(placeholder, "")
        if entity_type not in _NEVER_RESTORE_TYPES:
            continue
        # Word-boundary check: "Huber" inside "Hubergruppe" (compound word, no
        # word boundary after "Huber") must not trigger a false-positive alarm.
        # A genuine standalone occurrence (word boundary on both sides) still fires.
        if len(original) > 3 and re.search(r'\b' + re.escape(original) + r'\b', text):
            leaks.append((placeholder, entity_type))
    if leaks:
        raise ValueError(
            f"smart_anonymize PII leak: {len(leaks)} hard-PII entity value(s) "
            f"appear as plaintext in smart_anonymized_text — "
            + ", ".join(f"{ph}({t})" for ph, t in leaks[:5])
        )


async def smart_anonymize(
    text: str,
    language: str = "de",
    context_hint: Optional[str] = None,
    prefix: Optional[str] = None
) -> Dict[str, Any]:
    """
    Smart anonymization: local Presidio + Flair detection, deterministic replace.

    Args:
        text: Input text with potential PII
        language: Language code ('de' or 'en')
        context_hint: Accepted for API compatibility; unused since the AI
            refinement stage was removed (2026-07-03)
        prefix: Document-scoped prefix for placeholders (e.g. 'Da1b2c3'). Default: 'ANON'

    Returns:
        Anonymization result; `raw_*` and `smart_*` fields carry the same
        detection outcome, `restored_entities` is always empty.
    """
    anonymizer = PresidioAnonymizer(language=language)

    raw_result = await anonymizer.anonymize_async(text, language, prefix=prefix)

    if raw_result.entity_count == 0:
        # Detector ran and genuinely found no PII. Attest the run so consumers
        # can distinguish this from a disabled no-op (byte-identical otherwise).
        return {
            "status": "success",
            "anonymization_performed": True,
            "raw_anonymized_text": text,
            "raw_entity_count": 0,
            "smart_anonymized_text": text,
            "smart_entity_count": 0,
            "restored_entities": [],
            "mapping": {},
            "detected_entities": []
        }

    mapping = dict(raw_result.mapping)
    anonymized_text = raw_result.anonymized_text
    type_by_placeholder = {e.placeholder: e.entity_type for e in raw_result.detected_entities}
    _assert_mapping_text_consistent(mapping, anonymized_text)
    _assert_no_hard_pii_leak(mapping, anonymized_text, type_by_placeholder)
    return {
        "status": "success",
        "anonymization_performed": True,
        "raw_anonymized_text": anonymized_text,
        "raw_entity_count": raw_result.entity_count,
        "smart_anonymized_text": anonymized_text,
        "smart_entity_count": len(mapping),
        "restored_entities": [],
        "mapping": mapping,
        "detected_entities": [
            {
                "placeholder": e.placeholder,
                "type": e.entity_type,
                "original": e.original_text,
                "confidence": e.confidence,
                "decision": "KEEP",
                "reason": ""
            }
            for e in raw_result.detected_entities
        ]
    }
