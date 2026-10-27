"""Gedeckelte echte KI (nur Haiku, Direktweg) fuer Testkonten von werking-works und werking-report.

Auftrag KD 91624155, 27.10.2026 (Rafael 16:36Z: Tester pruefen mit echter KI,
Haiku, gedeckelt). R2 nach Codex R1 (local-storage/bridge-ki-test-gedeckelt-
20261027/PRUEFUNG-R1.md, ENTWURF-R2.md). Die Apps sperren Testaufrufe selbst
und lassen nur Haiku-Chat in der Preview durch; dieses Tor ist der Deckel
dahinter. Jede der drei R1-Klassen ist an EINER Stelle geschlossen:

  R1-1 Herkunft   — `ist_ausgenommen`: nur der vom Token aufgeloeste Prinzipal
                    (request.state.principal, src/auth.py) kann einen Aufruf
                    ausnehmen, nie ein Kopf. X-App-Env spielt keine Rolle.
  R1-2 Obergrenze — `tor`: gedeckelte Aufrufe laufen IMMER ueber den Direktweg
                    (claude-direct-notools, ein Messages-API-Aufruf, max_tokens
                    wirksam, Usage gemessen); reserviert wird `obergrenze_eur`
                    der Anfrage, nach dem Aufruf `abrechnen` aus gemessener
                    Usage. Ohne Messung bleibt die volle Reservierung stehen.
  R1-3 Idempotenz — reservierung_id je Aufruf, Dedup per Primaerschluessel in
                    test_ki_buchung, einziger Schreibweg platform-api. Unklarer
                    Ausgang beim Reservieren = 503, beim Abrechnen bleibt sie
                    offen (zaehlt voll). Kein DB-Rueckfall.

Wann das Tor greift (sonst bleibt der Aufruf unberuehrt):
  1. BRIDGE_TEST_KI_DECKEL=1 auf dieser Bridge (nur Dev-Compose).
  2. App (X-App-ID / X-Client-ID) werking-report, werking-works oder engelmann.
     Die App ist schon vom Prinzipal geprueft (principal.may_use_app).
  3. Der Prinzipal steht NICHT in BRIDGE_TEST_KI_PROD_PRINZIPALE.
  4. tenants.account_type = 'test' fuer den Nutzer (Bridge-DB ueber platform-api).

Dann gilt: angefragtes Modell (nach Alias-Aufloesung) claude-haiku-5-5, keine
Werkzeuge, X-Test-Lauf (64 Hex, sha256 einer App-Sitzungskennung, gesetzt von
der App-Sperre) Pflicht; Weg wird auf den Direktweg gezwungen und am
aufgeloesten Backend nachgeprueft; Reservierung gegen Lauf (0,50), App-Tag
(10,00) und Gesamt-Tag (20,00 EUR). Fehlercodes:
  403 KI_IM_TEST_GESPERRT (grund modell_nicht_haiku | werkzeuge | weg | ohne_lauf)
  402 KI_TEST_DECKEL_ERREICHT (ebene lauf | tag | gesamt)
  503 KI_TEST_DECKEL_NICHT_LESBAR (platform-api nicht erreichbar / unklar)
"""
from __future__ import annotations

import contextvars
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
PROD_PRINZIPALE = "BRIDGE_TEST_KI_PROD_PRINZIPALE"
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
CODE_DECKEL = "KI_TEST_DECKEL_ERREICHT"
CODE_NICHT_LESBAR = "KI_TEST_DECKEL_NICHT_LESBAR"

# Schluessel in der Job-Attribution: Prinzipal des ANLEGENDEN Aufrufs,
# serverseitig gesetzt (src/jobs/routes.py), nie aus dem Rumpf uebernommen.
JOB_PRINZIPAL_KEY = "test_ki_prinzipal"


def schalter_an() -> bool:
    return os.getenv(SCHALTER, "").strip() == "1"


def _prod_prinzipale() -> frozenset[str]:
    roh = os.getenv(PROD_PRINZIPALE, "")
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


