"""Gedeckelte echte KI (nur Haiku-Chat, Direktweg) fuer Testaufrufe von werking-works und werking-report.

Auftrag KD 91624155, 27.10.2026 (Rafael 16:36Z: Tester pruefen mit echter KI,
Haiku, gedeckelt). R4 nach Codex (PRUEFUNG-ENTWURF-R4.md) mit der Umfangs-
entscheidung des Vize (a): Test-KI heisst NUR Haiku-Chat.

Vertrauensrichtung: Angaben koennen nur EINSCHRAENKEN, nie befreien. Ein Aufruf
ist ein TESTAUFRUF, wenn mindestens eins gilt (`testgrund`):
  (a) der vom Token aufgeloeste Prinzipal steht in BRIDGE_TEST_KI_PRINZIPALE;
  (b) Job-Selbstaufruf (Worker-/Legacy-Schluessel) eines Jobs, den ein solcher
      Prinzipal angelegt hat (gelesen aus dem Job, nie aus einem Kopf);
  (c) der Aufruf traegt X-Test-Lauf — egal welcher Prinzipal, welcher Mandant.
Alles andere bleibt unberuehrt (kein account_type, keine Ausnahmeliste).

Ein Testaufruf darf an dieser Bridge genau eins: Chat ueber das Tor. Drei Stellen,
jede fuer ALLE Wege ihrer Art:
  1. `TestKiWegeMiddleware` (ASGI, vor jedem Worker-Endpunkt; in platform-api
     nur fuer (c), dort gibt es keine App-Prinzipale): ein vorhandener,
     aber leerer/ungueltiger X-Test-Lauf -> 403 lauf_ungueltig, ueberall.
     Testaufruf + Schalter aus -> 403 KI_TEST_DECKEL_INAKTIV, ueberall.
     Testaufruf auf jeder Route ausser dem Chat-Tor, dem Job-Anlegen und den
     kostenfreien Lese-/Verwaltungsrouten in `DURCHLASS` -> 403 KI_TEST_NUR_CHAT.
     Die Liste ist eine Erlaubnisliste: eine neue Route ist fuer Testaufrufe
     zu, bis sie hier eingetragen wird.
  2. `job_anlegen_pruefen` (POST /v1/jobs): Testaufruf nur mit Art chat/ping,
     chat nur mit gueltigem X-Test-Lauf; der Marker bleibt am Job.
     `job_lauf_pruefen` (Job-Runner, vor JEDEM Executor): dieselbe Regel fuer
     gespeicherte Jobs — auch alte, auch Erkunder (eigener Transport).
  3. `tor` (Chat): Haiku, keine Werkzeuge, Direktweg erzwungen, X-Test-Lauf
     Pflicht, Reservierung gegen Lauf 0,50 / App-Tag 10,00 / Gesamt-Tag 20,00 EUR,
     idempotent (test_ki_buchung), Abrechnung nur aus `UsageMessung`.

Fehlercodes:
  403 KI_IM_TEST_GESPERRT (grund modell_nicht_haiku | werkzeuge | weg | ohne_lauf
      | lauf_ungueltig | app_unbekannt | ohne_nutzer | nutzer_unbekannt)
  403 KI_TEST_NUR_CHAT        (Nicht-Chat-Weg fuer einen Testaufruf)
  403 KI_TEST_DECKEL_INAKTIV  (Testaufruf auf einer Bridge mit Schalter aus)
  402 KI_TEST_DECKEL_ERREICHT (ebene lauf | tag | gesamt)
  503 KI_TEST_DECKEL_NICHT_LESBAR (platform-api / Job-Store / Prinzipal nicht
      lesbar; mit X-Bridge-Dependency-Unavailable, ein Job parkt)
"""
from __future__ import annotations

import contextvars
import hmac
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)

SCHALTER = "BRIDGE_TEST_KI_DECKEL"
TEST_PRINZIPALE = "BRIDGE_TEST_KI_PRINZIPALE"
MODELL = "claude-haiku-5-5"
DIREKT_TIER = "claude-direct-notools"
DECKEL_LAUF_EUR = 0.50
DECKEL_TAG_EUR = 10.00
DECKEL_GESAMT_EUR = 20.00
LAUF_KOPF = "X-Test-Lauf"
_LAUF_MUSTER = re.compile(r"^[0-9a-f]{64}$")

# Bridge-App-ID -> Deckel-App. Works meldet sich als 'engelmann'.
DECKEL_APP = {
    "werking-report": "werking-report",
    "werking-works": "werking-works",
    "engelmann": "werking-works",
}

# Obergrenze der Eingabe-Tokens: ein Token deckt mindestens ein UTF-8-Byte;
# dazu Rahmen je Nachricht und fest, je Bild ein Pauschalwert ueber dem
# Hoechstwert der API (Bilder werden vor der Tokenisierung verkleinert).
TOKENS_JE_NACHRICHT = 64
TOKENS_FEST = 2_000
TOKENS_JE_BILD = 6_000
# Pessimistische Preisfaktoren: Cache-Anlage zum 1h-Satz (2x), Grundzeile
# (hoechste Stufe) fuer jede Prompt-Groesse.
CACHE_ANLAGE_MULT = 2.0

