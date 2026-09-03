"""
Vision Router - Handles image/multimodal request routing

Centralizes vision detection and API call logic to avoid duplication
between streaming and non-streaming endpoints.
"""

import logging
import os
from typing import Optional, List, Dict, Any
from dataclasses import dataclass

from src.activity.providers import PROVIDER_ANTHROPIC, PROVIDER_GEMINI
from src.vision_provider import VisionProvider, get_vision_provider

logger = logging.getLogger(__name__)

# usage_events.provider_metadata->>'api_key_lane' — WELCHER Schluessel bezahlt
# hat. Getrennt gehalten, weil beide echtes Geld auf verschiedenen Konten sind:
# die Anthropic-Tageskappe (src/routing/prepaid_cap.py) summiert ausschliesslich
# 'vision_prepaid'. Wuerde der Gemini-Weg dieselbe Fahrspur beschriften,
# zaehlte er gegen ein Guthaben, das er gar nicht belastet — und die Kappe waere
# ab dem ersten Gemini-Aufruf falsch.
LANE_ANTHROPIC_PREPAID = "vision_prepaid"
LANE_GEMINI_TEST = "vision_gemini"


# Ziele der Vision-Weiche. Kein Enum, weil die Werte roh in Log- und
# Ledger-Zeilen landen und dort lesbar bleiben sollen.
VISION_TARGET_ANTHROPIC = "anthropic"
VISION_TARGET_GEMINI = "gemini"


@dataclass
class VisionResult:
    """Result from vision analysis"""
    content: str
    model: str
    usage: Dict[str, int]
    # Echter Anthropic stop_reason — Pflichtfeld ohne Default: ein Konstruktions-
    # pfad, der ihn vergisst, soll beim Bauen scheitern, nicht erst beim
    # finish_reason-Mapping in main.py (genau so entstand der 500 vom 31.08.).
    stop_reason: str
    # Wer die Daten physisch bekommen hat + welcher Schluessel bezahlt hat.
    # Ebenfalls Pflichtfelder ohne Default, aus demselben Grund wie stop_reason
    # und aus einem zweiten: usage_events.provider ist der Nachweis fuer die
    # Datenresidenz-Zusage. Ein Default haette bedeutet, dass ein neuer
    # Vision-Anbieter still das Etikett des alten erbt — genau die Fehlerklasse,
    # die src/activity/providers.py beseitigt hat.
    ledger_provider: str
    api_key_lane: str


def default_vision_target() -> str:
    """Der Bild-Anbieter dieser Bridge, wenn der Aufrufer keinen nennt.

    Rafael, 2026-09-03: auf der DEV-Bridge soll die Bildanalyse
    **standardmaessig** ueber Gemini laufen — fuer alle Apps, ohne dass jede
    App einen provider_tier mitschicken muss. Das ist genau der Sinn dieses
    Schalters: der Anbieterwechsel ist eine Eigenschaft des Deployments, keine
    Aenderung an vier Aufrufstellen in jeder App.

    Default ist ``anthropic``, also das bisherige Verhalten. Der Schalter wird
    auf der dev-Bridge GEMEINSAM mit dem API-Key gesetzt und nicht davor: ohne
    Key wuerde er jeden Bildaufruf auf Staging in eine 403-Absage laufen
    lassen (die Sperre weist fail-loud ab, sie faellt bewusst nicht auf
    Anthropic zurueck). Reihenfolge also: Key hinterlegen, Flag einschalten,
    dann diesen Schalter umlegen.

    Ein unbekannter Wert faellt NICHT still auf den Default zurueck — ein
    Tippfehler in der Compose-Zeile soll auffallen, nicht monatelang so
    aussehen, als liefe alles ueber Gemini.
    """
    raw = (os.getenv("BRIDGE_VISION_DEFAULT_PROVIDER") or "").strip().lower()
    if not raw:
        return VISION_TARGET_ANTHROPIC
    if raw not in (VISION_TARGET_ANTHROPIC, VISION_TARGET_GEMINI):
        raise ValueError(
            f"BRIDGE_VISION_DEFAULT_PROVIDER={raw!r} ist unbekannt. Erlaubt: "
            f"{VISION_TARGET_ANTHROPIC!r}, {VISION_TARGET_GEMINI!r}."
        )
    return raw


def resolve_vision_target(backend_config: Any) -> str:
    """Welcher Anbieter soll dieses Bild sehen?

    Zwei Wege fuehren zu Gemini, und beide muessen dieselbe Sperre passieren
    (``gemini_vision_gate``):

    * Der Aufrufer waehlt ihn ausdruecklich per ``provider_tier`` — die
      normale Bridge-Provider-Mechanik, kein Sonderpfad. Diese Wahl gewinnt
      immer, damit eine Messung gezielt ein Modell ansprechen kann.
    * Oder das Deployment hat ihn als Standard gesetzt
      (``BRIDGE_VISION_DEFAULT_PROVIDER``, s.o.).

    Ein BEDROCK-Backend fasst diese Funktion nicht an: dort werden Bilder
    ohnehin nativ verarbeitet, und der Aufrufer laeuft gar nicht erst in den
    Vision-Zweig (main.py prueft das getrennt). Der Standard-Schalter darf
    einen Bedrock-Pin also nie ueberstimmen — das waere ein stiller Wechsel der
    Datenresidenz.
    """
    from src.models import BackendType

    backend = getattr(backend_config, "backend", None) if backend_config is not None else None

    if backend == BackendType.GEMINI_API:
        return VISION_TARGET_GEMINI
    if backend == BackendType.BEDROCK:
        return VISION_TARGET_ANTHROPIC
    if backend_config is not None and getattr(backend_config, "provider_tier", None):
        # Der Aufrufer hat ausdruecklich einen anderen Tier gewaehlt (z.B.
        # claude-direct-notools). Den ueberstimmt der Bridge-Standard nicht.
        return VISION_TARGET_ANTHROPIC
    return default_vision_target()


