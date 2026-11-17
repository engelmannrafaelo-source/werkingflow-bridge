"""Server-side fallback of the provider: capability and fallback chains (S1–S3).

Owner: Bridge-Owner 51c8185c (Festlegung FESTLEGUNG-S1-OWNER-HUNKS-51c8185c-20261117.md).
Schema: SCHEMA-ABLEHNUNG-FALLBACK-51c8185c-20261117.md, S-5 to S-9.

A caller orders the provider's default fallback per request with
``provider_fallback: "default"``. The provider then may answer a classifier
refusal with a model it recommends. This is NOT a bridge-side retry or model
switch: the provider decides inside one API call, and the bridge reports
every attempt (``x_bridge_finish.iterations``) and never presents a fallback
answer as a normal one.

``enabled`` is DERIVED, never a parked switch:
  1. the credit pool is active (the only path that settles per model),
  2. the pool client can settle a chain (``credit_pool.post_messages`` takes
     ``kette`` — pool candidate of c9a50d3b), and
  3. the fallback chains were read from the Models API.
Only when 1 and 2 hold does this module contact the provider at all (a
metadata GET, free). On a bridge without the pool chain code it never does.

The chain of a model is ``allowed_fallback_models`` from the Models API with
the beta header (measured 17.11.2026, BEFUND-C1-C3-C4-FALLBACK f9a0645a94b8:
claude-sonnet-5-5 -> [claude-sonnet-5], claude-haiku-5-5 -> []). That the
``default`` mode picks its target from this list is DERIVED, not stated by the
provider ("not published for each model") — every answer is checked against
it (vision_provider.abschluss_felder).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from typing import Any, Dict, List, Optional

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

MODUS = "default"
PFAD = "claude-direct-notools"
BETA_KOPF = ("anthropic-beta", "server-side-fallback-2026-07-01")
API_VERSION_OPT_IN = "2026-10-21"
MODELS_URL = "https://api.anthropic.com/v1/models?limit=100"
#: Deckel fuer Versuche je Aufruf (1 + bis zu drei Listenmodelle, Doku Z. 504):
#: Konfigurationsgrenze, nicht zitiert. Die belegte Grenze je Modell ist
#: 1 + len(allowed_fallback_models).
ITERATIONEN_DECKEL = 4
KETTEN_TTL_S = 6 * 3600
KETTEN_TIMEOUT_S = 5.0

_cache: Dict[str, Any] = {"ketten": None, "gelesen_am": None, "gelesen_mono": 0.0}
_lock = asyncio.Lock()


class FallbackNichtMoeglich(HTTPException):
    """A fallback request that cannot be served. Always a loud, non-retryable
    4xx with a code — never a silent normal call without the fallback."""

    def __init__(self, code: str, grund: str, status: int = 400):
        self.code = code
        self.grund = grund
        # Flache Form: der HTTPException-Handler (main.py) macht daraus den
        # Bridge-Umschlag mit fehler_code/grund im error-Objekt, retryable false.
        super().__init__(status_code=status, detail={
            "message": f"provider_fallback: {code} ({grund})",
            "fehler_code": code,
            "grund": grund,
            "retryable": False,
        })


def _norm(model: str) -> str:
    from src.pricing import normalize_model_id
    return re.sub(r"-\d{8}$", "", normalize_model_id(model))


def pool_kann_kette() -> bool:
    """True iff the pool client in this build can settle a chain."""
    from src.providers import credit_pool
    try:
        return "kette" in inspect.signature(credit_pool.post_messages).parameters
    except (TypeError, ValueError):
        return False


def _vorbedingung() -> Optional[str]:
    from src.providers import credit_pool
    if not credit_pool.enabled():
        return "pool_aus"
    if not pool_kann_kette():
        return "pool_settlement_je_modell_fehlt"
    return None


async def _ketten_lesen() -> Dict[str, List[str]]:
    """allowed_fallback_models per model, from the first pool account (read only)."""
    from src.providers import credit_pool
    konten = (await credit_pool.coordinator("GET", "/accounts")).get("accounts") or []
    if not konten:
        raise RuntimeError("no pool account to read the Models API")
    kopf = {"x-api-key": konten[0]["api_key"], "anthropic-version": "2023-06-01",
            BETA_KOPF[0]: BETA_KOPF[1]}
    async with httpx.AsyncClient(timeout=KETTEN_TIMEOUT_S) as client:
        antwort = await client.get(MODELS_URL, headers=kopf)
    if antwort.status_code != 200:
        raise RuntimeError(f"Models API HTTP {antwort.status_code}")
    daten = antwort.json()
    ketten: Dict[str, List[str]] = {}
    for eintrag in daten.get("data") or []:
        if not isinstance(eintrag, dict) or not isinstance(eintrag.get("id"), str):
            continue
        ziele = eintrag.get("allowed_fallback_models")
        if not isinstance(ziele, list):
            # Without the field the beta header did not take: no chain known.
            raise RuntimeError("Models API without allowed_fallback_models (beta header not applied)")
        ketten[_norm(eintrag["id"])] = [z for z in ziele if isinstance(z, str) and z]
    if daten.get("has_more"):
        raise RuntimeError("Models API has_more: chain list incomplete")
    if not ketten:
        raise RuntimeError("Models API returned no models")
    return ketten


async def ketten(*, auffrischen: bool = True) -> Optional[Dict[str, List[str]]]:
    """Cached chains, refreshed when stale (only if the preconditions hold).
    None = unknown (logged loud)."""
    if _vorbedingung() is not None:
        return None
    frisch = _cache["ketten"] is not None and time.monotonic() - _cache["gelesen_mono"] < KETTEN_TTL_S
    if frisch or not auffrischen:
        return _cache["ketten"]
    async with _lock:
        if _cache["ketten"] is not None and time.monotonic() - _cache["gelesen_mono"] < KETTEN_TTL_S:
            return _cache["ketten"]
        try:
            gelesen = await _ketten_lesen()
        except Exception as e:  # noqa: BLE001 — unknown chains disable the feature, loud
            logger.error("provider_fallback: Ketten nicht lesbar (%s: %s) — Fallback nicht verfuegbar",
                         type(e).__name__, e)
            return _cache["ketten"] if _cache["ketten"] is not None else None
        _cache.update(ketten=gelesen, gelesen_mono=time.monotonic(),
                      gelesen_am=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        return gelesen


def kette_fuer(alle: Dict[str, List[str]], model: str) -> Optional[List[str]]:
    return alle.get(_norm(model))


async def faehigkeit() -> Dict[str, Any]:
    """The provider_fallback section of GET /health (schema S-8)."""
    from src.vision_provider import BERECHNETE_ABLEHNUNGSKATEGORIEN, _build_sha
    grund = _vorbedingung()
    alle = None
    if grund is None:
        alle = await ketten()
        if alle is None:
            grund = "ketten_unbekannt"
    mit_kette = {m: z for m, z in (alle or {}).items() if z}
    return {
        "enabled": grund is None,
        "grund": grund,
        "modus": [MODUS],
        "pfade": [PFAD],
        "beta": BETA_KOPF[1],
        "api_version": API_VERSION_OPT_IN,
        "fallback_iterationen_max": ITERATIONEN_DECKEL,
        "fallback_iterationen_max_quelle": "konfiguration (1+3, Doku Z. 504); je Modell 1+len(kette)",
        "ketten": mit_kette,
        "ketten_quelle": "models_api_abgeleitet",
        "ketten_gelesen_am": _cache["gelesen_am"],
        "haiku_ausgenommen": True,
        "kategorienliste_version": BERECHNETE_ABLEHNUNGSKATEGORIEN["version"],
        "build_sha": _build_sha(),
    }


async def bestellung_pruefen(model: str) -> List[str]:
    """Check an order for ``provider_fallback: "default"`` on ``model``.
    Returns the chain; raises FallbackNichtMoeglich otherwise."""
    grund = _vorbedingung()
    if grund is not None:
        raise FallbackNichtMoeglich("fallback_not_enabled", grund)
    alle = await ketten()
    if alle is None:
        raise FallbackNichtMoeglich("fallback_not_enabled", "ketten_unbekannt")
    kette = kette_fuer(alle, model)
    if kette is None:
        raise FallbackNichtMoeglich("fallback_nicht_verfuegbar_modell", f"modell_unbekannt:{model}")
    if not kette:
        # Haiku 5.5 and all older models: no server-side fallback (Doku Z. 37).
        raise FallbackNichtMoeglich("fallback_nicht_verfuegbar_modell", f"keine_kette:{model}")
    from src.pricing import is_priced
    unbepreist = [z for z in kette if not is_priced(z)]
    if unbepreist:
        raise FallbackNichtMoeglich("fallback_nicht_verfuegbar_modell", f"zielmodell_ohne_preis:{unbepreist}")
    if 1 + len(kette) > ITERATIONEN_DECKEL:
        raise FallbackNichtMoeglich("fallback_nicht_verfuegbar_modell",
                                    f"kette_laenger_als_deckel:{1 + len(kette)}>{ITERATIONEN_DECKEL}")
    return list(kette)


def body_vorpruefen(body: Dict[str, Any]) -> None:
    """Pre-check C4 for API version 2026-10-21 (Beleg BELEG-S1-3-MIGRATION
    b0e0e525c4b0, Z. 115/119/121/127). Never strips anything: an incompatible
    body is a loud 400 ``fallback_body_unvertraeglich``."""
    funde: List[str] = []
    for feld in ("temperature", "top_p", "top_k"):
        if feld in body:
            funde.append(f"{feld} (Z. 127)")
    for i, nachricht in enumerate(body.get("messages") or []):
        inhalt = nachricht.get("content") if isinstance(nachricht, dict) else None
        if not isinstance(inhalt, list):
            continue
        rolle = nachricht.get("role")
        for block in inhalt:
            if not isinstance(block, dict):
                continue
            if rolle == "assistant" and block.get("type") == "document":
                funde.append(f"messages[{i}]: document in assistant (Z. 115)")
            if rolle == "user":
                kandidaten = [block]
                if block.get("type") == "search_result" and isinstance(block.get("content"), list):
                    kandidaten += [b for b in block["content"] if isinstance(b, dict)]
                for b in kandidaten:
                    if b.get("type") == "text" and isinstance(b.get("citations"), list) and b["citations"]:
                        funde.append(f"messages[{i}]: citations in user text (Z. 119/121)")
    if funde:
        raise FallbackNichtMoeglich("fallback_body_unvertraeglich", "; ".join(funde))