CODE_GESPERRT = "KI_IM_TEST_GESPERRT"
CODE_NUR_CHAT = "KI_TEST_NUR_CHAT"
CODE_INAKTIV = "KI_TEST_DECKEL_INAKTIV"
CODE_DECKEL = "KI_TEST_DECKEL_ERREICHT"
CODE_NICHT_LESBAR = "KI_TEST_DECKEL_NICHT_LESBAR"

# Wert von X-Bridge-Dependency-Unavailable, wenn Job-Store/Prinzipal/platform-api
# nicht antworten: der Job-Executor parkt den Job (registry.DEPENDENCY_PATIENCE).
ABHAENGIGKEIT = "test-ki-deckel"

# Schluessel in der Job-Attribution: Prinzipal des ANLEGENDEN Aufrufs,
# serverseitig gesetzt (src/jobs/routes.py), nie aus dem Rumpf uebernommen.
JOB_PRINZIPAL_KEY = "test_ki_prinzipal"
JOB_LAUF_KEY = "test_lauf"

# Job-Arten, die ein Testaufruf anlegen/ausfuehren darf: chat (Selbstaufruf
# durch das Tor) und ping (ohne Modell, ohne Selbstaufruf).
JOB_ARTEN_TEST = frozenset({"chat", "ping"})

CHAT_PFAD = "/v1/chat/completions"
JOBS_PFAD = "/v1/jobs"

# Erlaubnisliste fuer Testaufrufe an einem Worker (Methode, Pfadmuster). Alles
# andere ist fuer Testaufrufe zu. Belegt 27.10.2026 an cc132ce: keine dieser
# Routen loest einen Modell-, Render- oder Rechenaufruf aus.
DURCHLASS = (
    ("POST", re.compile(r"^/v1/chat/completions$")),    # tor()
    ("POST", re.compile(r"^/v1/jobs$")),                # job_anlegen_pruefen()
    ("POST", re.compile(r"^/v1/jobs/lookup$")),         # Store-Lesen (Idempotenz)
    ("GET", re.compile(r"^/v1/jobs$")),                 # Store-Lesen
    ("GET", re.compile(r"^/v1/jobs/[^/]+$")),           # Store-Lesen
    ("DELETE", re.compile(r"^/v1/jobs/[^/]+$")),        # Abbruch eines Jobs
    ("GET", re.compile(r"^/v1/models$")),
    ("GET", re.compile(r"^/v1/version$")),
    ("GET", re.compile(r"^/health$")),
    ("GET", re.compile(r"^/ready$")),
    ("OPTIONS", re.compile(r"^/.*$")),                  # CORS-Vorabfrage
)
# Von diesen Durchlass-Routen nimmt das Tor bzw. die Job-Pruefung die Entscheidung
# selbst vor; die uebrigen sind kostenfrei und brauchen keine.
_TORWEGE = (("POST", CHAT_PFAD), ("POST", JOBS_PFAD))


def schalter_an() -> bool:
    return os.getenv(SCHALTER, "").strip() == "1"


def test_prinzipale() -> frozenset[str]:
    roh = os.getenv(TEST_PRINZIPALE, "")
    return frozenset(n.strip() for n in roh.split(",") if n.strip())


@dataclass
class Reservierung:
    reservierung_id: str
    app: str
    lauf: str
    tag: str
    betrag_eur: float
    erledigt: bool = field(default=False)


_aktuell: contextvars.ContextVar[Optional[Reservierung]] = contextvars.ContextVar(
    "test_ki_deckel_reservierung", default=None
)


def ist_gedeckelt() -> bool:
    """Laeuft der aktuelle Aufruf unter einer Test-Reservierung? (Fallback-Sperre)"""
    return _aktuell.get() is not None


def _fehler(status: int, code: str, message: str, headers: Optional[Dict[str, str]] = None,
            **extra: Any) -> HTTPException:
    """Flache Huelle MIT `source`: der HTTPException-Handler der App reicht sie
    dann unveraendert als {"error": {...}} durch (main.http_exception_handler,
    erster Zweig). Ohne `source` machte er bis R2 aus `code` den HTTP-Status und
    schrieb diese Felder als Text in `message` — der Code kam nie an."""
    detail = {
        "message": message, "type": "test_ki_deckel", "code": code,
        "source": "bridge_internal", "retryable": status == 503, **extra,
    }
    return HTTPException(status_code=status, detail=detail, headers=headers)


def fehler_code(exc: HTTPException) -> Optional[str]:
    return exc.detail.get("code") if isinstance(exc.detail, dict) else None


