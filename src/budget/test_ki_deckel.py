"""Gedeckelte echte KI (nur Haiku) fuer Testkonten von werking-works und werking-report.

Auftrag KD 91624155, 27.10.2026 (Rafael 16:36Z: Tester pruefen mit echter KI,
Haiku, gedeckelt). Die Apps sperren Testaufrufe heute selbst
(KI_IM_TEST_GESPERRT, App-seitige fetch-Huelle) und lassen kuenftig nur Haiku
durch. Dieses Tor ist der Deckel dahinter: es entscheidet allein aus Bridge-
eigenen Daten und nie aus einem frei setzbaren Feld der Anfrage, ob ein Aufruf
ein Testaufruf ist.

Wann greift das Tor (alles muss zutreffen, sonst bleibt der Aufruf unberuehrt):
  1. BRIDGE_TEST_KI_DECKEL=1 auf DIESER Bridge (nur Dev-Compose, nie Prod).
  2. App (X-App-ID / X-Client-ID) ist werking-report, werking-works oder
     engelmann (die Bridge-App-ID von Works).
  3. X-App-Env ist nicht 'production'. Works-Prod spricht dieselbe Dev-Bridge
     an und sein Mandant 'engelmann' steht in der DB auf account_type='test'
     (gemessen 27.10.2026). Ohne diese Ausnahme waeren echte Works-Nutzer
     gedeckelt. Ein fehlender Header zaehlt als Nicht-Prod: das Tor greift.
  4. Der Mandant des Nutzers hat in der Bridge-DB tenants.account_type='test'.

Dann gilt:
  - Das tatsaechlich geroutete Modell (backend_config.provider_model, sonst das
    aufgeloeste Modell) muss genau claude-haiku-5-5 sein und das Backend ein
    Claude-Weg (anthropic / anthropic_direct). Alias, Override oder Tier, der
    auf etwas anderes aufloest -> 403 KI_IM_TEST_GESPERRT.
  - Ohne X-Workflow-ID -> 403 KI_IM_TEST_GESPERRT (grund ohne_lauf).
  - Reservierung der Schaetzung gegen Lauf- (0,50 EUR) und Tagesdeckel (10 EUR
    je App) in einer Transaktion. Ueberschritten -> 402 KI_TEST_DECKEL_ERREICHT.
  - Jeder Lese-/Schreibfehler -> 503 KI_TEST_DECKEL_NICHT_LESBAR (fail-closed;
    anders als das Budget-Tor in gate.py, das bewusst fail-open ist).
  - Fallback-Ketten auf andere Anbieter sind fuer diese Aufrufe gesperrt
    (main.py prueft `ist_gedeckelt`).
  - Nach dem Aufruf verrechnet persist_ai_call_activity die Reservierung auf
    die tatsaechlichen Kosten (`verrechne_falls_offen`).

Lauf = (UTC-Tag, Nutzer, X-Workflow-ID). Der Tag steckt im Schluessel, weil
mehrere App-Wege feste Workflow-Namen senden (Works 'praesentation-analyse');
ohne ihn waere ein solcher "Lauf" nach 0,50 EUR fuer immer gesperrt.

Kosten = Listenpreis (src/pricing.cost_eur). Auf der Dev-Bridge laufen die
Aufrufe ueber Pauschal-Konten (real_cost_eur 0); der Deckel misst trotzdem den
Listenpreis, damit er auch dann haelt, wenn ein Weg echtes Geld kostet.

Datenbank: der Worker geht ueber platform-api (/v1/internal/test-ki-deckel/*,
ADR-0009), bei PlatformUnavailable direkt auf die DB, wenn er sie hat. Sonst 503.
"""
from __future__ import annotations

import contextvars
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)

SCHALTER = "BRIDGE_TEST_KI_DECKEL"
MODELL = "claude-haiku-5-5"
DECKEL_LAUF_EUR = 0.50
DECKEL_TAG_EUR = 10.00
# Ohne max_tokens schaetzt das Tor die Ausgabe mit diesem Wert (Haiku: ~0,02 EUR).
AUSGABE_SCHAETZUNG_OHNE_MAX = 8192

# Bridge-App-ID -> Deckel-App. Works meldet sich als 'engelmann'.
DECKEL_APP = {
    "werking-report": "werking-report",
    "werking-works": "werking-works",
    "engelmann": "werking-works",
}