def _fehler(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    detail = {"error": {"message": message, "type": "test_ki_deckel", "code": code, **extra}}
    return HTTPException(status_code=status, detail=detail)


def _tag_heute() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


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

async def db_ist_testkonto(user_id: uuid.UUID) -> Optional[bool]:
    """True/False aus tenants.account_type; None = Nutzer unbekannt."""
    from src.db.client import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT t.account_type::text AS account_type FROM users u "
            "JOIN tenants t ON t.id = u.tenant_id WHERE u.id = $1",
            user_id,
        )
    if row is None:
        return None
    return row["account_type"] == "test"


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


async def _job_prinzipal(request: Any, user_id: str) -> Optional[str]:
    """Prinzipal des Aufrufs, der den laufenden Job ANGELEGT hat — nur fuer den
    Selbstaufruf des Job-Executors (der mit dem Worker-Schluessel laeuft).
    Gilt nur, wenn der Job laeuft und demselben Nutzer gehoert."""
    from src.jobs.selbstaufruf import JOB_LAUF_HEADER

    job_id = (request.headers.get(JOB_LAUF_HEADER) or "").strip()
    if not job_id:
        return None
    from src.jobs import store_client

    job = await store_client.get_job(job_id)
    if not job:
        return None
    attr = (job.get("attribution") if isinstance(job, dict) else getattr(job, "attribution", None)) or {}
    status = job.get("status") if isinstance(job, dict) else getattr(job, "status", None)
    if status != "running" or str(attr.get("user_id") or "") != str(user_id):
        return None
    name = attr.get(JOB_PRINZIPAL_KEY)
    return name if isinstance(name, str) and name else None


async def ist_ausgenommen(request: Any, user_id: str) -> bool:
    """R1-1: allein der vom Token aufgeloeste Prinzipal entscheidet."""
    erlaubt = _prod_prinzipale()
    if not erlaubt:
        return False
    principal = getattr(getattr(request, "state", None), "principal", None)
    name = getattr(principal, "name", None)
    if name in erlaubt and not getattr(principal, "is_legacy", False):
        return True
    # Job-Selbstaufruf: laeuft mit dem Worker-Schluessel (Legacy oder keiner).
    if principal is None or getattr(principal, "is_legacy", False):
        try:
            return (await _job_prinzipal(request, user_id)) in erlaubt
        except Exception as e:
            logger.error("test-ki-deckel: Job-Herkunft nicht lesbar (%s) — nicht ausgenommen", e)
            return False
    return False