def _nicht_lesbar(text: str) -> HTTPException:
    from src.platform_client import DEPENDENCY_UNAVAILABLE_HEADER

    return _fehler(503, CODE_NICHT_LESBAR, text, headers={DEPENDENCY_UNAVAILABLE_HEADER: ABHAENGIGKEIT})


def _gesperrt(grund: str, text: str) -> HTTPException:
    logger.warning("test-ki gesperrt grund=%s: %s", grund, text)
    return _fehler(403, CODE_GESPERRT, text, grund=grund)


def _inaktiv(testgrund_: str) -> HTTPException:
    logger.warning("test-ki inaktiv: Testaufruf (%s) auf einer Bridge ohne %s=1", testgrund_, SCHALTER)
    return _fehler(
        403, CODE_INAKTIV,
        f"Test-KI ist auf dieser Bridge aus ({SCHALTER} != 1); Testaufrufe sind gesperrt.",
        testgrund=testgrund_,
    )


def _nur_chat(testgrund_: str, weg: str) -> HTTPException:
    logger.warning("test-ki nur chat: Testaufruf (%s) auf %s abgewiesen", testgrund_, weg)
    return _fehler(
        403, CODE_NUR_CHAT,
        f"Testaufrufe duerfen nur Haiku-Chat ({CHAT_PFAD}) nutzen, nicht {weg}.",
        testgrund=testgrund_, weg=weg,
    )


def _tag_heute() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Testaufruf erkennen (gemeinsam fuer Wege-Schicht, Job-Anlegen und Tor) ────

def lauf_aus_koepfen(werte: list[str]) -> Optional[str]:
    """X-Test-Lauf aus ALLEN Vorkommen des Kopfs. None = kein Kopf. Ein
    vorhandener Kopf, der leer, ungueltig oder mehrfach ist, wird abgewiesen —
    nie zu "kein Test" normalisiert (Codex R4 F2)."""
    if not werte:
        return None
    if len(werte) != 1:
        raise _gesperrt("lauf_ungueltig", f"{LAUF_KOPF} mehrfach gesetzt.")
    wert = werte[0].strip().lower()
    if not _LAUF_MUSTER.match(wert):
        raise _gesperrt("lauf_ungueltig", f"{LAUF_KOPF} ist gesetzt, aber kein Lauf (64 Hex).")
    return wert


def _alle_werte(headers: Any, name: str) -> list[str]:
    """Alle Vorkommen eines Kopfs. Starlette-Headers liefern Mehrfachwerte;
    ein einfaches Mapping (Aufrufer ausserhalb von HTTP) hoechstens einen."""
    if hasattr(headers, "getlist"):
        return list(headers.getlist(name))
    wert = headers.get(name)
    if wert is None:
        wert = next((v for k, v in headers.items() if k.lower() == name.lower()), None)
    return [] if wert is None else [wert]


def lauf_aus_request(request: Any) -> Optional[str]:
    return lauf_aus_koepfen(_alle_werte(request.headers, LAUF_KOPF))


def lauf_aus_attribution(attribution: Optional[Dict[str, Any]]) -> Optional[str]:
    """Gespeicherter Marker eines Jobs; derselbe Massstab wie fuer den Kopf."""
    if not attribution or JOB_LAUF_KEY not in attribution:
        return None
    wert = attribution.get(JOB_LAUF_KEY)
    return lauf_aus_koepfen([wert if isinstance(wert, str) else ""])


def _ist_legacy_oder_keiner(principal: Any) -> bool:
    return principal is None or bool(getattr(principal, "is_legacy", False))


async def _job_ist_test(job_id: str, liste: frozenset[str]) -> bool:
    """(b): der referenzierte Job wurde von einem Test-Prinzipal angelegt oder
    traegt einen Marker. Ein unbekannter Job ist keiner (es gibt nichts, das
    einzuschraenken waere); ein nicht lesbarer Store ist KEIN "kein Test"."""
    from src.jobs import store_client

    try:
        job = await store_client.get_job(job_id)
    except Exception as e:
        logger.error("test-ki-deckel: Job-Store nicht lesbar (%s) — kein Durchlass", e)
        raise _nicht_lesbar("Job-Herkunft nicht lesbar (fail-closed).")
    if not job:
        return False
    attr = (job.get("attribution") if isinstance(job, dict) else getattr(job, "attribution", None)) or {}
    return attr.get(JOB_PRINZIPAL_KEY) in liste or JOB_LAUF_KEY in attr


async def testgrund(*, lauf: Optional[str], principal: Any, job_id: Optional[str]) -> Optional[str]:
    """'c' | 'a' | 'b' | None. `lauf` ist bereits mit lauf_aus_* geprueft."""
    if lauf is not None:
        return "c"
    liste = test_prinzipale()
    if not liste:
        return None
    if not _ist_legacy_oder_keiner(principal) and getattr(principal, "name", None) in liste:
        return "a"
    if job_id and _ist_legacy_oder_keiner(principal) and await _job_ist_test(job_id, liste):
        return "b"
    return None


