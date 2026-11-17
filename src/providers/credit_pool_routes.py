"""Platform-only shared credit ledger. One configured primary for both bridges.

Secrets come from a private manifest, never from the DB. Advisory transaction
locks serialize grant admission across platform processes and worker hosts.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, StrictInt, StrictStr

from src.db.client import get_pool
from src.pricing import normalize_model_id, price_entry

log = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/internal/api-credit-pool", tags=["internal"])

# Iterationsabrechnung (Entwurf ENTWURF-POOL-ITERATIONSABRECHNUNG-V2-c9a50d3b-20261117.md, R4 Envelope).
ENVELOPE_VERSION = 2
LAGEN = ("kein_fallback", "fallback_belegt", "sticky_belegt", "abweichung_unbelegt")
ITER_TYPEN = ("message", "fallback_message")
TOKEN_FELDER = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
# A4 (Codepruefung R1): Iteration streng, genau diese Schluessel (kein server_tool_use: A2, Server-Tools mit Kette gesperrt).
ITER_PFLICHT = ("type", "model", *TOKEN_FELDER, "billed")
ITER_ERLAUBT = frozenset(ITER_PFLICHT)


def _fallback_max() -> int | None:
    """Hoechstzahl Versuche je Kette (Ausgangsmodell + Ersatz). Ohne Konfiguration: keine Kette (fail closed)."""
    raw = os.getenv("ANTHROPIC_CREDIT_POOL_FALLBACK_ITERATIONEN_MAX", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 2 else None


def _norm(model: str) -> str:
    return re.sub(r"-\d{8}$", "", normalize_model_id(model))


def _jsonb(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _kanonisch(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
# Longer than any single pool request may legitimately run (research-cloud
# read timeout 900 s). A reservation still 'reserved' after this is hanging.
HANGING_AFTER = timedelta(hours=1)


def authenticate(x_credit_pool_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("ANTHROPIC_CREDIT_POOL_TOKEN", "")
    if not expected or not x_credit_pool_token or not hmac.compare_digest(expected, x_credit_pool_token):
        raise HTTPException(403, "Credit pool authentication required")


def _manifest_data() -> dict:
    path = os.getenv("ANTHROPIC_CREDIT_POOL_ACCOUNTS_FILE", "/app/secrets/api-credit-pool.json")
    try:
        data = json.loads(Path(path).read_text())
        if not isinstance(data.get("accounts"), list) or not data["accounts"]:
            raise ValueError("Missing account inventory")
        return data
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        raise HTTPException(503, "Credit pool manifest unavailable or invalid") from exc


def manifest(data: dict | None = None) -> list[dict]:
    try:
        data = _manifest_data() if data is None else data
        now = datetime.now(timezone.utc)
        accounts = []
        seen_grants = set()
        for a in data["accounts"]:
            # A failed individual refresh cannot be hidden by another account.
            observed = datetime.fromisoformat(a["observed_at"].replace("Z", "+00:00"))
            if now - observed > timedelta(hours=1) or observed > now + timedelta(minutes=1):
                continue
            if not a.get("api_key") or a.get("disabled") or a.get("billing_guard") != "free_only_verified":
                continue
            for g in a["grants"]:
                if g["id"] in seen_grants:
                    raise ValueError("Duplicate credit grant")
                seen_grants.add(g["id"])
                for field in ("granted_usd", "remaining_usd"):
                    value = Decimal(str(g[field]))
                    if not value.is_finite() or value < 0:
                        raise ValueError("Invalid credit balance")
                expires = datetime.fromisoformat(g["expires_at"].replace("Z", "+00:00"))
                if expires <= now + timedelta(hours=1):
                    continue
                # Keep $2 headroom against upstream accounting delays.
                budget = Decimal(str(g["granted_usd"])) - Decimal("2")
                if budget <= 0 or Decimal(str(g["remaining_usd"])) <= 2:
                    continue
                accounts.append({**a, "grant_id": g["id"], "expires_at": g["expires_at"],
                                 "budget": budget, "remaining": Decimal(str(g["remaining_usd"])) - 2,
                                 "observed": observed})
        return sorted(accounts, key=lambda a: a["expires_at"])
    except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
        raise HTTPException(503, "Credit pool manifest unavailable or invalid") from exc


def reservation_cost(model: str, inputs: int, outputs: int) -> Decimal:
    p = price_entry(model)
    if not p or not model.startswith("claude-"):
        raise HTTPException(400, "Unpriced credit-pool model")
    # Upper tier, 1h cache write (2x), plus 10% headroom. count_tokens is
    # authoritative even for images; max_tokens includes thinking output.
    cost = (Decimal(inputs) * Decimal(str(p["in"])) * 2
            + Decimal(outputs) * Decimal(str(p["out"]))) / 1_000_000
    return max(Decimal("0.000001"), (cost * Decimal("1.1")).quantize(Decimal("0.000000001"), rounding=ROUND_CEILING))


def usage_cost(model: str, usage: dict) -> Decimal:
    """Grant-Verbrauch in USD, bewusst konservativ (Kanzler-Festlegung S0 A3, 17.11.2026): Cache-Anlage zum
    1h-Satz (2 x Eingabe), Cache-Lesen mit cache_read_mult, Websuche 0,01 je Anfrage, plus 10 % Aufschlag.
    Weicht damit absichtlich vom Bridge-Ledger ab (dort Cache nach tatsaechlicher Stufe, Energy-Preis kommt
    allein aus dem Ledger). Keine Angleichung: der Pool-Betrag ist Deckung, kein Preis."""
    names = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    if any(type(usage.get(n)) is not int or usage[n] < 0 for n in names):
        raise HTTPException(422, "Incomplete usage; reservation retained")
    # Deliberately upper tier and 1h write rate. Conservative monetary ledger;
    # app billing continues to use its existing exact SSoT independently.
    p = price_entry(model)
    if not p:
        raise HTTPException(422, "Missing price; reservation retained")
    value = (Decimal(usage["input_tokens"]) * Decimal(str(p["in"]))
             + Decimal(usage["output_tokens"]) * Decimal(str(p["out"]))
             + Decimal(usage["cache_read_input_tokens"]) * Decimal(str(p["in"])) * Decimal(str(p.get("cache_read_mult", .1)))
             + Decimal(usage["cache_creation_input_tokens"]) * Decimal(str(p["in"])) * 2) / 1_000_000
    server = usage.get("server_tool_use", {})
    if not isinstance(server, dict):
        raise HTTPException(422, "Unknown server tool usage; reservation retained")
    if any(k not in ("web_search_requests", "web_fetch_requests") for k in server):
        raise HTTPException(422, "Unpriced server usage; reservation retained")
    if any(type(v) is not int or v < 0 for v in server.values()):
        raise HTTPException(422, "Invalid server tool usage; reservation retained")
    value += Decimal(server.get("web_search_requests", 0)) * Decimal("0.01")
    return (value * Decimal("1.1")).quantize(Decimal("0.000000001"), rounding=ROUND_CEILING)


async def available(conn, a: dict) -> Decimal:
    row = await conn.fetchrow("""SELECT COALESCE(SUM(charged_usd),0) AS total,
        COALESCE(SUM(charged_usd) FILTER (WHERE created_at >= $2 OR state='reserved'),0) AS recent
        FROM anthropic_credit_reservations WHERE grant_id=$1""", a["grant_id"], a["observed"] - timedelta(minutes=5))
    # Recent/uncertain costs may not yet be reflected in the console balance.
    return min(a["budget"] - row["total"], a["remaining"] - row["recent"])


@router.get("/accounts", dependencies=[Depends(authenticate)])
async def accounts():
    result = []
    async with get_pool().acquire() as conn:
        for a in manifest():
            if await available(conn, a) > 0:
                result.append({k: a[k] for k in ("id", "org_id", "grant_id", "api_key")})
    return {"accounts": result}


class Reserve(BaseModel):
    id: UUID
    account_id: str
    grant_id: str
    model: str
    input_tokens: int = Field(ge=0, le=32_000_000, strict=True)
    max_tokens: int = Field(gt=0, le=8_000_000, strict=True)
    # Kette (Server-Fallback). Ohne fallback_models: Vertrag byte-gleich zu vorher.
    fallback_models: list[StrictStr] = Field(default_factory=list)
    input_tokens_je_modell: dict[StrictStr, StrictInt] | None = None
    # A1 (Root 01a1e624): nur der Anbieter-Default "default"; die Kette ist dann eine Ableitung der Bridge.
    fallbacks_modus: StrictStr | None = None
    anthropic_version: str | None = None
    anthropic_beta: str | None = None
    kategorienliste_version: str | None = None


def _reserve_vertrag(body: Reserve) -> tuple[dict | None, Decimal]:
    """(unveraenderliche Reserve-Envelope oder None, Betrag). Ohne Kette exakt die alte Rechnung."""
    extra = (body.input_tokens_je_modell, body.anthropic_version, body.anthropic_beta, body.kategorienliste_version,
             body.fallbacks_modus)
    if not body.fallback_models:
        if any(v is not None for v in extra):
            raise HTTPException(400, "Chain fields without fallback_models")
        return None, reservation_cost(body.model, body.input_tokens, body.max_tokens)
    deckel = _fallback_max()
    if deckel is None:
        raise HTTPException(400, "Fallback chain not configured (ANTHROPIC_CREDIT_POOL_FALLBACK_ITERATIONEN_MAX)")
    kette = [body.model, *body.fallback_models]
    if len(kette) > deckel:
        raise HTTPException(400, "Fallback chain longer than configured maximum")
    if len({_norm(m) for m in kette}) != len(kette):
        raise HTTPException(400, "Duplicate model in fallback chain")
    tokens = body.input_tokens_je_modell or {}
    if set(tokens) != set(kette) or any(type(v) is not int or v < 0 for v in tokens.values()):
        # P5/R5: count_tokens je Modell ist Pflicht; keine Schaetzung, kein Faktor.
        raise HTTPException(400, "count_tokens_fehlt: input_tokens_je_modell must cover every chain model")
    if tokens[body.model] != body.input_tokens:
        raise HTTPException(400, "input_tokens differs from input_tokens_je_modell[model]")
    if not body.kategorienliste_version or not body.anthropic_version:
        raise HTTPException(400, "Chain requires anthropic_version and kategorienliste_version")
    if body.fallbacks_modus != "default":
        raise HTTPException(400, "fallbacks_liste_nicht_erlaubt: only fallbacks_modus \"default\"")
    amount = sum((reservation_cost(m, tokens[m], body.max_tokens) for m in kette), Decimal(0))
    vertrag = {"model": body.model, "fallback_models": list(body.fallback_models), "input_tokens_je_modell": tokens,
               "max_tokens": body.max_tokens, "fallbacks_modus": "default",
               # Kette = Ableitung der Bridge; ein Antwortmodell ausserhalb bleibt der laute Fall (modell_abweichung).
               "kette_quelle": "ableitung",
               "anthropic_version": body.anthropic_version,
               "anthropic_beta": body.anthropic_beta, "kategorienliste_version": body.kategorienliste_version,
               "amount_usd": str(amount)}
    return vertrag, amount


@router.post("/reserve", dependencies=[Depends(authenticate)])
async def reserve(body: Reserve):
    a = next((a for a in manifest() if a["id"] == body.account_id and a["grant_id"] == body.grant_id), None)
    if not a:
        raise HTTPException(409, "Grant expired or unavailable")
    vertrag, amount = _reserve_vertrag(body)
    async with get_pool().acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", body.grant_id)
        old = await conn.fetchrow("SELECT * FROM anthropic_credit_reservations WHERE id=$1", body.id)
        if old:
            if any(old[k] != getattr(body, k) for k in ("account_id", "grant_id", "model", "input_tokens", "max_tokens")):
                raise HTTPException(409, "Reservation identity conflict")
            alt = _jsonb(old["usage"]) or {}
            alt_vertrag = alt.get("reserve") if isinstance(alt, dict) and alt.get("v") == ENVELOPE_VERSION else None
            if alt_vertrag != vertrag:
                raise HTTPException(409, "Reservation identity conflict")
            return {"id": str(body.id), "state": old["state"]}
        if amount > await available(conn, a):
            raise HTTPException(429, "Insufficient promotional credit budget")
        if vertrag is None:
            await conn.execute("""INSERT INTO anthropic_credit_reservations
                (id,account_id,grant_id,model,input_tokens,max_tokens,reserved_usd,charged_usd)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$7)""", body.id, body.account_id, body.grant_id,
                               body.model, body.input_tokens, body.max_tokens, amount)
        else:
            await conn.execute("""INSERT INTO anthropic_credit_reservations
                (id,account_id,grant_id,model,input_tokens,max_tokens,reserved_usd,charged_usd,usage)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$7,$8::jsonb)""", body.id, body.account_id, body.grant_id,
                               body.model, body.input_tokens, body.max_tokens, amount,
                               json.dumps({"v": ENVELOPE_VERSION, "reserve": vertrag, "unknown": []}))
    return {"id": str(body.id), "state": "reserved"}


class Settle(BaseModel):
    id: UUID
    usage: dict | None = None
    rejected: bool = False
    # A4: Kettenfelder roh annehmen und im Handler streng pruefen, damit JEDER Formfehler als
    # iterations_ungueltig markiert wird (kein Lax-Umwandeln von "false"/0/"5", kein 422 vor dem Handler).
    # Je Iteration: type, model, 4 Tokenfelder (int), billed (true|false|"unbekannt"; setzt die Bridge
    # nach ihrer Regel 51c8185c, der Pool leitet nichts ab). Keine weiteren Schluessel (A2: kein server_tool_use).
    iterations: Any = None
    lage: Any = None
    antwort_iteration: Any = None
    kategorienliste_version: Any = None


def _iterationen_pruefen(vertrag: dict, body: Settle) -> str | None:
    """Form- und Lagepruefung. Rueckgabe: Fehlertext oder None. Keine Ableitung von billed."""
    its = body.iterations
    kette = [vertrag["model"], *vertrag["fallback_models"]]
    erlaubt = {_norm(m) for m in kette}
    if not isinstance(body.lage, str) or body.lage not in LAGEN:
        return "lage_ungueltig"
    if (not isinstance(body.kategorienliste_version, str)
            or body.kategorienliste_version != vertrag.get("kategorienliste_version")):
        return "kategorienliste_version_abweichend"
    if not isinstance(its, list):
        return "iterationen_form"
    if not its or len(its) > len(kette):
        return "iterationen_anzahl"
    for it in its:
        if not isinstance(it, dict) or not set(it) <= ITER_ERLAUBT or any(k not in it for k in ITER_PFLICHT):
            return "iteration_form"
        if not isinstance(it["type"], str) or it["type"] not in ITER_TYPEN:
            return "iteration_typ"
        if not isinstance(it["model"], str) or not it["model"] or _norm(it["model"]) not in erlaubt \
                or price_entry(it["model"]) is None:
            return "iteration_modell"
        if any(type(it[f]) is not int or it[f] < 0 for f in TOKEN_FELDER):
            return "iteration_tokens"
        b = it["billed"]
        if not (b is True or b is False or (isinstance(b, str) and b == "unbekannt")):
            return "iteration_billed"
    oben = body.usage if isinstance(body.usage, dict) else {}
    server = oben.get("server_tool_use")
    if server is not None and (not isinstance(server, dict) or any(v != 0 for v in server.values())):
        # A2: Gebuehren je Iteration nicht belegt -> keine Abrechnung von Server-Tools ueber eine Kette.
        return "server_tool_use_mit_kette"
    if body.lage == "kein_fallback" and (len(its) != 1 or _norm(its[0]["model"]) != _norm(vertrag["model"])):
        return "lage_kein_fallback_widerspruch"
    if body.antwort_iteration is not None:
        if type(body.antwort_iteration) is not int or not 0 <= body.antwort_iteration < len(its):
            return "antwort_iteration_ausserhalb"
        oben = body.usage or {}
        if any(oben.get(f) != its[body.antwort_iteration][f] for f in TOKEN_FELDER):
            return "top_level_usage_abweichend"
    return None


def _replay_hash(body: Settle) -> str:
    roh = {"usage": body.usage, "lage": body.lage, "antwort_iteration": body.antwort_iteration,
           "kategorienliste_version": body.kategorienliste_version,
           "iterations": body.iterations}
    return hashlib.sha256(_kanonisch(roh).encode()).hexdigest()


async def _settle_envelope(conn, row, envelope: dict, body: Settle) -> dict:
    """Kette (usage.v == 2). PV-1: Response unveraenderlich, Replay nur Vergleich, kein rejected auf 0,
    unbekannt-Zeilen schliesst kein Client. PV-2: charged_usd >= belegter Betrag."""
    if body.rejected:
        raise HTTPException(409, "rejected_nicht_zulaessig: chain reservation cannot be released by a client")
    neu = _replay_hash(body)
    if envelope.get("response") is not None:
        if envelope["response"].get("replay_hash") == neu:
            return {"id": str(body.id), "state": row["state"]}
        log.error("Credit reservation %s: settle_konflikt (different response for an already settled chain)", body.id)
        raise HTTPException(409, "settle_konflikt")
    if envelope.get("unknown"):
        # A3/PV-1: nach /unknown oder iterations_ungueltig schliesst kein Client die Zeile; Abschluss nur
        # ueber den Betreiberabgleich (Anbieterbeleg, Audit, Idempotenz; eigener Auftrag).
        log.error("Credit reservation %s: settle on a chain row marked unknown refused (operator reconciliation only)",
                  body.id)
        raise HTTPException(409, "unbekannt_offen: chain reservation marked unknown; operator reconciliation only")
    if row["state"] != "reserved":
        raise HTTPException(409, "settle_konflikt")
    vertrag = envelope["reserve"]
    fehler = "iterationen_fehlen" if body.iterations is None else _iterationen_pruefen(vertrag, body)
    belegt = frei = Decimal(0)
    unbekannt = 0
    if fehler is None:
        try:
            for it in body.iterations:
                betrag = usage_cost(it["model"], {f: it[f] for f in TOKEN_FELDER})
                if body.lage == "abweichung_unbelegt" or it["billed"] == "unbekannt":
                    unbekannt += 1
                elif it["billed"] is True:
                    belegt += betrag
                else:
                    frei += betrag
        except HTTPException as exc:
            fehler = f"kosten:{exc.detail}"
    if fehler is not None:
        # Nichts gekappt, nichts verworfen: Zeile bleibt auf der Obergrenze, sichtbar markiert.
        await conn.execute("""UPDATE anthropic_credit_reservations SET usage = jsonb_set(usage, '{unknown}',
            COALESCE(usage->'unknown','[]'::jsonb) || jsonb_build_array(jsonb_build_object(
            'reason','iterations_ungueltig','detail',$2::text,'marked_at',now()))) WHERE id=$1""", body.id, fehler)
        return {"_fehler": fehler}
    reserved = Decimal(row["reserved_usd"])
    if belegt > reserved:
        log.error("Credit reservation %s: belegt %s USD > Obergrenze %s USD (ueberschreitung_usd=%s)",
                  body.id, belegt, reserved, belegt - reserved)
    response = {"iterations": body.iterations, "lage": body.lage,
                "antwort_iteration": body.antwort_iteration, "usage": body.usage,
                "kategorienliste_version": body.kategorienliste_version, "belegt_usd": str(belegt),
                "frei_usd": str(frei), "unbekannt_anzahl": unbekannt, "replay_hash": neu}
    if unbekannt == 0:
        state, charged = "settled", belegt
        await conn.execute("""UPDATE anthropic_credit_reservations SET state=$2, charged_usd=$3, settled_at=now(),
            usage = jsonb_set(usage, '{response}', $4::jsonb) WHERE id=$1""", body.id, state, charged, json.dumps(response))
    else:
        # R1/PV-1: bleibt reserved; Abschluss nur ueber Betreiberabgleich (Anbieterbeleg, Audit, Idempotenz), kein Timeout.
        state, charged = "reserved", max(reserved, belegt)
        await conn.execute("""UPDATE anthropic_credit_reservations SET charged_usd=$2,
            usage = jsonb_set(jsonb_set(usage, '{response}', $3::jsonb), '{unknown}',
                COALESCE(usage->'unknown','[]'::jsonb) || jsonb_build_array(jsonb_build_object(
                'reason','billed_unbekannt','marked_at',now()))) WHERE id=$1""", body.id, charged, json.dumps(response))
    return {"id": str(body.id), "state": state, "unbekannt_anzahl": unbekannt}


@router.post("/settle", dependencies=[Depends(authenticate)])
async def settle(body: Settle):
    if body.rejected == (body.usage is not None):
        raise HTTPException(422, "Provide usage OR a confirmed rejection")
    fehler = None
    async with get_pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT * FROM anthropic_credit_reservations WHERE id=$1 FOR UPDATE", body.id)
        if not row:
            raise HTTPException(404, "Unknown reservation")
        envelope = _jsonb(row["usage"])
        if isinstance(envelope, dict) and envelope.get("v") == ENVELOPE_VERSION:
            result = await _settle_envelope(conn, row, envelope, body)
            fehler = result.pop("_fehler", None)
        else:
            if body.iterations is not None or body.lage is not None:
                raise HTTPException(422, "Iterations only for chain reservations")
            if row["state"] != "reserved":
                return {"id": str(body.id), "state": row["state"]}
            cost = Decimal(0) if body.rejected else usage_cost(row["model"], body.usage)
            state = "released" if body.rejected else "settled"
            await conn.execute("""UPDATE anthropic_credit_reservations SET state=$2, charged_usd=$3,
                settled_at=now(), usage=$4::jsonb WHERE id=$1""", body.id, state, cost, json.dumps(body.usage))
            result = {"id": str(body.id), "state": state}
    if fehler is not None:
        raise HTTPException(422, f"iterations_ungueltig ({fehler}); reservation retained")
    return result


class Unknown(BaseModel):
    id: UUID
    reason: Literal["cancelled", "transport_error", "http_status", "unreadable_usage", "error",
                    "modell_abweichung", "bewertung_fehlt", "nicht_gesendet"]
    # L2-L4 (Betreiberabgleich): optionaler Beleg, nur bei Kettenzeilen gespeichert, nie abrechnungswirksam.
    # Roh angenommen: ein ungueltiger Beleg darf die Marke nie verhindern (nur beleg_ungueltig).
    request_id: Any = None
    usage: Any = None
    model: Any = None
    phase: Any = None
    detail: Any = None


REQUEST_ID_MUSTER = re.compile(r"[A-Za-z0-9_\-]{1,128}")
NICHT_GESENDET_KLASSEN = ("ConnectError", "ConnectTimeout", "PoolTimeout")


def beleg_usage_pruefen(usage: Any) -> bool:
    """Beleg-usage streng (A4-Regeln): 4 Tokenfelder int >= 0; optional iterations (je model + 4 Tokenfelder)."""
    if not isinstance(usage, dict) or any(type(usage.get(f)) is not int or usage[f] < 0 for f in TOKEN_FELDER):
        return False
    its = usage.get("iterations")
    if its is None:
        return True
    return isinstance(its, list) and all(
        isinstance(i, dict) and isinstance(i.get("model"), str) and i["model"]
        and all(type(i.get(f)) is int and i[f] >= 0 for f in TOKEN_FELDER) for i in its)


def _beleg(body: "Unknown") -> dict:
    eintrag: dict = {}
    ungueltig = []
    for feld, ok in (("request_id", isinstance(body.request_id, str) and REQUEST_ID_MUSTER.fullmatch(body.request_id)),
                     ("usage", beleg_usage_pruefen(body.usage)),
                     ("model", isinstance(body.model, str) and 0 < len(body.model) <= 128),
                     ("phase", body.phase in ("reserve", "versand")),
                     ("detail", isinstance(body.detail, str) and re.fullmatch(r"[A-Za-z]{1,64}", body.detail))):
        wert = getattr(body, feld)
        if wert is None:
            continue
        if ok:
            eintrag[feld] = wert
        else:
            ungueltig.append(feld)
    if ungueltig:
        eintrag["beleg_ungueltig"] = ungueltig
    return eintrag


GESENDET_MAX = 8


class Gesendet(BaseModel):
    id: UUID
    request_id: Any = None
    status: Any = None


@router.post("/gesendet", dependencies=[Depends(authenticate)])
async def gesendet(body: Gesendet):
    """L2 (Kanzler-Entscheid E3): Versandbeleg einer Kettenzeile, vom Client sofort nach Eingang der Anbieter-
    antwort und VOR jeder Abrechnung/Markierung gemeldet. Nur anhaengen, kein Zustandswechsel, keine
    Abrechnungswirkung. Widerlegt jede spaetere Marke nicht_gesendet (Betreiberabgleich N weist dann ab)."""
    eintrag = {"request_id": body.request_id if isinstance(body.request_id, str)
               and REQUEST_ID_MUSTER.fullmatch(body.request_id) else None,
               "status": body.status if type(body.status) is int and 100 <= body.status <= 599 else None}
    async with get_pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT state, usage FROM anthropic_credit_reservations WHERE id=$1 FOR UPDATE", body.id)
        if not row:
            raise HTTPException(404, "Unknown reservation")
        envelope = _jsonb(row.get("usage"))
        if not (isinstance(envelope, dict) and envelope.get("v") == ENVELOPE_VERSION):
            raise HTTPException(400, "send receipt only for chain reservations")
        # G1 (Codepruefung 5f295d8): nur offene Zeilen; gleiche Belege einmal; hoechstens GESENDET_MAX Eintraege.
        # Jeder vorhandene Eintrag widerlegt N bereits, ein abgewiesener Zusatz aendert daran nichts.
        if row["state"] != "reserved":
            return {"id": str(body.id), "state": row["state"]}
        bisher = [{"request_id": e.get("request_id"), "status": e.get("status")}
                  for e in envelope.get("gesendet") or [] if isinstance(e, dict)]
        if eintrag in bisher:
            return {"id": str(body.id), "state": row["state"]}
        if len(bisher) >= GESENDET_MAX:
            log.error("Credit reservation %s: send receipt limit reached (%s); further receipt not stored",
                      body.id, GESENDET_MAX)
            return {"id": str(body.id), "state": row["state"]}
        await conn.execute("""UPDATE anthropic_credit_reservations SET usage = jsonb_set(usage, '{gesendet}',
            COALESCE(usage->'gesendet','[]'::jsonb) || jsonb_build_array($2::jsonb || jsonb_build_object('at',now())))
            WHERE id=$1""", body.id, json.dumps(eintrag))
    return {"id": str(body.id), "state": row["state"]}


@router.post("/unknown", dependencies=[Depends(authenticate)])
async def unknown(body: Unknown):
    """The request may have reached Anthropic and been billed. Keep the full
    upper-bound debit (state stays 'reserved', still deducted by available())
    and mark it in the existing ``usage`` JSONB so it is visible; no schema
    change. Without a chain (old form) a later /settle with usage or rejected=True
    still finishes it. A chain reservation (usage.v == 2) only gets the mark
    appended; its reserve contract is kept and no client can close it (PV-1)."""
    async with get_pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow("SELECT state, usage FROM anthropic_credit_reservations WHERE id=$1 FOR UPDATE", body.id)
        if not row:
            raise HTTPException(404, "Unknown reservation")
        if row["state"] != "reserved":
            return {"id": str(body.id), "state": row["state"]}
        envelope = _jsonb(row.get("usage"))  # asyncpg.Record und dict kennen .get
        if isinstance(envelope, dict) and envelope.get("v") == ENVELOPE_VERSION:
            eintrag = {"reason": body.reason, **_beleg(body)}
            if body.reason == "nicht_gesendet" and (envelope.get("gesendet") or envelope.get("response") is not None):
                # Kanzler-Entscheid E3: Versand ist belegt -> Marke widerspricht; sichtbar halten, nie aufloesbar.
                eintrag["widerspruch_versand"] = True
                log.error("Credit reservation %s: nicht_gesendet contradicts a stored send receipt", body.id)
            await conn.execute("""UPDATE anthropic_credit_reservations SET usage = jsonb_set(usage, '{unknown}',
                COALESCE(usage->'unknown','[]'::jsonb) || jsonb_build_array($2::jsonb || jsonb_build_object(
                'marked_at',now()))) WHERE id=$1""", body.id, json.dumps(eintrag))
            return {"id": str(body.id), "state": "reserved", "outcome": "unknown"}
        await conn.execute("""UPDATE anthropic_credit_reservations SET usage=jsonb_build_object(
            'outcome','unknown','reason',$2::text,'marked_at',now()) WHERE id=$1""", body.id, body.reason)
    return {"id": str(body.id), "state": "reserved", "outcome": "unknown"}


async def hanging(conn, grant_ids: list[str]) -> dict:
    """Aggregate only: counts, sums and ages. No reservation IDs, accounts or
    grants leave this function, so it is safe for /summary and metrics."""
    row = await conn.fetchrow("""SELECT
        count(*) FILTER (WHERE state='reserved') AS reserved,
        min(created_at) FILTER (WHERE state='reserved') AS oldest,
        count(*) FILTER (WHERE state='reserved' AND created_at < now() - $2::interval) AS hanging,
        COALESCE(sum(charged_usd) FILTER (WHERE state='reserved' AND created_at < now() - $2::interval),0) AS hanging_usd,
        count(*) FILTER (WHERE state='reserved' AND (usage->>'outcome'='unknown'
            OR jsonb_typeof(usage->'unknown')='array' AND jsonb_array_length(usage->'unknown')>0)) AS unknown,
        COALESCE(sum(charged_usd) FILTER (WHERE state='reserved' AND (usage->>'outcome'='unknown'
            OR jsonb_typeof(usage->'unknown')='array' AND jsonb_array_length(usage->'unknown')>0)),0) AS unknown_usd,
        now() AS now
        FROM anthropic_credit_reservations WHERE grant_id = ANY($1::text[])""", grant_ids, HANGING_AFTER)
    oldest = row["oldest"]
    return {
        "reserved_count": row["reserved"],
        "oldest_reserved_age_s": int((row["now"] - oldest).total_seconds()) if oldest else None,
        "hanging_reserved_count": row["hanging"],
        "hanging_reserved_usd": float(row["hanging_usd"]),
        "hanging_after_s": int(HANGING_AFTER.total_seconds()),
        "unknown_outcome_count": row["unknown"],
        "unknown_outcome_usd": float(row["unknown_usd"]),
    }


@router.get("/status", dependencies=[Depends(authenticate)])
async def status():
    result = []
    async with get_pool().acquire() as conn:
        for a in manifest():
            row = await conn.fetchrow("""SELECT count(*) AS requests,
                count(*) FILTER (WHERE state='reserved') AS pending,
                COALESCE(sum(charged_usd),0) AS charged FROM anthropic_credit_reservations WHERE grant_id=$1""", a["grant_id"])
            result.append({"account": a["id"], "org_id": a["org_id"], "grant_id": a["grant_id"],
                           "expires_at": a["expires_at"], "available_usd": float(max(Decimal(0), await available(conn, a))),
                           "requests": row["requests"], "pending": row["pending"], "charged_usd": float(row["charged"]),
                           **await hanging(conn, [a["grant_id"]])})
    return {"accounts": result, "paid_fallback": False}


@router.get("/summary", dependencies=[Depends(authenticate)])
async def summary():
    """One shared monetary reserve, never four additional subscription slots.

    No account identities, organization IDs, cookies or keys leave this route.
    Expired/stale observations do not contribute spendable money.

    Scope of the reservation fields differs on purpose:
    - ``reserved_usd`` (like ``available_usd``) counts ONLY eligible grants,
      i.e. money that currently limits admission;
    - ``reserved_count``, ``oldest_reserved_age_s``, ``hanging_reserved_*`` and
      ``unknown_outcome_*`` cover EVERY grant of the inventory, eligible or
      not (exhausted, stale, blocked), so stuck money never disappears from
      view. Hence ``hanging_reserved_usd`` may exceed ``reserved_usd``.
    """
    data = _manifest_data()
    eligible = manifest(data)
    now = datetime.now(timezone.utc)
    granted = balance = reserved = usable = Decimal(0)
    observed_at, expires_at = [], []
    stale = blocked = 0
    try:
        for account in data["accounts"]:
            observed = datetime.fromisoformat(account["observed_at"].replace("Z", "+00:00"))
            if now - observed > timedelta(hours=1) or observed > now + timedelta(minutes=1):
                stale += 1
                continue
            if account.get("disabled") or not account.get("api_key") or account.get("billing_guard") != "free_only_verified":
                blocked += 1
                continue
            observed_at.append(observed)
            for grant in account["grants"]:
                expiry = datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00"))
                if expiry <= now + timedelta(hours=1):
                    continue
                amounts = [Decimal(str(grant[k])) for k in ("granted_usd", "remaining_usd")]
                if any(not amount.is_finite() or amount < 0 for amount in amounts):
                    raise ValueError("Invalid credit balance")
                granted += amounts[0]
                balance += amounts[1]
                expires_at.append(expiry)
        async with get_pool().acquire() as conn:
            for account in eligible:
                usable += max(Decimal(0), await available(conn, account))
                reserved += await conn.fetchval(
                    "SELECT COALESCE(sum(charged_usd),0) FROM anthropic_credit_reservations WHERE grant_id=$1 AND state='reserved'",
                    account["grant_id"],
                )
            # Over every grant of the inventory, eligible or not: money stuck
            # on an exhausted/stale grant must stay visible too.
            reservations = await hanging(conn, sorted({g["id"] for a in data["accounts"] for g in a.get("grants") or []
                                               if isinstance(g, dict) and g.get("id")}))
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        raise HTTPException(503, "API reserve summary unavailable") from exc
    state = "available" if usable > 0 else "unavailable" if stale or blocked else "exhausted"
    return {
        "pool_id": "anthropic-subscription-api-reserve", "currency": "USD",
        "shared_across_bridges": True, "state": state,
        "granted_usd": float(granted), "remaining_usd": float(balance),
        "available_usd": float(usable), "reserved_usd": float(reserved),
        "account_count": len(data["accounts"]),
        "eligible_accounts": len({a["id"] for a in eligible}),
        "stale_accounts": stale, "blocked_accounts": blocked,
        "measured_at": min(observed_at).isoformat() if observed_at else None,
        "checked_at": now.isoformat(),
        "next_expiry_at": min(expires_at).isoformat() if expires_at else None,
        "automatic_paid_fallback": False,
        **reservations,
        "supported_operations": ["text", "vision"],
        "supports_tools": False, "supports_claude_code": False,
        "requires_stateless_request": True,
    }
