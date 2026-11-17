"""Provisionierungswerkzeug fuer gebundene Principals (bpx_), nur Dev-Bridge.

Aufruf IM Worker-Container der Ziel-Bridge (BRIDGE_DB_URL gesetzt):

  pruefen    nur lesen; meldet je Bedingung ok/FEHLT als JSON auf stderr; rc 0 nur wenn alles ok
  anlegen    erst dieselbe Pruefung (fail-closed), dann create_bound_principal. Der Token geht
             AUSSCHLIESSLICH auf stdout (genau eine Zeile) und nur, wenn stdout KEIN Terminal ist.
             Der Aufrufer leitet stdout in eine root-eigene 0600-Datei um, z. B.
               ssh root@<dev-host> 'umask 077; docker exec -i <worker> python -I -B -m src.bound_provision \
                 anlegen ... > /root/bound-uebergabe/<name>.token'
             Bericht (id, Ablauf, Hash12 + Laenge des Tokens) auf stderr. Nie Klartext im Bericht.
  widerrufen setzt active=false fuer genau einen GEBUNDENEN Principal (proxy_routes nicht leer) und
             liest zurueck. Mit --token-stdin (Token aus der Uebergabedatei per stdin) belegt es
             zusaetzlich, dass der Verifier-Lookup ihn nicht mehr aufloest (= 401 im Verifier).

Feste Grenzen (SEC 17.11.2026): nur Ziel dev (BRIDGE_ORIGIN_ID == "dev"), nur App werking-works,
Routen nur aus BOUND_ROUTEN_OBERGRENZE, kurze Ablaufzeit (max. MAX_GUELTIG_STUNDEN), und ein
strikter Monatsplan mit hartem Deckel ist PFLICHT: ohne passenden Plan, aktive Subscription,
Monatstopf = Deckel und ohne TopUp-Lots legt das Werkzeug nichts an.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

ZIEL_ERLAUBT = "dev"
APP_ERLAUBT = "werking-works"
MAX_GUELTIG_STUNDEN = 72


def _fp(token: str) -> Dict[str, Any]:
    return {"hash12": hashlib.sha256(token.encode()).hexdigest()[:12], "laenge": len(token)}


def _melde(obj: Dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str), file=sys.stderr)


def _deckel(raw: str) -> Decimal:
    try:
        d = Decimal(raw)
    except InvalidOperation:
        raise SystemExit("deckel ist keine Zahl")
    if d <= 0:
        raise SystemExit("deckel muss > 0 sein")
    return d


def _ziel_ok(ziel: str) -> Optional[str]:
    """None wenn ok, sonst Grund. Ziel kommt aus dem Prozess der Bridge, nicht vom Aufrufer."""
    from src.federation import self_origin_id
    if ziel != ZIEL_ERLAUBT:
        return f"ziel {ziel!r} nicht erlaubt (nur {ZIEL_ERLAUBT!r})"
    eigen = (self_origin_id() or "").strip().lower()
    if eigen != ZIEL_ERLAUBT:
        return "diese Bridge ist nicht dev (BRIDGE_ORIGIN_ID)"
    return None


async def pruefe(conn: Any, a: argparse.Namespace) -> List[Dict[str, Any]]:
    """Alle Vorbedingungen, nur lesend. Jede Zeile: {pruefung, ok, info}."""
    from src.principals import BOUND_ROUTEN_OBERGRENZE
    erg: List[Dict[str, Any]] = []

    def z(name: str, ok: bool, info: Any = None) -> None:
        erg.append({"pruefung": name, "ok": bool(ok), "info": info})

    grund = _ziel_ok(a.ziel)
    z("ziel_dev", grund is None, grund)
    try:
        uuid.UUID(a.infisical_projekt)
        z("infisical_projekt_uuid", True, a.infisical_projekt)
    except ValueError:
        z("infisical_projekt_uuid", False, None)
    z("app_werking_works", a.app == APP_ERLAUBT, a.app)
    unbekannt = sorted(set(a.routen) - BOUND_ROUTEN_OBERGRENZE)
    z("routen_in_obergrenze", bool(a.routen) and not unbekannt, unbekannt or len(a.routen))
    z("gueltigkeit_kurz", 0 < a.gueltig_stunden <= MAX_GUELTIG_STUNDEN, a.gueltig_stunden)
    try:
        uid = uuid.UUID(a.user_id)
    except ValueError:
        z("user_id_uuid", False, None)
        return erg

    cols = await conn.fetchval(
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'service_principals' "
        "AND column_name IN ('proxy_routes','proxy_user_id','proxy_tenant_id','proxy_expires_at')")
    z("migration_081", cols == 4, cols)

    u = await conn.fetchrow("SELECT email, tenant_id FROM users WHERE id = $1", uid)
    z("nutzer_existiert", u is not None)
    z("nutzer_email_exakt", u is not None and (u["email"] or "") == a.email)
    z("nutzer_mandant_exakt", u is not None and u["tenant_id"] == a.tenant)

    deckel = _deckel(a.deckel)
    p = await conn.fetchrow(
        "SELECT app_id::text AS app_id, is_active, interval, is_trial, api_budget_eur, sellable "
        "FROM plans WHERE id::text = $1", a.plan_id)
    z("plan_existiert", p is not None, a.plan_id)
    if p is not None:
        z("plan_app", p["app_id"] == a.app, p["app_id"])
        z("plan_aktiv_monat_kein_trial",
          p["is_active"] is True and p["interval"] == "month" and p["is_trial"] is False)
        z("plan_deckel", Decimal(str(p["api_budget_eur"])) == deckel, str(p["api_budget_eur"]))
        z("plan_nicht_verkaeuflich", p["sellable"] is False)
    n_monat = await conn.fetchval(
        "SELECT count(*) FROM plans WHERE app_id::text = $1 AND is_active AND NOT is_trial "
        "AND interval = 'month'", a.app)
    z("genau_ein_monatsplan_der_app", n_monat == 1, n_monat)

    subs = await conn.fetch(
        "SELECT plan_id::text AS plan_id FROM subscriptions WHERE user_id = $1 "
        "AND app_id::text = $2 AND status = 'active'", uid, a.app)
    z("subscription_aktiv_genau_eine", len(subs) == 1 and subs[0]["plan_id"] == a.plan_id,
      [s["plan_id"] for s in subs])

    raw = await conn.fetchval("SELECT monthly_budgets FROM user_budgets WHERE user_id = $1", uid)
    if isinstance(raw, str):
        raw = json.loads(raw)
    eintrag = (raw or {}).get(a.plan_id)
    limit_ok = False
    if isinstance(eintrag, dict) and "limitEur" in eintrag:
        try:
            limit_ok = Decimal(str(eintrag["limitEur"])) == deckel
        except InvalidOperation:
            limit_ok = False
    z("monatstopf_gleich_deckel", limit_ok, eintrag.get("limitEur") if isinstance(eintrag, dict) else None)

    lots = await conn.fetchval(
        "SELECT count(*) FROM user_topup_lots WHERE user_id = $1 AND expires_at > NOW() "
        "AND amount_eur > 0", uid)
    z("keine_topup_lots", lots == 0, lots)

    aktiv = await conn.fetchval(
        "SELECT count(*) FROM service_principals WHERE proxy_user_id = $1 AND active "
        "AND cardinality(proxy_routes) > 0 AND proxy_expires_at > NOW()", uid) if cols == 4 else None
    z("kein_aktiver_gebundener_principal", aktiv == 0, aktiv)
    return erg


async def _verbinden():
    from src.db.client import init_pool, get_pool, is_db_enabled
    if not is_db_enabled():
        raise SystemExit("BRIDGE_DB_URL fehlt: nur im Worker-Container der Ziel-Bridge ausfuehren")
    await init_pool()
    return get_pool()


async def lauf_pruefen(a: argparse.Namespace) -> int:
    pool = await _verbinden()
    async with pool.acquire() as conn:
        erg = await pruefe(conn, a)
    alles = all(e["ok"] for e in erg)
    _melde({"modus": "pruefen", "alles_ok": alles, "pruefungen": erg})
    return 0 if alles else 2


async def lauf_anlegen(a: argparse.Namespace) -> int:
    if sys.stdout.isatty():
        _melde({"modus": "anlegen", "abbruch": "stdout ist ein Terminal; Token nur in Datei umleiten"})
        return 3
    from src.principals import create_bound_principal
    pool = await _verbinden()
    async with pool.acquire() as conn:
        erg = await pruefe(conn, a)
    if not all(e["ok"] for e in erg):
        _melde({"modus": "anlegen", "abbruch": "vorbedingung", "pruefungen": erg})
        return 2
    ablauf = datetime.now(timezone.utc) + timedelta(hours=a.gueltig_stunden)
    neu = await create_bound_principal(a.name, a.app, list(a.routen), a.user_id, a.tenant, ablauf)
    token = neu.pop("token")
    sys.stdout.write(token + "\n")
    sys.stdout.flush()
    _melde({"modus": "anlegen", "angelegt": neu, "token": _fp(token),
            "uebergabe_an_infisical_projekt": a.infisical_projekt})
    return 0


async def lauf_widerrufen(a: argparse.Namespace) -> int:
    from src.principals import get_bound_principal_by_token, invalidate_cache
    grund = _ziel_ok(a.ziel)
    if grund:
        _melde({"modus": "widerrufen", "abbruch": grund})
        return 2
    pid = uuid.UUID(a.id)
    pool = await _verbinden()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE service_principals SET active = FALSE WHERE id = $1 "
            "AND cardinality(proxy_routes) > 0 RETURNING id, active", pid)
        zurueck = await conn.fetchrow(
            "SELECT active, cardinality(proxy_routes) AS r FROM service_principals WHERE id = $1", pid)
    invalidate_cache()
    bericht: Dict[str, Any] = {
        "modus": "widerrufen", "id": str(pid), "getroffen": row is not None,
        "readback_active": None if zurueck is None else zurueck["active"],
    }
    ok = row is not None and zurueck is not None and zurueck["active"] is False
    if a.token_stdin:
        token = sys.stdin.readline().strip()
        aufgeloest = await get_bound_principal_by_token(token) if token else None
        bericht["token"] = _fp(token) if token else None
        bericht["verifier_lookup_leer"] = aufgeloest is None
        ok = ok and bool(token) and aufgeloest is None
    bericht["ok"] = ok
    _melde(bericht)
    return 0 if ok else 2


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m src.bound_provision")
    sp = ap.add_subparsers(dest="modus", required=True)
    for m in ("pruefen", "anlegen"):
        p = sp.add_parser(m)
        p.add_argument("--ziel", required=True)
        p.add_argument("--name", required=True)
        p.add_argument("--app", required=True)
        p.add_argument("--user-id", required=True)
        p.add_argument("--email", required=True)
        p.add_argument("--tenant", required=True)
        p.add_argument("--plan-id", required=True)
        p.add_argument("--deckel", required=True)
        p.add_argument("--gueltig-stunden", type=int, required=True)
        p.add_argument("--route", dest="routen", action="append", required=True)
        # Ziel der Uebergabe (Infisical-Projekt je Daemon, ACL B). Kommt aus der Lauf-Konfiguration,
        # nie hardcodiert; das Werkzeug schreibt NICHT nach Infisical, es nennt das Ziel im Bericht.
        p.add_argument("--infisical-projekt", required=True)
    w = sp.add_parser("widerrufen")
    w.add_argument("--ziel", required=True)
    w.add_argument("--id", required=True)
    w.add_argument("--token-stdin", action="store_true")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    a = parser().parse_args(argv)
    lauf = {"pruefen": lauf_pruefen, "anlegen": lauf_anlegen, "widerrufen": lauf_widerrufen}[a.modus]
    return asyncio.run(lauf(a))


if __name__ == "__main__":
    sys.exit(main())
