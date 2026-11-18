"""Worker-Seite der Bild-Receipts: platform-api (/v1/internal/privacy-image/*) mit Direkt-DB-Rueckfall, Muster
src/jobs/store_client.py (keine Retries; ohne beides laut). SQL in src/privacy_image/speicher.py."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from src.db.client import get_pool, is_db_enabled
from src.platform_client import PlatformUnavailable, call_platform
from src.privacy_image import speicher
from src.privacy_image.fehler import BildFehler

logger = logging.getLogger(__name__)


def _rueckfall(op: str, e: PlatformUnavailable) -> None:
    if is_db_enabled():
        logger.error("privacy-image %s via platform-api failed (%s) — direct DB", op, type(e).__name__)
        return
    raise BildFehler(503, "image_mask_unavailable")


def _antwort(resp, op: str):
    body = resp.json if isinstance(resp.json, dict) else {}
    d = body.get("detail")
    if isinstance(d, dict) and d.get("reason") == "bild":
        try:
            raise BildFehler(resp.status_code, str(d.get("code")))
        except ValueError:
            raise BildFehler(503, "image_mask_unavailable")
    if resp.status_code not in (200, 201):
        logger.error("privacy-image %s: platform-api status %s", op, resp.status_code)
        raise BildFehler(503, "image_mask_unavailable")
    return body


async def receipt_anlegen(*, token_hash: str, felder: Dict[str, Any], app_id: str, bestaetigung_version: str,
                          bestaetigt_um: datetime) -> Dict[str, Any]:
    try:
        resp = await call_platform("POST", "/v1/internal/privacy-image/receipts", json={
            "token_hash": token_hash, "felder": felder, "app_id": app_id,
            "bestaetigung_version": bestaetigung_version, "bestaetigt_um": bestaetigt_um.isoformat()},
            timeout_s=10.0)
    except PlatformUnavailable as e:
        _rueckfall("receipt_anlegen", e)
        async with get_pool().acquire() as conn:
            return await speicher.receipt_anlegen(conn, token_hash=token_hash, felder=felder, app_id=app_id,
                                                  bestaetigung_version=bestaetigung_version, bestaetigt_um=bestaetigt_um)
    return _antwort(resp, "receipt_anlegen")


async def receipt_fuer_job(job_id: str) -> Optional[Dict[str, Any]]:
    try:
        resp = await call_platform("GET", f"/v1/internal/privacy-image/receipts/by-job/{job_id}", timeout_s=5.0)
    except PlatformUnavailable as e:
        _rueckfall("receipt_fuer_job", e)
        async with get_pool().acquire() as conn:
            return await speicher.receipt_fuer_job(conn, job_id)
    if resp.status_code == 404:
        return None
    return _antwort(resp, "receipt_fuer_job")
