"""
Vision Provider - Direct Anthropic API for Image Analysis

The Claude Code SDK doesn't support multimodal (image) inputs.
This provider routes image-containing requests directly to the Anthropic API,
bypassing the SDK while maintaining the same response format.

Usage:
    provider = VisionProvider()
    response = await provider.analyze(messages, model="claude-sonnet-4-5-20250929")
"""

import os
import base64
import re
import httpx
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass
from fastapi import HTTPException
from config.logging_config import get_logger
from src.model_request import adapt_model_request
from src.model_registry import get_default_model, model_supports_temperature
from src.providers import credit_pool

logger = get_logger(__name__)


#: Herkunft einer UsageMessung: die Rohantwort der Anthropic Messages API.
QUELLE_MESSAGES_API = "anthropic_messages_api"
_MESSUNG_FELDER = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


@dataclass(frozen=True)
class UsageMessung:
    """Gemessene Usage, abgeleitet aus den UNVERAENDERTEN Rohwerten der Antwort
    (Test-KI-Deckel, src/budget/test_ki_deckel.py). `vollstaendig` nur, wenn
    `usage` ein dict ist und alle vier Felder echte int >= 0 sind (type(x) is
    int, also kein bool, kein Float, kein None). Unvollstaendig -> die Felder
    stehen auf 0 und duerfen nicht abgerechnet werden."""
    quelle: str
    vollstaendig: bool
    eingabe: int = 0
    ausgabe: int = 0
    cache_lesen: int = 0
    cache_anlage: int = 0


def usage_messung_aus(roh: Any) -> UsageMessung:
    if not isinstance(roh, dict):
        return UsageMessung(quelle=QUELLE_MESSAGES_API, vollstaendig=False)
    werte = [roh.get(f) for f in _MESSUNG_FELDER]
    if not all(type(w) is int and w >= 0 for w in werte):
        return UsageMessung(quelle=QUELLE_MESSAGES_API, vollstaendig=False)
    return UsageMessung(QUELLE_MESSAGES_API, True, *werte)


@dataclass
class VisionResponse:
    """Response from vision analysis."""
    content: str
    model: str
    usage: Dict[str, int]
    stop_reason: str
    # Nur der Anthropic-Messages-Weg setzt sie (_vision_response_from).
    messung: Optional[UsageMessung] = None
    credit_meta: Optional[Dict[str, Any]] = None
    # Nur der Abo-Bildweg (src/providers/subscription_vision.py) setzt sie:
    # eigene Ledger-Fahrspur statt vision_prepaid und die Finanzierungsfelder.
    api_key_lane: Optional[str] = None
    ledger_meta: Optional[Dict[str, Any]] = None
    # Nur der Anthropic-Messages-Weg setzt sie (_vision_response_from, S0
    # Direktweg-Ablehnung 17.11.2026): stop_details roh (Kategorie einer
    # Ablehnung), Antwortkopf request-id und Body-id des Anbieters, das
    # gesendete Modell und usage.iterations roh, falls geliefert.
    stop_details: Optional[Dict[str, Any]] = None
    provider_request_id: Optional[str] = None
    provider_message_id: Optional[str] = None
    requested_model: Optional[str] = None
    usage_iterations: Optional[List[Dict[str, Any]]] = None


