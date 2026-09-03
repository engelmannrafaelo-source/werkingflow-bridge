"""Google Gemini vision provider — API-Key-Weg mit Bildeingabe.

WHY THIS EXISTS
---------------
Die Energy-Bildanalyse (Plan-/Blockbeschreibung) laeuft ueber Sonnet 5 und ist
im Test-/Staging-Betrieb der teuerste Posten der Pipeline. Rafael (2026-09-03)
will fuer den TEST-Modus ein guenstiges Bildmodell messen koennen; produktive
Berichte bleiben auf Sonnet 5, bis die Qualitaet belegt ist.

Die Bridge konnte Gemini bisher NUR ueber den CLI-Subprozess mit Google-OAuth
(``src/gemini_cli.py``, ``src/providers/gemini_oauth.py``) — und dieser Weg
kennt keine Bildeingabe (``main.py`` extrahiert dort ausdruecklich nur die
Text-Parts). Dieses Modul ist der fehlende Weg: Gemini per API-Key mit
Bildeingabe, eingehaengt in dieselbe Vision-Mechanik wie der direkte
Anthropic-Weg (``src/vision_provider.py``).

DATENSCHUTZ — DIESER WEG IST KEIN ALLGEMEINER PROVIDER
------------------------------------------------------
Google ist in ``werkingflow-business/legal/compliance/avv.md`` §5.4 NICHT als
Unterauftragsverarbeiter gelistet, und die ueber die USA bezogenen Leistungen
sind dort abschliessend aufgezaehlt (Spracherkennung/OpenAI,
Transaktionsmail/Resend, Recherche/Anthropic). Fuer KI-Inferenz ist EU-Residenz
zugesagt (avv.md:84/86). Echte Kundenplaene duerfen deshalb NICHT hierher —
auch nicht ueber einen EU-Endpunkt, denn der beseitigt zwar den
Drittlandtransfer, macht Google aber trotzdem zum NEUEN Unterauftragsverarbeiter
(= AVV-Aenderung + Kundeninformation, Rafaels Entscheidung).

Die Sperre steht deshalb technisch in ``src/routing/gemini_vision_gate.py`` und
NICHT nur in dieser Doku. Dieses Modul fuehrt aus; es entscheidet nicht, ob es
darf.

ENDPUNKT BLEIBT KONFIGURATION (DevOps 57eb62f8, 2026-09-03)
-----------------------------------------------------------
``GEMINI_VISION_BASE_URL`` ist bewusst eine Env-Variable und keine Konstante:
faellt spaeter die Entscheidung fuer Vertex EU, ist das eine Konfigurations-
aenderung. ACHTUNG — sie ist NICHT allein ausreichend: Vertex AI authentifiziert
mit OAuth/Service-Account statt mit einem API-Key und hat einen anderen Pfad
(``/v1/projects/{p}/locations/{r}/publishers/google/models/{m}:generateContent``).
Eine Vertex-URL hier einzutragen wuerde also NICHT funktionieren, sie wuerde mit
401 sterben. Der Vertex-Weg ist bewusst NICHT gebaut (Auth fehlt, und die Frage,
ob Flash-Lite in europe-west MIT Bildeingabe verfuegbar ist, ist zum
Zeitpunkt dieses Commits ungeklaert — sie wird nicht mit einer Annahme
geschlossen).

MODELLWAHL (Preise geprueft 2026-09-03, ai.google.dev/gemini-api/docs/pricing)
------------------------------------------------------------------------------
Neuer ist hier NICHT guenstiger — innerhalb beider Familien ist die 2.5er die
guenstigste, deshalb steht sie vorne:

    gemini-2.5-flash         0,30 / 2,50 USD je 1M   <- Default
    gemini-3.5-flash         1,50 / 9,00 USD
    gemini-2.5-flash-lite    0,10 / 0,40 USD
    gemini-3.1-flash-lite    0,25 / 1,50 USD
    gemini-3.5-flash-lite    0,30 / 2,50 USD

Zum Vergleich, weil es die Entscheidung traegt: claude-sonnet-5 liegt bei
2,00 / 10,00 USD. Gemini 2.5 Flash ist also input rund 6,7x und output 4x
guenstiger.

Alle nehmen Bildeingabe, Bilder werden zum Token-Preis abgerechnet (kein
getrennter Bildposten). ``gemini-3.1-flash-lite-image`` gehoert NICHT in diese
Liste: das ist ein Modell zur Bild-ERZEUGUNG (Text -> Bild), nicht zum
Bildverstehen.

Ein weiteres Modell aufzunehmen ist bewusst ein kleiner, sichtbarer Schritt und
kein Sonderpfad: Zeile in ``GEMINI_VISION_MODELS`` plus Preiszeile in
``src/pricing.py``. Die Allowlist ist keine Gaengelung, sondern die Kopplung an
die Preis-SSoT — ein unbepreistes Modell wuerde 0,00 EUR ins Ledger schreiben,
obwohl der Key echtes Geld kostet.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
from fastapi import HTTPException

from src.vision_provider import VisionProvider

logger = logging.getLogger(__name__)


# Geschlossene Allowlist. Jeder Eintrag MUSS in src/pricing.py MODEL_PRICING
# stehen — sonst buchte das Ledger 0,00 EUR fuer echtes Geld (die
# Silent-Gratis-Klasse, die die Preis-SSoT ausdruecklich verbietet). Der
# Startup-Invariant test_gemini_vision_models_are_priced haelt das fest.
GEMINI_VISION_MODELS = frozenset({
    "gemini-2.5-flash",       # Default (Rafael 2026-09-03)
    "gemini-3.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
})

# Rafael, 2026-09-03: der Vision-Standard auf der DEV-Bridge ist Gemini 2.5
# FLASH, nicht Flash-Lite. Flash-Lite waere dreimal guenstiger (0,10/0,40 gegen
# 0,30/2,50), aber hier geht es um die Qualitaet der Analyseberichte — und die
# ist der Grund, warum ueberhaupt gemessen wird. Flash-Lite bleibt bepreist und
# per Env waehlbar, falls die Messung zeigt, dass es reicht.
DEFAULT_GEMINI_VISION_MODEL = "gemini-2.5-flash"
DEFAULT_GEMINI_VISION_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

API_KEY_ENV = "GEMINI_VISION_API_KEY"

#: Gemini ``finishReason`` -> Anthropic ``stop_reason``-Vokabular. Uebersetzt
#: an der Provider-Grenze, damit stromabwaerts NICHTS zwischen den beiden
#: Anbietern unterscheiden muss: ``vision_provider.finish_reason_for`` und
#: ``ensure_usable_vision_content`` arbeiten unveraendert weiter. Unbekannte
#: Werte werden durchgereicht statt beschoenigt — dieselbe Politik wie dort.
_GEMINI_FINISH_REASON_MAP = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
}


@dataclass
class GeminiVisionResponse:
    """Antwort einer Gemini-Bildanalyse.

    Feldgleich mit ``src.vision_provider.VisionResponse``, damit der
    Vision-Router beide Anbieter uniform behandeln kann.
    """
    content: str
    model: str
    usage: Dict[str, int]
    stop_reason: str


class GeminiVisionError(RuntimeError):
    """Konfigurations-/Antwortfehler des Gemini-Vision-Wegs.

    Bewusst eigene Klasse: ein Fehler hier darf NIE als "dann eben Sonnet"
    enden. Es gibt keinen stillen Fallback auf ein anderes Modell — der
    Testlauf soll messen, was Gemini liefert, und ein heimlich von Anthropic
    beantworteter Aufruf haette genau diese Messung verfaelscht (und dazu
    Kundendaten-Routing verschleiert, siehe Modul-Docstring).
    """


def resolve_gemini_vision_model() -> str:
    """Das konfigurierte Gemini-Bildmodell — Env, gegen die Allowlist geprueft.

    Pro Aufruf gelesen (nicht beim Import): ein Modellwechsel ist damit eine
    Konfigurationsaenderung mit Container-Neustart, kein Rebuild.

    Faellt NICHT auf den Default zurueck, wenn die Env einen unbekannten Wert
    traegt — ein Tippfehler wuerde sonst still das teurere/falsche Modell
    messen lassen. Fail loud.
    """
    raw = (os.getenv("GEMINI_VISION_MODEL") or "").strip()
    if not raw:
        return DEFAULT_GEMINI_VISION_MODEL
    if raw not in GEMINI_VISION_MODELS:
        raise GeminiVisionError(
            f"GEMINI_VISION_MODEL={raw!r} ist kein bekanntes Gemini-Bildmodell. "
            f"Erlaubt (und in src/pricing.py bepreist): {sorted(GEMINI_VISION_MODELS)}. "
            "Ein unbepreistes Modell wuerde 0,00 EUR ins Ledger schreiben, obwohl "
            "der Key echtes Geld kostet."
        )
    return raw


def gemini_vision_base_url() -> str:
    """Basis-URL der Gemini-API ohne Schluss-Slash (siehe Modul-Docstring)."""
    return (os.getenv("GEMINI_VISION_BASE_URL") or DEFAULT_GEMINI_VISION_BASE_URL).rstrip("/")


def gemini_vision_key_present() -> bool:
    """True genau dann, wenn ein NICHT-LEERER API-Key gesetzt ist.

    Gegen ``os.getenv`` geprueft und nicht gegen ein Provider-Singleton — aus
    demselben Grund wie ``vision_provider.vision_available()``: eine
    Compose-Zeile ``GEMINI_VISION_API_KEY=${GEMINI_API_KEY:-}`` erzeugt bei
    fehlendem Host-Wert eine EXISTIERENDE, aber leere Variable.
    """
    return bool((os.getenv(API_KEY_ENV) or "").strip())


def _to_gemini_contents(
    anthropic_messages: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Anthropic-Nachrichtenbloecke -> Gemini ``contents``.

    Bewusst zweistufig (OpenAI/roh -> Anthropic -> Gemini) statt direkt: das
    Herausloesen der Bilder aus den vier Eingabeformaten (OpenAI ``image_url``,
    Anthropic ``image``, roher und in eckigen Klammern eingebetteter
    data:-URL-Text) steckt in ``VisionProvider`` und hat dort eine
    Fehlerhistorie hinter sich (Base64-Padding, Alphabet-Begrenzung, externe
    URLs). Diese Logik ein zweites Mal zu schreiben hiesse, dieselben Fehler
    ein zweites Mal zu machen.
    """
    contents: List[Dict[str, Any]] = []
    for message in anthropic_messages:
        # Gemini kennt "user" und "model" — "assistant" gibt es dort nicht.
        role = "model" if message.get("role") == "assistant" else "user"
        content = message.get("content")

        parts: List[Dict[str, Any]] = []
        if isinstance(content, str):
            if content:
                parts.append({"text": content})
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = block.get("text", "")
                    if text:
                        parts.append({"text": text})
                elif block.get("type") == "image":
                    source = block.get("source", {})
                    if source.get("type") != "base64":
                        raise GeminiVisionError(
                            f"Bildquelle {source.get('type')!r} wird auf dem "
                            "Gemini-Weg nicht unterstuetzt — erwartet wird "
                            "base64 (VisionProvider laedt externe URLs bereits "
                            "vorher herunter)."
                        )
                    parts.append({
                        "inline_data": {
                            "mime_type": source.get("media_type", "image/png"),
                            "data": source.get("data", ""),
                        }
                    })

        if parts:
            contents.append({"role": role, "parts": parts})

    return contents


