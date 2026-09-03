"""Umgebungssperre fuer den Gemini-Bildweg — Staging ja, Produktion nein.

WAS HIER ENTSCHIEDEN IST (Rafael, 2026-09-03, ueber X1 84c60c8b)
----------------------------------------------------------------
Auf der **DEV-Bridge (Staging)** soll die Bildanalyse **standardmaessig ueber
Gemini 2.5 Flash** laufen — fuer alle Apps, ausdruecklich einschliesslich
Energy, und ausdruecklich mit **normalen Staging-Inhalten**. Die urspruengliche
Beschraenkung "nur synthetische Plaene" ist damit **aufgehoben**: Rafael gibt
die Inhalte frei, die auf Staging liegen. Das ist seine Entscheidung als
Verantwortlicher, und sie ist hier umgesetzt, nicht kommentiert.

**Auf Produktion aendert sich nichts.** Dort bleibt es beim bisherigen Weg;
Gemini ist dort nicht erreichbar. Der Grund ist unveraendert und gilt weiter:
Google ist in ``werkingflow-business/legal/compliance/avv.md`` §5.4 nicht als
Unterauftragsverarbeiter gelistet, die ueber die USA bezogenen Leistungen sind
dort abschliessend aufgezaehlt, und fuer die KI-Inferenz ist EU-Residenz
zugesagt (avv.md:84/86). Diese Zusage betrifft **produktive Kundendaten** — und
genau die liegen per Definition nicht auf Staging. Ein EU-Endpunkt (Vertex)
wuerde daran nichts aendern: er beseitigt den Drittlandtransfer, macht Google
aber trotzdem zum NEUEN Unterauftragsverarbeiter, also AVV-Aenderung plus
Information der Bestandskunden. Diese Entscheidung steht bei Rafael, nicht hier.

VORBEDINGUNG, DIE KEIN CODE PRUEFEN KANN: PAID TIER
---------------------------------------------------
Der Google-Tarif entscheidet mit ueber die Datenschutz-Lage, und die Bridge kann
ihn einem API-Key nicht ansehen. Laut ai.google.dev/gemini-api/terms (geprueft
2026-09-03) nutzt Google auf dem **Free Tier** die eingereichten Inhalte und die
Antworten, um eigene Produkte zu verbessern und weiterzuentwickeln, und
menschliche Pruefer duerfen sie lesen. Auf dem **Paid Tier** ist beides
ausdruecklich ausgeschlossen.

Der Unterschied ist die Rolle: auf dem Free Tier verarbeitet Google nicht nur
FUER UNS, sondern auch FUER SICH — das ist keine Auftragsverarbeitung mehr.
Rafaels Freigabe vom 2026-09-03 betrifft, dass Gemini die Staging-Inhalte
ANALYSIERT; dass Google sie behaelt und mitlernt, ist davon nicht gedeckt.

Deshalb: dieser Weg wird erst scharfgeschaltet, wenn das Google-Projekt auf Paid
Tier steht. Das laesst sich hier nicht erzwingen — es steht in der
Umschalt-Anleitung (docker/docker-compose.yml) an der Stelle, an der jemand die
Schalter umlegt, und es ist bewusst KEIN weiteres Flag: ein Haekchen, das sich
selbst bestaetigt, waere Theater und keine Sicherung.

WAS DIE SPERRE NOCH IST — und was davon wirklich traegt
--------------------------------------------------------
Nach dem Wegfall der Synthetik-Erklaerung bleiben drei Schichten. Sie sind
NICHT gleich stark, und das gehoert dazugesagt:

1. **Kein Key auf der Prod-Bridge.** Das ist die tragende Schicht. Dev- und
   Prod-Bridge sind getrennte Deployments mit getrennten Secrets; ohne
   ``GEMINI_VISION_API_KEY`` kann ein Prod-Worker nicht zu Google senden,
   unabhaengig davon, ob dieser Code richtig ist. Deshalb steht der Key
   ausdruecklich NUR in der dev-Ablage, und die prod-Compose-Datei traegt einen
   Kommentar, der erklaert, warum die Zeile dort fehlt.
2. **Master-Flag ``BRIDGE_GEMINI_VISION_ENABLED``**, default aus.
3. **``app_env`` muss ein positiv erkanntes Nicht-Prod sein** (``staging`` oder
   ``local``); ``None`` wird abgewiesen. Diese Schicht ist die schwaechste, weil
   ``app_env`` aus einem Client-Header stammt — ein Aufrufer, der sich falsch
   ausweist, kaeme daran vorbei. Sie schuetzt gegen Versehen, nicht gegen einen
   luegenden Aufrufer. Dass das reicht, liegt allein an Schicht 1: auf dem
   Worker, der Produktionsverkehr bedient, gibt es keinen Key.

Bewusst NICHT (mehr) gebaut: die Erklaerung ``X-Vision-Test-Mode: synthetic``.
Sie hatte genau einen Zweck — sicherzustellen, dass nur synthetische Plaene
hierher kommen. Diese Anforderung ist entfallen; einen Pflicht-Header
stehenzulassen, dessen Begruendung weg ist, waere Ballast, der spaeter als
Sicherheit missverstanden wird.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

#: Positiv erkannte Nicht-Produktionsumgebungen (Werte aus
#: ``src.tenant.middleware.normalize_app_env``). ``None`` gehoert bewusst NICHT
#: dazu — eine Umgebung, die sich nicht ausweist, koennte Produktion sein.
ALLOWED_APP_ENVS = frozenset({"staging", "local"})


class GeminiVisionRefused(RuntimeError):
    """Der Gemini-Bildweg wurde angefragt, ist hier aber nicht zulaessig.

    Bewusst eine eigene Klasse und bewusst KEIN Rueckfall auf den
    Anthropic-Bildweg: wer Gemini angefragt hat, soll entweder Gemini bekommen
    oder eine Absage lesen. Ein stiller Wechsel des Anbieters waere in beide
    Richtungen falsch — er wuerde eine Qualitaets-/Kostenmessung verfaelschen
    und, in der anderen Richtung, ein Datenschutzversprechen unbemerkt aendern.
    """


def gemini_vision_enabled() -> bool:
    """Master-Flag (Schicht 2). Pro Aufruf gelesen — Abschalten wirkt sofort."""
    return (os.getenv("BRIDGE_GEMINI_VISION_ENABLED") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def assert_gemini_vision_allowed(
    *,
    app_env: Optional[str],
    has_images: bool,
) -> None:
    """Alle Schichten pruefen. Wirft ``GeminiVisionRefused``, sonst kehrt sie
    still zurueck.

    ``app_env`` ist der bereits normalisierte Bucket (prod|staging|local|None)
    aus ``normalize_app_env`` — dieselbe Konvention wie im Bedrock-Gate, damit
    beide Sperren dieselbe Sprache sprechen.

    ``has_images`` ist nicht kosmetisch: dies ist ein BILD-Weg. Ein Aufruf ohne
    Bild wuerde sonst durch die Vision-Weiche fallen und still vom Claude-SDK
    beantwortet — der Aufrufer bekaeme ein anderes Modell als angefragt und
    wuesste es nicht.
    """
    from src.providers.gemini_vision import API_KEY_ENV, gemini_vision_key_present

    if not gemini_vision_enabled():
        raise GeminiVisionRefused(
            "Der Gemini-Bildweg ist auf dieser Bridge nicht eingeschaltet "
            "(BRIDGE_GEMINI_VISION_ENABLED ist nicht gesetzt)."
        )

    if not gemini_vision_key_present():
        raise GeminiVisionRefused(
            f"{API_KEY_ENV} ist auf dieser Bridge nicht hinterlegt. Auf "
            "Produktions-Workern ist das ABSICHT und die tragende Schicht der "
            "Sperre: produktive Kundendaten gehen nicht an Google (avv.md §5.4 "
            "— Google ist kein gelisteter Unterauftragsverarbeiter). Kein "
            "Rueckfall auf ein anderes Modell."
        )

    if app_env not in ALLOWED_APP_ENVS:
        raise GeminiVisionRefused(
            f"Der Gemini-Bildweg ist auf Nicht-Produktion beschraenkt, dieser "
            f"Aufruf meldet aber app_env={app_env!r}. Erlaubt: "
            f"{sorted(ALLOWED_APP_ENVS)}. Ein fehlender oder unbekannter "
            "X-App-Env-Wert gilt als NICHT nachgewiesenes Nicht-Prod und wird "
            "abgewiesen — eine Umgebung, die sich nicht ausweist, koennte "
            "Produktion sein."
        )

    if not has_images:
        raise GeminiVisionRefused(
            "Der Gemini-Weg ist ein Bildanalyse-Weg, dieser Aufruf enthaelt "
            "aber kein Bild. Abgewiesen statt still vom Claude-SDK beantwortet "
            "zu werden — der Aufrufer bekaeme sonst ein anderes Modell als "
            "angefragt, ohne es zu merken."
        )

    logger.info("🖼️ Gemini-Bildweg zugelassen (app_env=%s)", app_env)


def assert_no_anthropic_only_params(
    *, thinking: object = None, output_config: object = None
) -> None:
    """Anthropic-eigene Parameter duerfen nicht still unter den Tisch fallen.

    ``thinking`` und ``output_config`` sind Felder der Anthropic Messages API
    und haben bei Gemini keine Entsprechung, die man ohne Bedeutungsverlust
    einsetzen koennte (Gemini steuert das ueber ``thinkingConfig`` bzw.
    ``thinking_level``, mit anderer Semantik). Sie einfach wegzulassen waere der
    teuerste denkbare Silent-Fail: die Antwort kaeme mit 200 zurueck, waere aber
    unter anderen Bedingungen erzeugt worden als angefordert — und genau diese
    Bedingungen sind der Kostenposten, um den es in dieser Messung geht.

    Von E3 am 2026-09-03 ausdruecklich bestaetigt: keine Uebersetzung
    ``effort`` -> ``thinkingConfig``, die 403-Abweisung ist gewollt. Eine
    Abbildung waere geraten.
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