class VisionProvider:
    """
    Direct Anthropic API provider for image/vision requests.

    Bypasses Claude Code SDK which doesn't support multimodal inputs.
    """

    ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
    ANTHROPIC_VERSION = "2023-06-01"

    # Supported image formats
    SUPPORTED_MIME_TYPES = [
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp"
    ]

    def __init__(self):
        # ONLY use ANTHROPIC_VISION_API_KEY (set directly in docker-compose.yml)
        # NO fallback to ANTHROPIC_API_KEY — that key must never exist in environment!
        self.api_key = os.getenv("ANTHROPIC_VISION_API_KEY")
        if not self.api_key and not credit_pool.enabled():
            logger.warning("ANTHROPIC_VISION_API_KEY not set - vision requests will fail")
            logger.warning("Set ANTHROPIC_VISION_API_KEY in docker-compose.yml for image analysis")

    @staticmethod
    def has_images(messages: List[Dict[str, Any]]) -> bool:
        """
        Check if any USER message contains image content.

        Only checks user messages - assistant messages may contain base64 data
        from API responses (e.g., QR codes from MFA setup) which should NOT
        trigger vision routing.

        Supports multiple formats:
        - OpenAI format: {"type": "image_url", "image_url": {"url": "data:image/..."}}
        - Anthropic format: {"type": "image", "source": {"type": "base64", ...}}
        - Inline base64: "data:image/jpeg;base64,..." in text content
        - Bracketed format: "[data:image/jpeg;base64,...]" in text content
        """
        for message in messages:
            # Skip non-user messages - assistant responses may contain base64
            # API data that shouldn't trigger vision processing
            role = message.get("role", "")
            if role != "user":
                continue

            content = message.get("content")

            # Check array content (OpenAI/Anthropic multimodal format)
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        # OpenAI image_url format
                        if block.get("type") == "image_url":
                            return True
                        # Anthropic image format
                        if block.get("type") == "image":
                            return True

            # Check string content for inline base64 images
            elif isinstance(content, str):
                # Pattern: data:image/xxx;base64,...
                if "data:image/" in content and ";base64," in content:
                    return True
                # Pattern: [data:image/xxx;base64,...]
                if re.search(r'\[data:image/[^;]+;base64,[^\]]+\]', content):
                    return True

        return False

    @staticmethod
    def _extract_images_from_content(content: Any) -> Tuple[List[Dict], str]:
        """
        Extract images and text from message content.

        Returns:
            Tuple of (image_blocks, remaining_text)
        """
        images = []
        text_parts = []

        if isinstance(content, list):
            # Already in multimodal format
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "image_url":
                        # OpenAI format -> Anthropic format
                        url = block.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            # Parse data URL - use regex that only matches valid base64 chars
                            # Base64 alphabet: A-Z, a-z, 0-9, +, /, = (padding)
                            match = re.match(r'data:(image/[^;]+);base64,([A-Za-z0-9+/=]+)', url)
                            if match:
                                base64_data = match.group(2).strip()
                                # Ensure proper padding
                                padding_needed = len(base64_data) % 4
                                if padding_needed:
                                    base64_data += '=' * (4 - padding_needed)
                                images.append({
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": match.group(1),
                                        "data": base64_data
                                    }
                                })
                        elif url.startswith("http://") or url.startswith("https://"):
                            # External URL - download and convert to base64
                            try:
                                import httpx
                                import base64
                                headers = {"User-Agent": "Mozilla/5.0 (compatible; AIBridge/1.0)"}
                                with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
                                    response = client.get(url)
                                    response.raise_for_status()
                                    image_data = base64.b64encode(response.content).decode('utf-8')
                                    # Determine media type from content-type header or URL
                                    content_type = response.headers.get('content-type', 'image/png')
                                    if ';' in content_type:
                                        content_type = content_type.split(';')[0].strip()
                                    images.append({
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": content_type,
                                            "data": image_data
                                        }
                                    })
                            except Exception as e:
                                logger.warning(f"Failed to download image from {url}: {e}")
                    elif block.get("type") == "image":
                        # Already Anthropic format
                        images.append(block)
                    elif block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_parts.append(block)

        elif isinstance(content, str):
            # Extract inline base64 images from text
            # Pattern 1: [data:image/xxx;base64,...] - ONLY valid base64 chars
            pattern = r'\[data:(image/[^;]+);base64,([A-Za-z0-9+/=]+)\]'

            last_end = 0
            for match in re.finditer(pattern, content):
                # Add text before this image
                text_before = content[last_end:match.start()]
                if text_before.strip():
                    text_parts.append(text_before)

                # Add image
                images.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": match.group(1),
                        "data": match.group(2)
                    }
                })

                last_end = match.end()

            # Add remaining text
            remaining = content[last_end:]
            if remaining.strip():
                text_parts.append(remaining)

            # If no bracketed images found, check for raw data URLs
            if not images:
                # ONLY valid base64 chars: A-Z, a-z, 0-9, +, /, =
                pattern2 = r'data:(image/[^;]+);base64,([A-Za-z0-9+/=]+)'
                for match in re.finditer(pattern2, content):
                    images.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": match.group(1),
                            "data": match.group(2)
                        }
                    })
                # Remove image data from text
                text_parts = [re.sub(pattern2, '[Image]', content)]

        combined_text = "\n".join(text_parts).strip()
        return images, combined_text

    @staticmethod
    def _convert_to_anthropic_messages(
        messages: List[Dict[str, Any]]
    ) -> Tuple[List[Dict], Optional[str]]:
        """
        Convert OpenAI-format messages to Anthropic format with image support.

        Returns:
            Tuple of (messages, system_prompt)
        """
        anthropic_messages = []
        system_prompt = None

        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")

            # Extract system prompt
            if role == "system":
                system_prompt = content if isinstance(content, str) else str(content)
                continue

            # Map assistant role
            if role == "assistant":
                anthropic_messages.append({
                    "role": "assistant",
                    "content": content if isinstance(content, str) else str(content)
                })
                continue

            # Process user messages (may contain images)
            images, text = VisionProvider._extract_images_from_content(content)

            if images:
                # Build multimodal content array
                content_blocks = []

                # Add images first
                for img in images:
                    content_blocks.append(img)

                # Add text if present
                if text:
                    content_blocks.append({
                        "type": "text",
                        "text": text
                    })

                anthropic_messages.append({
                    "role": "user",
                    "content": content_blocks
                })
            else:
                # Plain text message
                anthropic_messages.append({
                    "role": "user",
                    "content": text or content
                })

        return anthropic_messages, system_prompt

    async def analyze(
        self,
        messages: List[Dict[str, Any]],
        model: str = get_default_model("sonnet").id,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        system_prompt: Optional[str] = None,
        timeout: float = 300.0,
        thinking: Optional[Dict[str, Any]] = None,
        output_config: Optional[Dict[str, Any]] = None,
        subscription_fallback: Optional[bool] = None,
    ) -> VisionResponse:
        """
        Analyze images using direct Anthropic API.

        ``subscription_fallback`` (Abo-Bildweg, KD 03.11.2026): True = der
        Aufrufer weiß, dass seine Anfrage im Staging-Rückfall läuft (Chat-Pfad,
        request.state); None = aus dem Marker der laufenden Anfrage lesen
        (Dokument-Vision, funding_policy.subscription_fallback_in_context).
        Dann geht das Bild über die CLI (src/providers/subscription_vision.py)
        und NIE über den Pool oder ANTHROPIC_VISION_API_KEY.

        ``timeout`` defaults to 300s (fine for image analysis) but callers
        reusing this path for non-vision fallback (see
        src/providers/anthropic_direct.py) with a large max_tokens / Extended
        Thinking budget should pass a longer value — measured: a 32K-output
        call needs ~290s direct, so 300s has little headroom for the 40-77K
        output calls this fallback exists for.

        Args:
            messages: OpenAI-format messages (may contain images)
            model: Claude model to use
            max_tokens: Maximum response tokens
            temperature: Sampling temperature
            system_prompt: Optional system prompt override
            thinking: Optional passthrough for the Anthropic Messages API
                'thinking' param — forwarded verbatim, not validated here
                (see ChatCompletionRequest.thinking for the full rationale).
            output_config: Optional passthrough for the Anthropic Messages
                API 'output_config' param — forwarded verbatim.

        Returns:
            VisionResponse with analysis result
        """
        if subscription_fallback is None:
            # Erst der Schalter, dann der Import: der GPU-Privacy-Dienst nutzt
            # diese Datei ohne den Rückfall (dort nie gesetzt) und braucht
            # src/providers/funding_policy.py dann nicht.
            subscription_fallback = False
            if os.getenv("ENERGY_STAGING_SUBSCRIPTION_FALLBACK"):
                from src.providers import funding_policy as _fp
                subscription_fallback = _fp.subscription_fallback_in_context()
        if subscription_fallback:
            # Abo-Bildweg vor jedem anderen Zweig: kein Pool-Nachversuch, kein
            # bezahlter Schlüssel. Die Prüfung der Kennung wiederholt das Modul.
            from src.providers.subscription_vision import analyze_via_subscription
            anthropic_messages, extracted_system = self._convert_to_anthropic_messages(messages)
            return await analyze_via_subscription(
                anthropic_messages, system_prompt=system_prompt or extracted_system, model=model,
                max_tokens=max_tokens, thinking=thinking, output_config=output_config, timeout=timeout,
            )
        if not self.api_key and not credit_pool.enabled():
            raise ValueError(
                "ANTHROPIC_VISION_API_KEY environment variable not set. "
                "Vision/Image requests require direct API access (not OAuth)."
            )

        # Convert messages to Anthropic format
        anthropic_messages, extracted_system = self._convert_to_anthropic_messages(messages)

        # Use provided system prompt or extracted one
        final_system = system_prompt or extracted_system

        # Count images for logging
        image_count = sum(
            1 for msg in anthropic_messages
            if isinstance(msg.get("content"), list)
            for block in msg["content"]
            if isinstance(block, dict) and block.get("type") == "image"
        )

        logger.info(
            f"Vision request: {image_count} images, model={model}",
            extra={
                "image_count": image_count,
                "model": model,
                "message_count": len(anthropic_messages)
            }
        )

        # Build request body. Only forward `temperature` if this model accepts
        # it — newer (4.5+) models deprecate the legacy sampling controls and the
        # provider rejects the field. This mirrors the Bedrock adapter so the
        # Bridge strips provider-incompatible params at a single boundary, no
        # matter which backend serves the call (see model_supports_temperature).
        request_body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": anthropic_messages
        }
        if model_supports_temperature(model):
            request_body["temperature"] = temperature
        else:
            logger.info(
                "Dropping deprecated `temperature` for model %s (not supported).", model
            )

        if final_system:
            request_body["system"] = final_system

        # Extended-thinking passthrough — forwarded verbatim, never validated
        # here. An unsupported shape or incompatible combination comes back
        # from Anthropic as a 4xx, which the status-code handling below
        # already turns into a clean HTTPException — see 2026-07-24.
        if thinking is not None:
            request_body["thinking"] = thinking
        if output_config is not None:
            request_body["output_config"] = output_config

        adapt_model_request(model, request_body)

        credit_meta = None
        if credit_pool.enabled():
            response, credit_meta = await credit_pool.post_messages(request_body, timeout)
        else:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    self.ANTHROPIC_API_URL,
                    headers={"Content-Type": "application/json", "x-api-key": self.api_key,
                             "anthropic-version": self.ANTHROPIC_VERSION},
                    json=request_body,
                )

        if response.status_code != 200:
            error_body = response.text
            logger.error(
                f"Anthropic API error: {response.status_code}",
                extra={
                    "status_code": response.status_code,
                    "error": error_body[:500]
                }
            )
            if 400 <= response.status_code < 500 and response.status_code != 429:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=error_body[:1000]
                )
            raise RuntimeError(
                f"Anthropic API error ({response.status_code}): {error_body[:200]}"
            )

        # Alles nach der 200 liest eine bezahlte Antwort. Laesst sie sich nicht
        # lesen, sieht sie beim naechsten Versuch genauso aus: nicht
        # wiederholbar statt 500 (BR9f).
        from src.middleware.bridge_error import (
            UNREADABLE_RESPONSE_ERRORS,
            UpstreamResponseUnreadable,
        )
        try:
            result = _vision_response_from(response.json(), model)
            result.credit_meta = credit_meta
            result.provider_request_id = _request_id_aus(response)
            return result
        except UNREADABLE_RESPONSE_ERRORS as exc:
            raise UpstreamResponseUnreadable("anthropic", exc) from exc


