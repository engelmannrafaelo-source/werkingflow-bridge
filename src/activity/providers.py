"""Ledger provider vocabulary — SSoT for ``usage_events.provider``.

WHY THIS EXISTS
---------------
``usage_events.provider`` answers one question, and it is a compliance
question before it is a billing question: **who physically received this
call's data?** It is the evidence behind the customer-facing assurance
"keine Uebermittlung an Anthropic USA" (Datenschutz-Fassung 3.1, Kainer-AVV).

Until migration 053 the column carried ``DEFAULT 'anthropic'`` and the writer
carried ``provider: str = "anthropic"``. Every caller that did not think about
the question got "anthropic" written into the compliance ledger for free —
including calls that never left our own infrastructure and calls that went to
an entirely different company. Measured on the dev ledger before the fix:
27.292 of 69.321 rows (39%) claimed "anthropic" for local-only service calls
(docling, html-renderer, privacy-service). Transcription calls to OpenAI
Whisper were booked as "anthropic" as well.

That is not a rounding error in a dashboard — it is the audit trail saying the
opposite of what happened, in both directions:
  - it invents Anthropic transmissions that never occurred (inflating the
    apparent US exposure), and
  - it hides real third-party processors (OpenAI, Google, OpenRouter) behind
    the Anthropic label.

So the vocabulary is explicit and closed, the writer has no default, and the
column has no default. A caller MUST decide. Related prior art: the Bedrock
branch in ``main.py`` had to set ``ai_call_error_persisted`` specifically to
stop the generic error handler from booking a second, context-less
``'anthropic'`` row — 240 real Bedrock 400s showed up as 480 errors
(2026-07-20). Same root cause, patched locally; this module removes it.

WHAT IS DELIBERATELY *NOT* CHANGED HERE
---------------------------------------
``REAL_COST_PROVIDERS`` reproduces the pre-existing cost semantics exactly
(only bedrock + research-cloud book real EUR). Naming it does not change it.
Two known cost gaps stay open on purpose — they are billing decisions, not
labeling decisions, and bundling them into a compliance fix would hide them:
  - STT (openai / aws-sagemaker) is pay-per-use but books 0.00 EUR real cost.
  - ANTHROPIC_DIRECT (prepaid vision key) is real money on a prepaid key and
    also books 0.00 EUR.
Both are now at least *visible* as their own provider values instead of being
indistinguishable from subscription-covered pool traffic.
"""
from __future__ import annotations

from typing import Optional

from src.models import BackendType

# --- External providers: data physically left our infrastructure -----------
PROVIDER_ANTHROPIC = "anthropic"            # Anthropic API (subscription pool or prepaid key)
PROVIDER_BEDROCK = "bedrock"                # AWS Bedrock (EU region pinned)
PROVIDER_RESEARCH_CLOUD = "research-cloud"  # direct Anthropic API key (Weg C, src/research_cloud/)
PROVIDER_OPENAI = "openai"                  # OpenAI (Whisper STT)
PROVIDER_SAGEMAKER = "aws-sagemaker"        # self-hosted STT model on AWS SageMaker
PROVIDER_OPENAI_COMPATIBLE = "openai-compatible"  # OpenRouter et al. (BackendType.OPENAI_COMPATIBLE)
PROVIDER_GEMINI = "gemini"                  # Google Gemini CLI

# --- Non-external: nothing was transmitted to a third party ----------------
PROVIDER_LOCAL = "local"
"""Computed on our own infrastructure — no external provider call at all.
docling (PDF -> Markdown), html-renderer (PDF/screenshot), privacy-service
(anonymisation). These rows are zero-cost by design and must never appear in
an "was sent to Anthropic" query."""

PROVIDER_UNROUTED = "unrouted"
"""The call was rejected BEFORE a backend was resolved — entitlement gate,
budget gate, malformed identity. Nothing was transmitted anywhere. Typical
row: status='error', error_code='402'. Previously indistinguishable from a
real Anthropic call that failed."""