def _job_id_aus(headers: Any) -> Optional[str]:
    from src.jobs.selbstaufruf import JOB_LAUF_HEADER

    return (headers.get(JOB_LAUF_HEADER) or "").strip() or None


async def testgrund_fuer_request(request: Any) -> tuple[Optional[str], Optional[str]]:
    """(testgrund, lauf) eines authentisierten Requests (request.state.principal)."""
    lauf = lauf_aus_request(request)
    principal = getattr(getattr(request, "state", None), "principal", None)
    return await testgrund(lauf=lauf, principal=principal, job_id=_job_id_aus(request.headers)), lauf


# ── 1. Wege-Schicht: jede Worker-Route ausser dem Chat-Tor ───────────────────

def _im_durchlass(methode: str, pfad: str) -> bool:
    return any(m == methode and r.match(pfad) for m, r in DURCHLASS)


async def _prinzipal_aus_koepfen(headers: Any) -> Any:
    """Derselbe Prinzipal, den verify_api_key aufloesen wird (gleicher Cache),
    ohne dessen Abweisungen — die bleiben beim Endpunkt. None = keiner."""
    from src.auth import _principals_enabled, auth_manager

    if not _principals_enabled():
        return None
    roh = headers.get("authorization") or ""
    if not roh.lower().startswith("bearer "):
        return None
    token = roh[7:].strip()
    if not token:
        return None
    from src.principals import LEGACY_PRINCIPAL, resolve_principal_by_token

    schluessel = [k for k in (auth_manager.get_api_key(), auth_manager.env_api_key_previous) if k]
    if any(hmac.compare_digest(token, k) for k in schluessel):
        return LEGACY_PRINCIPAL
    return await resolve_principal_by_token(token)


async def wege_pruefen(methode: str, pfad: str, headers: Any, *, nur_marker: bool = False) -> None:
    """Wirft 403/503 fuer einen Testaufruf, der hier nicht hin darf; sonst None.
    `nur_marker`: platform-api — dort gibt es keine App-Prinzipale (Service-Token
    bzw. Nutzer-JWT), also nur (c); kein Tor, keine Job-Anlage."""
    lauf = lauf_aus_koepfen(_alle_werte(headers, LAUF_KOPF))
    if _im_durchlass(methode, pfad) and (methode, pfad) not in _TORWEGE:
        return  # kostenfrei
    an = schalter_an()
    if an and not nur_marker and (methode, pfad) in _TORWEGE:
        return  # tor() bzw. job_anlegen_pruefen() entscheiden selbst
    if lauf is not None:
        grund = "c"
    elif nur_marker:
        return
    else:
        liste = test_prinzipale()
        if not liste:
            return
        try:
            principal = await _prinzipal_aus_koepfen(headers)
        except Exception as e:
            logger.error("test-ki-deckel: Prinzipal nicht lesbar (%s) — kein Durchlass", e)
            raise _nicht_lesbar("Prinzipal nicht lesbar (fail-closed).")
        grund = await testgrund(lauf=None, principal=principal, job_id=_job_id_aus(headers))
    if grund is None:
        return
    if not an:
        raise _inaktiv(grund)
    raise _nur_chat(grund, f"{methode} {pfad}")


class TestKiWegeMiddleware:
    """Reines ASGI. `antwort(request, exc)` formt die Abweisung wie der
    HTTPException-Handler der App (gleiche Huelle wie alle Bridge-Fehler)."""

    def __init__(self, app, antwort, nur_marker: bool = False):
        self.app = app
        self.antwort = antwort
        self.nur_marker = nur_marker

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        from starlette.datastructures import Headers
        from starlette.requests import Request

        try:
            await wege_pruefen(
                scope.get("method", ""), scope.get("path", ""), Headers(scope=scope),
                nur_marker=self.nur_marker,
            )
        except HTTPException as exc:
            response = await self.antwort(Request(scope, receive), exc)
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)


# ── 2. Jobs: Anlegen und Ausfuehren ──────────────────────────────────────────

async def job_anlegen_pruefen(request: Any, art: str) -> Optional[str]:
    """POST /v1/jobs, nach verify_api_key. Rueckgabe: der gepruefte Marker
    (wird mit dem Job gespeichert) oder None. Wirft fuer unzulaessige Testjobs."""
    grund, lauf = await testgrund_fuer_request(request)
    if grund is None:
        return None
    if not schalter_an():
        raise _inaktiv(grund)
    if art not in JOB_ARTEN_TEST:
        raise _nur_chat(grund, f"Job-Art {art!r}")
    if art == "chat" and lauf is None:
        raise _gesperrt("ohne_lauf", f"Testaufruf ({grund}): Chat-Job ohne gueltigen {LAUF_KOPF}.")
    return lauf