def _request_id_aus(response: Any) -> Optional[str]:
    """Antwortkopf ``request-id`` des Anbieters (Nachweis gegenueber Anthropic).
    Fehlt er, bleibt er None und wird laut geloggt — nie ein Ersatzwert."""
    try:
        wert = response.headers.get("request-id")
    except AttributeError:
        wert = None
    if not wert:
        logger.warning("Anthropic-Antwort ohne Kopf request-id — provider_request_id bleibt leer")
        return None
    return str(wert)


def _int0(wert: Any) -> int:
    return wert if type(wert) is int and wert >= 0 else 0


def _usage_mit_cache(usage: Any) -> Dict[str, int]:
    """OpenAI-usage mit Ledger-Semantik wie der Abo-Bildweg (Codepruefung S0 A3):
    prompt_tokens = gesamter Eingang (ungecacht + Cache), dazu ungecacht und
    beide Cache-Felder getrennt, damit Ledger und x_bridge_finish dieselben
    Werte zum jeweils eigenen Satz rechnen. Bis 17.11.2026 fehlte der Cache
    hier, das Ledger buchte ihn mit 0."""
    u = usage if isinstance(usage, dict) else {}
    ungueltig = [f for f in _MESSUNG_FELDER if not (type(u.get(f)) is int and u.get(f) >= 0)]
    if ungueltig:
        # Codepruefung R2 B1: kein stilles 0. Der Wert bleibt 0 fuer das
        # Ledger (konservativ, Zeile wird geschrieben), aber die Antwort traegt
        # cost_status "unbekannt" (UsageMessung.vollstaendig False -> kein
        # Preis-Kopf, siehe messung_kostenstatus / abschluss_felder).
        logger.warning("Anthropic-usage unvollstaendig (Felder %s nicht int>=0) — Kosten unbekannt", ungueltig)
    ungecacht = _int0(u.get("input_tokens"))
    lesen = _int0(u.get("cache_read_input_tokens"))
    anlage = _int0(u.get("cache_creation_input_tokens"))
    ausgabe = _int0(u.get("output_tokens"))
    eingang = ungecacht + lesen + anlage
    return {
        "prompt_tokens": eingang,
        "completion_tokens": ausgabe,
        "total_tokens": eingang + ausgabe,
        "uncached_input_tokens": ungecacht,
        "cache_read_tokens": lesen,
        "cache_creation_tokens": anlage,
    }


