"""
Smart Anonymizer — AI-refined pseudonymization

2-stage process:
1. Presidio detects ALL potential PII (aggressive, catches everything)
2. Claude evaluates each detection: real PII → keep anonymized,
   context-relevant non-PII → restore original value

This produces a "smart" pseudonymized text where only actual personal data
is masked, while context-relevant information (city names, dates,
organization types) is preserved.

IMPORTANT: AI refinement uses the Bridge's own /v1/chat/completions endpoint
(localhost self-call via OAuth — free, no API key needed). This is a text-only
call, NOT vision — there is zero reason to use the paid ANTHROPIC_API_KEY.
"""

import os
import json
import logging
import re
import time
from typing import List, Dict, Any, Optional

import httpx

from .anonymizer import PresidioAnonymizer, AnonymizationResult

logger = logging.getLogger(__name__)

# Refinement uses the Bridge's own OpenAI-compatible endpoint (OAuth, free)
# NOT the direct Anthropic API (paid, requires ANTHROPIC_API_KEY)
# When running in privacy-service container, localhost:8000 won't work.
# Use BRIDGE_SELF_URL env var to point to nginx LB.
BRIDGE_SELF_URL = os.getenv("BRIDGE_SELF_URL", "http://localhost:8000/v1/chat/completions")
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Stage-2 refinement toggle. Default OFF: with the local Flair NER now doing
# precise detection, the cloud-Haiku refiner is redundant — it is blind (sees
# only placeholders + types, never the values), restored ~1 in 22 entities, and
# cost 25-52s PER call. Kept behind a flag purely for A/B comparison. The
# value-aware successor is a LOCAL LLM judge that receives the entity VALUES
# (see judge prototype) — not this endpoint.
USE_REFINEMENT = os.getenv("SMART_ANONYMIZE_USE_REFINEMENT", "false").lower() == "true"

# Refinement runs through the full worker pool (nginx → worker → Anthropic) and,
# under concurrent document-pipeline load, legitimately takes 25-52s+ (observed
# live; latency is pool-concurrency-bound, NOT output-size-bound — an 8-entity and
# a 169-entity call both land ~50s). The old hard 60s sat right on that latency
# band → intermittent ReadTimeout mid-generation. Keep this strictly BELOW the
# outer proxy timeout (main.py smart-anonymize proxy, 270s) so this inner timeout
# surfaces first with a clear error instead of being cut blind by the proxy.
REFINEMENT_TIMEOUT_S = 240.0
# Above this we log a loud WARNING (call succeeded but pool is under heavy load).
REFINEMENT_SLOW_WARN_S = 90.0


# ── PII restore guard (GDPR fail-safe) ──────────────────────────────────────
# Stage 2 (AI refinement) may choose to RESTORE a placeholder for readability.
# But an LLM is probabilistic, and Presidio sometimes mislabels a span (observed
# live: an e-mail tagged BOTH as EMAIL_ADDRESS and LOCATION, then "restored" as a
# general place name → raw PII leaked into the anonymized output). For a GDPR
# guarantee we never trust the LLM for hard PII. A placeholder is kept anonymized
# — regardless of the RESTORE decision — if EITHER its entity type is critical
# (correctly-labelled PII) OR its original value matches a hard PII pattern
# (catches the mislabel case by value, independent of the wrong type). The LLM
# stays advisory; this guard is the deterministic backstop.
_NEVER_RESTORE_TYPES = frozenset({
    "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE",
    "CREDIT_CARD", "IP_ADDRESS", "US_SSN", "CRYPTO", "MEDICAL_LICENSE",
})

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Za-z0-9]{10,30}\b")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_CC_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PHONE_CHARS_RE = re.compile(r"^[\d\s+/().\-]+$")