def serialize_message_content(content) -> Any:
    """Convert Pydantic content parts to dicts for VisionProvider compatibility"""
    if isinstance(content, str):
        return content
    # List of ContentPart Pydantic models -> list of dicts
    return [
        part.model_dump() if hasattr(part, 'model_dump') else part
        for part in content
    ]


def prepare_messages_for_vision(messages: List[Any]) -> List[Dict[str, Any]]:
    """Convert request messages to vision-compatible format"""
    return [
        {'role': m.role, 'content': serialize_message_content(m.content)}
        for m in messages
    ]


def has_vision_content(messages: List[Dict[str, Any]]) -> bool:
    """Check if messages contain images requiring vision routing"""
    return VisionProvider.has_images(messages)


async def route_to_vision(
    messages: List[Dict[str, Any]],
    model: str,
    max_tokens: Optional[int] = None,
    temperature: float = 0.7,
    timeout: float = 300.0,
    thinking: Optional[Dict[str, Any]] = None,
    output_config: Optional[Dict[str, Any]] = None,
    target: str = VISION_TARGET_ANTHROPIC,
) -> VisionResult:
    """
    Route request to Vision API

    Args:
        messages: Messages in vision-compatible format (use prepare_messages_for_vision)
        model: Model to use
        max_tokens: Maximum tokens for response
        temperature: Temperature for generation
        timeout: HTTP timeout in seconds — see VisionProvider.analyze()'s
            docstring for why non-vision fallback callers need this raised.
        thinking: Optional passthrough for the Anthropic Messages API
            'thinking' param — forwarded verbatim to VisionProvider.analyze().
        output_config: Optional passthrough for the Anthropic Messages API
            'output_config' param — forwarded verbatim.

    Returns:
        VisionResult with content, model, and usage info

    Raises:
        Exception: If vision analysis fails
    """
    if target == VISION_TARGET_GEMINI:
        from src.providers.gemini_vision import (
            get_gemini_vision_provider,
            resolve_gemini_vision_model,
        )
        from src.routing.gemini_vision_gate import assert_no_anthropic_only_params

        # Anthropic-eigene Regler duerfen hier nicht stillschweigend verfallen.
        assert_no_anthropic_only_params(thinking=thinking, output_config=output_config)

        logger.info("🖼️ Routing to Vision API (Gemini)")
        gemini_response = await get_gemini_vision_provider().analyze(
            messages=messages,
            model=resolve_gemini_vision_model(),
            # BEWUSST durchgereicht statt auf 4096 defaultet: die Bridge
            # erfindet auf diesem Weg keinen Ausgabendeckel (Rafael 2026-09-03).
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
        )
        return VisionResult(
            content=gemini_response.content,
            model=gemini_response.model,
            usage=gemini_response.usage,
            stop_reason=gemini_response.stop_reason,
            ledger_provider=PROVIDER_GEMINI,
            api_key_lane=LANE_GEMINI_TEST,
        )

    logger.info("🖼️ Routing to Vision API (direct Anthropic)")

    vision_provider = get_vision_provider()

    vision_response = await vision_provider.analyze(
        messages=messages,
        model=model,
        # Der Anthropic-Weg BRAUCHT max_tokens (Pflichtfeld der Messages API),
        # deshalb bleibt der bisherige Default genau hier stehen — und nur hier.
        max_tokens=max_tokens if max_tokens is not None else 4096,
        temperature=temperature,
        timeout=timeout,
        thinking=thinking,
        output_config=output_config
    )

    return VisionResult(
        content=vision_response.content,
        model=vision_response.model,
        usage=vision_response.usage,
        stop_reason=vision_response.stop_reason,
        ledger_provider=PROVIDER_ANTHROPIC,
        api_key_lane=LANE_ANTHROPIC_PREPAID,
    )


async def check_and_route_vision(
    messages: List[Any],
    model: str,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    thinking: Optional[Dict[str, Any]] = None,
    output_config: Optional[Dict[str, Any]] = None,
    target: str = VISION_TARGET_ANTHROPIC,
) -> Optional[VisionResult]:
    """
    Check if messages need vision routing, and route if needed

    Convenience function that combines has_vision_content check with routing.

    Args:
        messages: Original request messages (Pydantic models)
        model: Model to use
        max_tokens: Maximum tokens (default 4096)
        temperature: Temperature (default 0.7)
        thinking: Optional passthrough for the Anthropic Messages API
            'thinking' param — forwarded verbatim to route_to_vision().
        output_config: Optional passthrough for the Anthropic Messages API
            'output_config' param — forwarded verbatim.

    Returns:
        VisionResult if vision was needed, None otherwise

    Raises:
        Exception: If vision analysis fails
    """
    messages_for_vision = prepare_messages_for_vision(messages)

    if not has_vision_content(messages_for_vision):
        return None

    return await route_to_vision(
        messages=messages_for_vision,
        model=model,
        # Kein "or 4096" mehr: der Anthropic-Zweig setzt seinen Pflicht-Default
        # selbst, und der Gemini-Zweig soll ohne Deckel laufen, wenn der
        # Aufrufer keinen genannt hat.
        max_tokens=max_tokens,
        temperature=temperature or 0.7,
        thinking=thinking,
        output_config=output_config,
        target=target,
    )