def _vision_response_from(data: Dict[str, Any], model: str) -> VisionResponse:
    """Die 200-Antwort der Anthropic Messages API -> VisionResponse."""
    # Extract response content
    content_blocks = data.get("content", [])
    response_text = ""
    for block in content_blocks:
        if block.get("type") == "text":
            response_text += block.get("text", "")

    # Messung VOR jeder Ersatzwert-Bildung, aus dem Rohobjekt.
    messung = usage_messung_aus(data.get("usage"))
    usage = data.get("usage", {})

    stop_details = data.get("stop_details") if isinstance(data.get("stop_details"), dict) else None
    iterationen = usage.get("iterations") if isinstance(usage, dict) else None
    log = logger.warning if not response_text else logger.info
    log(
        f"Vision response: {len(response_text)} chars"
        + ("" if response_text else
           f" — EMPTY (stop_reason={data.get('stop_reason')!r}, "
           f"category={(stop_details or {}).get('category')!r}, "
           f"output_tokens={usage.get('output_tokens', 0)}"
           + ("; vermutlich Thinking hat max_tokens aufgebraucht)"
              if data.get("stop_reason") == "max_tokens" else ")")),
        extra={
            "response_length": len(response_text),
            "stop_reason": data.get("stop_reason"),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0)
        }
    )

    return VisionResponse(
        content=response_text,
        model=data.get("model", model),
        usage=_usage_mit_cache(usage),
        stop_reason=_stop_reason_or_unknown(data.get("stop_reason")),
        messung=messung,
        stop_details=stop_details,
        provider_message_id=data.get("id") if isinstance(data.get("id"), str) else None,
        requested_model=model,
        usage_iterations=(
            [e for e in iterationen if isinstance(e, dict)]
            if isinstance(iterationen, list) else None
        ),
    )


def _stop_reason_or_unknown(stop_reason: Optional[str]) -> str:
    """Fehlender stop_reason ist kein Erfolg: bis 2026-10 wurde er zu "end_turn"
    (-> finish_reason "stop") erfunden. Jetzt "unknown" — wird von
    finish_reason_for durchgereicht, der Aufrufer sieht es, und es wird geloggt."""
    if stop_reason:
        return stop_reason
    logger.error("Vision-Antwort ohne stop_reason — Abbruchgrund unbekannt, melde 'unknown'")
    return "unknown"


# ── Antwort-Politik (ADR-0011-Nachlese, 2026-08-31) ─────────────────────────
# Pure Helfer, damit der Endpoint-Code in main.py testbar bleibt.

# Anthropic stop_reason → OpenAI finish_reason. Bis 2026-08-31 log der
# Vision-Pfad hier pauschal "stop" — eine per max_tokens abgeschnittene
# Antwort war vom Erfolg nicht unterscheidbar.
_FINISH_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    # BRREF (12.10.2026): a safety-classifier stop is a cut answer, OpenAI's
    # value for it is content_filter (same as bedrock_service).
    "refusal": "content_filter",
}


def finish_reason_for(stop_reason: Optional[str]) -> str:
    """OpenAI-kompatibler finish_reason fuer einen Anthropic stop_reason.
    Unbekannte Werte werden durchgereicht statt beschoenigt."""
    if not stop_reason:
        return "stop"
    return _FINISH_REASON_MAP.get(stop_reason, stop_reason)


def ensure_usable_vision_content(content: str, stop_reason: Optional[str]) -> None:
    """FAIL-LOUD fuer die leere Vision-Antwort (Befund 2026-08-31, werking-energy
    schema_analysis): claude-sonnet-5 verbraucht bei dichten Schema-Bildern das
    gesamte Output-Budget in Thinking-Bloecken; die Antwort enthaelt dann NULL
    Text-Bloecke und ging bis zu diesem Fix als 200 mit content="" raus — der
    Client sah "Bridge returned empty content", retryte blind und verbrannte
    pro Versuch ~15k Prepaid-Tokens.

    MUSS NACH dem Ledger-Persist aufgerufen werden, nie davor: der Upstream-Call
    hat real Geld gekostet (vision_prepaid), und die Tageskappe summiert
    Ledger-Zeilen — ein Raise vor dem Persist macht genau diesen Spend fuer die
    Kappe unsichtbar (die 2026-07-18-Runaway-Klasse).

    Wirft BridgeError mit vorklassifiziertem Envelope — bewusst KEINE nackte
    HTTPException: die wuerde vom Vision-Branch-Handler durch
    classify_exception zu internal_error/500 zerdrueckt, nginx retryte 500
    ueber alle Worker und verkleidete den Befund als "at capacity" (dieselbe
    Pathologie wie bei client_request_error dokumentiert).
    """
    from src.middleware.bridge_error import BridgeError, vision_empty_response_error

    if stop_reason == "refusal":
        # Waechter (S0, Review A3): eine Ablehnung ist nie verwertbar und nie
        # wiederholbar. Die regulaeren Aufrufer liefern vorher die
        # Ablehnungsmarke (abschluss_felder); wer hier ankommt, hat sie
        # vergessen. Laut und NICHT retrybar — nie 502 "Retry moeglich", nie
        # eine stille 200-Leerantwort.
        logger.error("ensure_usable_vision_content: Ablehnung ohne Ablehnungsmarke erreicht den Waechter")
        from src.middleware.bridge_error import (
            SOURCE_UPSTREAM_ANTHROPIC, TYPE_UPSTREAM_ERROR, bridge_error,
        )
        raise BridgeError(bridge_error(
            source=SOURCE_UPSTREAM_ANTHROPIC,
            error_type=TYPE_UPSTREAM_ERROR,
            reason="modell_abbruch",
            message=(
                "Der Anbieter hat die Antwort abgelehnt (stop_reason refusal). "
                "Nicht wiederholen: eine Ablehnung ist kein Transportfehler."
            ),
            status_code=422,
            retryable_override=False,
            extra={"fehler_code": "modell_abbruch", "stop_reason": "refusal"},
        ))
    if content:
        return
    raise BridgeError(vision_empty_response_error(stop_reason))