CODE_GESPERRT = "KI_IM_TEST_GESPERRT"
CODE_DECKEL = "KI_TEST_DECKEL_ERREICHT"
CODE_NICHT_LESBAR = "KI_TEST_DECKEL_NICHT_LESBAR"


def schalter_an() -> bool:
    return os.getenv(SCHALTER, "").strip() == "1"


@dataclass
class Reservierung:
    app: str
    tag: str
    lauf: str
    betrag_eur: float
    nutzer: str
    workflow_id: str
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


# ── Datenbank (laeuft in platform-api bzw. im Rueckfall direkt) ───────────────

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


async def db_reservieren(
    *, user_id: uuid.UUID, app: str, tag: str, lauf: str, betrag_eur: float,
    deckel_lauf: float, deckel_tag: float,
) -> Dict[str, Any]:
    """Prueft account_type und reserviert atomar gegen beide Deckel.

    Antwort: {testkonto: bool|None} und, nur fuer Testkonten,
    {ok: bool, ebene?: 'lauf'|'tag', lauf_summe, tag_summe}.
    """
    from src.db.client import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT t.account_type::text AS account_type FROM users u "
                "JOIN tenants t ON t.id = u.tenant_id WHERE u.id = $1",
                user_id,
            )
            if row is None:
                return {"testkonto": None}
            if row["account_type"] != "test":
                return {"testkonto": False}
            await conn.execute(
                "INSERT INTO test_ki_deckel (app_id, ebene, schluessel) VALUES "
                "($1, 'lauf', $2), ($1, 'tag', $3) ON CONFLICT DO NOTHING",
                app, lauf, tag,
            )
            # Feste Sperrreihenfolge (ebene: 'lauf' < 'tag'), damit zwei
            # gleichzeitige Aufrufe sich nicht gegenseitig verklemmen.
            zeilen = await conn.fetch(
                "SELECT ebene, summe_eur FROM test_ki_deckel WHERE app_id = $1 AND "
                "((ebene = 'lauf' AND schluessel = $2) OR (ebene = 'tag' AND schluessel = $3)) "
                "ORDER BY ebene FOR UPDATE",
                app, lauf, tag,
            )
            summen = {z["ebene"]: float(z["summe_eur"]) for z in zeilen}
            if set(summen) != {"lauf", "tag"}:
                raise RuntimeError(f"test_ki_deckel: Zeilen fehlen nach INSERT ({sorted(summen)})")
            lauf_summe, tag_summe = summen["lauf"], summen["tag"]
            for ebene, summe, deckel in (("lauf", lauf_summe, deckel_lauf), ("tag", tag_summe, deckel_tag)):
                if summe + betrag_eur > deckel:
                    return {
                        "testkonto": True, "ok": False, "ebene": ebene,
                        "lauf_summe": lauf_summe, "tag_summe": tag_summe,
                    }
            await conn.execute(
                "UPDATE test_ki_deckel SET summe_eur = summe_eur + $4, aufrufe = aufrufe + 1, "
                "aktualisiert = now() WHERE app_id = $1 AND "
                "((ebene = 'lauf' AND schluessel = $2) OR (ebene = 'tag' AND schluessel = $3))",
                app, lauf, tag, betrag_eur,
            )
    return {
        "testkonto": True, "ok": True,
        "lauf_summe": lauf_summe + betrag_eur, "tag_summe": tag_summe + betrag_eur,
    }


async def db_verrechnen(*, app: str, tag: str, lauf: str, delta_eur: float) -> Dict[str, Any]:
    """Bucht die Differenz (ist - reserviert) auf beide Zeilen. Nicht idempotent:
    der Worker ruft es hoechstens einmal je Reservierung (Reservierung.erledigt)."""
    from src.db.client import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        zeilen = await conn.fetch(
            "UPDATE test_ki_deckel SET summe_eur = GREATEST(summe_eur + $4, 0), "
            "aktualisiert = now() WHERE app_id = $1 AND "
            "((ebene = 'lauf' AND schluessel = $2) OR (ebene = 'tag' AND schluessel = $3)) "
            "RETURNING ebene, summe_eur",
            app, lauf, tag, delta_eur,
        )
    summen = {z["ebene"]: float(z["summe_eur"]) for z in zeilen}
    if set(summen) != {"lauf", "tag"}:
        raise RuntimeError(f"test_ki_deckel: Verrechnung traf {sorted(summen)} statt lauf+tag")
    return {"lauf_summe": summen["lauf"], "tag_summe": summen["tag"]}


