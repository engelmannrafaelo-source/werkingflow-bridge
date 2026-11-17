"""Anthropic-Hauptpfad fuer GEBUNDENE Sandbox-Leases ueber die Bridge (SEC/Kanzler 17.11.2026, Wire-Vertrag v2).

Bisher gab POST /v1/sandbox/lease-token das langlebige Abo-OAuth des Pool-Kontos im Klartext aus; der Daemon
setzte es als ANTHROPIC_AUTH_TOKEN in den Sandbox-Container, und Lease-Ende/Widerruf beendeten es nicht.
Fuer gebundene Aufrufer (bpx) verlaesst das OAuth die Bridge nicht mehr:

  POST /v1/sandbox/lease-token/{lease_id}/anthropic/v1/messages
    Auth: X-Bridge-Service-Token: <bpx> (strikter Verifier: Principal aktiv/unabgelaufen, Mandant, App,
          Route im Scope, Lease gehoert dem Principal) - nur kind=bound.
    Je Aufruf ohne Cache: Lease aktiv (sonst 410), Kostendeckel (402), Modell bepreist (422).
    Weiterleitung an api.anthropic.com mit dem OAuth des Lease-Kontos (nur im Prozess), Kopf-Allowlist
    ein/aus, eingehende authorization/x-api-key werden NIE weitergereicht, Antwort gestreamt (SSE),
    Usage verbucht die Bridge selbst (source sandbox, lease_id, Idempotenz ueber request-id).
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from src import principals as _PR
from src.api_auth import AuthClaims
from src.api_auth.deps import require_service_or_bound
from src.db.client import get_pool
from src.sandbox import lease_service as _ls
from src.sandbox.pricing import compute_hypothetical_cost_eur

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sandbox", tags=["sandbox"])

ANTHROPIC_BASE = "https://api.anthropic.com"
ROUTE = "/lease-token/{lease_id}/anthropic/v1/messages"
# Eingehende Koepfe, die weitergereicht werden (alles andere, v. a. authorization/x-api-key, nie).
KOPF_EIN = ("anthropic-version", "anthropic-beta", "content-type", "accept")
# Ausgehende Koepfe an den Aufrufer.
KOPF_AUS = ("content-type", "request-id", "retry-after")
KOPF_AUS_PRAEFIX = ("anthropic-ratelimit-",)
UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)
# Konservative Buchung bei unvollstaendigem Strom (R1 N1, 18962da3): fehlt das Ende (message_delta bzw.
# message_stop, oder bei JSON eine lesbare usage), bucht die Bridge output = max(beobachtet, max_tokens).
# max_tokens ist bei /v1/messages Pflicht (sonst 400 upstream, keine Buchung); fehlt es oder ist es
# ungueltig, gilt dieser Default (hoechstes Ausgabelimit der aktuellen Modellfamilie).
MAX_TOKENS_DEFAULT_KONSERVATIV = 64000


def _client() -> httpx.AsyncClient:
    """Eigene Fabrik, damit Proben eine Attrappe (ASGITransport) einsetzen koennen."""
    return httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT)


async def _aktive_lease(lease_id: uuid.UUID) -> Dict[str, Any]:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT account_id, user_id, tenant_id, app,
                   (released_at IS NULL AND expires_at > NOW()) AS aktiv
              FROM sandbox_leases WHERE lease_id = $1
            """,
            lease_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "lease_unbekannt"})
    if not row["aktiv"]:
        raise HTTPException(status_code=410, detail={"error": "lease_beendet"})
    return dict(row)


def _usage_aus_json(data: Any) -> Dict[str, int]:
    u = data.get("usage") if isinstance(data, dict) else None
    return _usage_felder(u)


def _usage_felder(u: Any) -> Dict[str, int]:
    out = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    if isinstance(u, dict):
        for k in out:
            v = u.get(k)
            if isinstance(v, int) and v >= 0:
                out[k] = v
    return out


class _SseZaehler:
    """Liest Usage aus dem SSE-Strom (message_start: input/cache; message_delta: output kumulativ)."""

    def __init__(self) -> None:
        self.puffer = b""
        self.ende = False      # message_delta (finale kumulierte usage) oder message_stop gesehen
        self.start = False     # message_start gesehen (input/cache bekannt)
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0}

    def futter(self, chunk: bytes) -> None:
        self.puffer += chunk
        while b"\n" in self.puffer:
            zeile, self.puffer = self.puffer.split(b"\n", 1)
            zeile = zeile.strip()
            if not zeile.startswith(b"data:"):
                continue
            try:
                ev = json.loads(zeile[5:].strip() or b"{}")
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "message_stop":
                self.ende = True
            elif ev.get("type") == "message_start":
                self.start = True
                u = _usage_felder((ev.get("message") or {}).get("usage"))
                for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                    self.usage[k] = max(self.usage[k], u[k])
                self.usage["output_tokens"] = max(self.usage["output_tokens"], u["output_tokens"])
            elif ev.get("type") == "message_delta":
                self.ende = True
                u = _usage_felder(ev.get("usage"))
                self.usage["output_tokens"] = max(self.usage["output_tokens"], u["output_tokens"])
                for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                    self.usage[k] = max(self.usage[k], u[k])