def _extract_text(candidate: Dict[str, Any]) -> str:
    """Alle Text-Parts einer Kandidaten-Antwort zusammengefuegt.

    Denk-Parts (``thought: true``) werden ausgelassen: sie sind bezahlter,
    aber nicht ausgebbarer Inhalt. Sie zaehlen trotzdem in den Output-Tokens
    (siehe ``_usage_from``) — genau das ist der Kostenposten, um den es geht.
    """
    text = ""
    for part in candidate.get("content", {}).get("parts", []) or []:
        if isinstance(part, dict) and not part.get("thought") and "text" in part:
            text += part.get("text") or ""
    return text


def _usage_from(data: Dict[str, Any]) -> Dict[str, int]:
    """``usageMetadata`` -> das OpenAI-Usage-Trio der Bridge.

    ENTSCHEIDEND fuer die Abrechnung: ``thoughtsTokenCount`` wird zu den
    Output-Tokens ADDIERT. Gemini weist Denk-Tokens getrennt aus, verrechnet
    sie aber zum Output-Preis. Wer nur ``candidatesTokenCount`` bucht,
    unterschaetzt genau den Posten, der bei einer Bildanalyse dominiert (E3s
    Messung: der Kostenposten ist das Thinking, nicht der Text) — die Zahl im
    Ledger waere dann systematisch zu niedrig, und der Test haette Gemini
    guenstiger aussehen lassen, als es ist.
    """
    meta = data.get("usageMetadata") or {}
    prompt = int(meta.get("promptTokenCount") or 0)
    completion = int(meta.get("candidatesTokenCount") or 0) + int(
        meta.get("thoughtsTokenCount") or 0
    )
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