# ── Ablehnung ehrlich ausweisen (S0, Bridge-Owner 51c8185c, 17.11.2026) ─────
# Schemaquelle: SCHEMA-ABLEHNUNG-FALLBACK-51c8185c-20261117.md (S-1 bis S-4).
# Eine Anbieter-Ablehnung (stop_reason "refusal") geht als 200 mit
# finish_reason content_filter und vollstaendigem x_bridge_finish raus
# (refusal true, retryable false, verwertbar false) — gleicher Vertrag wie der
# CLI-Weg (BRREF). Nie umgangen: kein Retry, kein Modellwechsel.

#: Ablehnungskategorien, die der Anbieter auch VOR jeder Ausgabe berechnet.
#: Einzige Quelle in der Bridge; Pool und Energy referenzieren nur. Aendern
#: nur mit neuem Doku-Beleg (Datei + sha12) und neuer Version — die Liste
#: kann sich laut Anbieter aendern.
BERECHNETE_ABLEHNUNGSKATEGORIEN: Dict[str, Any] = {
    "version": "2026-11-17",
    "quelle": (
        "platform.claude.com/docs/en/build-with-claude/refusals-and-fallback — "
        "Tabelle 'Billed before any output' und 'How refusals are billed' "
        "(Stand 'as of September 2026'); Beleg "
        "BELEG-refusals-and-fallback-doku-20261117.md sha12 d9268d1bf0b6, "
        "Z. 202-208, 229, 235"
    ),
    "gelesen_am": "2026-11-17",
    "kategorien": ("bio", "frontier_llm", "reasoning_extraction"),
    "nicht_berechnet_vor_ausgabe": ("cyber", "general_harms", None),
}

#: Regel V (R3, Review B2): vollstaendig nur, wo der stop_reason es bestimmt.
_VOLLSTAENDIG_JE_STOP_REASON = {
    "end_turn": True,
    "stop_sequence": True,
    "max_tokens": False,
    "model_context_window_exceeded": False,
    "refusal": False,
}

_WORKER_NAME_FORM = re.compile(r"^[a-z0-9-]{1,40}$")
_GIT_SHA_FORM = re.compile(r"^[0-9a-f]{40}$")
_build_sha_cache: Dict[str, Optional[str]] = {}


def _modell_kanon(model: Optional[str]) -> Optional[str]:
    """Gleiche Normalisierung wie pricing.price_entry (Anbieter-IDs, Datums-
    Suffix): ein Alias und seine datierte ID sind dasselbe Modell (Review B3)."""
    if not model:
        return None
    from src.pricing import normalize_model_id
    return re.sub(r"-\d{8}$", "", normalize_model_id(model))


def _bridge_worker() -> Optional[str]:
    """Worker-NAME fuer die Diagnose (R8, strenge Lesart): nur Kleinbuchstaben,
    Ziffern und Bindestrich — nie IP, Pfad oder Hostname mit Punkten."""
    name = os.getenv("INSTANCE_NAME") or ""
    return name if _WORKER_NAME_FORM.match(name) else None


def _build_sha() -> Optional[str]:
    if "sha" not in _build_sha_cache:
        try:
            from src.version import read_version
            sha = read_version().git_sha
        except Exception as e:  # noqa: BLE001 — Diagnosefeld, nie Aufrufabbruch
            logger.warning("build_sha unbekannt (Build-Metadaten nicht lesbar: %s)", type(e).__name__)
            sha = None
        _build_sha_cache["sha"] = sha if sha and _GIT_SHA_FORM.match(sha) else None
    return _build_sha_cache["sha"]


def _str_oder_none(wert: Any, feld: str) -> Optional[str]:
    """Typwaechter (Codepruefung S0 A2): Anbieterfelder sind Strings oder
    fehlen. Alles andere wird None und laut geloggt — nie ein 500 nach einem
    bezahlten Aufruf."""
    if wert is None or isinstance(wert, str):
        return wert
    logger.warning("Anbieterfeld %s mit unerwartetem Typ %s — als fehlend behandelt", feld, type(wert).__name__)
    return None


def ist_ablehnung(stop_reason: Optional[str], stop_details: Optional[Dict[str, Any]]) -> bool:
    sd = stop_details if isinstance(stop_details, dict) else {}
    return stop_reason == "refusal" or sd.get("type") == "refusal"


def _billed_fuer_ablehnung(category: Optional[str], *, kategorie_bekannt: bool) -> Tuple[Any, str]:
    """billed eines abgelehnten Versuchs (Doku Z. 229, 231, 758; keine
    Token-Heuristik). Der Direkt- und Bildweg ruft den Anbieter nie als Strom
    auf; eine Ablehnung ausserhalb der berechneten Kategorien ist daher nicht
    entscheidbar (vor oder mitten in der Ausgabe) -> "unbekannt", nie False."""
    if kategorie_bekannt and category in BERECHNETE_ABLEHNUNGSKATEGORIEN["kategorien"]:
        return True, "kategorie_berechnet"
    if not kategorie_bekannt:
        return "unbekannt", "kategorie_fehlt"
    return "unbekannt", "nicht_streamend_unentscheidbar"


