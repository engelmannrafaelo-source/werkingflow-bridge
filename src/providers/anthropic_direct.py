"""Direct Anthropic Messages API — fallback tier for the Claude Code SDK path.

Reuses the existing vision-routing machinery (src/routing/vision_router.py,
ANTHROPIC_VISION_API_KEY) for plain text calls. That path has no CLI
subprocess and no tool support — safe ONLY for requests that already run
with enable_tools=false (see get_fallback_tiers(tools_required=...) in
src/providers/fallback.py, which excludes this tier otherwise).

Why this exists: the Claude Code SDK path (claude_cli.py) spawns a `claude`
CLI subprocess with ~4K tokens of fixed tool-schema scaffolding per call and
its own MAX_TIMEOUT ceiling (default 2400s). A long Extended Thinking
generation (measured: 40-77K output tokens on the "Heimbau" energy-report
incident, 2026-07) can hit that ceiling. Measured 2026-07-10: an equivalent
large call (38K in / 32K out) took 290s going direct vs. timing out on the
CLI path. Rafael 2026-07-11: route calls that keep failing on the CLI path
through this direct path instead, once they're not tool-capable anyway.
"""

import logging
import time
import uuid
from typing import Any, Dict, Optional

from src.models import ChatCompletionRequest
from src.routing.backend_router import BackendConfig
from src.routing.vision_router import prepare_messages_for_vision, route_to_vision
from src.vision_provider import abschluss_felder_sicher, finish_reason_for

logger = logging.getLogger(__name__)

# The CLI path's own session-level ceiling (claude_cli.py MAX_TIMEOUT default)
# — give the direct path at least as much room, since it exists specifically
# to rescue calls that would otherwise hit that ceiling.
DEFAULT_TIMEOUT_S = 2400.0

#: Nur wenn der Aufrufer kein max_tokens schickt. Wird geloggt und im Antwortkopf
#: (x_bridge_max_tokens_defaulted) gemeldet.
DEFAULT_MAX_TOKENS = 4096