def job_lauf_pruefen(art: str, attribution: Optional[Dict[str, Any]]) -> Optional[HTTPException]:
    """Job-Runner, vor JEDEM Executor (auch Erkunder mit eigenem Transport und
    Jobs von vor dem Schalter). Liest nur den gespeicherten Job. Rueckgabe: die
    Abweisung oder None."""
    try:
        lauf = lauf_aus_attribution(attribution)
    except HTTPException as e:
        return e
    liste = test_prinzipale()
    if lauf is not None:
        grund = "c"
    elif liste and (attribution or {}).get(JOB_PRINZIPAL_KEY) in liste:
        grund = "b"
    else:
        return None
    if not schalter_an():
        return _inaktiv(grund)
    if art not in JOB_ARTEN_TEST:
        return _nur_chat(grund, f"Job-Art {art!r}")
    if art == "chat" and lauf is None:
        return _gesperrt("ohne_lauf", f"Testjob ({grund}) ohne gueltigen {LAUF_KOPF}.")
    return None


# ── Usage-Messung (R2-1) ─────────────────────────────────────────────────────

class MessungZiel:
    """Je Aufruf FRISCH im Torzweig angelegt und nur dort an
    call_anthropic_direct uebergeben; nimmt hoechstens eine Messung auf."""

    __slots__ = ("_messung", "_gesetzt")

    def __init__(self) -> None:
        self._messung = None
        self._gesetzt = False

    def setzen(self, messung: Any) -> None:
        if self._gesetzt:
            raise RuntimeError("MessungZiel wiederverwendet — eine Messung je Aufruf")
        self._gesetzt = True
        self._messung = messung

    @property
    def messung(self) -> Any:
        return self._messung


# ── Preis-Obergrenze ─────────────────────────────────────────────────────────

def _saetze_usd_je_mtok() -> tuple[float, float]:
    from src.pricing import MODEL_PRICING

    p = MODEL_PRICING[MODELL]
    unten = p.get("bis_prompt") or {}
    return max(p["in"], unten.get("in", 0.0)), max(p["out"], unten.get("out", 0.0))


def kosten_obergrenze_eur(
    *, eingabe: int, ausgabe: int, cache_lesen: int = 0, cache_anlage: int = 0,
) -> float:
    """Kosten zu den hoechsten Saetzen des Modells — fuer Reservierung UND
    Abrechnung (gemessene Tokens), damit beide ueber dem echten Preis liegen."""
    from src.pricing import CACHE_READ_MULT, usd_to_eur_rate

    ein, aus = _saetze_usd_je_mtok()
    usd = (
        max(eingabe, 0) * ein
        + max(cache_lesen, 0) * ein * CACHE_READ_MULT
        + max(cache_anlage, 0) * ein * CACHE_ANLAGE_MULT
        + max(ausgabe, 0) * aus
    ) / 1_000_000.0
    return round(usd * usd_to_eur_rate(), 6)


def eingabe_obergrenze_tokens(request_body: Any) -> int:
    """Obergrenze der Eingabe-Tokens: UTF-8-Bytes aller Texte + Rahmen + Bilder."""
    tokens = TOKENS_FEST
    for m in request_body.messages:
        tokens += TOKENS_JE_NACHRICHT
        inhalt = m.content
        if isinstance(inhalt, str):
            tokens += len(inhalt.encode("utf-8"))
            continue
        for teil in inhalt or []:
            typ = getattr(teil, "type", None) or (teil.get("type") if isinstance(teil, dict) else None)
            if typ in ("image_url", "image"):
                tokens += TOKENS_JE_BILD
                continue
            text = getattr(teil, "text", None)
            if text is None and isinstance(teil, dict):
                text = teil.get("text")
            if text is None:
                # Unbekannter Teil: ganz serialisiert zaehlen (nie zu wenig).
                roh = teil.model_dump() if hasattr(teil, "model_dump") else teil
                text = json.dumps(roh, ensure_ascii=False, default=str)
            tokens += len(str(text).encode("utf-8"))
    for extra in (request_body.stop, request_body.thinking, request_body.output_config):
        if extra:
            tokens += len(json.dumps(extra, ensure_ascii=False, default=str).encode("utf-8"))
    return tokens


# ── Datenbank (laeuft NUR in platform-api) ───────────────────────────────────

def _summen_schluessel(app: str, tag: str, lauf: str) -> list[tuple[str, str, str]]:
    # Feste Sperrreihenfolge: ebene alphabetisch (gesamt < lauf < tag).
    return [("gesamt", "*", tag), ("lauf", app, lauf), ("tag", app, tag)]


