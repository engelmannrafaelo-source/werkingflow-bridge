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

DESIGN — defensive, fail-fast, no silent degradation:
  * Stage 1 (Presidio detection) is the SAFETY boundary. If it fails, no
    anonymization happened — the exception propagates (hard error). The caller
    must NOT proceed with cleartext.
  * Stage 2 (Haiku refinement) only un-masks over-detected NON-PII. It is run
    in batches so a large document never overflows the response budget. Any
    refinement failure — non-200, timeout, malformed JSON, or a TRUNCATED
    response — raises a descriptive hard error. We never keep a partial /
    truncated decision list, because a silently-incomplete decision list is
    almost impossible to trace and would silently restore or keep the wrong
    entities.
"""

import os
import json
import logging
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

# Refinement is batched so the JSON decision array never overflows the response
# budget (which historically truncated → unparseable JSON → HTTP 500 on >5 KB
# inputs). Each batch's response budget is sized to its entity count with
# generous headroom; a batch that still hits the budget (finish_reason=length)
# is a HARD ERROR, never a silent truncation.
REFINE_BATCH_SIZE = 120          # entities per Haiku refinement call
TOKENS_PER_DECISION = 30         # rough budget per decision JSON object
REFINE_TOKEN_HEADROOM = 500      # fixed overhead for array framing / reasons
MAX_REFINE_TOKENS = 8000         # ceiling per single call


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


class RefinementError(RuntimeError):
    """Raised when AI refinement cannot produce a complete, valid decision list.

    Always a hard, descriptive error — never a silent fallback or a partial /
    truncated result. The caller fails fast; it must never proceed as if the
    refinement succeeded.
    """


async def _refine_batch(
    anonymized_text: str,
    batch_entities: List[Dict[str, Any]],
    context_hint: Optional[str],
) -> List[Dict[str, Any]]:
    """Run ONE Haiku refinement call for a batch of entities.

    Raises ``RefinementError`` on any problem (non-200, timeout, truncation,
    unparseable / non-list JSON). Never returns a partial or guessed result.
    """
    prompt = _build_refinement_prompt(anonymized_text, batch_entities, context_hint)

    # Size the response budget to this batch with headroom, capped. With
    # REFINE_BATCH_SIZE=120 this is ~4100 tokens — comfortably below the cap, so
    # a well-behaved model never truncates. If it does, we detect and hard-fail.
    max_tokens = min(MAX_REFINE_TOKENS, len(batch_entities) * TOKENS_PER_DECISION + REFINE_TOKEN_HEADROOM)

    request_body = {
        "model": HAIKU_MODEL,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
    }

    headers = {"Content-Type": "application/json"}
    bridge_api_key = os.getenv("API_KEY")
    if bridge_api_key:
        headers["Authorization"] = f"Bearer {bridge_api_key}"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(BRIDGE_SELF_URL, headers=headers, json=request_body)
    except httpx.HTTPError as e:
        raise RefinementError(f"refinement self-call transport error: {type(e).__name__}: {e}") from e

    if response.status_code != 200:
        raise RefinementError(
            f"refinement self-call returned HTTP {response.status_code}: {response.text[:200]}"
        )

    try:
        data = response.json()
    except ValueError as e:
        raise RefinementError(f"refinement self-call returned non-JSON envelope: {e}") from e

    choices = data.get("choices") or []
    if not choices:
        raise RefinementError("refinement self-call returned no choices")

    choice = choices[0]
    finish_reason = choice.get("finish_reason")
    response_text = (choice.get("message") or {}).get("content", "") or ""

    usage = data.get("usage", {})
    logger.info(
        "Refinement batch: %s entities, finish_reason=%s, %s in / %s out tokens (max_tokens=%s)",
        len(batch_entities), finish_reason,
        usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), max_tokens,
    )

    # HARD FAIL on truncation. A response cut off at the token budget yields a
    # silently-incomplete decision list — refuse it loudly rather than guess.
    if finish_reason == "length":
        raise RefinementError(
            f"refinement response TRUNCATED (finish_reason=length) at max_tokens={max_tokens} "
            f"for a batch of {len(batch_entities)} entities — refusing a partial decision list. "
            f"Lower REFINE_BATCH_SIZE if this recurs."
        )

    # Strip markdown code fences if present, then parse.
    clean = response_text.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
        clean = clean.rsplit("```", 1)[0]
        clean = clean.strip()

    try:
        decisions = json.loads(clean)
    except (json.JSONDecodeError, ValueError) as e:
        # Not flagged as truncated but still unparseable → a complete-but-malformed
        # response. Hard error; do not fabricate decisions.
        raise RefinementError(
            f"refinement response was not valid JSON ({e}); head={clean[:200]!r}"
        ) from e

    if not isinstance(decisions, list):
        raise RefinementError(f"refinement JSON is {type(decisions).__name__}, expected a list")

    return decisions


async def refine_anonymization(
    result: AnonymizationResult,
    context_hint: Optional[str] = None
) -> Dict[str, Any]:
    """
    Use Claude to evaluate which anonymized entities should be restored.

    Routes through the Bridge's own /v1/chat/completions endpoint (OAuth, free).
    This is a TEXT-ONLY call — no vision, no paid API key needed.

    Entities are refined in batches so the decision array never overflows the
    response budget. Any batch failure raises ``RefinementError`` (fail fast,
    no silent degradation, no silent truncation).

    Returns:
        Dict with:
        - decisions: List of {placeholder, decision, reason}
        - restore_placeholders: List of placeholders to restore
        - keep_placeholders: List of placeholders to keep anonymized
    """
    if not result.detected_entities:
        return {"decisions": [], "restore_placeholders": [], "keep_placeholders": []}

    entities = [
        {"placeholder": e.placeholder, "type": e.entity_type, "confidence": e.confidence}
        for e in result.detected_entities
    ]

    batches = [
        entities[i:i + REFINE_BATCH_SIZE]
        for i in range(0, len(entities), REFINE_BATCH_SIZE)
    ]
    logger.info(
        "Smart anonymize: refining %s entities in %s batch(es) of <=%s with %s",
        len(entities), len(batches), REFINE_BATCH_SIZE, HAIKU_MODEL,
    )

    decisions: List[Dict[str, Any]] = []
    for idx, batch in enumerate(batches, start=1):
        try:
            decisions.extend(await _refine_batch(result.anonymized_text, batch, context_hint))
        except RefinementError as e:
            # Add batch context and re-raise. No fallback — the caller fails fast.
            raise RefinementError(f"refinement batch {idx}/{len(batches)} failed: {e}") from e

    restore_placeholders = [
        d["placeholder"] for d in decisions
        if isinstance(d, dict) and d.get("decision") == "RESTORE" and "placeholder" in d
    ]
    keep_placeholders = [
        d["placeholder"] for d in decisions
        if isinstance(d, dict) and d.get("decision") == "KEEP" and "placeholder" in d
    ]

    return {
        "decisions": decisions,
        "restore_placeholders": restore_placeholders,
        "keep_placeholders": keep_placeholders,
    }


async def smart_anonymize(
    text: str,
    language: str = "de",
    context_hint: Optional[str] = None,
    prefix: Optional[str] = None
) -> Dict[str, Any]:
    """
    Full smart anonymization pipeline: Presidio + AI refinement.

    Fail-fast: a failure in either stage propagates. Detection (Presidio) is the
    safety boundary; refinement raises ``RefinementError`` on any problem
    (including truncation). The endpoint converts a raised exception into an
    explicit ``status="error"`` response — it NEVER returns the input as
    cleartext-success.

    Args:
        text: Input text with potential PII
        language: Language code ('de' or 'en')
        context_hint: Document type for better AI decisions
        prefix: Document-scoped prefix for placeholders (e.g. 'Da1b2c3'). Default: 'ANON'

    Returns:
        Complete result with raw + refined anonymization
    """
    anonymizer = PresidioAnonymizer(language=language)

    # Stage 1: Presidio (aggressive detection). Safety-critical — let failures propagate.
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

    # Stage 2: AI refinement. Raises on any failure (no silent degrade/truncate).
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
            (d for d in refinement["decisions"] if d.get("placeholder") == entity.placeholder),
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