def _iterations_kosten(model: Optional[str], tokens: Dict[str, int], billed: Any) -> Tuple[Optional[float], Optional[float]]:
    """(cost_eur, schaetzung_eur). Preis nur bei billed True und bepreistem
    Modell (pricing.cost_eur liefert sonst still 0.0)."""
    from src.pricing import cost_eur, is_priced
    if not is_priced(model):
        return None, None
    betrag = cost_eur(
        model,
        tokens.get("input_tokens", 0),
        tokens.get("output_tokens", 0),
        cache_read_tokens=tokens.get("cache_read_input_tokens", 0),
        cache_creation_tokens=tokens.get("cache_creation_input_tokens", 0),
    )
    if billed is True:
        return betrag, None
    if billed == "unbekannt":
        return None, betrag
    return None, None


def _token_felder(quelle: Dict[str, Any]) -> Dict[str, int]:
    return {
        f: quelle.get(f) if type(quelle.get(f)) is int else 0
        for f in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    }


def abschluss_felder(
    *,
    content: str,
    stop_reason: Optional[str],
    stop_details: Optional[Dict[str, Any]],
    requested_model: Optional[str],
    responding_model: Optional[str],
    provider_request_id: Optional[str] = None,
    provider_message_id: Optional[str] = None,
    messung: Optional[UsageMessung] = None,
    usage_iterations: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """x_bridge_finish einer Messages-API-Antwort (Schema S-1/S-2).

    Baut auf lauf_abschluss.abschluss_aus_stop_reason(...).details() auf und
    UEBERSCHREIBT dessen vollstaendig ausdruecklich mit Regel V (Review B2):
    sonst ergaebe ein fehlender stop_reason, pause_turn oder tool_use ein
    vollstaendig False, das Energy als Schnitt fortsetzen wuerde."""
    from src.lauf_abschluss import abschluss_aus_stop_reason

    roh_stop = None if stop_reason in (None, "", "unknown") else stop_reason
    details = dict(stop_details) if isinstance(stop_details, dict) else None
    refusal = ist_ablehnung(roh_stop, details)
    category = _str_oder_none((details or {}).get("category"), "category") if refusal else None

    f: Dict[str, Any] = abschluss_aus_stop_reason(
        "refusal" if refusal else roh_stop, category=category,
    ).details()
    f.pop("vollstaendig", None)
    f["stop_reason"] = roh_stop
    vollstaendig = _VOLLSTAENDIG_JE_STOP_REASON.get("refusal" if refusal else roh_stop)
    if vollstaendig is None:
        f["vollstaendig_unbestimmt"] = True
    else:
        f["vollstaendig"] = vollstaendig
    f["refusal"] = refusal
    f["empty_response"] = not content
    f["category"] = category
    f["explanation"] = _str_oder_none((details or {}).get("explanation"), "explanation") if refusal else None
    f["recommended_model"] = (
        _str_oder_none((details or {}).get("recommended_model"), "recommended_model") if refusal else None
    )
    f["stop_details"] = details
    f.setdefault("folgezuege", 0)
    f["provider_request_id"] = _str_oder_none(provider_request_id, "request-id")
    f["provider_message_id"] = _str_oder_none(provider_message_id, "id")
    f["requested_model"] = requested_model
    f["responding_model"] = responding_model

    # Iterationen: jede gelieferte Position (K3), sonst ein Eintrag aus usage.
    kv = BERECHNETE_ABLEHNUNGSKATEGORIEN["version"]
    roh_iter = [e for e in (usage_iterations or []) if isinstance(e, dict)]
    if roh_iter:
        quellen = [("iterations", e) for e in roh_iter]
    else:
        oben: Dict[str, Any] = {"type": "message", "model": responding_model}
        if messung is not None and messung.vollstaendig:
            oben.update(
                input_tokens=messung.eingabe, output_tokens=messung.ausgabe,
                cache_read_input_tokens=messung.cache_lesen,
                cache_creation_input_tokens=messung.cache_anlage,
            )
        quellen = [("top_level", oben)]
    iterations: List[Dict[str, Any]] = []
    letzte = len(quellen) - 1
    for i, (quelle, e) in enumerate(quellen):
        it_model = _str_oder_none(e.get("model"), "iterations.model") or responding_model
        tokens = _token_felder(e)
        abgelehnt = (i == letzte and refusal) or (i < letzte and e.get("type") == "message")
        it_category = (
            _str_oder_none(e.get("category"), "iterations.category") if "category" in e
            else (category if i == letzte else None)
        )
        if abgelehnt:
            kategorie_bekannt = (
                "category" in e or (i == letzte and details is not None and "category" in details)
            )
            billed, regel = _billed_fuer_ablehnung(it_category, kategorie_bekannt=kategorie_bekannt)
        else:
            billed, regel = True, "regulaer"
        tokens_vollstaendig = (
            all(type(e.get(k)) is int and e.get(k) >= 0 for k in _MESSUNG_FELDER) if quelle == "iterations"
            else (messung is not None and messung.vollstaendig)
        )
        if tokens_vollstaendig:
            cost, schaetzung = _iterations_kosten(it_model, tokens, billed)
        else:
            # Keine vollstaendige Messung: keine erfundenen Nullen.
            tokens = {k: None for k in tokens}
            cost, schaetzung = None, None
            regel = regel + "+usage_unvollstaendig"
        iterations.append({
            "index": i,
            "type": e.get("type") or "message",
            "model": it_model,
            **tokens,
            "stop_reason": e.get("stop_reason") if "stop_reason" in e else (roh_stop if i == letzte else None),
            "category": it_category if abgelehnt else None,
            "recommended_model": f["recommended_model"] if (abgelehnt and i == letzte) else None,
            "billed": billed,
            "cost_eur": cost,
            "cost_schaetzung_eur": schaetzung,
            "cost_basis": {"regel": regel, "kategorienliste_version": kv, "preis_modell": it_model},
            "quelle": quelle,
        })
    f["iterations"] = iterations
    # B1 (Codepruefung R2): eine unvollstaendige Messung ist nie "belegt".
    unvollstaendig = any("usage_unvollstaendig" in it["cost_basis"]["regel"] for it in iterations)
    unbekannt = unvollstaendig or any(it["billed"] == "unbekannt" for it in iterations)
    ohne_preis = any(it["billed"] is True and it["cost_eur"] is None for it in iterations)
    # cost_status "unbekannt" heisst: ob der Anbieter berechnet hat, ist nicht
    # belegt (R1). Ein fehlender Preis bei belegter Berechnung macht nur die
    # Summe unbekannt, nicht die Berechnung.
    f["cost_status"] = "unbekannt" if unbekannt else "belegt"
    f["cost_total_eur"] = (
        None if (unbekannt or ohne_preis)
        else round(sum(it["cost_eur"] or 0.0 for it in iterations if it["billed"] is True), 6)
    )
    schaetzungen = [it["cost_schaetzung_eur"] for it in iterations if it["cost_schaetzung_eur"] is not None]
    f["cost_schaetzung_eur"] = round(sum(schaetzungen), 6) if unbekannt and schaetzungen else None
    f["kategorienliste_version"] = kv

    # Modellvergleich (B3): Alias/datierte ID gelten als gleich.
    grund = None
    if _modell_kanon(responding_model) != _modell_kanon(requested_model):
        grund = "antwortmodell_ungleich_angefragt"
    elif len(roh_iter) > 1:
        grund = "iterationen_ohne_fallback"
    f["modell_abweichung"] = grund is not None
    f["modell_abweichung_grund"] = grund
    if grund:
        logger.error(
            "Modellabweichung ohne Fallback-Bestellung: angefragt=%s geantwortet=%s iterationen=%d grund=%s",
            requested_model, responding_model, len(roh_iter), grund,
        )
    f["verwertbar"] = not refusal and grund is None
    f["retryable"] = False if refusal else None
    f["fallback_angefordert"] = False
    f["fallback"] = {
        "lage": "abweichung_unbelegt" if grund else "kein_fallback",
        "from": None, "to": None, "trigger": None,
    }
    f["bridge_worker"] = _bridge_worker()
    f["build_sha"] = _build_sha()
    if refusal:
        logger.warning(
            "Anbieter-Ablehnung: stop_reason=refusal category=%s request-id=%s modell=%s",
            category, provider_request_id, responding_model,
        )
    return f


def abschluss_felder_sicher(**kw: Any) -> Dict[str, Any]:
    """abschluss_felder, die nie werfen (Codepruefung S0 A2). Scheitert die
    Auswertung an einer unerwarteten Anbieterantwort, entsteht ein Notabschluss:
    Ablehnung nach stop_reason, nicht verwertbar, Kosten unbekannt — laut."""
    try:
        return abschluss_felder(**kw)
    except Exception as e:  # noqa: BLE001 — nach einem bezahlten Aufruf nie 500
        logger.error("abschluss_felder: Auswertung gescheitert (%s: %s) — Notabschluss", type(e).__name__, e)
        stop = kw.get("stop_reason")
        sd = kw.get("stop_details")
        refusal = stop == "refusal" or (isinstance(sd, dict) and sd.get("type") == "refusal")
        return {
            "auswertung_fehler": type(e).__name__,
            "stop_reason": stop if isinstance(stop, str) else None,
            "refusal": refusal,
            "vollstaendig_unbestimmt": True,
            "empty_response": not kw.get("content"),
            "verwertbar": False,
            "retryable": False if refusal else None,
            "cost_status": "unbekannt",
            "cost_total_eur": None,
            "cost_schaetzung_eur": None,
            "iterations": [],
            "kategorienliste_version": BERECHNETE_ABLEHNUNGSKATEGORIEN["version"],
            "bridge_worker": _bridge_worker(),
        }


#: Ankuendigung eines Aufrufers, dass er die Ablehnungsmarke (200 +
#: content_filter + x_bridge_finish) versteht (Kanzler-Entscheid zu
#: Codepruefung S0 A1). Kopf fuer direkte Aufrufer; das gleichnamige Body-Feld
#: ``bridge_ablehnungsmarke`` uebersteht auch /v1/jobs (der Job-Selbstaufruf
#: reicht nur Attributionskoepfe weiter, jobs/executors._build_headers).
ABLEHNUNGSMARKE_KOPF = "X-Bridge-Ablehnungsmarke"


def ablehnungsmarke_angekuendigt(http_request: Any, request_body: Any) -> bool:
    if getattr(request_body, "bridge_ablehnungsmarke", None) is True:
        return True
    try:
        wert = http_request.headers.get(ABLEHNUNGSMARKE_KOPF) if http_request is not None else None
    except AttributeError:
        wert = None
    return (wert or "").strip().lower() in ("1", "true", "ja")


def ablehnung_ohne_marke(finish: Dict[str, Any]) -> Any:
    """Antwort fuer Aufrufer OHNE Ankuendigung (Kanzler-Entscheid A1): ein
    Fehlerstatus statt 200, damit keine App eine Ablehnung still als leeren
    Erfolg verwertet. 422 modell_abbruch, nicht wiederholbar (wie der
    Waechter), Kopf X-Bridge-Finish, x_bridge_finish im Fehlerobjekt."""
    from src.middleware.bridge_error import (
        BridgeError, SOURCE_UPSTREAM_ANTHROPIC, TYPE_UPSTREAM_ERROR, bridge_error,
    )
    antwort = bridge_error(
        source=SOURCE_UPSTREAM_ANTHROPIC,
        error_type=TYPE_UPSTREAM_ERROR,
        reason="modell_abbruch",
        message=(
            "Der Anbieter hat die Antwort abgelehnt (stop_reason refusal, "
            f"category {finish.get('category')!r}). Nicht wiederholen: eine Ablehnung ist kein "
            "Transportfehler. Die Ablehnungsmarke als 200 gibt es mit "
            f"{ABLEHNUNGSMARKE_KOPF}: 1 bzw. bridge_ablehnungsmarke: true."
        ),
        status_code=422,
        retryable_override=False,
        extra={
            "fehler_code": "modell_abbruch",
            "stop_reason": "refusal",
            "category": finish.get("category"),
            "provider_request_id": finish.get("provider_request_id"),
            "x_bridge_finish": finish,
        },
    )
    antwort.headers["X-Bridge-Finish"] = "content_filter"
    return BridgeError(antwort)


def ledger_felder(finish: Dict[str, Any]) -> Dict[str, Any]:
    """provider_metadata-Felder einer Antwort fuer das Ledger. Wirft nie: die
    Buchung eines bezahlten Aufrufs darf an der Auswertung nicht scheitern
    (Codepruefung S0 A2); ein Fehler wird laut als auswertung_fehler gebucht."""
    try:
        return _ledger_felder(finish)
    except Exception as e:  # noqa: BLE001 — gebucht wird trotzdem, laut markiert
        logger.error("ledger_felder: Auswertung gescheitert (%s: %s) — Buchung mit cost_status unbekannt",
                     type(e).__name__, e)
        return {"auswertung_fehler": type(e).__name__, "cost_status": "unbekannt",
                "cost_art": "schaetzung_aus_usage"}


def _ledger_felder(finish: Dict[str, Any]) -> Dict[str, Any]:
    """Bei cost_status "unbekannt" ist die Ledger-Zeile nur eine Schaetzung aus
    usage (R1): ai_call_writer meldet sie dann nie als Preis (Kostenkopf)."""
    billed_werte = [it.get("billed") for it in finish.get("iterations") or []]
    if "unbekannt" in billed_werte:
        billed: Any = "unbekannt"
    elif billed_werte and all(b is True for b in billed_werte):
        billed = True
    elif billed_werte and all(b is False for b in billed_werte):
        billed = False
    else:
        billed = "gemischt" if billed_werte else None
    meta: Dict[str, Any] = {
        "stop_reason": finish.get("stop_reason"),
        "refusal": bool(finish.get("refusal")),
        "empty_response": bool(finish.get("empty_response")),
        "refusal_category": finish.get("category"),
        "provider_request_id": finish.get("provider_request_id"),
        "billed": billed,
        "cost_status": finish.get("cost_status"),
        "kategorienliste_version": finish.get("kategorienliste_version"),
        "modell_abweichung": bool(finish.get("modell_abweichung")),
    }
    if finish.get("cost_status") == "unbekannt":
        meta["cost_art"] = "schaetzung_aus_usage"
    if finish.get("auswertung_fehler"):
        # B3 (Codepruefung R2): der Notabschluss ist in der Ledger-Zeile sichtbar.
        meta["auswertung_fehler"] = finish["auswertung_fehler"]
    if finish.get("refusal"):
        sd = finish.get("stop_details") or {}
        meta["stop_details"] = {
            "type": sd.get("type"),
            "category": sd.get("category"),
            "recommended_model": sd.get("recommended_model"),
            "explanation": (sd.get("explanation")[:300] or None) if isinstance(sd.get("explanation"), str) else None,
        }
    return meta


def messung_kostenstatus(messung: Optional[UsageMessung]) -> Dict[str, Any]:
    """Ledger-Meta fuer eine Antwort ohne x_bridge_finish (Bildweg ohne
    Ablehnung): bei unvollstaendiger Messung cost_status "unbekannt", damit
    keine 0-Eingang-Buchung als Preis zum Aufrufer geht (Codepruefung R2 B1).
    Ohne Messung (Abo-Bildweg) oder vollstaendig: nichts."""
    if messung is not None and not messung.vollstaendig:
        return {"cost_status": "unbekannt", "cost_art": "schaetzung_aus_usage", "usage_unvollstaendig": True}
    return {}


def ablehnungs_kopf(finish: Dict[str, Any]) -> Dict[str, str]:
    """Kopf der Ablehnungsmarke (Variante A)."""
    return {"X-Bridge-Finish": "content_filter"} if finish.get("refusal") else {}


# Singleton instance
_vision_provider: Optional[VisionProvider] = None


def get_vision_provider() -> VisionProvider:
    """Get or create the vision provider singleton."""
    global _vision_provider
    if _vision_provider is None:
        _vision_provider = VisionProvider()
    return _vision_provider


def vision_available() -> bool:
    """True genau dann, wenn ein NICHT-LEERER ANTHROPIC_VISION_API_KEY gesetzt ist.

    Bewusst gegen os.getenv statt gegen das Provider-Singleton geprueft: die
    Compose-Zeile ``ANTHROPIC_VISION_API_KEY=${ANTHROPIC_API_KEY:-}`` liefert
    bei fehlendem Host-Wert einen LEEREN String — die Variable existiert dann,
    traegt aber nichts. Genau dieser Zustand liess am 26.08.2026 auf
    gpu-privacy-1 jede describe_images-Anfrage erst NACH der Docling-Arbeit an
    einem unhandled ValueError sterben (Kunde: werking-energy, 3x 500).
    """
    return credit_pool.enabled() or bool(os.getenv("ANTHROPIC_VISION_API_KEY"))
