"""Cache-Grenze vom Aufrufer bis zur Claude-CLI (CACHE2, Fix 1).

WARUM
-----
Die Bridge flacht alle Turns zu EINEM Prompt-String ab (``Message.normalize_content``,
``MessageAdapter.messages_to_prompt``). Ein ``cache_control`` am Inhaltsblock ging dabei
verloren, und die CLI setzt ihre eigenen Cache-Marken nur ans Ende des System-Prompts
und ans Ende ihres Umgebungsblocks hinter der Nutzernachricht. Ein Treffer ist nur an
Blockgrenzen moeglich, also war die ganze Nutzernachricht ueber Aufrufe hinweg nie
lesbar und wurde bei jedem Aufruf neu geschrieben (CACHE1-ergebnis.md, Ursache 1).

WEG (a): Inhaltsbloecke ueber den Stream-JSON-Eingang
------------------------------------------------------
Gemessen an der Worker-CLI ``@anthropic-ai/claude-code@2.1.285`` gegen eine lokale
Attrappe der Messages-API (kein KI-Aufruf): eine Stream-JSON-Nutzernachricht aus
Bloecken mit ``cache_control`` kommt Block fuer Block mit der Marke in der API-Anfrage
an. Zwei Marken des Aufrufers ergeben dort FUENF Breakpoints (System 2, Aufrufer 2,
CLI-Umgebungsblock 1); die API erlaubt vier. Deshalb reicht die Bridge genau EINE
Grenze durch: die letzte markierte. Alles davor ist ein cachebarer Block, der Rest
ein zweiter Block ohne Marke.

WIE DIE GRENZE DEN WEG UEBERLEBT
--------------------------------
Zwischen Anfrage und CLI liegen Session-Zusammenfuehrung, Anonymisierung (schreibt
den Text um und baut die Message-Objekte neu) und ``filter_content`` (Regex, strip).
Ein Zeichen-Offset waere danach falsch. Die Grenze reist deshalb als Marke im Text
(drei Zeichen aus dem Unicode-Bereich fuer private Nutzung, keine Buchstaben, die eine
Erkennung als personenbezogen ausloesen koennten) und wird direkt vor der CLI wieder
herausgeschnitten. Geht sie unterwegs verloren oder verdoppelt sich, wird der Aufruf
ohne Grenze ausgefuehrt (Verhalten wie vor dem Fix) und das steht als ERROR im Log
und im Antwortkopf ``X-Cache-Boundary`` — nicht still.

Ohne mitgeschickte Grenze aendert sich nichts: kein Marker, ``cache_boundary=None``,
die CLI bekommt denselben String wie bisher.

EINE TTL FUER DIE GANZE ANFRAGE (CACHEFIX, 11.10.2026)
------------------------------------------------------
Die API verarbeitet tools, system, messages in dieser Reihenfolge und weist eine
Anfrage mit 400 ab, wenn ein Block mit ``ttl='1h'`` nach einem mit ``ttl='5m'``
kommt (ohne ttl = 5m). Die CLI 2.1.285 waehlt ihre ttl je Aufruf selbst
(``H0t``/``XLt`` im Binary): ``FORCE_PROMPT_CACHING_5M`` → 5m; sonst
``CLAUDE_CODE_PROMPT_CACHE_TTL``, Einstellung ``promptCacheTtl``,
``ENABLE_PROMPT_CACHING_1H``; sonst Abo-Konto innerhalb der Limits mit Quelle
``sdk`` → 1h, Overage oder API-Key → 5m. Auf den Workern (Abo) setzte sie also 1h
an System und Umgebungsblock, unser Block ohne ttl stand als 5m davor — DBD2,
"API Error: 400 ... ttl='1h' ... must not come after a ttl='5m'". Eine feste
1h an unserem Block waere bei Overage/API-Key (System 5m davor) genauso falsch;
die Bridge kennt den Overage-Zustand nicht.

Darum legt die Bridge die ttl der CLI fest, nur fuer Laeufe mit Grenze:
``FORCE_PROMPT_CACHING_5M=1`` im Prozess-Env dieses Laufs. Das ist die erste Regel
der CLI und schlaegt jede andere Quelle (gemessen gegen eine Attrappe der
Messages-API, auch gegen ``ENABLE_PROMPT_CACHING_1H=1`` und
``CLAUDE_CODE_PROMPT_CACHE_TTL=1h``): alle Marken 5m, wie unser Block ohne ttl.
Ohne Grenze bleibt das Env unberuehrt.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple, Union

from config.logging_config import get_logger

logger = get_logger(__name__)

# Drei Zeichen aus der Private Use Area. Selten, aber nicht unmoeglich (Symbolschriften
# in extrahiertem PDF-Text). Nur wenn eine Grenze angefordert ist, wird eine Anfrage mit
# diesen Zeichen abgewiesen statt still falsch geschnitten; ohne Grenze bleiben sie
# unangetastet wie vor CACHE2.
SENTINEL = ""
_SENTINEL_CHARS = set(SENTINEL)

# Status fuer Log, Antwortkopf X-Cache-Boundary und x_claude_metadata.cache_boundary
STATUS_APPLIED = "applied"
LOST_SENTINEL_MISSING = "lost:marker_removed_in_transit"
LOST_SENTINEL_DUPLICATED = "lost:marker_duplicated_in_transit"
LOST_EMPTY_PREFIX = "lost:empty_prefix"


def contains_sentinel_chars(text: str) -> bool:
    return any(ch in _SENTINEL_CHARS for ch in text)


def strip_sentinel_chars(text: str) -> str:
    if not text or not contains_sentinel_chars(text):
        return text
    return "".join(ch for ch in text if ch not in _SENTINEL_CHARS)


def boundary_requested(messages: Sequence[Any]) -> bool:
    """True, wenn eine Nutzernachricht eine Grenze mitbringt — dieselbe Regel wie
    in ``mark_messages``. Davon haengt ab, ob die Markenzeichen reserviert sind."""
    return any(
        getattr(m, "role", None) == "user"
        and isinstance(getattr(m, "content", None), str)
        and getattr(m, "cache_prefix_chars", None) is not None
        for m in messages
    )


def mark_messages(messages: Sequence[Any]) -> Tuple[List[Any], bool]:
    """Setzt die Marke an die LETZTE mitgeschickte Grenze einer Nutzernachricht.

    Liest ``Message.cache_prefix_chars`` (gesetzt in ``Message.normalize_content``).
    Nur Nutzernachrichten zaehlen: der System-Prompt wird von der CLI ohnehin am Ende
    markiert, und Assistenz-Turns sind kein vom Aufrufer gesteuerter Inhalt.
    Gibt (Nachrichten, angefordert) zurueck. Die Eingabeliste wird nicht veraendert;
    die markierte Nachricht ist eine Kopie, damit eine Session-Historie keine Marke
    speichert.

    Ohne Grenze kommt die Liste unveraendert zurueck, auch mit Markenzeichen im Text.

    Raises:
        ValueError: wenn eine Grenze angefordert ist und eine Nachricht die
            Markenzeichen schon selbst enthaelt — die Grenze waere dann nicht
            eindeutig. (Die Anfrage selbst weist der Request-Validator schon ab;
            das hier trifft nur eine Session-Historie mit solchen Zeichen.)
    """
    from src.models import Message  # lokaler Import: models importiert dieses Modul

    out = list(messages)
    last_idx: Optional[int] = None
    for i, m in enumerate(out):
        if getattr(m, "role", None) == "user" and isinstance(getattr(m, "content", None), str) \
                and getattr(m, "cache_prefix_chars", None) is not None:
            last_idx = i
    if last_idx is None:
        return out, False

    for m in out:
        content = getattr(m, "content", None)
        if isinstance(content, str) and contains_sentinel_chars(content):
            raise ValueError(
                "Nachrichtentext enthaelt reservierte Zeichen U+E000..U+E002 "
                "(Cache-Grenzmarke der Bridge) zusammen mit cache_control — bitte entfernen."
            )

    m = out[last_idx]
    pos = m.cache_prefix_chars
    if not (0 < pos <= len(m.content)):
        # Kann nur ein Programmierfehler in normalize_content sein — laut, aber
        # der Aufruf laeuft ohne Grenze weiter wie vor dem Fix.
        logger.error(
            "cache_boundary: cache_prefix_chars=%s ausserhalb 1..%s — Grenze verworfen",
            pos, len(m.content),
        )
        return out, True
    marked = Message(role=m.role, content=m.content[:pos] + SENTINEL + m.content[pos:], name=m.name)
    out[last_idx] = marked
    return out, True


def split_prompt(prompt: str, requested: bool) -> Tuple[str, Optional[int], Optional[str]]:
    """Schneidet die Marke heraus und liefert (prompt, grenze, status).

    ``grenze`` ist der Zeichenindex im bereinigten Prompt, bis zu dem die CLI einen
    cachebaren Block bekommt. ``status`` ist None, wenn keine Grenze angefordert war.
    Ohne Grenze bleibt der Prompt Zeichen fuer Zeichen unveraendert (wie vor CACHE2,
    auch mit U+E000..U+E002 aus dem Aufrufertext). Mit Grenze werden Reste der
    Markenzeichen immer entfernt — das Modell sieht sie nie.
    """
    if not requested:
        return prompt, None, None
    count = prompt.count(SENTINEL)
    if count != 1:
        status = LOST_SENTINEL_MISSING if count == 0 else LOST_SENTINEL_DUPLICATED
        logger.error(
            "cache_boundary: Grenze angefordert, aber %s (Marke %dx im Prompt) — "
            "Aufruf laeuft OHNE Cache-Grenze (Verhalten wie vor CACHE2)",
            status, count,
        )
        return strip_sentinel_chars(prompt), None, status
    pos = prompt.index(SENTINEL)
    clean = strip_sentinel_chars(prompt)
    if pos == 0:
        logger.error("cache_boundary: Grenze steht am Anfang des Prompts — nichts cachebar")
        return clean, None, LOST_EMPTY_PREFIX
    return clean, pos, STATUS_APPLIED


def cli_user_content(prompt: str, boundary: Optional[int]) -> Union[str, List[dict]]:
    """Inhalt der Stream-JSON-Nutzernachricht an die CLI.

    Ohne Grenze der unveraenderte String. Mit Grenze zwei Textbloecke: der stabile
    Praefix mit ``cache_control`` und der wechselnde Rest ohne Marke (entfaellt, wenn
    leer). Die Bloecke ergeben aneinandergehaengt exakt ``prompt``.
    """
    if boundary is None:
        return prompt
    if not (0 < boundary <= len(prompt)):
        raise ValueError(f"cache boundary {boundary} ausserhalb 1..{len(prompt)}")
    blocks: List[dict] = [
        {"type": "text", "text": prompt[:boundary], "cache_control": {"type": "ephemeral"}},
    ]
    rest = prompt[boundary:]
    if rest:
        blocks.append({"type": "text", "text": rest})
    return blocks


# Env der CLI fuer Laeufe mit Grenze (siehe oben "EINE TTL FUER DIE GANZE ANFRAGE").
CLI_TTL_PIN_ENV = {"FORCE_PROMPT_CACHING_5M": "1"}


def pin_cli_cache_ttl(options: Any) -> None:
    """Setzt die ttl aller CLI-Marken dieses Laufs auf 5m — dieselbe wie der Block
    aus ``cli_user_content``. ``options.env`` statt ``os.environ``: parallele
    Anfragen auf diesem Worker ohne Grenze bleiben unberuehrt."""
    options.env = {**(getattr(options, "env", None) or {}), **CLI_TTL_PIN_ENV}
    logger.info(
        "cache_boundary: CLI-Cache-ttl fuer diesen Lauf auf 5m festgelegt "
        "(FORCE_PROMPT_CACHING_5M)"
    )
