"""Wegwahl fuer Textaufrufe: was nur der direkte Weg einhalten kann, geht dorthin.

Der Standardweg der Bridge (Claude-Code-SDK im Abo-Pool) reicht weder
``output_config`` (Denk-Stufe, Ausgabeformat) noch ``max_tokens`` an das Modell
weiter (``ChatCompletionRequest.log_unsupported_parameters``). Die CLI setzt nach
ihrer eigenen Ausgabegrenze sogar selbst fort ("Output token limit hit. Resume
directly"). Ein verlangtes Budget gilt dort also nicht: Muehls Langbeschreibung
kam am 14.10.2026 mit 71 197 Ausgabe-Token bei max_tokens 64 000 zurueck.

Bis hierher hat die App das selbst entschieden und ``provider_tier=
claude-direct-notools`` fest mitgeschickt (werking-energy
schema_analysis/bridge_client.py). Die Entscheidung gehoert in die Bridge — sie
allein weiss, welcher Weg welches Feld einhaelt. Diese Regel ergaenzt die
tabellengesteuerte ``app_tier_policy``: dort entscheidet (App, Agent, Umgebung),
hier die Form der Anfrage.

Ein Aufruf geht auf den direkten Weg, wenn ALLE Bedingungen gelten:

- kein Weg ist schon gewaehlt — weder vom Aufrufer (``provider_tier``) noch von
  der app_tier_policy — und kein Nutzer-Pin steht dagegen (das prueft main.py).
  Die App-Provider-Regel ``anthropic`` (jede Kunden-App, auch per globaler
  Vorgabe) haelt die Regel nicht auf: sie sperrt Bedrock und fremde Gateways,
  der direkte Weg ist derselbe Anbieter. Sie raeumt allerdings jeden vom
  Aufrufer geschickten ``provider_tier`` ab — deshalb kommt das feste
  ``claude-direct-notools`` der App bei der Bridge gar nicht an, und diese
  Regel ist der einzige Weg, auf dem es wirkt;
- Standard-Backend ``anthropic`` (Bedrock/OpenAI-kompatibel bleiben unberuehrt);
- reiner Textaufruf: ``enable_tools`` nicht True, kein Bild, keine
  ``session_id`` (der direkte Weg ist zustandslos, eine SDK-Sitzung liesse sich
  dort nicht fortsetzen);
- der Aufruf verlangt etwas, das nur der direkte Weg einhaelt: ``output_config``
  ist gesetzt, ODER ``max_tokens`` ist ausdruecklich gesetzt und groesser als die
  Abo-Grenze (``BRIDGE_ABO_AUSGABEGRENZE``, Vorgabe 32 000 — die Ausgabegrenze je
  Zug der CLI, ab der sie selbst fortsetzt). Der Feld-Default (64 000) zaehlt
  nicht: nur was der Aufrufer schickt, ist verlangt.

Eingeengter Geltungsbereich ``BRIDGE_TEXT_DIRECT_ROUTING_APPS`` — unabhaengig
vom globalen Schalter: kommagetrennte Eintraege ``app-id:agent-praefix``, z. B.
``werking-energy:schema_analysis/beschreibung``. Der Praefix greift
segmentweise (``schema_analysis`` deckt ``schema_analysis/beschreibung``, aber
nicht ``schema_analysis_alt``). Im Bereich geht JEDER reine Textaufruf direkt,
auch ohne output_config und mit kleinem max_tokens — der SDK-Weg haelt
max_tokens ueberhaupt nicht ein (Probe TEILE 14.10.2026: max_tokens 64 → 4 759
Ausgabe-Token). Alle anderen Aufrufe bleiben unveraendert. Ein Tippfehler im
Format oder eine unbekannte App-ID (gegen ``KNOWN_APP_IDS``) ist laut: der
Worker startet nicht (lifespan, ``pruefe_beim_start``). Zur Laufzeit gilt der
beim Start gepruefte Wert. Nur ohne Startpruefung wird tolerant gelesen; dann
trifft ein fehlerhafter Eintrag nur Aufrufe der darin genannten App (503
``text_direct_config_invalid``), alle anderen laufen unveraendert.
Fehlt bei gesetztem Bereich der Schluessel des direkten Wegs, startet der
Worker ebenfalls nicht; zur Laufzeit antwortet ein Aufruf im Bereich 503
``text_direct_key_missing`` statt still auf den SDK-Weg zu fallen.

Globaler Schalter ``BRIDGE_TEXT_DIRECT_ROUTING_ENABLED`` (Vorgabe AUS). Er gilt
fuer JEDEN Aufrufer ohne Nutzer-Pin: alle Kunden-Apps, Aufrufe ohne X-App-ID
und die Nicht-Kunden-Apps (cui, platform, dev-tooling, partner-platform). Ein
Nutzer-Pin sperrt die Regel immer, auch wenn eine App-Regel ihn ueberstimmt
(Engelmann). Fuer einen einzelnen Pfad ist der Geltungsbereich oben der Weg. Grund: der direkte
Weg bucht auf den Prepaid-Schluessel mit Tagesdeckel (prepaid_cap), der Abo-Weg
nicht. Die Regel verschiebt also Kosten und kann den Deckel erreichen; das
Einschalten ist eine Entscheidung, keine Nebenwirkung eines Deploys.

Fail-open wie die app_tier_policy: ist der direkte Weg auf diesem Host nicht
eingerichtet (kein Schluessel), bleibt der Aufruf auf dem Standardweg — laut im
Log, weil sein Budget dann wieder nicht gilt.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

DIRECT_TIER = "claude-direct-notools"

_FLAG_ENV = "BRIDGE_TEXT_DIRECT_ROUTING_ENABLED"
_GRENZE_ENV = "BRIDGE_ABO_AUSGABEGRENZE"
DEFAULT_ABO_AUSGABEGRENZE = 32_000


_APPS_ENV = "BRIDGE_TEXT_DIRECT_ROUTING_APPS"
_SEGMENT = r"[A-Za-z0-9._-]+"
_EINTRAG = re.compile(rf"^({_SEGMENT}):({_SEGMENT}(?:/{_SEGMENT})*/?)$")


class TextDirectRoutingError(Exception):
    """Ein Aufruf IM Geltungsbereich kann nicht wie verlangt bedient werden.

    main.py antwortet mit ``status`` und ``code`` — laut, aber nur fuer diesen
    Aufruf. Aufrufe ausserhalb des Bereichs bekommen diesen Fehler nie.
    """

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _zerlege(raw: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(gueltige Eintraege, fehlerhafte Rohtexte)."""
    gueltig: list[tuple[str, str]] = []
    fehlerhaft: list[str] = []
    for teil in raw.split(","):
        eintrag = teil.strip()
        m = _EINTRAG.match(eintrag)
        if m:
            gueltig.append((m.group(1), m.group(2).rstrip("/")))
        else:
            fehlerhaft.append(eintrag)
    return gueltig, fehlerhaft