class GeminiVisionProvider:
    """Gemini ``generateContent`` mit Bildeingabe, per API-Key.

    Spiegelt ``VisionProvider`` in Vertrag und Fehlerverhalten: 4xx (ausser
    429) werden zur ``HTTPException`` mit durchgereichtem Upstream-Text, alles
    andere zu ``RuntimeError`` — so greift die vorhandene Klassifikation in
    ``main.py`` (``classify_exception``) unveraendert.
    """

    def __init__(self) -> None:
        self.api_key = (os.getenv(API_KEY_ENV) or "").strip()
        if not self.api_key:
            logger.warning(
                "%s nicht gesetzt — der Gemini-Bildweg weist Anfragen ab "
                "(kein Fallback auf ein anderes Modell).", API_KEY_ENV,
            )

    async def analyze(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None,
        timeout: float = 300.0,
    ) -> GeminiVisionResponse:
        """Bildanalyse ueber Gemini.

        ``max_tokens`` wird NUR gesendet, wenn der Aufrufer selbst einen Wert
        gesetzt hat. Die Bridge erfindet hier bewusst KEINEN Ausgabedeckel
        (Rafael, 2026-09-03): alles Sichtbare wird stromabwaerts genutzt, und
        ein gedeckelter Lauf haette die Qualitaetsmessung verfaelscht statt
        Kosten zu sparen — der Kostenposten ist das Thinking, und das steuert
        die App an ihrer eigenen Seite.

        Anthropic-spezifische Parameter (``thinking``, ``output_config``) nimmt
        diese Methode bewusst NICHT entgegen; siehe
        ``gemini_vision_gate.assert_no_anthropic_only_params``.
        """
        if not self.api_key:
            raise GeminiVisionError(
                f"{API_KEY_ENV} ist nicht gesetzt. Der Gemini-Bildweg ist damit "
                "nicht benutzbar — es gibt bewusst keinen stillen Rueckfall auf "
                "das Anthropic-Bildmodell."
            )

        resolved_model = model or resolve_gemini_vision_model()
        if resolved_model not in GEMINI_VISION_MODELS:
            raise GeminiVisionError(
                f"Modell {resolved_model!r} ist fuer den Gemini-Bildweg nicht "
                f"freigegeben. Erlaubt: {sorted(GEMINI_VISION_MODELS)}."
            )

        anthropic_messages, extracted_system = VisionProvider._convert_to_anthropic_messages(
            messages
        )
        final_system = system_prompt or extracted_system
        contents = _to_gemini_contents(anthropic_messages)

        if not contents:
            raise GeminiVisionError(
                "Nach der Konvertierung blieb kein Inhalt uebrig — der Aufruf "
                "haette ein leeres contents-Array an Gemini geschickt."
            )

        image_count = sum(
            1
            for c in contents
            for p in c["parts"]
            if "inline_data" in p
        )

        body: Dict[str, Any] = {"contents": contents}
        if final_system:
            body["systemInstruction"] = {"parts": [{"text": final_system}]}

        generation_config: Dict[str, Any] = {}
        if temperature is not None:
            generation_config["temperature"] = temperature
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        if generation_config:
            body["generationConfig"] = generation_config

        url = f"{gemini_vision_base_url()}/models/{resolved_model}:generateContent"
        logger.info(
            "Gemini-Vision-Anfrage: %d Bilder, model=%s",
            image_count, resolved_model,
            extra={"image_count": image_count, "model": resolved_model},
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    # Key im Header, NICHT als ?key=-Query-Parameter: ein
                    # Query-String landet in Zugriffs-/Proxy-Logs.
                    "x-goog-api-key": self.api_key,
                },
                json=body,
            )

        if response.status_code != 200:
            error_body = response.text
            logger.error(
                "Gemini API error: %s", response.status_code,
                extra={"status_code": response.status_code, "error": error_body[:500]},
            )
            if 400 <= response.status_code < 500 and response.status_code != 429:
                raise HTTPException(status_code=response.status_code, detail=error_body[:1000])
            raise RuntimeError(
                f"Gemini API error ({response.status_code}): {error_body[:200]}"
            )

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            # Prompt-seitige Blockade (Safety) liefert gar keinen Kandidaten.
            # Ohne diesen Zweig waere das ein IndexError statt einer Aussage.
            block = (data.get("promptFeedback") or {}).get("blockReason")
            raise GeminiVisionError(
                "Gemini lieferte keinen Kandidaten"
                + (f" (blockReason={block!r})" if block else "")
                + " — die Anfrage wurde upstream abgewiesen."
            )

        candidate = candidates[0]
        raw_finish = candidate.get("finishReason") or "STOP"
        stop_reason = _GEMINI_FINISH_REASON_MAP.get(raw_finish, raw_finish.lower())
        response_text = _extract_text(candidate)
        usage = _usage_from(data)

        log = logger.warning if not response_text else logger.info
        log(
            "Gemini-Vision-Antwort: %d Zeichen%s",
            len(response_text),
            "" if response_text else f" — LEER (finishReason={raw_finish!r})",
            extra={
                "response_length": len(response_text),
                "stop_reason": stop_reason,
                "input_tokens": usage["prompt_tokens"],
                "output_tokens": usage["completion_tokens"],
            },
        )

        served = data.get("modelVersion")
        if served and served != resolved_model:
            # Nur protokollieren, NICHT ins Ledger uebernehmen: Google haengt an
            # ``modelVersion`` gern eine Punktfassung an (…-001). Die stuende in
            # keiner Preiszeile, und ``price_entry`` kuerzt nur ein
            # Datums-Suffix — die Zeile buchte dann still 0,00 EUR. Bepreist und
            # deterministisch ist der ANGEFRAGTE Name, und der ist es auch, den
            # eine Auswertung wiederfinden koennen muss.
            logger.info(
                "Gemini bediente modelVersion=%r (angefragt: %r) — im Ledger "
                "steht der angefragte Name, weil nur der bepreist ist.",
                served, resolved_model,
            )

        return GeminiVisionResponse(
            content=response_text,
            model=resolved_model,
            usage=usage,
            stop_reason=stop_reason,
        )


_provider: Optional[GeminiVisionProvider] = None


def get_gemini_vision_provider() -> GeminiVisionProvider:
    """Singleton — spiegelt ``vision_provider.get_vision_provider()``."""
    global _provider
    if _provider is None:
        _provider = GeminiVisionProvider()
    return _provider
