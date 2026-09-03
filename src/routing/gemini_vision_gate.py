"""Zugangssperre fuer den Gemini-Bildweg — Testmodus, synthetische Plaene.

WORUM ES GEHT
-------------
Google ist kein zugesagter Unterauftragsverarbeiter. In
``werkingflow-business/legal/compliance/avv.md`` §5.4 ist die Liste der
Unterauftragsverarbeiter abschliessend (Hetzner, AWS EMEA, Anthropic, Vercel,
OpenAI, Resend, ConvertAPI, Sentry) — Google steht nicht darin. Die ueber die
USA bezogenen Leistungen sind ebenfalls abschliessend genannt (avv.md:191/209:
Spracherkennung/OpenAI, Transaktionsmail/Resend, Recherche/Anthropic), und fuer
die KI-Inferenz ist EU-Residenz zugesagt (avv.md:84/86, Bedrock Frankfurt).

Daraus folgt hart: **echte Kundenplaene duerfen nicht an Gemini** — weder ueber
den US-Endpunkt noch ueber Vertex EU. Vertex EU beseitigt den
Drittlandtransfer, macht Google aber trotzdem zum NEUEN
Unterauftragsverarbeiter; das ist eine AVV-Aenderung samt Kundeninformation und
damit Rafaels Entscheidung, nicht die einer Session.

Was der Testmodus dagegen darf und soll: mit SYNTHETISCHEN Plaenen messen, ob
ein Flash-Lite-Modell die Bildanalyse ueberhaupt in brauchbarer Qualitaet
liefert. Genau diese Frage will Rafael beantwortet haben, und sie beruehrt
keine Kundendaten.

WARUM DAS HIER CODE IST UND NICHT DOKUMENTATION
-----------------------------------------------
Eine Regel, die nur im Text steht, ist beim naechsten Deploy eine Bitte. Diese
hier ist eine Bedingung: ``assert_gemini_vision_allowed`` wirft, und der
Aufrufer hat keinen Zweig, in dem der Aufruf trotzdem an Google geht.

VIER UNABHAENGIGE SCHICHTEN — und was jede WIRKLICH beweist
------------------------------------------------------------
Keine einzelne Schicht traegt die Zusage; sie tragen sie gemeinsam, und zwei
davon liegen ausserhalb der Reichweite der aufrufenden App:

1. **Kein Key in prod.** ``GEMINI_VISION_API_KEY`` wird bewusst nur auf der
   dev-Bridge hinterlegt (DevOps/Rafael verteilen ihn, das prod-Gate blockt
   Agent-Sessions ohnehin). Ohne Key kann der Provider nicht senden. Das ist
   die einzige Schicht, die auch dann haelt, wenn dieser Code Fehler hat —
   deshalb steht sie an erster Stelle und wird NICHT durch eine "praktischere"
   Compose-Zeile aufgeweicht.
2. **Master-Flag ``BRIDGE_GEMINI_VISION_ENABLED``**, default aus. Derselbe
   Ein-Schalter-Aus-Zustand wie bei ``PREPAID_VISION_DAILY_CAP_ENABLED``: der
   Weg ist inert, bis ihn jemand bewusst einschaltet.
3. **``app_env`` muss ein positiv erkanntes Nicht-Prod sein** (``staging`` oder
   ``local``). ``None`` wird ABGEWIESEN, nicht durchgelassen — das ist die
   Umkehrung des Bedrock-Gates und aus demselben Grund richtig: eine Umgebung,
   die sich nicht ausweist, koennte Produktion sein. Bei Bedrock war
   fail-closed "kein Bedrock ohne Prod-Nachweis", hier ist es "kein Google ohne
   Nicht-Prod-Nachweis". Beide Male gewinnt die vorsichtige Richtung.
4. **Ausdrueckliche Testmodus-Erklaerung des Aufrufers** (Header
   ``X-Vision-Test-Mode: synthetic``). Diese Schicht ist die schwaechste und
   soll auch nicht mehr sein als sie ist: sie beweist NICHT, dass die Bilder
   synthetisch sind — das kann die Bridge nicht wissen, ein Plan ist ein Bild.
   Sie stellt sicher, dass ein normaler Anwendungsaufruf niemals versehentlich
   hier landet, und sie schreibt die Behauptung als ``synthetic_declared`` ins
   Ledger, wo sie pruefbar wird. Die Verantwortung fuer "synthetisch" bleibt
   beim Aufrufer, und dass sie dort liegt, steht damit im Datensatz statt in
   einem Kopf.

Bewusst NICHT gebaut: eine Inhaltspruefung "ist dieser Plan synthetisch".
Sie waere ratend und wuerde eine Sicherheit vortaeuschen, die es nicht gibt.
Der belastbare Schutz ist Schicht 1 und 3.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Header, mit dem der Aufrufer den Testmodus ausdruecklich erklaert.
TEST_MODE_HEADER = "X-Vision-Test-Mode"
TEST_MODE_VALUE = "synthetic"

#: Positiv erkannte Nicht-Produktionsumgebungen (Werte aus
#: ``src.tenant.middleware.normalize_app_env``). ``None`` gehoert bewusst NICHT
#: dazu — siehe Modul-Docstring, Schicht 3.
ALLOWED_APP_ENVS = frozenset({"staging", "local"})


class GeminiVisionRefused(RuntimeError):
    """Der Gemini-Bildweg wurde angefragt, ist hier aber nicht zulaessig.

    Bewusst eine eigene Klasse und bewusst KEIN Rueckfall auf den
    Anthropic-Bildweg: wer Gemini angefragt hat, soll entweder Gemini bekommen
    oder eine Absage lesen. Ein stiller Wechsel des Anbieters waere in beide
    Richtungen falsch — er wuerde eine Qualitaetsmessung verfaelschen und, in
    der anderen Richtung, ein Datenschutzversprechen unbemerkt aendern.
    """


def gemini_vision_enabled() -> bool:
    """Master-Flag (Schicht 2). Pro Aufruf gelesen — Abschalten wirkt sofort."""
    return (os.getenv("BRIDGE_GEMINI_VISION_ENABLED") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def declares_synthetic_test_mode(request: Any) -> bool:
    """Hat der Aufrufer den Testmodus ausdruecklich erklaert (Schicht 4)?

    Toleriert ein fehlendes Request-Objekt (interne Aufrufer ohne HTTP-Kontext)
    mit ``False`` — nicht mit ``True``. Eine fehlende Erklaerung ist keine
    Erklaerung.
    """
    if request is None:
        return False
    headers = getattr(request, "headers", None)
    if headers is None:
        return False
    value = headers.get(TEST_MODE_HEADER) or headers.get(TEST_MODE_HEADER.lower())
    return (value or "").strip().lower() == TEST_MODE_VALUE


def assert_gemini_vision_allowed(
    *,
    app_env: Optional[str],
    has_images: bool,
    declares_synthetic: bool,
) -> None:
    """Alle Schichten pruefen. Wirft ``GeminiVisionRefused``, sonst kehrt sie
    still zurueck.

    ``app_env`` ist der bereits normalisierte Bucket (prod|staging|local|None)
    aus ``normalize_app_env`` — dieselbe Konvention wie im Bedrock-Gate, damit
    beide Sperren dieselbe Sprache sprechen.

    ``has_images`` ist nicht kosmetisch: der Gemini-Testweg ist ein
    BILD-Analyseweg. Ein Aufruf ohne Bild wuerde sonst durch die Vision-Weiche
    fallen und still vom Claude-SDK beantwortet — der Aufrufer bekaeme ein
    anderes Modell als angefragt und wuesste es nicht. Deshalb: laut abweisen.
    """
    from src.providers.gemini_vision import API_KEY_ENV, gemini_vision_key_present

    if not gemini_vision_enabled():
        raise GeminiVisionRefused(
            "Der Gemini-Bildweg ist nicht eingeschaltet "
            "(BRIDGE_GEMINI_VISION_ENABLED ist nicht gesetzt). Er ist ein "
            "Testweg fuer synthetische Plaene und standardmaessig inert."
        )

    if not gemini_vision_key_present():
        raise GeminiVisionRefused(
            f"{API_KEY_ENV} ist auf dieser Bridge nicht hinterlegt. Das ist auf "
            "Produktions-Workern ABSICHT: echte Kundenplaene duerfen nicht an "
            "Google (avv.md §5.4 — Google ist kein gelisteter "
            "Unterauftragsverarbeiter). Kein Rueckfall auf ein anderes Modell."
        )

    if app_env not in ALLOWED_APP_ENVS:
        raise GeminiVisionRefused(
            f"Der Gemini-Bildweg ist auf Nicht-Produktion beschraenkt, dieser "
            f"Aufruf meldet aber app_env={app_env!r}. Erlaubt: "
            f"{sorted(ALLOWED_APP_ENVS)}. Ein fehlender oder unbekannter "
            "X-App-Env-Wert gilt als NICHT nachgewiesenes Nicht-Prod und wird "
            "abgewiesen — eine Umgebung, die sich nicht ausweist, koennte "
            "Produktion sein, und dort duerfen keine Kundenplaene an Google."
        )

    if not declares_synthetic:
        raise GeminiVisionRefused(
            f"Dem Aufruf fehlt die ausdrueckliche Testmodus-Erklaerung "
            f"({TEST_MODE_HEADER}: {TEST_MODE_VALUE}). Der Gemini-Weg ist "
            "ausschliesslich fuer synthetische Plaene gedacht; die Bridge kann "
            "einem Bild nicht ansehen, ob es synthetisch ist, also muss der "
            "Aufrufer es erklaeren — und die Erklaerung wird mitprotokolliert."
        )

    if not has_images:
        raise GeminiVisionRefused(
            "Der Gemini-Testweg ist ein Bildanalyse-Weg, dieser Aufruf enthaelt "
            "aber kein Bild. Abgewiesen statt still vom Claude-SDK beantwortet "
            "zu werden — der Aufrufer bekaeme sonst ein anderes Modell als "
            "angefragt, ohne es zu merken."
        )

    logger.info(
        "🧪 Gemini-Bildweg zugelassen (app_env=%s, Testmodus erklaert)", app_env,
    )


def assert_no_anthropic_only_params(
    *, thinking: Any = None, output_config: Any = None
) -> None:
    """Anthropic-eigene Parameter duerfen nicht still unter den Tisch fallen.

    ``thinking`` und ``output_config`` sind Felder der Anthropic Messages API
    und haben bei Gemini keine Entsprechung, die man ohne Bedeutungsverlust
    einsetzen koennte (Gemini steuert das ueber ``thinkingConfig`` bzw.
    ``thinking_level``, mit anderer Semantik). Sie einfach wegzulassen waere der
    teuerste denkbare Silent-Fail: die Antwort kaeme mit 200 zurueck, waere
    aber unter anderen Bedingungen erzeugt worden als angefordert — und genau
    diese Bedingungen sind der Kostenposten, um den es in dieser Messung geht.

    Deshalb: laut abweisen und den Aufrufer entscheiden lassen. Eine Abbildung
    der beiden Regler auf Gemini ist bewusst NICHT gebaut; sie waere geraten.
    """
    offending = [
        name
        for name, value in (("thinking", thinking), ("output_config", output_config))
        if value is not None
    ]
    if not offending:
        return
    raise GeminiVisionRefused(
        "Anthropic-spezifische Parameter auf dem Gemini-Weg: "
        + ", ".join(offending)
        + ". Sie haben bei Gemini keine verlustfreie Entsprechung und werden "
        "NICHT stillschweigend ignoriert — eine Antwort, die unter anderen "
        "Denk-Einstellungen entstanden ist als angefordert, waere als Messung "
        "wertlos. Entweder ohne diese Felder aufrufen oder auf dem "
        "Anthropic-Bildweg bleiben."
    )
