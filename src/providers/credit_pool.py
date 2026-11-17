"""Direct Messages API credits, shared by both bridges through ONE coordinator.

Never exported as ANTHROPIC_API_KEY: the CLI subscription path stays separate.
Only non-streaming, tool-free Messages requests are supported here. A lost
response keeps its full reservation; no speculative release or paid fallback.

Reservation outcome (KD, 02.11.2026):
- provably NOT sent to Anthropic (failure/cancel before the Messages post, or
  a connect/pool error that precedes every request byte) -> released
  (settle rejected=True);
- sent, outcome unknown (cancel/transport error during the post, 5xx, paid
  200 with unreadable usage) -> stays reserved at its upper bound AND is
  marked ``outcome=unknown`` via ``/unknown`` so status endpoints show it.
  Never silently released: Anthropic may have billed it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextvars import ContextVar
import re
from typing import Any, Optional

import httpx
from fastapi import HTTPException

log = logging.getLogger(__name__)
API = "https://api.anthropic.com/v1/messages"
VERSION = "2023-06-01"
# PL-1: Positivliste der API-Versionen. 2026-10-21 nur mit Fallback-Kette und Vorpruefung C4 der Bridge.
VERSIONS_ERLAUBT = ("2023-06-01", "2026-10-21")
VERSION_FALLBACK = "2026-10-21"
# PV-3: einziger zusaetzlicher Kopf (Doku Server-Fallback Z. 259/499); nur mit Kette, nie fuer count_tokens.
KOPF_ERLAUBT = ("anthropic-beta", "server-side-fallback-2026-07-01")
# Raised before any request byte left this process: no connection was ever
# obtained (pool wait) or established (TCP/TLS). Everything else after the
# post started may have reached Anthropic and is treated as outcome unknown.
NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_background: set[asyncio.Task] = set()

# Pool booking log of ONE ledger row (KD Finanzierungsquelle, 03.11.2026).
# A research-cloud run is many Messages calls (rounds, gap checker) but one
# usage_events row; without this log the row cannot name the reservations
# that paid for it, and pool hours cannot be attributed to a job. Set by the
# booking caller (start_booking_log), filled here for every committed
# reservation, read back into provider_metadata (booking_ledger_meta).
# asyncio copies the context into child tasks, so the list object is shared.
_booking_log: ContextVar[Optional[list]] = ContextVar("credit_pool_booking_log", default=None)


def start_booking_log() -> list:
    """Start collecting the pool reservations of the current run."""
    entries: list = []
    _booking_log.set(entries)
    return entries


def _note_booking(reservation: str, grant_id: Any) -> None:
    entries = _booking_log.get()
    if entries is not None:
        entries.append({"reservation": reservation, "grant": grant_id})


def booking_ledger_meta(entries: Optional[list]) -> dict:
    """provider_metadata fields for a row funded by these pool reservations.

    Only reservation and grant ids plus the policy marker: never the account
    label, organisation or key (KD: "grant ohne Kontoidentitaeten/Schluessel").
    Empty dict when the run made no pool reservation, so a run that never
    touched the pool carries no funding claim."""
    if not entries:
        return {}
    grants: list = []
    for e in entries:
        if e.get("grant") is not None and e["grant"] not in grants:
            grants.append(e["grant"])
    return {
        "funding_policy": "promotional_only",
        "automatic_paid_fallback": False,
        "credit_pool_reservations": [e["reservation"] for e in entries],
        "credit_pool_grants": grants,
    }


def enabled() -> bool:
    return bool(os.getenv("ANTHROPIC_CREDIT_POOL_URL"))


class PoolUnavailable(HTTPException):
    """The pool as a whole cannot fund this call: coordinator not configured,
    unreachable or refusing, or no account with eligible budget. Raised before
    any Messages request was sent. Status codes are unchanged (503/429/409);
    the subclass only lets a caller tell "pool cannot pay" apart from a
    request or provider failure (staging subscription fallback,
    src/providers/funding_policy.py)."""


async def coordinator(method: str, path: str, data: dict | None = None) -> dict:
    url = os.getenv("ANTHROPIC_CREDIT_POOL_URL", "").rstrip("/")
    token = os.getenv("ANTHROPIC_CREDIT_POOL_TOKEN", "")
    if not url or not token:
        raise PoolUnavailable(503, "Anthropic credit coordinator is not configured")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.request(method, url + "/v1/internal/api-credit-pool" + path,
                                     headers={"X-Credit-Pool-Token": token}, json=data)
        if r.status_code != 200:
            # Never log the body: /accounts carries API keys.
            fehler = PoolUnavailable(r.status_code if r.status_code in (409, 429) else 503,
                                     "Anthropic credit coordinator rejected request")
            # B2: Originalstatus fuer die Kette erhalten (400 = Vertrag/Konfiguration, nicht "Pool kann nicht zahlen").
            fehler.koordinator_status = r.status_code
            raise fehler
        return r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PoolUnavailable(503, "Anthropic credit coordinator unavailable") from exc


async def _finish(reservation: str, **data: Any) -> None:
    try:
        await coordinator("POST", "/settle", {"id": reservation, **data})
    except Exception:
        # A conservative pending debit survives process/network loss. Never
        # discard a paid successful response just because accounting is down.
        # The coordinator client maps 404 to 503, so the cause is not known here.
        if data.get("rejected"):
            log.error("Credit reservation %s: release not confirmed (coordinator unavailable, or the "
                      "reservation was never committed); if it was committed it stays reserved at its "
                      "upper bound and shows up as hanging", reservation)
        else:
            log.error("Credit reservation %s: settlement not stored; remains charged at its upper bound",
                      reservation)


async def _survive_cancel(coro) -> None:
    """Run ledger bookkeeping to completion even while the caller is being
    cancelled. A cancellation of the caller while waiting here is re-raised
    (never swallowed: a cancelled call must not continue with the next account
    or return a response normally); the shielded task keeps running in the
    background until the bookkeeping is done."""
    task = asyncio.ensure_future(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    await asyncio.shield(task)


async def _release_unsent(reservation: str) -> None:
    # Nothing reached Anthropic: free the budget. If /reserve itself never
    # committed, the coordinator answers 404 and _finish only logs.
    await _survive_cancel(_finish(reservation, rejected=True))


async def _nicht_gesendet(reservation: str, kette: dict | None, phase: str = "versand",
                          exc: BaseException | None = None) -> None:
    """Belegt nicht gesendet. Ohne Kette wie bisher freigeben. Mit Kette (B1, Root 01a1e624) bleibt die
    Reserve erhalten und die Zeile wird als unknown/nicht_gesendet markiert; aufloesen darf sie nur der
    auditierte Betreiberabgleich, nie der Client, nie ein Timeout. L4: Phase und Ausnahmeklasse als Beleg."""
    if kette is None:
        await _release_unsent(reservation)
    else:
        klasse = type(exc).__name__ if exc is not None else None
        # Restluecke F (Kanzler 17.11.): feste Worker-Logzeile, unabhaengig vom Koordinator. Der Betreiber
        # gibt sie als --beleg-ref an; "send receipt not stored" oder eine Antwortzeile zur ID widerlegt N.
        log.error("Credit reservation %s: NICHT_GESENDET phase=%s klasse=%s", reservation, phase, klasse)
        await _mark_unknown(reservation, "nicht_gesendet", phase=phase, detail=klasse)


def _request_id(response: httpx.Response) -> str | None:
    rid = response.headers.get("request-id")
    return rid if isinstance(rid, str) and re.fullmatch(r"[A-Za-z0-9_\-]{1,128}", rid) else None


async def _gesendet(reservation: str, response: httpx.Response) -> None:
    """L2 (Kanzler-Entscheid E3): Versandbeleg sofort nach Eingang der Anbieterantwort festhalten, VOR jeder
    Abrechnung oder Markierung. Danach ist eine Marke nicht_gesendet fuer diese Zeile widerlegt. Laeuft nur
    innerhalb von _kette_nachbearbeiten (eine abgeschirmte Aufgabe, K1)."""
    try:
        await coordinator("POST", "/gesendet", {"id": reservation, "request_id": _request_id(response),
                                                "status": response.status_code})
    except Exception:
        log.error("Credit reservation %s: send receipt not stored (request-id kept in later mark)", reservation)


async def _kette_nachbearbeiten(reservation: str, body: dict, kette: dict, response: httpx.Response) -> None:
    """K1 (Codepruefung 5f295d8): Versandbeleg UND Abrechnung bzw. Marke einer Kettenantwort in EINER
    abgeschirmten Aufgabe. Ein Abbruch des Aufrufers nach der bezahlten Antwort laesst die Zeile nie ohne
    /settle und ohne Marke zurueck."""
    rid = _request_id(response)
    await _gesendet(reservation, response)
    if response.status_code != 200:
        # Ob mit fallbacks vor einem 4xx schon ein Versuch lief, ist nicht belegt: nie freigeben (auch 5xx).
        await _mark_unknown(reservation, "http_status", request_id=rid)
        return
    try:
        antwort = response.json()
        usage = antwort["usage"]
    except (ValueError, KeyError, TypeError):
        log.error("Credit reservation %s: unreadable paid response; retained", reservation)
        await _mark_unknown(reservation, "unreadable_usage", request_id=rid)
        return
    await _kette_abrechnen(reservation, body, kette, antwort, usage, rid)


async def _mark_unknown(reservation: str, reason: str, **beleg: Any) -> None:
    daten = {"id": reservation, "reason": reason, **{k: v for k, v in beleg.items() if v is not None}}

    async def mark():
        try:
            await coordinator("POST", "/unknown", daten)
        except Exception:
            log.error("Credit reservation %s: outcome unknown (%s), marker not stored; "
                      "remains charged at its upper bound", reservation, reason)
    log.warning("Credit reservation %s: request may have reached Anthropic (%s); "
                "retained at upper bound, marked outcome unknown", reservation, reason)
    await _survive_cancel(mark())


# P-1 (Kanzler, R2 978bb4f; Schema-Nachtrag S1-4 von 51c8185c, MAPPING-ITERATIONEN-15-AUF-7 bf7d01d38c45):
# kanonische Iterationsform ist die von abschluss_felder()/fallback_bewerten(). Der Koordinator bleibt streng
# bei genau diesen 7 Schluesseln; der Client bildet ausdruecklich und ohne Lax-Umwandlung darauf ab.
ITER_SETTLE = ("type", "model", "input_tokens", "output_tokens", "cache_read_input_tokens",
               "cache_creation_input_tokens", "billed")
# Bekannte Zusatzfelder der S1-4-Form, die das Settlement nicht traegt (Begruendung im Ruecklauf P-1):
# index = Listenposition (geprueft), kategorienliste_version in cost_basis = Aufrufebene (geprueft), Rest Anzeige/Ledger.
ITER_S14_ZUSATZ = frozenset({"index", "stop_reason", "category", "recommended_model", "cost_eur",
                             "cost_schaetzung_eur", "cost_basis", "quelle"})


def iterationen_abbilden(roh: Any, kategorienliste_version: str) -> list[dict]:
    """S1-4-Iterationen (7er- oder 15er-Form) -> genau die 7 Settle-Schluessel. Wirft ValueError statt
    umzuwandeln: fehlendes Pflichtfeld, Token nicht int >= 0 (auch None = usage unvollstaendig), billed
    ausserhalb true|false|"unbekannt", unbekannter Zusatzschluessel, index != Position, abweichende
    kategorienliste_version. Folge beim Aufrufer: Marke bewertung_fehlt, Reserve bleibt."""
    if not isinstance(roh, list) or not roh:
        raise ValueError("iterations fehlen")
    aus = []
    for pos, it in enumerate(roh):
        if not isinstance(it, dict):
            raise ValueError(f"iteration {pos}: kein Objekt")
        fremd = set(it) - set(ITER_SETTLE) - ITER_S14_ZUSATZ
        if fremd:
            raise ValueError(f"iteration {pos}: unbekannte Schluessel {sorted(fremd)}")
        fehlt = [k for k in ITER_SETTLE if k not in it]
        if fehlt:
            raise ValueError(f"iteration {pos}: fehlt {fehlt}")
        if not isinstance(it["type"], str) or not isinstance(it["model"], str) or not it["model"]:
            raise ValueError(f"iteration {pos}: type/model")
        if any(type(it[k]) is not int or it[k] < 0 for k in ITER_SETTLE[2:6]):
            raise ValueError(f"iteration {pos}: Tokenfelder nicht int >= 0")
        b = it["billed"]
        if not (b is True or b is False or (isinstance(b, str) and b == "unbekannt")):
            raise ValueError(f"iteration {pos}: billed")
        if "index" in it and (type(it["index"]) is not int or it["index"] != pos):
            raise ValueError(f"iteration {pos}: index != Position")
        basis = it.get("cost_basis")
        if basis is not None and (not isinstance(basis, dict)
                                  or basis.get("kategorienliste_version", kategorienliste_version) != kategorienliste_version):
            raise ValueError(f"iteration {pos}: kategorienliste_version abweichend")
        aus.append({k: it[k] for k in ITER_SETTLE})
    return aus


async def _kette_abrechnen(reservation: str, body: dict, kette: dict, antwort: dict, usage: dict,
                          request_id: str | None = None) -> None:
    erlaubt = {_norm(m) for m in [body["model"], *kette["fallback_models"]]}
    if not isinstance(antwort.get("model"), str) or _norm(antwort["model"]) not in erlaubt:
        log.error("Credit reservation %s: answering model outside the chain; retained", reservation)
        await _mark_unknown(reservation, "modell_abweichung", request_id=request_id, usage=usage,
                            model=antwort.get("model"))
        return
    try:
        bewertung = kette["bewerter"](antwort)
        daten = {"usage": usage, "iterations": iterationen_abbilden(bewertung["iterations"],
                                                                   kette["kategorienliste_version"]),
                 "lage": bewertung["lage"],
                 "antwort_iteration": bewertung.get("antwort_iteration"),
                 "kategorienliste_version": kette["kategorienliste_version"]}
    except Exception:
        log.error("Credit reservation %s: chain evaluation failed; retained at upper bound", reservation)
        await _mark_unknown(reservation, "bewertung_fehlt", request_id=request_id, usage=usage,
                            model=antwort.get("model"))
        return
    await _survive_cancel(_finish(reservation, **daten))


class PinnedAccountLost(HTTPException):
    """The account a run is pinned to can no longer serve it (409). Raised only
    with ``pin``; never for a coordinator 409 or any other pool failure, so a
    caller can tell "pinned account gone" apart from every other refusal."""

    def __init__(self, detail: str):
        super().__init__(409, detail)


def _account_bound_400(response: httpx.Response) -> bool:
    """A 400 that depends on the ACCOUNT, not on the request: blocks carrying
    encrypted_content of another organisation, or an empty credit balance.
    Every other 400 is the request itself and is the same on every account."""
    if response.status_code != 400:
        return False
    text = response.text.lower()
    return "encrypted_content" in text or "credit balance" in text


def _norm(model: str) -> str:
    from src.pricing import normalize_model_id
    return re.sub(r"-\d{8}$", "", normalize_model_id(model))


def _opt_in_pruefen(body: dict, anthropic_version: str | None, extra_headers: dict | None,
                    kette: dict | None) -> tuple[str, dict]:
    """Sofortmassnahme + PL-1/PV-3. Rueckgabe (Version, zusaetzliche Koepfe). Ohne Opt-in: (VERSION, {})."""
    if kette is None:
        if "fallbacks" in body:
            raise HTTPException(400, "pool_settlement_je_modell_fehlt: fallbacks require a chain contract")
        if anthropic_version is not None or extra_headers:
            raise HTTPException(400, "fallback_body_unvertraeglich: version/headers only with a chain")
        return VERSION, {}
    modelle = kette.get("fallback_models")
    if (not isinstance(modelle, list) or not modelle or not all(isinstance(m, str) and m for m in modelle)
            or not isinstance(kette.get("kategorienliste_version"), str) or not kette["kategorienliste_version"]
            or kette.get("vorpruefung_c4") is not True or not callable(kette.get("bewerter"))
            or "fallbacks" not in body):
        raise HTTPException(400, "fallback_body_unvertraeglich: incomplete chain contract")
    if body["fallbacks"] != "default":
        # A1 (Root 01a1e624): nur der Anbieter-Default; Listen und Ueberschreibungen je Eintrag sind laut verboten.
        raise HTTPException(400, "fallbacks_liste_nicht_erlaubt: only fallbacks=\"default\" is supported")
    version = anthropic_version or VERSION
    if version not in VERSIONS_ERLAUBT:
        raise HTTPException(400, "fallback_body_unvertraeglich: anthropic-version not allowed")
    koepfe: dict = {}
    for name, wert in (extra_headers or {}).items():
        if (str(name).lower(), wert) != KOPF_ERLAUBT or koepfe:
            raise HTTPException(400, "kopf_nicht_erlaubt")
        koepfe[KOPF_ERLAUBT[0]] = KOPF_ERLAUBT[1]
    return version, koepfe


async def post_messages(body: dict, timeout: float, pin: dict | None = None, *,
                        anthropic_version: str | None = None, extra_headers: dict | None = None,
                        kette: dict | None = None) -> tuple[httpx.Response, dict]:
    """``pin``: Zustand EINES Laufs (leeres dict). Der erste erfolgreiche Aufruf
    heftet das Konto an; jeder Folgeaufruf darf nur dieses Konto nehmen, weil
    encrypted_content (web_search_tool_result) an Organisation/Schluessel des
    erzeugenden Aufrufs gebunden ist. Ist das Konto nicht mehr verfuegbar oder
    lehnt es ab: PinnedAccountLost (409), laut, kein Wechsel auf ein anderes Konto.

    count_tokens mit ``pin``: Vor der Anheftung darf ein kontogebundener 400
    (_account_bound_400) aufs naechste Konto gehen; es gibt noch keinen Block,
    der an ein Konto gebunden waere. Nach der Anheftung ist jeder kontogebundene
    400 des angehefteten Kontos ein laut gemeldeter PinnedAccountLost. Ohne
    ``pin`` (Vision, Lueckenpruefung) bleibt alles wie bisher: der 400 geht
    unveraendert zurueck.

    ``kette`` (Server-Fallback, Entwurf V2 E3): ``{fallback_models, kategorienliste_version,
    vorpruefung_c4: True, bewerter}``. ``bewerter(antwort_json) -> {iterations, lage,
    antwort_iteration}`` liefert die Bridge (51c8185c): billed je Iteration, der Pool leitet nichts ab.
    Ohne ``kette`` ist ein Body mit ``fallbacks`` gesperrt (400), alles andere byte-gleich."""
    version, zusatz_koepfe = _opt_in_pruefen(body, anthropic_version, extra_headers, kette)
    if body.get("stream") or body.get("speed") or body.get("inference_geo") or body.get("container"):
        raise HTTPException(400, "Credit pool supports standard non-streaming text/vision requests")
    server_steps = 0
    for tool in body.get("tools", []):
        if "type" not in tool:
            continue  # Client schemas execute no provider-side tool.
        if tool["type"] not in ("web_search_20250305", "web_fetch_20250910"):
            raise HTTPException(400, "Unpriced server tool; promotional_only stopped")
        uses = tool.get("max_uses")
        if type(uses) is not int or not 1 <= uses <= 3:
            raise HTTPException(400, "Server tools require max_uses between 1 and 3")
        server_steps += uses
    if kette is not None and server_steps:
        # A2 (Root 01a1e624): Server-Tool-Gebuehren je Iteration sind nicht belegt -> keine Server-Tools mit Kette.
        raise HTTPException(400, "server_tools_mit_kette_gesperrt")
    accounts = (await coordinator("GET", "/accounts"))["accounts"]
    pinned = (pin or {}).get("account_id")
    if pinned:
        accounts = [a for a in accounts if a["id"] == pinned]
        if not accounts:
            raise PinnedAccountLost(f"pinned credit account {pinned} unavailable; encrypted_content of this run "
                                    "is bound to it, no switch to another account")
    async with httpx.AsyncClient(timeout=timeout) as client:
        for account in accounts:
            headers = {"x-api-key": account["api_key"], "anthropic-version": version}
            counted = {k: body[k] for k in ("model", "messages", "system", "thinking", "tools", "tool_choice") if k in body}
            try:
                count = await client.post(API + "/count_tokens", headers=headers, json=counted)
            except httpx.HTTPError as exc:
                raise HTTPException(503, "Anthropic token count unavailable; no credit reserved") from exc
            if count.status_code in (401, 403, 429):
                continue
            if count.status_code != 200:
                if pin is not None and _account_bound_400(count):
                    if pinned:
                        raise PinnedAccountLost(
                            f"pinned credit account {pinned} rejected count_tokens (HTTP 400, account-bound: "
                            "encrypted_content/credit balance); no switch to another account")
                    continue
                return count, {}
            tokens = count.json().get("input_tokens")
            if type(tokens) is not int or tokens < 0:
                raise HTTPException(502, "Anthropic returned an invalid input token count")

            def _mit_werkzeug(n: int) -> int:
                # Reserve whole context windows for internal tool turns,
                # plus output ceilings and search fees conservatively as
                # token budget. No extra fee column / idempotency gap.
                return max(n, 2_000_000 * (server_steps + 1)) if server_steps else n

            reserve = {"id": "", "account_id": account["id"], "grant_id": account["grant_id"],
                       "model": body["model"], "input_tokens": _mit_werkzeug(tokens),
                       "max_tokens": body["max_tokens"] * (server_steps + 1)}
            if kette is not None:
                # R5: count_tokens je Kettenmodell Pflicht (kostenlos, eigenes RPM-Kontingent; Beleg
                # BELEG-R5-COUNT-TOKENS-KOSTEN-RPM-c9a50d3b-20261117.md). Kein Faktor, keine Schaetzung.
                je_modell = {body["model"]: _mit_werkzeug(tokens)}
                naechstes_konto = False
                for m in kette["fallback_models"]:
                    try:
                        c2 = await client.post(API + "/count_tokens", headers=headers, json={**counted, "model": m})
                    except httpx.HTTPError as exc:
                        raise HTTPException(503, "Anthropic token count unavailable; no credit reserved") from exc
                    if c2.status_code in (401, 403, 429):
                        naechstes_konto = True
                        break
                    t2 = c2.json().get("input_tokens") if c2.status_code == 200 else None
                    if type(t2) is not int or t2 < 0:
                        raise HTTPException(502, f"count_tokens for chain model failed (HTTP {c2.status_code}); "
                                                 "no credit reserved")
                    je_modell[m] = _mit_werkzeug(t2)
                if naechstes_konto:
                    continue
                reserve.update({"fallback_models": list(kette["fallback_models"]), "input_tokens_je_modell": je_modell,
                                "fallbacks_modus": "default", "anthropic_version": version,
                                "anthropic_beta": zusatz_koepfe.get(KOPF_ERLAUBT[0]),
                                "kategorienliste_version": kette["kategorienliste_version"]})
            reservation = str(uuid.uuid4())
            reserve["id"] = reservation
            try:
                await coordinator("POST", "/reserve", reserve)
            except HTTPException as exc:
                if exc.status_code in (409, 429):
                    continue
                if kette is not None and getattr(exc, "koordinator_status", None) == 400:
                    # B2 (Codepruefung ca6cbcb R1): Der Koordinator hat den Kettenvertrag abgelehnt (Deckel fehlt,
                    # Kette zu lang, count_tokens_fehlt, unbepreist). /reserve prueft das VOR der Transaktion, es gibt
                    # also keine Zeile. Laut als Vertragsfehler, NICHT als PoolUnavailable (kein Rueckfall eines
                    # Aufrufers auf einen anderen Finanzierungsweg). Ohne Kette unveraendert.
                    log.error("Credit reservation %s: chain contract rejected by coordinator (400); nothing sent",
                              reservation)
                    raise HTTPException(400, "pool_kettenvertrag_abgelehnt") from exc
                # A coordinator timeout may still have committed the row.
                await _nicht_gesendet(reservation, kette, "reserve", exc)
                raise
            except asyncio.CancelledError as exc:
                await _nicht_gesendet(reservation, kette, "reserve", exc)
                raise
            # Committed: from here on the pool table has a row for it, whatever
            # its later state (settled / released / unknown).
            _note_booking(reservation, account["grant_id"])
            # No retry after a transport failure or 5xx: it may already have
            # generated a paid answer. Keep the reservation, fail visibly.
            try:
                response = await client.post(API, headers={**headers, **zusatz_koepfe}, json=body)
            except NOT_SENT_ERRORS as exc:
                await _nicht_gesendet(reservation, kette, "versand", exc)
                raise
            except asyncio.CancelledError:
                await _mark_unknown(reservation, "cancelled")
                raise
            except httpx.HTTPError:
                await _mark_unknown(reservation, "transport_error")
                raise
            except Exception:
                await _mark_unknown(reservation, "error")
                raise
            meta = {"credit_pool_account": account["id"], "credit_pool_org": account["org_id"],
                    "credit_pool_grant": account["grant_id"], "credit_pool_reservation": reservation,
                    "funding_policy": "promotional_only", "automatic_paid_fallback": False}
            if kette is not None:
                if response.status_code == 200 and pin is not None:
                    pin.setdefault("account_id", account["id"])
                await _survive_cancel(_kette_nachbearbeiten(reservation, body, kette, response))
                return response, meta
            if response.status_code == 200:
                if pin is not None:
                    pin.setdefault("account_id", account["id"])
                try:
                    antwort = response.json()
                    usage = antwort["usage"]
                except (ValueError, KeyError, TypeError):
                    log.error("Credit reservation %s: unreadable paid response; retained", reservation)
                    await _mark_unknown(reservation, "unreadable_usage")
                else:
                    await _survive_cancel(_finish(reservation, usage=usage))
                return response, meta
            if 400 <= response.status_code < 500:
                await _survive_cancel(_finish(reservation, rejected=True))
                if pinned and response.status_code == 400 and "encrypted_content" in response.text.lower():
                    raise PinnedAccountLost(
                        f"pinned credit account {pinned} rejected encrypted_content of its own run (HTTP 400); "
                        "no switch to another account")
                exhausted = response.status_code == 400 and "credit balance" in response.text.lower()
                if exhausted or response.status_code in (401, 403, 429):
                    continue
            else:
                await _mark_unknown(reservation, "http_status")
            return response, meta
    if pinned:
        raise PinnedAccountLost(f"pinned credit account {pinned} refused (count/reserve/limit); no switch to "
                                "another account because of encrypted_content")
    raise PoolUnavailable(429, "No eligible Anthropic API credit budget available; paid fallback disabled")