# ── Worker-Seite: platform-api zuerst, sonst direkt, sonst Fehler ─────────────

async def _ueber_plattform(pfad: str, koerper: Dict[str, Any], direkt) -> Dict[str, Any]:
    from src.platform_client import PlatformUnavailable, call_platform

    try:
        resp = await call_platform("POST", pfad, json=koerper, timeout_s=3.0, retries=0)
    except PlatformUnavailable as e:
        from src.db.client import is_db_enabled

        if not is_db_enabled():
            raise RuntimeError(f"platform-api nicht erreichbar und keine DB: {e}") from e
        logger.error("test-ki-deckel: platform-api nicht erreichbar (%s) — direkt auf die DB", e)
        return await direkt()
    if resp.status_code != 200 or not isinstance(resp.json, dict):
        raise RuntimeError(f"platform-api {pfad} antwortete {resp.status_code}")
    return resp.json


async def pruefe_und_reserviere(
    *,
    attribution: Dict[str, Any],
    backend: Optional[str],
    effektives_modell: Optional[str],
    eingabe_tokens: int,
    max_tokens: Optional[int],
) -> Optional[Reservierung]:
    """Das Tor. None = greift nicht (Aufruf unveraendert). Wirft HTTPException
    fuer Sperre (403), Deckel (402) und Lese-/Schreibfehler (503)."""
    # Jeder Chat-Aufruf beginnt ohne Reservierung. Noetig, weil ein In-Prozess-
    # Aufruf (Job-Executor) den Kontext seines Vorgaengers erben kann.
    _aktuell.set(None)
    if not schalter_an():
        return None
    app = DECKEL_APP.get((attribution.get("app_id") or "").strip())
    if app is None:
        return None
    if attribution.get("app_env") == "prod":
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
        # Kein bekannter Bridge-Nutzer -> kein bekanntes Testkonto. Das
        # Budget-Tor davor weist eine kaputte Identitaet ohnehin ab.
        return None
    except Exception as e:
        logger.error("test-ki-deckel: Nutzer nicht aufloesbar (%s) — gesperrt", e)
        raise _fehler(503, CODE_NICHT_LESBAR, "Testkonto-Pruefung nicht moeglich (fail-closed).")

    workflow_id = (attribution.get("workflow_id") or "").strip()
    modell_ok = effektives_modell == MODELL and backend in ("anthropic", "anthropic_direct")

    # Sperrgruende vor der Reservierung brauchen trotzdem das Testkonto-Urteil
    # aus der DB: Kunden-Aufrufe mit Sonnet duerfen hier nie abgewiesen werden.
    if not modell_ok or not workflow_id:
        try:
            test = await _ueber_plattform(
                "/v1/internal/test-ki-deckel/testkonto",
                {"user_id": str(uid)},
                lambda: _als_dict(db_ist_testkonto(uid)),
            )
        except Exception as e:
            logger.error("test-ki-deckel: account_type nicht lesbar (%s) — gesperrt", e)
            raise _fehler(503, CODE_NICHT_LESBAR, "Testkonto-Pruefung nicht moeglich (fail-closed).")
        if test.get("testkonto") is not True:
            return None
        grund = "modell_nicht_haiku" if not modell_ok else "ohne_lauf"
        logger.warning(
            "test-ki gesperrt app=%s grund=%s modell=%s backend=%s workflow=%s",
            app, grund, effektives_modell, backend, workflow_id or "-",
        )
        raise _fehler(
            403, CODE_GESPERRT,
            (f"Testkonto: nur {MODELL} ist freigegeben (geroutet: {effektives_modell}, {backend})."
             if grund == "modell_nicht_haiku"
             else "Testkonto: Aufruf ohne X-Workflow-ID hat keinen Lauf und bleibt gesperrt."),
            grund=grund,
        )

    from src.pricing import cost_eur

    ausgabe = int(max_tokens) if max_tokens else AUSGABE_SCHAETZUNG_OHNE_MAX
    betrag = round(cost_eur(MODELL, max(int(eingabe_tokens or 0), 0), ausgabe), 6)
    tag = _tag_heute()
    lauf = f"{tag}|{uid}|{workflow_id}"
    koerper = {
        "user_id": str(uid), "app": app, "tag": tag, "lauf": lauf, "betrag_eur": betrag,
        "deckel_lauf": DECKEL_LAUF_EUR, "deckel_tag": DECKEL_TAG_EUR,
    }
    try:
        erg = await _ueber_plattform(
            "/v1/internal/test-ki-deckel/reservieren", koerper,
            lambda: db_reservieren(
                user_id=uid, app=app, tag=tag, lauf=lauf, betrag_eur=betrag,
                deckel_lauf=DECKEL_LAUF_EUR, deckel_tag=DECKEL_TAG_EUR,
            ),
        )
    except Exception as e:
        logger.error("test-ki-deckel: Reservierung fehlgeschlagen (%s) — gesperrt", e)
        raise _fehler(503, CODE_NICHT_LESBAR, "Test-KI-Deckel nicht lesbar/schreibbar (fail-closed).")

    if erg.get("testkonto") is not True:
        return None
    if not erg.get("ok"):
        ebene = erg.get("ebene")
        summe = erg.get("lauf_summe") if ebene == "lauf" else erg.get("tag_summe")
        deckel = DECKEL_LAUF_EUR if ebene == "lauf" else DECKEL_TAG_EUR
        logger.warning(
            "test-ki deckel erreicht app=%s ebene=%s summe=%.4f deckel=%.2f lauf=%s",
            app, ebene, float(summe or 0), deckel, lauf,
        )
        raise _fehler(
            402, CODE_DECKEL,
            f"Test-KI-Deckel je {ebene} erreicht ({float(summe or 0):.4f} von {deckel:.2f} EUR).",
            ebene=ebene, summe_eur=summe, deckel_eur=deckel,
        )

    r = Reservierung(app=app, tag=tag, lauf=lauf, betrag_eur=betrag, nutzer=str(uid), workflow_id=workflow_id)
    _aktuell.set(r)
    logger.info(
        "test-ki echt, gedeckelt app=%s lauf=%s reserviert=%.6f lauf_summe=%.4f/%.2f tag_summe=%.4f/%.2f EUR",
        app, lauf, betrag, float(erg.get("lauf_summe") or 0), DECKEL_LAUF_EUR,
        float(erg.get("tag_summe") or 0), DECKEL_TAG_EUR,
    )
    return r