def _app_teil(eintrag: str) -> str:
    """App-Teil eines (auch fehlerhaften) Eintrags: alles vor dem ersten
    Trenner (":", ";", "/", Leerraum). Exakt verglichen, nie als
    Teilzeichenkette — ``werking-noise-beta;x`` nennt nicht ``werking-noise``."""
    return re.split(r"[:;/\s]", eintrag.strip(), maxsplit=1)[0]


def _fehlertext(fehlerhaft: list[str], raw: str) -> str:
    return (
        f"{_APPS_ENV}: Eintrag/Eintraege {fehlerhaft!r} nicht im Format "
        f"'app-id:agent-praefix' (z. B. 'werking-energy:schema_analysis/beschreibung'); "
        f"ganzer Wert {raw!r}"
    )


def geltungsbereich() -> list[tuple[str, str]]:
    """``BRIDGE_TEXT_DIRECT_ROUTING_APPS`` als [(app_id, agent_praefix)] — STRENG.

    Fuer den Start (``pruefe_beim_start``): jeder fehlerhafte Eintrag wirft.
    Zur Laufzeit gilt ``_laufzeit_bereich`` — dort darf ein Tippfehler nie
    Aufrufe ausserhalb des Bereichs treffen.
    """
    raw = os.getenv(_APPS_ENV, "").strip()
    if not raw:
        return []
    gueltig, fehlerhaft = _zerlege(raw)
    if fehlerhaft:
        raise RuntimeError(_fehlertext(fehlerhaft, raw))
    return gueltig