async def _summen_sperren(conn, app: str, tag: str, lauf: str) -> Dict[str, Dict[str, Any]]:
    schl = _summen_schluessel(app, tag, lauf)
    for ebene, a, s in schl:
        await conn.execute(
            "INSERT INTO test_ki_summe (ebene, app_id, schluessel) VALUES ($1, $2, $3) "
            "ON CONFLICT DO NOTHING", ebene, a, s,
        )
    zeilen = await conn.fetch(
        "SELECT ebene, summe_eur, geschlossen FROM test_ki_summe WHERE "
        "(ebene = 'gesamt' AND app_id = '*' AND schluessel = $2) OR "
        "(ebene = 'lauf' AND app_id = $1 AND schluessel = $3) OR "
        "(ebene = 'tag' AND app_id = $1 AND schluessel = $2) "
        "ORDER BY ebene FOR UPDATE",
        app, tag, lauf,
    )
    summen = {z["ebene"]: {"summe": float(z["summe_eur"]), "zu": bool(z["geschlossen"])} for z in zeilen}
    if set(summen) != {"gesamt", "lauf", "tag"}:
        raise RuntimeError(f"test_ki_summe: Zeilen fehlen nach INSERT ({sorted(summen)})")
    return summen


async def _summen_buchen(conn, app: str, tag: str, lauf: str, delta: float, *, aufruf: int = 0,
                         schliessen: bool = False) -> None:
    await conn.execute(
        "UPDATE test_ki_summe SET summe_eur = GREATEST(summe_eur + $4, 0), aufrufe = aufrufe + $5, "
        "geschlossen = geschlossen OR ($6 AND ebene IN ('lauf', 'tag')), aktualisiert = now() WHERE "
        "(ebene = 'gesamt' AND app_id = '*' AND schluessel = $2) OR "
        "(ebene = 'lauf' AND app_id = $1 AND schluessel = $3) OR "
        "(ebene = 'tag' AND app_id = $1 AND schluessel = $2)",
        app, tag, lauf, delta, aufruf, schliessen,
    )