def _value_is_hard_pii(value: str) -> bool:
    """True if the value looks like hard PII by pattern (type-independent).

    Conservative: a false positive only over-anonymizes (keeps a placeholder
    masked that could have been restored) — the safe direction.
    """
    if not value:
        return False
    if _EMAIL_RE.search(value) or _IBAN_RE.search(value) or _IP_RE.search(value):
        return True
    if _CC_RE.search(value):
        return True
    # Phone-like: only digits/phone separators AND at least 9 digits. The >=9
    # floor avoids over-blocking 8-digit dates (DDMMYYYY) which the refinement is
    # allowed to restore; correctly-typed phones are still caught by type.
    if _PHONE_CHARS_RE.match(value.strip()) and len(re.sub(r"\D", "", value)) >= 9:
        return True
    return False


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
            f"placeholder(s) absent from smart_anonymized_text: {missing[:5]} "
            f"(USE_REFINEMENT={USE_REFINEMENT})"
        )


def _assert_no_hard_pii_leak(
    mapping: Dict[str, str],
    text: str,
    type_by_placeholder: Dict[str, str],
) -> None:
    """Invariant 2: original values of hard-PII KEEP entities must NOT appear
    as plaintext in the anonymized text.

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
        if len(original) > 3 and original in text:
            leaks.append((placeholder, entity_type))
    if leaks:
        raise ValueError(
            f"smart_anonymize PII leak: {len(leaks)} hard-PII entity value(s) "
            f"appear as plaintext in smart_anonymized_text — "
            + ", ".join(f"{ph}({t})" for ph, t in leaks[:5])
        )


def _must_stay_anonymized(
    placeholder: str, value: str, type_by_placeholder: Dict[str, str]
) -> bool:
    """Refuse-restore decision: critical entity type OR hard-PII value pattern."""
    if type_by_placeholder.get(placeholder, "") in _NEVER_RESTORE_TYPES:
        return True
    return _value_is_hard_pii(value)


def _build_refinement_prompt(
    anonymized_text: str,
    entities: List[Dict[str, Any]],
    context_hint: Optional[str] = None
) -> str:
    """Build the prompt for AI refinement of anonymized entities."""

    entity_list = "\n".join(
        f"- {e['placeholder']}: Typ={e['type']}, Konfidenz={e['confidence']:.0%}"
        for e in entities
    )

    context = context_hint or "Fachtext / technisches Dokument"

    return f"""Du analysierst einen pseudonymisierten Text. Ein NLP-System hat potenzielle personenbezogene Daten erkannt und durch Platzhalter ersetzt.

<pseudonymisierter_text>
{anonymized_text}
</pseudonymisierter_text>

<erkannte_entitaeten>
{entity_list}
</erkannte_entitaeten>

<dokumenttyp>{context}</dokumenttyp>

Entscheide fuer JEDEN Platzhalter:

RESTORE — wenn der Originalwert:
- fuer das Textverstaendnis wesentlich ist UND
- keine personenbezogenen Daten enthaelt
- Beispiele: allgemeine Ortsnamen ("Wien", "Graz"), Datumsangaben, allgemeine Organisationsbezeichnungen ("Gemeinde", "Magistrat"), technische Begriffe die faelschlich erkannt wurden

KEEP — wenn der Originalwert:
- ein personenbezogenes Datum ist das anonymisiert bleiben muss
- Beispiele: Personennamen, Telefonnummern, E-Mail-Adressen, IBANs, Kreditkartennummern, IP-Adressen, spezifische Firmennamen die Rueckschluesse auf Personen erlauben

Antworte ausschliesslich als JSON-Array. Keine Erklaerung ausserhalb des Arrays.

