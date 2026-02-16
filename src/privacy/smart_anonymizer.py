"""
Smart Anonymizer — AI-refined pseudonymization

2-stage process:
1. Presidio detects ALL potential PII (aggressive, catches everything)
2. Claude Haiku evaluates each detection: real PII → keep anonymized,
   context-relevant non-PII → restore original value

This produces a "smart" pseudonymized text where only actual personal data
is masked, while context-relevant information (city names, dates,
organization types) is preserved.
"""

import os
import json
import logging
from typing import List, Dict, Any, Optional

import httpx

from .anonymizer import PresidioAnonymizer, AnonymizationResult

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
HAIKU_MODEL = "claude-haiku-4-5-20251001"


def _get_api_key() -> str:
    """Get Anthropic API key for refinement calls."""
    key = os.getenv("ANTHROPIC_VISION_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_VISION_API_KEY not set. "
            "Smart anonymization requires direct API access for AI refinement."
        )
    return key


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
    Use Claude Haiku to evaluate which anonymized entities should be restored.

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

    api_key = _get_api_key()

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

    # Call Claude Haiku via Anthropic API
    request_body = {
        "model": HAIKU_MODEL,
        "max_tokens": 2000,
        "temperature": 0,
        "messages": [
            {"role": "user", "content": prompt}
        ]
    }

    logger.info(
        f"Smart anonymize: refining {len(entities)} entities with {HAIKU_MODEL}",
        extra={"entity_count": len(entities), "context": context_hint}
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            ANTHROPIC_API_URL,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION
            },
            json=request_body
        )

    if response.status_code != 200:
        error_body = response.text
        logger.error(
            f"Refinement API error: {response.status_code}",
            extra={"status_code": response.status_code, "error": error_body[:500]}
        )
        raise RuntimeError(
            f"AI refinement failed ({response.status_code}): {error_body[:200]}"
        )

    data = response.json()

    # Extract text response
    content_blocks = data.get("content", [])
    response_text = ""
    for block in content_blocks:
        if block.get("type") == "text":
            response_text += block.get("text", "")

    usage = data.get("usage", {})
    logger.info(
        f"Refinement response: {usage.get('input_tokens', 0)} in, "
        f"{usage.get('output_tokens', 0)} out tokens"
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
        return {
            "status": "success",
            "raw_anonymized_text": text,
            "raw_entity_count": 0,
            "smart_anonymized_text": text,
            "smart_entity_count": 0,
            "restored_entities": [],
            "mapping": {},
            "detected_entities": []
        }

    # Stage 2: AI refinement
    refinement = await refine_anonymization(raw_result, context_hint)

    # Stage 2b: Selective restore
    smart_text = raw_result.anonymized_text
    smart_mapping = dict(raw_result.mapping)
    restored_entities = []

    for placeholder in refinement["restore_placeholders"]:
        if placeholder in smart_mapping:
            original = smart_mapping[placeholder]
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

    return {
        "status": "success",
        "raw_anonymized_text": raw_result.anonymized_text,
        "raw_entity_count": raw_result.entity_count,
        "smart_anonymized_text": smart_text,
        "smart_entity_count": len(smart_mapping),
        "restored_entities": restored_entities,
        "mapping": smart_mapping,
        "detected_entities": all_entities
    }