def _antwort(summen: Dict[str, Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    return {
        "lauf_summe": summen["lauf"]["summe"], "tag_summe": summen["tag"]["summe"],
        "gesamt_summe": summen["gesamt"]["summe"], **extra,
    }


async def db_reservieren(
    *, reservierung_id: uuid.UUID, app: str, tag: str, lauf: str, betrag_eur: float,
) -> Dict[str, Any]:
    """Atomar und idempotent: dieselbe reservierung_id bucht hoechstens einmal.

    Antwort {ok: bool, duplikat?: bool, ebene?: 'lauf'|'tag'|'gesamt', grund?,
    lauf_summe, tag_summe, gesamt_summe}.
    """
    from src.db.client import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            summen = await _summen_sperren(conn, app, tag, lauf)
            # Unter der Sperre der Summenzeilen: gibt es die ID schon? (Wiederholung
            # nach unklarem Ausgang) — dann das Urteil zurueckgeben, nichts buchen.
            vorhanden = await conn.fetchrow(
                "SELECT app_id, lauf FROM test_ki_buchung WHERE reservierung_id = $1", reservierung_id,
            )
            if vorhanden is not None:
                if vorhanden["app_id"] != app or vorhanden["lauf"] != lauf:
                    raise ValueError("reservierung_id gehoert zu einer anderen Buchung")
                return _antwort(summen, ok=True, duplikat=True)
            for ebene, deckel in (("gesamt", DECKEL_GESAMT_EUR), ("lauf", DECKEL_LAUF_EUR), ("tag", DECKEL_TAG_EUR)):
                if summen[ebene]["zu"]:
                    return _antwort(summen, ok=False, ebene=ebene, grund="geschlossen")
                if summen[ebene]["summe"] + betrag_eur > deckel:
                    return _antwort(summen, ok=False, ebene=ebene, grund="ueberschritten")
            await conn.execute(
                "INSERT INTO test_ki_buchung (reservierung_id, app_id, tag, lauf, reserviert_eur) "
                "VALUES ($1, $2, $3, $4, $5)",
                reservierung_id, app, tag, lauf, betrag_eur,
            )
            await _summen_buchen(conn, app, tag, lauf, betrag_eur, aufruf=1)
            for e in summen.values():
                e["summe"] += betrag_eur
    return _antwort(summen, ok=True, duplikat=False)


async def db_abrechnen(*, reservierung_id: uuid.UUID, ist_eur: Optional[float]) -> Dict[str, Any]:
    """Genau einmal je reservierung_id (status offen -> abgerechnet).

    ist_eur None = nicht gemessen: die Reservierung bleibt voll stehen. Ist der
    gemessene Wert ueber der Reservierung, wird er gebucht und Lauf + App-Tag
    werden geschlossen. Antwort {abgerechnet: bool, delta, geschlossen, Summen?}.
    """
    from src.db.client import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            b = await conn.fetchrow(
                "SELECT app_id, tag, lauf, reserviert_eur, status FROM test_ki_buchung "
                "WHERE reservierung_id = $1", reservierung_id,
            )
            if b is None:
                raise LookupError("reservierung_id unbekannt")
            summen = await _summen_sperren(conn, b["app_id"], b["tag"], b["lauf"])
            zeile = await conn.fetchrow(
                "UPDATE test_ki_buchung SET status = 'abgerechnet', ist_eur = $2, abgerechnet = now() "
                "WHERE reservierung_id = $1 AND status = 'offen' RETURNING reserviert_eur",
                reservierung_id, ist_eur,
            )
            if zeile is None:
                return _antwort(summen, abgerechnet=False, delta=0.0, geschlossen=False)
            reserviert = float(zeile["reserviert_eur"])
            delta = 0.0 if ist_eur is None else round(float(ist_eur) - reserviert, 6)
            schliessen = ist_eur is not None and float(ist_eur) > reserviert
            await _summen_buchen(conn, b["app_id"], b["tag"], b["lauf"], delta, schliessen=schliessen)
            for e in summen.values():
                e["summe"] = max(e["summe"] + delta, 0.0)
    return _antwort(summen, abgerechnet=True, delta=delta, geschlossen=schliessen)


# ── Worker-Seite: nur platform-api, sonst fail-closed ────────────────────────

async def _plattform(pfad: str, koerper: Dict[str, Any]) -> Dict[str, Any]:
    """Ein Aufruf mit einer Wiederholung (Aufrufe sind idempotent). Jede
    andere Antwort als 200 mit JSON wirft — der Aufrufer entscheidet fail-closed."""
    from src.platform_client import call_platform

    resp = await call_platform("POST", pfad, json=koerper, timeout_s=3.0, retries=1)
    if resp.status_code != 200 or not isinstance(resp.json, dict):
        raise RuntimeError(f"platform-api {pfad} antwortete {resp.status_code}")
    return resp.json


# ── 3. Das Chat-Tor ──────────────────────────────────────────────────────────

async def tor(
    *,
    request: Any,
    request_body: Any,
    attribution: Dict[str, Any],
    resolved_model: Optional[str],
    erzwinge_direktweg,
):
    """Rueckgabe None = kein Testaufruf; sonst die erzwungene BackendConfig des
    Direktwegs. Wirft 403/402/503 (siehe Modulkopf). Ab einem positiven
    Testgrund endet jeder Zweig in einer Reservierung oder einer Abweisung —
    nie in None. `erzwinge_direktweg()` liefert die BackendConfig fuer DIREKT_TIER."""
    _aktuell.set(None)
    grund, test_lauf = await testgrund_fuer_request(request)
    if grund is None:
        return None
    if not schalter_an():
        raise _inaktiv(grund)  # die Wege-Schicht hat das schon; hier zur Sicherheit

    principal = getattr(getattr(request, "state", None), "principal", None)
    p_name = getattr(principal, "name", None) or "-"
    roh_app = (attribution.get("app_id") or "").strip()
    app = DECKEL_APP.get(roh_app)
    if app is None:
        raise _gesperrt("app_unbekannt", f"Testaufruf ({grund}, {p_name}): App {roh_app or '-'!r} hat keinen Test-Deckel.")
    roh_nutzer = attribution.get("user_id")
    if not roh_nutzer:
        raise _gesperrt("ohne_nutzer", f"Testaufruf ({grund}, {p_name}): ohne X-User-ID kein Lauf.")

    from src.identity.user_resolver import (
        MalformedUserIdentity, UnknownUserIdentity, resolve_user_id,
    )

    try:
        uid = await resolve_user_id(roh_nutzer)
    except (MalformedUserIdentity, UnknownUserIdentity):
        raise _gesperrt("nutzer_unbekannt", f"Testaufruf ({grund}, {p_name}): Nutzer nicht aufloesbar.")
    except Exception as e:
        logger.error("test-ki-deckel: Nutzer nicht aufloesbar (%s) — gesperrt", e)
        raise _nicht_lesbar("Nutzer-Aufloesung nicht moeglich (fail-closed).")

    def _sperre(g: str, text: str) -> HTTPException:
        return _gesperrt(g, f"Testaufruf ({grund}, {p_name}, {app}): {text}")

    if resolved_model != MODELL:
        raise _sperre("modell_nicht_haiku", f"nur {MODELL} ist freigegeben (angefragt: {resolved_model}).")
    if request_body.enable_tools:
        raise _sperre("werkzeuge", "Aufrufe mit Werkzeugen sind gesperrt.")

    try:
        cfg = erzwinge_direktweg()
    except Exception as e:  # z. B. Direktweg auf dieser Bridge nicht konfiguriert
        logger.error("test-ki-deckel: Direktweg nicht herstellbar (%s)", e)
        cfg = None
    backend = getattr(cfg.backend, "value", cfg.backend) if cfg else None
    if not cfg or backend != "anthropic_direct" or cfg.provider_model != MODELL:
        raise _sperre("weg", f"Direktweg mit {MODELL} nicht herstellbar ({backend}, {getattr(cfg, 'provider_model', None)}).")

    if test_lauf is None:
        raise _sperre("ohne_lauf", f"ohne gueltigen {LAUF_KOPF} (64 Hex, gesetzt von der App-Sperre) kein Lauf.")

    eingabe = eingabe_obergrenze_tokens(request_body)
    ausgabe = int(request_body.max_tokens) if request_body.max_tokens else 4096
    request_body.max_tokens = ausgabe  # was reserviert ist, ist auch die Grenze des Aufrufs
    betrag = kosten_obergrenze_eur(eingabe=0, ausgabe=ausgabe, cache_anlage=eingabe)
    tag = _tag_heute()
    lauf = f"{tag}|{uid}|{test_lauf}"
    rid = str(uuid.uuid4())
    try:
        erg = await _plattform("/v1/internal/test-ki-deckel/reservieren", {
            "reservierung_id": rid, "app": app, "tag": tag, "lauf": lauf, "betrag_eur": betrag,
        })
    except Exception as e:
        logger.error("test-ki-deckel: Reservierung ohne klares Ergebnis (%s) — gesperrt", e)
        raise _nicht_lesbar("Test-KI-Deckel nicht lesbar/schreibbar (fail-closed).")

    if not erg.get("ok"):
        ebene = erg.get("ebene")
        deckel = {"lauf": DECKEL_LAUF_EUR, "tag": DECKEL_TAG_EUR, "gesamt": DECKEL_GESAMT_EUR}.get(ebene)
        summe = erg.get(f"{ebene}_summe")
        logger.warning(
            "test-ki deckel erreicht testgrund=%s prinzipal=%s app=%s ebene=%s grund=%s summe=%s deckel=%s betrag=%.6f lauf=%s",
            grund, p_name, app, ebene, erg.get("grund"), summe, deckel, betrag, lauf,
        )
        raise _fehler(
            402, CODE_DECKEL,
            f"Test-KI-Deckel je {ebene} erreicht ({erg.get('grund')}; {summe} von {deckel} EUR, Aufruf braeuchte bis {betrag:.4f}).",
            ebene=ebene, grund=erg.get("grund"), summe_eur=summe, deckel_eur=deckel, reservierung_eur=betrag,
        )

    _aktuell.set(Reservierung(reservierung_id=rid, app=app, lauf=lauf, tag=tag, betrag_eur=betrag))
    logger.info(
        "test-ki echt, gedeckelt testgrund=%s prinzipal=%s app=%s lauf=%s reserviert=%.6f (ein<=%d aus<=%d) "
        "lauf_summe=%.4f/%.2f tag_summe=%.4f/%.2f gesamt=%.4f/%.2f EUR",
        grund, p_name, app, lauf, betrag, eingabe, ausgabe, float(erg.get("lauf_summe") or 0), DECKEL_LAUF_EUR,
        float(erg.get("tag_summe") or 0), DECKEL_TAG_EUR, float(erg.get("gesamt_summe") or 0), DECKEL_GESAMT_EUR,
    )
    return cfg


def _erstattbar(messung: Any) -> bool:
    from src.vision_provider import QUELLE_MESSAGES_API, UsageMessung

    return (
        type(messung) is UsageMessung
        and messung.quelle == QUELLE_MESSAGES_API
        and messung.vollstaendig is True
    )


async def abrechnen(messung: Any) -> None:
    """Nach dem Direktaufruf. Erstattet wird NUR auf eine vollstaendige
    UsageMessung aus der Messages-API-Rohantwort; alles andere (None, Fehler,
    unvollstaendig) laesst die volle Reservierung stehen. Wirft nie; scheitert
    der Aufruf, bleibt die Buchung offen und zaehlt voll (Ausgang unklar)."""
    r = _aktuell.get()
    if r is None or r.erledigt:
        return
    r.erledigt = True
    ist: Optional[float] = None
    if _erstattbar(messung):
        ist = kosten_obergrenze_eur(
            eingabe=messung.eingabe, ausgabe=messung.ausgabe,
            cache_lesen=messung.cache_lesen, cache_anlage=messung.cache_anlage,
        )
    try:
        erg = await _plattform("/v1/internal/test-ki-deckel/abrechnen", {
            "reservierung_id": r.reservierung_id, "ist_eur": ist,
        })
        logger.info(
            "test-ki echt, gedeckelt app=%s lauf=%s ist=%s reserviert=%.6f geschlossen=%s "
            "lauf_summe=%.4f/%.2f tag_summe=%.4f/%.2f gesamt=%.4f/%.2f EUR",
            r.app, r.lauf, "nicht gemessen" if ist is None else f"{ist:.6f}", r.betrag_eur,
            erg.get("geschlossen"), float(erg.get("lauf_summe") or 0), DECKEL_LAUF_EUR,
            float(erg.get("tag_summe") or 0), DECKEL_TAG_EUR,
            float(erg.get("gesamt_summe") or 0), DECKEL_GESAMT_EUR,
        )
    except Exception as e:
        logger.error(
            "test-ki-deckel: Abrechnung Ausgang unklar app=%s lauf=%s id=%s (%s) — Reservierung %.6f bleibt offen",
            r.app, r.lauf, r.reservierung_id, e, r.betrag_eur,
        )