async def tor(
    *,
    request: Any,
    request_body: Any,
    attribution: Dict[str, Any],
    resolved_model: Optional[str],
    erzwinge_direktweg,
):
    """Das Tor. Rueckgabe None = greift nicht; sonst die erzwungene
    BackendConfig des Direktwegs. Wirft 403/402/503 (siehe Modulkopf).
    `erzwinge_direktweg()` liefert die BackendConfig fuer DIREKT_TIER."""
    _aktuell.set(None)
    if not schalter_an():
        return None
    app = DECKEL_APP.get((attribution.get("app_id") or "").strip())
    if app is None:
        return None
    roh_nutzer = attribution.get("user_id")
    if not roh_nutzer:
        return None

    from src.identity.user_resolver import (
        MalformedUserIdentity, UnknownUserIdentity, resolve_user_id,
    )

    try:
        uid = await resolve_user_id(roh_nutzer)
    except (MalformedUserIdentity, UnknownUserIdentity):
        return None
    except Exception as e:
        logger.error("test-ki-deckel: Nutzer nicht aufloesbar (%s) — gesperrt", e)
        raise _fehler(503, CODE_NICHT_LESBAR, "Testkonto-Pruefung nicht moeglich (fail-closed).")

    # Roh-Kennung (UUID oder E-Mail), so wie sie auch in der Job-Attribution steht.
    if await ist_ausgenommen(request, str(roh_nutzer)):
        return None

    try:
        test = await _plattform("/v1/internal/test-ki-deckel/testkonto", {"user_id": str(uid)})
    except Exception as e:
        logger.error("test-ki-deckel: account_type nicht lesbar (%s) — gesperrt", e)
        raise _fehler(503, CODE_NICHT_LESBAR, "Testkonto-Pruefung nicht moeglich (fail-closed).")
    if test.get("testkonto") is not True:
        return None

    def _sperre(grund: str, text: str) -> HTTPException:
        logger.warning("test-ki gesperrt app=%s grund=%s modell=%s", app, grund, resolved_model)
        return _fehler(403, CODE_GESPERRT, text, grund=grund)

    if resolved_model != MODELL:
        raise _sperre("modell_nicht_haiku", f"Testkonto: nur {MODELL} ist freigegeben (angefragt: {resolved_model}).")
    if request_body.enable_tools:
        raise _sperre("werkzeuge", "Testkonto: Aufrufe mit Werkzeugen sind gesperrt.")

    try:
        cfg = erzwinge_direktweg()
    except Exception as e:  # z. B. Direktweg auf dieser Bridge nicht konfiguriert
        logger.error("test-ki-deckel: Direktweg nicht herstellbar (%s)", e)
        cfg = None
    backend = getattr(cfg.backend, "value", cfg.backend) if cfg else None
    if not cfg or backend != "anthropic_direct" or cfg.provider_model != MODELL:
        raise _sperre("weg", f"Testkonto: Direktweg mit {MODELL} nicht herstellbar ({backend}, {getattr(cfg, 'provider_model', None)}).")

    test_lauf = (request.headers.get(LAUF_KOPF) or "").strip().lower()
    if not _LAUF_MUSTER.match(test_lauf):
        raise _sperre("ohne_lauf", f"Testkonto: ohne gueltigen {LAUF_KOPF} (64 Hex, gesetzt von der App-Sperre) kein Lauf.")

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
        raise _fehler(503, CODE_NICHT_LESBAR, "Test-KI-Deckel nicht lesbar/schreibbar (fail-closed).")

    if not erg.get("ok"):
        ebene = erg.get("ebene")
        deckel = {"lauf": DECKEL_LAUF_EUR, "tag": DECKEL_TAG_EUR, "gesamt": DECKEL_GESAMT_EUR}.get(ebene)
        summe = erg.get(f"{ebene}_summe")
        logger.warning(
            "test-ki deckel erreicht app=%s ebene=%s grund=%s summe=%s deckel=%s betrag=%.6f lauf=%s",
            app, ebene, erg.get("grund"), summe, deckel, betrag, lauf,
        )
        raise _fehler(
            402, CODE_DECKEL,
            f"Test-KI-Deckel je {ebene} erreicht ({erg.get('grund')}; {summe} von {deckel} EUR, Aufruf braeuchte bis {betrag:.4f}).",
            ebene=ebene, grund=erg.get("grund"), summe_eur=summe, deckel_eur=deckel, reservierung_eur=betrag,
        )

    _aktuell.set(Reservierung(reservierung_id=rid, app=app, lauf=lauf, tag=tag, betrag_eur=betrag))
    logger.info(
        "test-ki echt, gedeckelt app=%s lauf=%s reserviert=%.6f (ein<=%d aus<=%d) "
        "lauf_summe=%.4f/%.2f tag_summe=%.4f/%.2f gesamt=%.4f/%.2f EUR",
        app, lauf, betrag, eingabe, ausgabe, float(erg.get("lauf_summe") or 0), DECKEL_LAUF_EUR,
        float(erg.get("tag_summe") or 0), DECKEL_TAG_EUR, float(erg.get("gesamt_summe") or 0), DECKEL_GESAMT_EUR,
    )
    return cfg


async def abrechnen(usage: Optional[Dict[str, Any]]) -> None:
    """Nach dem Direktaufruf: aus GEMESSENER Usage abrechnen (usage None = nicht
    gemessen, Reservierung bleibt voll). Wirft nie; scheitert der Aufruf, bleibt
    die Buchung offen und zaehlt voll."""
    r = _aktuell.get()
    if r is None or r.erledigt:
        return
    r.erledigt = True
    ist: Optional[float] = None
    if usage is not None:
        ist = kosten_obergrenze_eur(
            eingabe=int(usage.get("prompt_tokens") or 0),
            ausgabe=int(usage.get("completion_tokens") or 0),
            cache_lesen=int(usage.get("cache_read_input_tokens") or 0),
            cache_anlage=int(usage.get("cache_creation_input_tokens") or 0),
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
            "test-ki-deckel: Abrechnung ohne klares Ergebnis app=%s lauf=%s id=%s (%s) — Reservierung %.6f bleibt offen",
            r.app, r.lauf, r.reservierung_id, e, r.betrag_eur,
        )