async def call_anthropic_direct(
    request: ChatCompletionRequest,
    config: BackendConfig,
    timeout: float = DEFAULT_TIMEOUT_S,
    messung_ziel: Optional[Any] = None,
) -> Dict[str, Any]:
    """Call the direct Anthropic Messages API and return an OpenAI-compatible
    chat-completion response dict — same shape call_openai_compatible()
    returns. Called from the direct-tier branch in main.py.

    The answer always carries ``x_bridge_finish`` (vision_provider.
    abschluss_felder). A provider refusal is the Ablehnungsmarke: finish_reason
    content_filter, refusal true, retryable false, verwertbar false — never
    retried, never rerouted (S0, 17.11.2026).

    Args:
        request: The original chat completion request (OpenAI format).
        config: Resolved BackendConfig for this tier (backend_router.py's
            _resolve_provider_tier ANTHROPIC_DIRECT branch — provider_model
            carries the model; provider_api_key is resolved there for
            validation but unused here, VisionProvider reads
            ANTHROPIC_VISION_API_KEY from env itself).
        timeout: HTTP timeout in seconds — see DEFAULT_TIMEOUT_S.
        messung_ziel: nur der Test-KI-Torzweig in main.py uebergibt hier ein
            frisches test_ki_deckel.MessungZiel; es bekommt die UsageMessung
            der Rohantwort. Sie geht NIE in das Antwort-dict (JSON, SSE,
            Job-Ergebnis und die Fallback-Aufrufe sehen sie nicht).

    Returns:
        OpenAI-compatible response dict with choices and usage.

    Raises:
        Exception: if the direct call fails — same contract as
            call_openai_compatible(), so the caller's existing retry/breaker
            logic (is_retryable_error / is_breaker_failure) applies unchanged.
    """
    messages = prepare_messages_for_vision(request.messages)

    # max_tokens: Aufrufer-Wert wird durchgereicht. Nur wenn keiner kommt, gilt
    # die Vorgabe — und die ist sichtbar (Log + x_bridge_max_tokens_defaulted),
    # nie still.
    max_tokens_defaulted = not request.max_tokens
    if max_tokens_defaulted:
        logger.warning(
            "claude-direct: kein max_tokens im Request — Vorgabe %d wird gesetzt",
            DEFAULT_MAX_TOKENS,
        )

    # S1: Anbieter-Fallback nur auf ausdrueckliche Bestellung je Aufruf.
    # Jede Unvertraeglichkeit ist ein lauter 4xx (fallback_faehigkeit), nie
    # ein stiller Aufruf ohne Fallback.
    fallback_kette = None
    bestellung = getattr(request, "provider_fallback", None)
    if bestellung is not None:
        from src.providers import fallback_faehigkeit as _ff
        if bestellung != _ff.MODUS:
            raise _ff.FallbackNichtMoeglich("provider_fallback_wert_unzulaessig", repr(bestellung))
        if getattr(request, "stream", False):
            raise _ff.FallbackNichtMoeglich("fallback_nicht_verfuegbar_pfad", "stream")
        gesetzt = getattr(request, "model_fields_set", set()) or set()
        ausdruecklich = sorted(f for f in ("temperature", "top_p") if f in gesetzt)
        if ausdruecklich:
            # API-Version 2026-10-21: 400 auch beim Standardwert (Beleg
            # b0e0e525c4b0 Z. 127). Nie still verwerfen.
            raise _ff.FallbackNichtMoeglich("fallback_body_unvertraeglich", f"ausdruecklich gesetzt: {ausdruecklich}")
        fallback_kette = await _ff.bestellung_pruefen(config.provider_model or request.model)

    from src.providers import modell_exakt as _modell_exakt
    exakt = _modell_exakt.bestellt(request)
    if exakt:
        # Vor jedem Anbieteraufruf: nicht gelistet / Liste unbekannt -> laut, kein Aufruf.
        await _modell_exakt.verfuegbar_pruefen(config.provider_model or request.model)

    result = await route_to_vision(
        **({"fallback_kette": fallback_kette} if fallback_kette is not None else {}),
        messages=messages,
        model=config.provider_model or request.model,
        max_tokens=request.max_tokens or DEFAULT_MAX_TOKENS,
        temperature=request.temperature if request.temperature is not None else 0.7,
        timeout=timeout,
        thinking=request.thinking,
        output_config=request.output_config,
    )
    if messung_ziel is not None:
        messung_ziel.setzen(result.messung)

    gesendetes_modell = config.provider_model or request.model
    # getattr: Felder, die nur der Anthropic-Messages-Weg setzt (VisionResult);
    # fehlen sie, gilt "nicht gemeldet" (None), nie ein Ersatzwert.
    finish = abschluss_felder_sicher(
        content=result.content,
        stop_reason=result.stop_reason,
        stop_details=getattr(result, "stop_details", None),
        requested_model=getattr(result, "requested_model", None) or gesendetes_modell,
        responding_model=result.model,
        provider_request_id=getattr(result, "provider_request_id", None),
        provider_message_id=getattr(result, "provider_message_id", None),
        messung=getattr(result, "messung", None),
        usage_iterations=getattr(result, "usage_iterations", None),
        fallback_kette=fallback_kette,
        fallback_blocks=getattr(result, "fallback_blocks", None),
    )
    if exakt:
        finish["modell_wahl"] = _modell_exakt.wahl(request.model, gesendetes_modell,
                                                    getattr(result, "anbieter_modell", None))

    return {
        "id": f"chatcmpl-direct-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": result.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.content},
                # Echter Abbruchgrund: max_tokens -> "length", nie pauschal "stop".
                "finish_reason": "content_filter" if finish["refusal"] else finish_reason_for(result.stop_reason),
            }
        ],
        "usage": result.usage,
        "x_api_reserve": result.extra_meta,
        "x_bridge_max_tokens": request.max_tokens or DEFAULT_MAX_TOKENS,
        "x_bridge_max_tokens_defaulted": max_tokens_defaulted,
        "x_bridge_finish": finish,
    }