async def _als_dict(coro) -> Dict[str, Any]:
    return {"testkonto": await coro}


async def verrechne_falls_offen(
    *, model: Optional[str], input_tokens: int, output_tokens: int,
    cache_read_tokens: int = 0, cache_creation_tokens: int = 0,
) -> None:
    """Aus persist_ai_call_activity: Reservierung auf die Ist-Kosten verrechnen.
    Wirft nie. Scheitert die Verrechnung, bleibt die Reservierung stehen
    (zaehlt zu viel, nie zu wenig)."""
    r = _aktuell.get()
    if r is None or r.erledigt:
        return
    r.erledigt = True
    try:
        from src.pricing import cost_eur

        ist = round(cost_eur(model or MODELL, int(input_tokens or 0), int(output_tokens or 0),
                             int(cache_read_tokens or 0), int(cache_creation_tokens or 0)), 6)
        delta = round(ist - r.betrag_eur, 6)
        erg = await _ueber_plattform(
            "/v1/internal/test-ki-deckel/verrechnen",
            {"app": r.app, "tag": r.tag, "lauf": r.lauf, "delta_eur": delta},
            lambda: db_verrechnen(app=r.app, tag=r.tag, lauf=r.lauf, delta_eur=delta),
        )
        logger.info(
            "test-ki echt, gedeckelt app=%s lauf=%s ist=%.6f lauf_summe=%.4f/%.2f tag_summe=%.4f/%.2f EUR",
            r.app, r.lauf, ist, float(erg.get("lauf_summe") or 0), DECKEL_LAUF_EUR,
            float(erg.get("tag_summe") or 0), DECKEL_TAG_EUR,
        )
    except Exception as e:
        logger.error(
            "test-ki-deckel: Verrechnung fehlgeschlagen app=%s lauf=%s (%s) — Reservierung %.6f bleibt stehen",
            r.app, r.lauf, e, r.betrag_eur,
        )