PROVIDER_UNKNOWN = "unknown"
"""Sentinel for a value the writer could not validate. Never written on
purpose — see ``normalize_ledger_provider``. Visible-wrong on purpose: the one
thing worse than an unknown provider is a *plausible* one."""

LEDGER_PROVIDERS = frozenset({
    PROVIDER_ANTHROPIC,
    PROVIDER_BEDROCK,
    PROVIDER_RESEARCH_CLOUD,
    PROVIDER_OPENAI,
    PROVIDER_SAGEMAKER,
    PROVIDER_OPENAI_COMPATIBLE,
    PROVIDER_GEMINI,
    PROVIDER_LOCAL,
    PROVIDER_UNROUTED,
    PROVIDER_UNKNOWN,
})

REAL_COST_PROVIDERS = frozenset({PROVIDER_BEDROCK, PROVIDER_RESEARCH_CLOUD})
"""Providers whose calls cost us real money per call, so the ledger's
real_cost_eur must carry the computed cost instead of 0.0. Verbatim the
pre-existing behaviour of ``resolve_ledger_cost`` — see the module docstring
for the two known gaps this intentionally does NOT close."""

EXTERNAL_PROVIDERS = LEDGER_PROVIDERS - {PROVIDER_LOCAL, PROVIDER_UNROUTED, PROVIDER_UNKNOWN}
"""Providers that received customer data. The set a data-protection query
should filter on — NOT ``provider = 'anthropic'`` alone."""

# BackendType -> ledger provider. Exhaustive on purpose: a new backend must be
# classified here before it can bill, otherwise we would be back to guessing.
_BACKEND_TO_PROVIDER = {
    BackendType.ANTHROPIC: PROVIDER_ANTHROPIC,
    BackendType.ANTHROPIC_DIRECT: PROVIDER_ANTHROPIC,
    BackendType.BEDROCK: PROVIDER_BEDROCK,
    BackendType.OPENAI_COMPATIBLE: PROVIDER_OPENAI_COMPATIBLE,
    BackendType.GEMINI_CLI: PROVIDER_GEMINI,
}


def ledger_provider_for_backend(backend: Optional[BackendType]) -> str:
    """Ledger provider value for a resolved backend.

    ``backend is None`` means no backend was ever resolved — the call was
    rejected upstream (gate/entitlement/budget) and nothing was transmitted.
    That is ``unrouted``, not "anthropic".

    Raises ValueError for a BackendType that nobody classified. Fail loud:
    the alternative is a new backend silently inheriting someone else's
    compliance label, which is the exact defect this module exists to remove.
    """
    if backend is None:
        return PROVIDER_UNROUTED
    try:
        return _BACKEND_TO_PROVIDER[backend]
    except KeyError:
        raise ValueError(
            f"BackendType {backend!r} has no ledger provider mapping. Add it to "
            "_BACKEND_TO_PROVIDER in src/activity/providers.py — usage_events.provider "
            "is compliance evidence and must not inherit an unrelated provider's label."
        ) from None


def normalize_ledger_provider(provider: str, *, context: str = "") -> str:
    """Validate a provider value on its way into the ledger.

    Returns the value unchanged when it is part of the vocabulary, otherwise
    logs an error and returns ``unknown``.

    Why not raise: ``persist_ai_call_activity`` is best-effort by contract —
    tracking must never break a customer's call, and not every call site is
    wrapped. Why not fall back to "anthropic": that is precisely the bug.
    An unknown value is loud in the logs and visibly wrong in every report,
    which is what a wrong value should be. Typos are caught before runtime by
    callers passing the module constants rather than string literals.
    """
    if provider in LEDGER_PROVIDERS:
        return provider
    # Local import: module-level logging config is owned by main.
    import logging

    logging.getLogger(__name__).error(
        "usage_events.provider=%r is not in the ledger vocabulary%s — booking %r "
        "instead. The ledger is the evidence for the EU data-residency assurance; "
        "an unclassified provider must not inherit a plausible label. Valid: %s",
        provider, f" ({context})" if context else "", PROVIDER_UNKNOWN,
        sorted(LEDGER_PROVIDERS),
    )
    return PROVIDER_UNKNOWN
