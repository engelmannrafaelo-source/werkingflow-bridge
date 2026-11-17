"""Betreiberabgleich offener Kettenreservierungen des Credit-Pools (Kanzler-Entscheid fe68dc46053c, 17.11.2026).

Schliesst eine Kettenzeile (Envelope v2, state 'reserved', mindestens ein unknown-Eintrag) NUR mit Beleg:

- N ``nicht_gesendet_belegt``: alle Marken nicht_gesendet, kein Versandbeleg (gesendet/request_id/response),
  Phase reserve oder Ausnahmeklasse aus NOT_SENT_ERRORS, Belegreferenz des Betreibers -> released, 0 USD.
- A ``abgerechnet`` (Belegart anbieter_antwort): request-id des Betreibers gleich einer an der Quelle
  gespeicherten, usage der Anbieterantwort vorhanden -> settled, charged = max(Kosten laut Antwort, belegt).

Kein HTTP, kein Router, keine Automatik (kein Cron/Timer/Worker/Client). Aufruf nur durch einen Betreiber
im platform-api-Container: ``python -m src.providers.credit_pool_abgleich --help``. Prod nur ueber die
bestehenden Prod-Gates mit Rafaels Wort. Eine Transaktion: Zeilensperre, Aenderung, audit_log (vorher/nachher).
Idempotent ueber --abgleich-id; ein zweiter, anderer Abgleich ist ein Konflikt (keine nachtraegliche Korrektur).

Exit: 0 erledigt/identisch wiederholt, 2 Aufruf/Reservierung unbekannt, 3 nicht abgleichbar/Konflikt,
4 Beleg ungueltig, 5 DB-Fehler.

Betreiberablauf N (Kanzler-Entscheid Restluecke F und F-R2, 17.11.2026):
1. Im Log ALLER Worker-Instanzen nach der Reservierungs-UUID suchen (``grep <uuid>``), Logger
   ``src.providers.credit_pool``.
2. Zulaessig sind zur UUID NUR diese drei Zeilen (jede andere Zeile zur UUID widerlegt N):
   - ``Credit reservation <uuid>: NICHT_GESENDET phase=<reserve|versand> klasse=<Ausnahmeklasse|None>``
   - ``Credit reservation <uuid>: request may have reached Anthropic (nicht_gesendet); ...``
   - ``Credit reservation <uuid>: outcome unknown (nicht_gesendet), marker not stored; ...``
   Belastbar widerlegt insbesondere ``Credit reservation <uuid>: send receipt not stored ...`` (Versand belegt).
   Auf dem Erfolgsweg der Kette schreibt der Worker KEINE Zeile mit der UUID; ein Versand zeigt sich also nur
   ueber den /gesendet-Eintrag an der Zeile (vom Befehl geprueft) oder ueber diese Widerlegungszeile.
3. Die NICHT_GESENDET-Zeile (mit freiem Praefix fuer Host/Zeitstempel) ist --beleg-ref. Der Befehl prueft:
   Logformat (Regex), genau diese UUID und keine weitere, phase/klasse gleich phase/detail der gespeicherten
   Marke, keine Widerlegungszeile (Gross-/Kleinschreibung egal). Was er NICHT pruefen kann: dass die Zeile
   wirklich im Log steht und dort keine andere Zeile zur UUID; das ist Betreiberpflicht (Audit mit Spur).
4. Fehlt das Worker-Log zur Zeit der Reservierung, ist N nicht zulaessig; die Reserve bleibt gebunden.
   Aufbewahrung: das Worker-Log muss mindestens bis zum Abgleich aufbewahrt werden. Vor Aktivierung der Kette
   am Zielsystem zu belegen: Aufbewahrungsdauer der Worker-Logs und dass ERROR-Zeilen von
   src.providers.credit_pool dort tatsaechlich ankommen.

Grenze A (A5): Eine Zeile ohne gespeicherte request-id (z. B. billed_unbekannt, deren /gesendet ausfiel) ist
per A nicht abschliessbar und bleibt auf der Obergrenze (geldsicher). A1-R2: Traegt die Zeile mehr als eine
verschiedene request-id (gesendet + unknown), ist A nicht zulaessig (Exit 4): eine Reservierung ist genau eine
Anbieteranfrage.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import os
import re
import socket
import sys
import uuid
from decimal import Decimal

from src.providers.credit_pool_routes import (ENVELOPE_VERSION, NICHT_GESENDET_KLASSEN, REQUEST_ID_MUSTER,
                                              TOKEN_FELDER, _jsonb, beleg_usage_pruefen, usage_cost)

log = logging.getLogger("credit_pool_abgleich")

# F-R2: Logformat der Worker-Zeile (credit_pool._nicht_gesendet), freies Praefix, Endung danach erlaubt.
UUID_MUSTER = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
NICHT_GESENDET_ZEILE = re.compile(r"Credit reservation (?P<uuid>[0-9a-fA-F-]{36}): NICHT_GESENDET "
                                  r"phase=(?P<phase>reserve|versand) klasse=(?P<klasse>[A-Za-z]+)(?![A-Za-z0-9_])")
WIDERLEGUNG = "send receipt not stored"

ERGEBNISSE = ("nicht_gesendet_belegt", "abgerechnet")
AUDIT_ACTION = "credit_pool.abgleich"


class Abbruch(Exception):
    def __init__(self, code: int, text: str):
        super().__init__(text)
        self.code = code
        self.text = text


def _zeile_json(row) -> dict:
    d = dict(row)
    out = {}
    for k, v in d.items():
        if k == "usage":
            out[k] = _jsonb(v)
        elif isinstance(v, Decimal):
            out[k] = str(v)
        elif v is None or isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = str(v)
    return out


def eingabe_hash(a: dict) -> str:
    # A4: betreiber gehoert dazu (Wiederholung durch einen anderen Betreiber ist kein identischer Aufruf).
    teile = {k: a.get(k) for k in ("reservierung", "ergebnis", "beleg_ref", "request_id", "betreiber")}
    return hashlib.sha256(json.dumps(teile, sort_keys=True).encode()).hexdigest()


def _betreiber_spur(betreiber: str) -> dict:
    try:
        user = getpass.getuser()
    except Exception:
        user = None
    return {"betreiber": betreiber, "sudo_user": os.environ.get("SUDO_USER"), "user": user,
            "host": socket.gethostname()}


def _pruefe_n(env: dict, a: dict) -> dict:
    unk = env.get("unknown") or []
    if env.get("response") is not None:
        raise Abbruch(4, "N abgewiesen: Antwort gespeichert (Versand belegt)")
    if env.get("gesendet"):
        raise Abbruch(4, "N abgewiesen: Versandbeleg (gesendet) vorhanden")
    for e in unk:
        if not isinstance(e, dict) or e.get("reason") != "nicht_gesendet":
            raise Abbruch(4, "N abgewiesen: nicht alle Marken nicht_gesendet")
        if e.get("widerspruch_versand") or e.get("request_id") or e.get("usage") is not None:
            raise Abbruch(4, "N abgewiesen: Marke traegt Versandbeleg")
        if e.get("beleg_ungueltig"):
            raise Abbruch(4, "N abgewiesen: Marke mit ungueltigem Beleg")
        phase = e.get("phase")
        if phase == "reserve":
            continue
        if phase == "versand" and e.get("detail") in NICHT_GESENDET_KLASSEN:
            continue
        raise Abbruch(4, "N abgewiesen: Phase/Ausnahmeklasse belegt keinen Nichtversand")
    ref = a.get("beleg_ref") or ""
    if not ref:
        raise Abbruch(4, "N abgewiesen: --beleg-ref fehlt")
    # Restluecke F / F-R2: die Worker-Logzeile NICHT_GESENDET zu GENAU dieser Reservierung, im Logformat,
    # gebunden an die gespeicherte Marke.
    if WIDERLEGUNG in ref.lower():
        raise Abbruch(4, "N abgewiesen: Beleg nennt einen Versand (send receipt not stored)")
    uuids = {x.lower() for x in UUID_MUSTER.findall(ref)}
    if uuids != {a["reservierung"]}:
        raise Abbruch(4, "N abgewiesen: --beleg-ref nennt eine fremde oder keine Reservierungs-UUID")
    treffer = list(NICHT_GESENDET_ZEILE.finditer(ref))
    if len(treffer) != 1 or treffer[0]["uuid"].lower() != a["reservierung"]:
        raise Abbruch(4, "N abgewiesen: --beleg-ref ist nicht genau eine Worker-Logzeile NICHT_GESENDET im Logformat")
    beleg_phase, beleg_klasse = treffer[0]["phase"], treffer[0]["klasse"]
    for e in unk:
        if e.get("phase") != beleg_phase or (e.get("detail") or "None") != beleg_klasse:
            raise Abbruch(4, "N abgewiesen: phase/klasse des Belegs widersprechen der gespeicherten Marke")
    return {"state": "released", "charged": Decimal(0), "belegart": "nicht_gesendet"}


def _kosten_iterationen(its: list, nur_belegt_oder_unbekannt: bool) -> Decimal:
    summe = Decimal(0)
    for i in its:
        if nur_belegt_oder_unbekannt and i.get("billed") is False:
            continue
        summe += usage_cost(i["model"], {f: i[f] for f in TOKEN_FELDER})
    return summe


def _pruefe_a(env: dict, a: dict) -> dict:
    rid = a.get("request_id")
    if not isinstance(rid, str) or not REQUEST_ID_MUSTER.fullmatch(rid):
        raise Abbruch(4, "A abgewiesen: --request-id fehlt oder ungueltig")
    quellen = [e for e in (env.get("gesendet") or []) + (env.get("unknown") or []) if isinstance(e, dict)]
    if not any(e.get("request_id") == rid for e in quellen):
        raise Abbruch(4, "A abgewiesen: request-id an der Quelle nicht gespeichert")
    ids = {e.get("request_id") for e in quellen if e.get("request_id") is not None}
    if len(ids) > 1:
        # A1-R2: eine Reservierung ist genau eine Anbieteranfrage; keine Abrechnung ueber eine zweite request-id.
        raise Abbruch(4, f"A abgewiesen: {len(ids)} verschiedene request-ids an der Zeile")
    from fastapi import HTTPException
    try:
        resp = env.get("response")
        belegt = Decimal(resp["belegt_usd"]) if isinstance(resp, dict) and resp.get("belegt_usd") else Decimal(0)
        kandidaten = []
        if isinstance(resp, dict) and isinstance(resp.get("iterations"), list):
            # Konservativ: billed true und "unbekannt" zaehlen; billed false nur dann weglassen, wenn die Lage
            # belegt ist. A2: bei abweichung_unbelegt zaehlt /settle jede Iteration als unbekannt -> alle zaehlen.
            kandidaten.append(_kosten_iterationen(resp["iterations"], resp.get("lage") != "abweichung_unbelegt"))
        marken = [e for e in env.get("unknown") or [] if isinstance(e, dict) and e.get("request_id") == rid
                  and e.get("usage") is not None]
        for marke in marken:
            # A1: JEDE Marke zur request-id zaehlt, es gilt das Maximum; eine spaetere kleinere senkt nie.
            usage = marke["usage"]
            if not beleg_usage_pruefen(usage):
                raise Abbruch(4, "A abgewiesen: usage formal ungueltig")
            if isinstance(usage.get("iterations"), list) and usage["iterations"]:
                kandidaten.append(_kosten_iterationen(usage["iterations"], False))
            else:
                model = marke.get("model")
                if not isinstance(model, str):
                    raise Abbruch(4, "A abgewiesen: Antwortmodell fehlt")
                kandidaten.append(usage_cost(model, {f: usage[f] for f in TOKEN_FELDER}))
        if not kandidaten:
            raise Abbruch(4, "A abgewiesen: keine usage der Anbieterantwort gespeichert")
        if len(set(kandidaten)) > 1:
            log.error("Abgleich %s: %d abweichende Belege zur request-id; es gilt das Maximum", a["reservierung"],
                      len(kandidaten))
        kosten = max(kandidaten)
    except HTTPException as exc:
        raise Abbruch(4, f"A abgewiesen: nicht bepreisbar ({exc.detail})") from exc
    except (KeyError, TypeError, ArithmeticError) as exc:
        raise Abbruch(4, f"A abgewiesen: Beleg formal ungueltig ({type(exc).__name__})") from exc
    return {"state": "settled", "charged": max(kosten, belegt), "belegart": "anbieter_antwort",
            "kosten_usd": str(kosten), "belegt_usd": str(belegt)}


async def _audit_schreiben(conn, target_id: str, vorher: dict, nachher: dict, metadata: dict) -> None:
    await conn.execute("""INSERT INTO audit_log (actor_label, action, target_kind, target_id, before_state,
        after_state, metadata) VALUES ($1, $2, 'anthropic_credit_reservation', $3, $4::jsonb, $5::jsonb, $6::jsonb)""",
                       "betreiber:" + metadata["spur"]["betreiber"], AUDIT_ACTION, target_id, json.dumps(vorher),
                       json.dumps(nachher), json.dumps(metadata))


async def abgleichen(conn, a: dict) -> dict:
    """Ein Abgleich in EINER Transaktion. a: reservierung, abgleich_id, ergebnis, betreiber, beleg_ref, request_id."""
    h = eingabe_hash(a)
    async with conn.transaction():
        # A3: abgleich_id global eindeutig. Sperre je ID serialisiert parallele Laeufe auf verschiedenen Zeilen.
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "credit_pool.abgleich:" + a["abgleich_id"])
        fremd = await conn.fetchval("""SELECT target_id FROM audit_log WHERE action=$1 AND metadata->>'abgleich_id'=$2
            AND target_id <> $3 LIMIT 1""", AUDIT_ACTION, a["abgleich_id"], a["reservierung"])
        if fremd:
            raise Abbruch(3, "Konflikt: abgleich_id bereits fuer eine andere Reservierung verwendet")
        row = await conn.fetchrow("SELECT * FROM anthropic_credit_reservations WHERE id=$1 FOR UPDATE",
                                  uuid.UUID(a["reservierung"]))
        if not row:
            raise Abbruch(2, "Reservierung unbekannt")
        env = _jsonb(row["usage"])
        if not (isinstance(env, dict) and env.get("v") == ENVELOPE_VERSION):
            raise Abbruch(3, "nicht abgleichbar: keine Kettenzeile (v2); Altform nur mit eigenem Entscheid")
        frueher = env.get("abgleich")
        if isinstance(frueher, dict):
            if frueher.get("abgleich_id") == a["abgleich_id"] and frueher.get("eingabe_hash") == h:
                return {"wiederholt": True, "state": row["state"], "charged_usd": str(row["charged_usd"]),
                        "abgleich": frueher}
            raise Abbruch(3, "Konflikt: Zeile bereits abgeglichen (keine nachtraegliche Korrektur)")
        if row["state"] != "reserved":
            raise Abbruch(3, "nicht abgleichbar: state != reserved")
        if not env.get("unknown"):
            raise Abbruch(3, "nicht abgleichbar: keine unknown-Marke")
        ergebnis = _pruefe_n(env, a) if a["ergebnis"] == "nicht_gesendet_belegt" else _pruefe_a(env, a)
        reserved = Decimal(row["reserved_usd"])
        if ergebnis["charged"] > reserved:
            log.error("Abgleich %s: charged %s USD > Obergrenze %s USD (ueberschreitung_usd=%s)", a["reservierung"],
                      ergebnis["charged"], reserved, ergebnis["charged"] - reserved)
        spur = _betreiber_spur(a["betreiber"])
        abgleich = {"abgleich_id": a["abgleich_id"], "eingabe_hash": h, "ergebnis": a["ergebnis"],
                    "belegart": ergebnis["belegart"], "beleg_ref": a.get("beleg_ref"),
                    "request_id": a.get("request_id"), "charged_usd": str(ergebnis["charged"]),
                    **{k: ergebnis[k] for k in ("kosten_usd", "belegt_usd") if k in ergebnis}, "spur": spur}
        vorher = _zeile_json(row)
        await conn.execute("""UPDATE anthropic_credit_reservations SET state=$2, charged_usd=$3, settled_at=now(),
            usage = jsonb_set(usage, '{abgleich}', $4::jsonb || jsonb_build_object('at', now())) WHERE id=$1""",
                           row["id"], ergebnis["state"], ergebnis["charged"], json.dumps(abgleich))
        nach = await conn.fetchrow("SELECT * FROM anthropic_credit_reservations WHERE id=$1", row["id"])
        await _audit_schreiben(conn, str(row["id"]), vorher, _zeile_json(nach),
                               {"abgleich_id": a["abgleich_id"], "ergebnis": a["ergebnis"],
                                "belegart": ergebnis["belegart"], "beleg_ref": a.get("beleg_ref"),
                                "request_id": a.get("request_id"), "spur": spur})
        return {"wiederholt": False, "state": nach["state"], "charged_usd": str(nach["charged_usd"]),
                "abgleich": abgleich}


def argumente(argv: list[str] | None = None) -> dict:
    p = argparse.ArgumentParser(prog="python -m src.providers.credit_pool_abgleich",
                                description="Betreiberabgleich einer Kettenreservierung (nur mit Beleg, auditiert).")
    p.add_argument("--reservierung", required=True)
    p.add_argument("--abgleich-id", required=True)
    p.add_argument("--ergebnis", required=True, choices=ERGEBNISSE)
    p.add_argument("--betreiber", required=True)
    p.add_argument("--beleg-ref", default=None, help="Pflicht bei nicht_gesendet_belegt")
    p.add_argument("--request-id", default=None, help="Pflicht bei abgerechnet")
    try:
        ns = p.parse_args(argv)
    except SystemExit as exc:
        raise Abbruch(2, "Aufruf ungueltig") from exc
    a = {"reservierung": ns.reservierung, "abgleich_id": ns.abgleich_id, "ergebnis": ns.ergebnis,
         "betreiber": ns.betreiber.strip(), "beleg_ref": ns.beleg_ref, "request_id": ns.request_id}
    for k in ("reservierung", "abgleich_id"):
        try:
            a[k] = str(uuid.UUID(a[k]))
        except ValueError as exc:
            raise Abbruch(2, f"--{k.replace('_', '-')} ist keine UUID") from exc
    if not a["betreiber"] or len(a["betreiber"]) > 128:
        raise Abbruch(2, "--betreiber fehlt")
    if a["beleg_ref"] is not None and not (0 < len(a["beleg_ref"]) <= 512):
        raise Abbruch(2, "--beleg-ref zu lang/leer")
    return a


async def main(argv: list[str] | None = None) -> int:
    try:
        a = argumente(argv)
    except Abbruch as exc:
        print(json.dumps({"exit": exc.code, "fehler": exc.text}, ensure_ascii=False))
        return exc.code
    from src.db.client import close_pool, get_pool, init_pool
    try:
        if not os.getenv("BRIDGE_DB_URL"):
            raise Abbruch(5, "DB nicht konfiguriert")
        await init_pool()
        try:
            async with get_pool().acquire() as conn:
                ergebnis = await abgleichen(conn, a)
        finally:
            await close_pool()
    except Abbruch as exc:
        log.error("Abgleich %s abgewiesen: %s", a["reservierung"], exc.text)
        print(json.dumps({"exit": exc.code, "fehler": exc.text}, ensure_ascii=False))
        return exc.code
    except Exception as exc:  # DB-Fehler: Transaktion zurueckgerollt, nichts geaendert, kein Audit
        log.error("Abgleich %s: DB-Fehler %s", a["reservierung"], type(exc).__name__)
        print(json.dumps({"exit": 5, "fehler": f"DB-Fehler {type(exc).__name__}"}, ensure_ascii=False))
        return 5
    print(json.dumps({"exit": 0, **ergebnis}, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
