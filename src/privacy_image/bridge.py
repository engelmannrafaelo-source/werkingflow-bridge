"""Bridge-Endpunkte und Chat-Pruefstelle der Bild-PII-Maske (Bildvertrag v3/v4, Schnittstelle Bild v3).

  image_mask(request)     POST /v1/privacy/image-mask     Flags, Principal-Zaun, strenge Feldpruefung, Weiterleitung der
                                                          ROHEN Bytes an den Privacy-Dienst, bei final: sha-Nachrechnung
                                                          + final_token. Cache-Control: no-store.
  image_receipt(request)  POST /v1/privacy/image-receipt  Token, Principal/Tenant, sha/MIME, Zeit (+-60 s), leer ->
                                                          Zweitbestaetigung; ein Token = ein Receipt (409).
  chat_pruefen(request)   im Chat-Handler fuer die Schema-App: None = reiner Text (unveraendert); sonst Pruefung
                          Selbstaufruf/Job/Receipt/Principal/Tenant/sha und danach §4 fail-closed (403 image_route_not_allowed).

Nie Inhalte in Logs oder Fehlern (nur Codes, Laengen, sha12).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from src.privacy_image import formular, pruefung, token
from src.privacy_image.fehler import BildFehler

logger = logging.getLogger(__name__)

FLAG_ENV = "BRIDGE_PRIVACY_IMAGE_MASK"
KOPF = {"Cache-Control": "no-store"}
#: §4 (Bildvertrag v3/v4, Kanzler 18:09:51Z): KEIN Bildweg ist freigegeben, bis Rafael/SEC entscheiden. Bewusst
#: hart kodiert und leer (kein Env-Schalter, der den Weg still oeffnen koennte). Aenderung nur per Code + Review.
FREIGEGEBENE_BILDROUTEN: frozenset = frozenset()


def _fehler(f: BildFehler) -> JSONResponse:
    return JSONResponse(status_code=f.status, content=f.body(), headers=KOPF)


def _an() -> bool:
    return (os.environ.get("BRIDGE_ANONYMIZE_ENABLED", "").lower() == "true"
            and os.environ.get(FLAG_ENV, "").lower() == "true")


def _zaun(request: Request):
    """Nur ein nicht-legacy Principal, der GENAU die Schema-App darf, mit X-App-ID der Schema-App. -> (name, tenant)."""
    p = getattr(request.state, "principal", None)
    app = (request.headers.get("x-app-id") or "").strip().lower()
    if p is None or getattr(p, "is_legacy", False) or app != pruefung.SCHEMA_APP_ID \
            or list(getattr(p, "allowed_apps", []) or []) != [pruefung.SCHEMA_APP_ID]:
        raise BildFehler(403, "image_mask_principal_not_allowed")
    tenant = (request.headers.get("x-tenant-id") or "").strip()
    if not tenant:
        raise BildFehler(403, "image_mask_principal_not_allowed")
    return p.name, tenant


async def _koerper(request: Request, grenze: int) -> bytes:
    laenge = int(request.headers.get("content-length") or 0)
    if laenge > grenze:
        raise BildFehler(413, "image_too_large")
    roh = b""
    async for stueck in request.stream():
        roh += stueck
        if len(roh) > grenze:
            raise BildFehler(413, "image_too_large")
    return roh


async def image_mask(request: Request, privacy_client, privacy_timeout) -> JSONResponse:
    try:
        if not _an():
            raise BildFehler(404, "feature_disabled")
        principal, tenant = _zaun(request)
        ct = request.headers.get("content-type", "")
        roh = await _koerper(request, formular.MAX_KOERPER_BYTES)
        anfrage = formular.maske_anfrage(formular.multipart_teile(ct, roh))   # Bridge prueft selbst, Dienst erneut
        try:
            resp = await privacy_client.post("/image-mask", content=roh, headers={"content-type": ct},
                                             timeout=privacy_timeout(120.0))
        except Exception as e:  # noqa: BLE001 — Dienst nicht erreichbar: kein Rueckfall auf ungepruefte Bilder
            logger.error("image-mask: Privacy-Dienst nicht erreichbar (%s)", type(e).__name__)
            raise BildFehler(503, "image_mask_unavailable")
        del roh
        try:
            daten = resp.json()
        except Exception:
            raise BildFehler(503, "image_mask_unavailable")
        if resp.status_code != 200:
            code = (daten.get("error") or {}).get("code") if isinstance(daten, dict) else None
            try:
                raise BildFehler(resp.status_code, str(code))
            except ValueError:
                raise BildFehler(503, "image_mask_unavailable")
        if anfrage.modus == "final":
            m = daten.get("modell") or {}
            try:
                bytes_ = base64.b64decode(m.get("b64", ""), validate=True)
            except (binascii.Error, ValueError):
                raise BildFehler(503, "image_mask_unavailable")
            if hashlib.sha256(bytes_).hexdigest() != m.get("sha256") or daten.get("eingang_sha256") != anfrage.eingang_sha256:
                raise BildFehler(503, "image_mask_unavailable")
            t = token.ausstellen(sha256=m["sha256"], mime=m["mime"], leer=bool(daten["automaske"]["leer"]),
                                 principal=principal, tenant=tenant, breite=int(m["breite"]), hoehe=int(m["hoehe"]),
                                 automasken=int(daten["automaske"]["masken"]),
                                 handmasken=int(daten.get("handmasken_angewendet", 0)))
            daten["final_token"] = t["token"]
            daten["final_token_ablauf"] = datetime.fromtimestamp(t["ablauf"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info("image-mask %s ok: principal=%s masken=%s", anfrage.modus, principal,
                    (daten.get("automaske") or {}).get("masken"))
        return JSONResponse(content=daten, headers=KOPF)
    except BildFehler as f:
        logger.info("image-mask abgewiesen: %s %s", f.status, f.code)
        return _fehler(f)


def _zeit(s: str) -> datetime:
    if not isinstance(s, str) or not s.endswith("Z"):
        raise BildFehler(403, "image_receipt_time_implausible")
    try:
        return datetime.fromisoformat(s[:-1] + "+00:00")
    except ValueError:
        raise BildFehler(403, "image_receipt_time_implausible")


async def image_receipt(request: Request) -> JSONResponse:
    from src.privacy_image import speicher_client

    try:
        if not _an():
            raise BildFehler(404, "feature_disabled")
        principal, tenant = _zaun(request)
        roh = await _koerper(request, 16384)
        try:
            obj = json.loads(roh)
        except ValueError:
            raise BildFehler(422, "image_field_unknown")
        a = formular.receipt_anfrage(obj)
        f = token.pruefen(a.final_token)
        if f["principal"] != principal or f["tenant"] != tenant:
            raise BildFehler(403, "image_receipt_principal_mismatch")
        if a.sha256 != f["sha256"] or a.mime != f["mime"]:
            raise BildFehler(403, "image_receipt_sha_mismatch")
        um = _zeit(a.zeit)
        jetzt = datetime.now(timezone.utc).timestamp()
        if not (int(f["ausgestellt"]) - 60 <= um.timestamp() <= jetzt + 60):
            raise BildFehler(403, "image_receipt_time_implausible")
        if f["leer"] and a.leer_zweitbestaetigung is not True:
            raise BildFehler(403, "image_empty_requires_second_confirmation")
        r = await speicher_client.receipt_anlegen(token_hash=token.token_hash(a.final_token), felder=f,
                                                  app_id=pruefung.SCHEMA_APP_ID, bestaetigung_version="bild-v1",
                                                  bestaetigt_um=um)
        exp = r["expires_at"]
        exp = exp if isinstance(exp, str) else exp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info("image-receipt angelegt: sha=%s hand=%s auto=%s", f["sha256"][:12], f["handmasken"], f["automasken"])
        return JSONResponse(status_code=201, headers=KOPF, content={
            "api": "bild-api-v1", "receipt_id": r["receipt_id"], "sha256": r["sha256"], "expires_at": exp})
    except BildFehler as f:
        logger.info("image-receipt abgewiesen: %s %s", f.status, f.code)
        return _fehler(f)


async def chat_pruefen(request: Request) -> Optional[JSONResponse]:
    """Fuer die Schema-App VOR jeder Verarbeitung im Chat-Handler (direkt, Stream, Job-Selbstaufruf).
    None = kein Bild (Textweg unveraendert). Sonst immer eine Antwort: Bindungsfehler oder §4 fail-closed."""
    from src.budget.test_ki_deckel import JOB_PRINZIPAL_KEY
    from src.jobs import store_client
    from src.jobs.selbstaufruf import JOB_LAUF_HEADER
    from src.privacy_image import speicher_client

    try:
        try:
            roh = json.loads(await request.body() or b"{}")
        except ValueError:
            return None          # kein JSON: das Request-Modell weist ab wie bisher
        b = pruefung.bild_aus_chat(roh)
        if b is None:
            return None
        # Selbstaufruf-Auth (wie _schema_noneu_principal_name, Pruefung R2 H5): nur Loopback-Peer mit Job-Kopf.
        job_id = (request.headers.get(JOB_LAUF_HEADER) or "").strip()
        peer = getattr(getattr(request, "client", None), "host", None)
        if not job_id or peer not in ("127.0.0.1", "::1"):
            raise BildFehler(403, "image_requires_job")
        try:
            job = await store_client.get_job(job_id)
        except Exception:
            raise BildFehler(503, "image_mask_unavailable")
        if not isinstance(job, dict) or job.get("status") != "running" or job.get("kind") != "chat":
            raise BildFehler(403, "image_receipt_invalid")
        r = await speicher_client.receipt_fuer_job(job_id)
        if r is None or r.get("receipt_id") != b.receipt_id:
            raise BildFehler(403, "image_receipt_invalid")
        attr = job.get("attribution") or {}
        if r.get("principal") != attr.get(JOB_PRINZIPAL_KEY) or r.get("tenant") != str(attr.get("tenant_id") or ""):
            raise BildFehler(403, "image_receipt_principal_mismatch")
        if r.get("sha256") != b.sha256 or r.get("mime") != b.mime:
            raise BildFehler(403, "image_receipt_sha_mismatch")
        # §4 fail-closed: Bindung ist gueltig, aber kein Bildweg ist freigegeben.
        if not FREIGEGEBENE_BILDROUTEN:
            raise BildFehler(403, "image_route_not_allowed")
        raise BildFehler(403, "image_route_not_allowed")
    except BildFehler as f:
        logger.info("schema-chat bild abgewiesen: %s %s", f.status, f.code)
        return _fehler(f)