[
  {{"placeholder": "ANON_XXX_001", "decision": "RESTORE", "reason": "Allgemeiner Ortsname, kein PII"}},
  {{"placeholder": "ANON_XXX_002", "decision": "KEEP", "reason": "Personenname"}}
]"""


async def refine_anonymization(
    result: AnonymizationResult,
    context_hint: Optional[str] = None
) -> Dict[str, Any]:
    """
    Use Claude to evaluate which anonymized entities should be restored.

    Routes through the Bridge's own /v1/chat/completions endpoint (OAuth, free).
    This is a TEXT-ONLY call — no vision, no paid API key needed.

    Args:
        result: Presidio AnonymizationResult with all detected entities
        context_hint: Optional document type hint for better decisions

    Returns:
        Dict with:
        - decisions: List of {placeholder, decision, reason}
        - restore_placeholders: List of placeholders to restore
        - keep_placeholders: List of placeholders to keep anonymized
    """
    if not result.detected_entities:
        return {
            "decisions": [],
            "restore_placeholders": [],
            "keep_placeholders": []
        }

    # Build entity info for the prompt
    entities = [
        {
            "placeholder": e.placeholder,
            "type": e.entity_type,
            "confidence": e.confidence
        }
        for e in result.detected_entities
    ]

    prompt = _build_refinement_prompt(
        result.anonymized_text, entities, context_hint
    )

    # Call via Bridge's own OpenAI-compatible endpoint (OAuth, free)
    # This is a localhost self-call — no external API key needed
    request_body = {
        "model": HAIKU_MODEL,
        "max_tokens": 2000,
        "temperature": 0,
        "stream": False,
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }

    logger.info(
        f"Smart anonymize: refining {len(entities)} entities with {HAIKU_MODEL} (via Bridge self-call)",
        extra={"entity_count": len(entities), "context": context_hint}
    )

    # Optional: Bridge API key for endpoint protection (not Anthropic key)
    headers = {"Content-Type": "application/json"}
    bridge_api_key = os.getenv("API_KEY")
    if bridge_api_key:
        headers["Authorization"] = f"Bearer {bridge_api_key}"

    start = time.monotonic()
    async with httpx.AsyncClient(timeout=REFINEMENT_TIMEOUT_S) as client:
        response = await client.post(
            BRIDGE_SELF_URL,
            headers=headers,
            json=request_body
        )
    elapsed = time.monotonic() - start
    if elapsed > REFINEMENT_SLOW_WARN_S:
        logger.warning(
            f"Refinement self-call slow: {elapsed:.1f}s for {len(entities)} entities "
            f"(timeout={REFINEMENT_TIMEOUT_S:.0f}s). Worker pool likely under concurrent load."
        )

    if response.status_code != 200:
        error_body = response.text
        logger.error(
            f"Refinement self-call error: {response.status_code}",
            extra={"status_code": response.status_code, "error": error_body[:500]}
        )
        raise RuntimeError(
            f"AI refinement failed ({response.status_code}): {error_body[:200]}"
        )

    data = response.json()

    # OpenAI-compatible response format
    choices = data.get("choices", [])
    response_text = ""
    if choices:
        message = choices[0].get("message", {})
        response_text = message.get("content", "")

    usage = data.get("usage", {})
    logger.info(
        f"Refinement response: {usage.get('prompt_tokens', 0)} in, "
        f"{usage.get('completion_tokens', 0)} out tokens"
    )

    # Parse JSON response
    try:
        # Strip markdown code fences if present
        clean = response_text.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
            clean = clean.rsplit("```", 1)[0]
            clean = clean.strip()

        decisions = json.loads(clean)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(
            f"Failed to parse refinement response: {e}",
            extra={"response": response_text[:500]}
        )
        # Fallback: keep everything anonymized (safe default)
        decisions = [
            {"placeholder": e.placeholder, "decision": "KEEP", "reason": "Parse error fallback"}
            for e in result.detected_entities
        ]

    restore_placeholders = [
        d["placeholder"] for d in decisions if d.get("decision") == "RESTORE"
    ]
    keep_placeholders = [
        d["placeholder"] for d in decisions if d.get("decision") == "KEEP"
    ]

    return {
        "decisions": decisions,
        "restore_placeholders": restore_placeholders,
        "keep_placeholders": keep_placeholders
    }


async def smart_anonymize(
    text: str,
    language: str = "de",
    context_hint: Optional[str] = None,
    prefix: Optional[str] = None
) -> Dict[str, Any]:
    """
    Full smart anonymization pipeline: Presidio + AI refinement.

    Args:
        text: Input text with potential PII
        language: Language code ('de' or 'en')
        context_hint: Document type for better AI decisions
        prefix: Document-scoped prefix for placeholders (e.g. 'Da1b2c3'). Default: 'ANON'

    Returns:
        Complete result with raw + refined anonymization
    """
    anonymizer = PresidioAnonymizer(language=language)

    # Stage 1: Presidio (aggressive detection)
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

    # Default path: local Flair NER is precise enough — skip the blind cloud
    # refiner entirely and return the detection result directly.
    if not USE_REFINEMENT:
        _mapping = dict(raw_result.mapping)
        _text = raw_result.anonymized_text
        _type_by_ph = {e.placeholder: e.entity_type for e in raw_result.detected_entities}
        _assert_mapping_text_consistent(_mapping, _text)
        _assert_no_hard_pii_leak(_mapping, _text, _type_by_ph)
        return {
            "status": "success",
            "anonymization_performed": True,
            "raw_anonymized_text": _text,
            "raw_entity_count": raw_result.entity_count,
            "smart_anonymized_text": _text,
            "smart_entity_count": len(_mapping),
            "restored_entities": [],
            "mapping": _mapping,
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

    # Stage 2: AI refinement (opt-in via SMART_ANONYMIZE_USE_REFINEMENT=true)
    refinement = await refine_anonymization(raw_result, context_hint)

    # Stage 2b: Selective restore
    smart_text = raw_result.anonymized_text
    smart_mapping = dict(raw_result.mapping)
    restored_entities = []

    # Entity type per placeholder, for the hard-PII restore guard.
    type_by_placeholder = {
        e.placeholder: e.entity_type for e in raw_result.detected_entities
    }
    refused_restores: List[str] = []

    for placeholder in refinement["restore_placeholders"]:
        if placeholder in smart_mapping:
            original = smart_mapping[placeholder]

            # GDPR fail-safe: never restore hard PII, even when the AI said
            # RESTORE — guards against LLM error and Presidio type-mislabelling.
            if _must_stay_anonymized(placeholder, original, type_by_placeholder):
                logger.warning(
                    "Refinement RESTORE refused for %s (type=%s): hard PII, "
                    "kept anonymized.",
                    placeholder, type_by_placeholder.get(placeholder, "?"),
                )
                refused_restores.append(placeholder)
                continue

            smart_text = smart_text.replace(placeholder, original)
            del smart_mapping[placeholder]

            # Find decision reason
            reason = ""
            for d in refinement["decisions"]:
                if d["placeholder"] == placeholder:
                    reason = d.get("reason", "")
                    break

            restored_entities.append({
                "placeholder": placeholder,
                "original": original,
                "reason": reason
            })

    # Build full entity list with decisions
    all_entities = []
    for entity in raw_result.detected_entities:
        decision_info = next(
            (d for d in refinement["decisions"] if d["placeholder"] == entity.placeholder),
            {"decision": "KEEP", "reason": ""}
        )
        all_entities.append({
            "placeholder": entity.placeholder,
            "type": entity.entity_type,
            "original": entity.original_text,
            "confidence": entity.confidence,
            "decision": decision_info.get("decision", "KEEP"),
            "reason": decision_info.get("reason", "")
        })

    _assert_mapping_text_consistent(smart_mapping, smart_text)
    _assert_no_hard_pii_leak(smart_mapping, smart_text, type_by_placeholder)
    return {
        "status": "success",
        "anonymization_performed": True,
        "raw_anonymized_text": raw_result.anonymized_text,
        "raw_entity_count": raw_result.entity_count,
        "smart_anonymized_text": smart_text,
        "smart_entity_count": len(smart_mapping),
        "restored_entities": restored_entities,
        "mapping": smart_mapping,
        "detected_entities": all_entities
    }