async def _verbuchen(lease: Dict[str, Any], lease_id: uuid.UUID, model: str, usage: Dict[str, int],
                     request_id: Optional[str]) -> None:
    """Serverseitige Usage-Buchung. Fehler werden laut geloggt (die Antwort ist schon gestreamt);
    die Lease-Minuten-Untergrenze des Deckels (H-KD-1) zaehlt unabhaengig davon."""
    try:
        kosten = compute_hypothetical_cost_eur(
            model=model,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cache_read_tokens=usage["cache_read_input_tokens"],
            cache_creation_tokens=usage["cache_creation_input_tokens"],
        )
        async with get_pool().acquire() as conn:
            info = await _ls.get_tenant_info(conn, lease["user_id"], auto_provision=False)
            await _ls.record_usage(
                conn,
                litellm_call_id=f"bridge-proxy:{request_id or uuid.uuid4()}",
                user_id=lease["user_id"],
                tenant_id=lease["tenant_id"],
                session_id=f"lease:{lease_id}",
                lease_id=lease_id,
                account_id=lease["account_id"],
                app=lease["app"],
                model=model,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cache_read_tokens=usage["cache_read_input_tokens"],
                cache_creation_tokens=usage["cache_creation_input_tokens"],
                hypothetical_cost_eur=kosten,
                billing_mode=info["billing_mode"],
            )
    except Exception:  # silent-ok: Buchungsfehler nach gestreamter Antwort - laut geloggt, Deckel zaehlt Lease-Minuten
        logger.exception("bound proxy usage booking failed lease=%s", lease_id)


def _max_tokens(body: Dict[str, Any]) -> int:
    v = body.get("max_tokens")
    if isinstance(v, int) and not isinstance(v, bool) and v > 0:
        return v
    return MAX_TOKENS_DEFAULT_KONSERVATIV


def _konservativ(usage: Dict[str, int], max_tokens: int, eingabe_bekannt: bool,
                 anfrage_bytes: int) -> Dict[str, int]:
    """Unvollstaendiger Strom: Ausgabe mindestens max_tokens; Eingabe, falls nie gemeldet,
    mindestens die Anfragegroesse in Bytes (obere Schranke fuer Text-Tokens)."""
    out = dict(usage)
    out["output_tokens"] = max(out["output_tokens"], max_tokens)
    if not eingabe_bekannt:
        out["input_tokens"] = max(out["input_tokens"], anfrage_bytes)
    return out


def _kopf_aus(h: httpx.Headers, status: int) -> Dict[str, str]:
    out = {k: v for k, v in h.items() if k.lower() in KOPF_AUS or k.lower().startswith(KOPF_AUS_PRAEFIX)}
    out["X-Accel-Buffering"] = "no"
    out["Cache-Control"] = "no-cache"
    if status == 429:
        out["x-bridge-lease-swap"] = "1"
    return out


@router.post(ROUTE)
async def anthropic_messages(
    lease_id: uuid.UUID,
    request: Request,
    claims: AuthClaims = Depends(require_service_or_bound),
):
    # Nur gebundene Aufrufer: Operator/Service bekommen weiter ihren alten Weg, nicht diesen.
    if claims.kind != "bound":
        raise HTTPException(status_code=403, detail={"error": "nur_gebunden"})
    bp = getattr(request.state, "bound_principal", None)
    if bp is None:
        raise HTTPException(status_code=403, detail={"error": "bound_principal_missing"})
    lease = await _aktive_lease(lease_id)
    await _PR.enforce_bound_cap(bp)

    raw = await request.body()
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("model"), str):
        raise HTTPException(status_code=422, detail={"error": "body_ungueltig"})
    from src.pricing import is_priced
    if not is_priced(body["model"]):
        raise HTTPException(status_code=422, detail={"error": "modell_ohne_preis"})

    oauth = _ls.read_oauth_token(lease["account_id"])   # nur im Prozess, nie in Antwort/Log
    kopf = {k: v for k, v in request.headers.items() if k.lower() in KOPF_EIN}
    kopf["authorization"] = f"Bearer {oauth}"
    del oauth

    client = _client()
    try:
        upstream_req = client.build_request(
            "POST", f"{ANTHROPIC_BASE}/v1/messages", params=dict(request.query_params), headers=kopf, content=raw)
        resp = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.error("bound proxy upstream unreachable lease=%s: %s", lease_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail={"error": "upstream_nicht_erreichbar"})

    status = resp.status_code
    ctype = resp.headers.get("content-type", "")
    request_id = resp.headers.get("request-id")
    model = body["model"]
    max_tok = _max_tokens(body)
    kopf_aus = _kopf_aus(resp.headers, status)

    async def strom() -> AsyncIterator[bytes]:
        zaehler = _SseZaehler() if "text/event-stream" in ctype else None
        gesammelt: List[bytes] = []
        try:
            async for chunk in resp.aiter_bytes():   # dekodiert (content-encoding geht nicht weiter)
                if zaehler is not None:
                    zaehler.futter(chunk)
                elif status == 200:
                    gesammelt.append(chunk)
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()
            if status == 200:
                if zaehler is not None:
                    usage = zaehler.usage
                    if not zaehler.ende:
                        usage = _konservativ(usage, max_tok, zaehler.start, len(raw))
                else:
                    try:
                        daten = json.loads(b"".join(gesammelt) or b"{}")
                    except ValueError:
                        daten = None
                    vollstaendig = isinstance(daten, dict) and isinstance(daten.get("usage"), dict)
                    usage = _usage_aus_json(daten)
                    if not vollstaendig:
                        usage = _konservativ(usage, max_tok, False, len(raw))
                await _verbuchen(lease, lease_id, model, usage, request_id)

    return StreamingResponse(strom(), status_code=status, headers=kopf_aus, media_type=None)