_gemeldet: set = set()

# Der beim Start gepruefte Bereich (``pruefe_beim_start``). Ist er gesetzt, gilt
# zur Laufzeit NUR er — die Umgebung wird nicht erneut gelesen.
_start_stand: Optional[tuple[str, list[tuple[str, str]]]] = None


def _laufzeit_bereich(app_id: Optional[str]) -> list[tuple[str, str]]:
    """Gueltige Eintraege fuer diesen Aufruf.

    Normalfall: der beim Start streng gepruefte Wert (``pruefe_beim_start``,
    lifespan). Ein Prozess mit fehlerhaftem Wert startet nicht; was hier
    ankommt, ist geprueft, und eine spaeter geaenderte Umgebung aendert nichts
    (einmal ERROR im Log).

    Zweite Linie — Prozess ohne Startpruefung (Test-Harness, anderer
    Einstieg): der Wert wird tolerant gelesen. Nennt ein fehlerhafter Eintrag
    die App dieses Aufrufs, scheitert nur dieser Aufruf laut (503
    text_direct_config_invalid); gueltige Eintraege wirken, jeder andere
    Aufruf laeuft unveraendert, der Fehler steht einmal je Wert als ERROR im Log.
    """
    raw = os.getenv(_APPS_ENV, "").strip()
    if _start_stand is not None:
        start_raw, start_bereich = _start_stand
        if raw != start_raw and ("geaendert", raw) not in _gemeldet:
            _gemeldet.add(("geaendert", raw))
            logger.error(
                "text_direct_routing: %s hat sich seit dem Start geaendert (%r → %r) — "
                "es gilt weiter der beim Start gepruefte Wert; wirksam erst nach Neustart.",
                _APPS_ENV, start_raw, raw,
            )
        return start_bereich
    if not raw:
        return []
    gueltig, fehlerhaft = _zerlege(raw)
    if not fehlerhaft:
        return gueltig
    if raw not in _gemeldet:
        _gemeldet.add(raw)
        logger.error("text_direct_routing: %s", _fehlertext(fehlerhaft, raw))
    if app_id and any(_app_teil(eintrag) == app_id for eintrag in fehlerhaft):
        raise TextDirectRoutingError(
            503, "text_direct_config_invalid", _fehlertext(fehlerhaft, raw)
        )
    return gueltig


def im_geltungsbereich(app_id: Optional[str], agent_id: Optional[str]) -> Optional[str]:
    """Der passende Eintrag ``app:praefix`` — oder None."""
    if not app_id:
        return None
    bereich = _laufzeit_bereich(app_id)
    if not agent_id:
        return None
    for app, praefix in bereich:
        if app == app_id and (agent_id == praefix or agent_id.startswith(praefix + "/")):
            return f"{app}:{praefix}"
    return None


def pruefe_beim_start(known_app_ids: Optional[set] = None) -> list[tuple[str, str]]:
    """Startpruefung (lifespan): Format, App-IDs, Abo-Grenze und — wenn ein Bereich
    gesetzt ist — der Schluessel des direkten Wegs. Wirft; der Worker startet
    dann nicht. Bei Erfolg gilt der Bereich fuer die ganze Laufzeit.

    ``known_app_ids`` (main.KNOWN_APP_IDS): ein Eintrag mit gueltigem Format,
    aber unbekannter App (``werking-enrgy:…``) bewirkte sonst still nichts.
    """
    global _start_stand
    raw = os.getenv(_APPS_ENV, "").strip()
    bereich = geltungsbereich()
    if known_app_ids is not None:
        unbekannt = sorted({app for app, _ in bereich if app not in known_app_ids})
        if unbekannt:
            raise RuntimeError(
                f"{_APPS_ENV}: unbekannte App-ID(s) {unbekannt} — bekannt sind "
                f"{sorted(known_app_ids)}; ganzer Wert {raw!r}"
            )
    abo_ausgabegrenze()
    if bereich:
        from src.providers.registry import is_tier_usable

        if not is_tier_usable(DIRECT_TIER):
            raise RuntimeError(
                f"{_APPS_ENV}={os.getenv(_APPS_ENV)!r} ist gesetzt, aber {DIRECT_TIER!r} ist "
                "auf diesem Host nicht eingerichtet (ANTHROPIC_VISION_API_KEY fehlt) — "
                "Aufrufe im Bereich koennten nicht direkt laufen."
            )
    _start_stand = (raw, bereich)
    return bereich


def is_enabled() -> bool:
    return os.getenv(_FLAG_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def abo_ausgabegrenze() -> int:
    raw = os.getenv(_GRENZE_ENV, "").strip()
    if not raw:
        return DEFAULT_ABO_AUSGABEGRENZE
    try:
        wert = int(raw)
    except ValueError as e:
        raise RuntimeError(f"{_GRENZE_ENV}={raw!r} ist keine ganze Zahl") from e
    if wert <= 0:
        raise RuntimeError(f"{_GRENZE_ENV}={raw!r} muss positiv sein")
    return wert


def _reiner_textaufruf(request_body, *, has_images: bool) -> bool:
    """Kein Weg gewaehlt, Backend anthropic, keine Werkzeuge, kein Bild, keine Sitzung."""
    from src.models import BackendType

    if getattr(request_body, "provider_tier", None):
        return False
    if getattr(request_body, "backend", None) not in (None, BackendType.ANTHROPIC):
        return False
    if getattr(request_body, "enable_tools", None) is True:
        return False
    if has_images:
        return False
    if getattr(request_body, "session_id", None):
        return False
    return True


def direct_route_reason(request_body, *, has_images: bool) -> Optional[str]:
    """Warum dieser Aufruf auf den direkten Weg gehoert — oder None.

    Reine Funktion ueber die Anfrage; Schalter, Pin und Einrichtung des Wegs
    prueft ``apply_text_direct_routing``.
    """
    if not _reiner_textaufruf(request_body, has_images=has_images):
        return None

    if getattr(request_body, "output_config", None):
        return "output_config"
    max_tokens = getattr(request_body, "max_tokens", None)
    if "max_tokens" in getattr(request_body, "model_fields_set", set()) and isinstance(
        max_tokens, int
    ):
        try:
            grenze = abo_ausgabegrenze()
        except RuntimeError as e:
            # Startpruefung faengt das ab; zur Laufzeit trifft ein Tippfehler
            # hier nur die Budget-Bedingung des globalen Schalters, nie alle Aufrufe.
            logger.error("text_direct_routing: %s — Budget-Bedingung aus", e)
            return None
        if max_tokens > grenze:
            return f"max_tokens={max_tokens}>{grenze}"
    return None


def apply_text_direct_routing(
    request_body,
    *,
    has_images: bool,
    app_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> Optional[str]:
    """Setzt ``provider_tier`` auf den direkten Weg, wenn die Regel greift.

    Gibt den Grund zurueck (fuer Log und Tests) oder None, wenn der Aufruf
    bleibt, wo er ist. Aufruf nur fuer nicht gepinnte Nutzer (main.py).
    """
    reason: Optional[str] = None
    eintrag = im_geltungsbereich(app_id, agent_id)
    if eintrag and _reiner_textaufruf(request_body, has_images=has_images):
        reason = f"bereich {eintrag}"
    elif is_enabled():
        reason = direct_route_reason(request_body, has_images=has_images)
    if reason is None:
        return None

    from src.providers.registry import is_tier_usable

    if not is_tier_usable(DIRECT_TIER):
        if eintrag:
            # Im Bereich ist der direkte Weg zugesagt: kein stiller Rueckfall.
            raise TextDirectRoutingError(
                503,
                "text_direct_key_missing",
                f"Aufruf im Geltungsbereich {eintrag} muss ueber {DIRECT_TIER!r} laufen, "
                "der Weg ist auf diesem Host nicht eingerichtet (ANTHROPIC_VISION_API_KEY "
                "fehlt). Der SDK-Weg haelt max_tokens/output_config nicht ein.",
            )
        logger.warning(
            "text_direct_routing: Aufruf verlangt %s, aber %r ist auf diesem Host "
            "nicht eingerichtet — bleibt auf dem SDK-Weg, wo das NICHT gilt.",
            reason, DIRECT_TIER,
        )
        return None

    request_body.provider_tier = DIRECT_TIER
    return reason
