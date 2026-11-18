"""SQL der Bild-Receipts (laeuft in platform-api bzw. im Direkt-DB-Rueckfall; Muster src/jobs/store.py).

  receipt_anlegen      -- INSERT; token_hash UNIQUE -> zweites Einloesen desselben Tokens = 409 image_receipt_conflict
  verbrauch_in_tx      -- in DERSELBEN Transaktion wie die Job-Anlage (src/jobs/store.create_job_idempotent):
                          prueft sha256/mime/principal/tenant/Ablauf/unverbraucht und setzt consumed_job_id;
                          jede Abweichung -> BildFehler, die Transaktion rollt zurueck (kein Job ohne Verbrauch)
  replay_pruefen       -- Replay derselben Idempotency: Receipt muss genau diesem Job gehoeren, sonst 409
  receipt_fuer_job     -- Executor-Pruefung (lesend): Receipt mit consumed_job_id = job_id
Retention: unverbrauchte Receipts 24 h nach Ablauf, verbrauchte mit ihrem Job (Bereinigung wie prune_jobs, eigener Schritt).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from src.privacy_image.fehler import BildFehler

RECEIPT_GUELTIG = timedelta(minutes=30)


async def receipt_anlegen(conn, *, token_hash: str, felder: Dict[str, Any], app_id: str,
                          bestaetigung_version: str, bestaetigt_um: datetime) -> Dict[str, Any]:
    rid = str(uuid.uuid4())
    jetzt = datetime.now(timezone.utc)
    ablauf = jetzt + RECEIPT_GUELTIG
    eingefuegt = await conn.fetchval(
        """
        INSERT INTO privacy_image_receipts (receipt_id, token_hash, sha256, mime, principal, tenant, app_id,
            breite, hoehe, leer, automasken_anzahl, handmasken_anzahl, bestaetigung_version, bestaetigt_um, expires_at)
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
        ON CONFLICT (token_hash) DO NOTHING
        RETURNING receipt_id
        """,
        rid, token_hash, felder["sha256"], felder["mime"], felder["principal"], felder["tenant"], app_id,
        int(felder["breite"]), int(felder["hoehe"]), bool(felder["leer"]), int(felder["automasken"]),
        int(felder["handmasken"]), bestaetigung_version, bestaetigt_um, ablauf,
    )
    if eingefuegt is None:
        raise BildFehler(409, "image_receipt_conflict")
    return {"receipt_id": rid, "sha256": felder["sha256"], "expires_at": ablauf}


async def verbrauch_in_tx(conn, *, receipt_id: str, job_id: str, sha256: str, mime: str,
                          principal: str, tenant: str) -> None:
    """MUSS innerhalb von conn.transaction() laufen, direkt nach dem INSERT des Jobs."""
    try:
        uuid.UUID(receipt_id)
    except (ValueError, TypeError, AttributeError):
        raise BildFehler(403, "image_receipt_missing")
    row = await conn.fetchrow(
        "SELECT sha256, mime, principal, tenant, expires_at, consumed_job_id FROM privacy_image_receipts "
        "WHERE receipt_id = $1::uuid FOR UPDATE", receipt_id)
    if row is None:
        raise BildFehler(403, "image_receipt_missing")
    if row["consumed_job_id"] is not None:
        raise BildFehler(409, "image_receipt_consumed")
    if row["expires_at"] <= datetime.now(timezone.utc):
        raise BildFehler(403, "image_receipt_expired")
    if row["principal"] != principal or row["tenant"] != tenant:
        raise BildFehler(403, "image_receipt_principal_mismatch")
    if row["sha256"] != sha256 or row["mime"] != mime:
        raise BildFehler(403, "image_receipt_sha_mismatch")
    n = await conn.execute(
        "UPDATE privacy_image_receipts SET consumed_job_id = $2, consumed_at = now() "
        "WHERE receipt_id = $1::uuid AND consumed_job_id IS NULL", receipt_id, job_id)
    if not str(n).endswith(" 1"):
        raise BildFehler(409, "image_receipt_consumed")


async def replay_pruefen(conn, *, receipt_id: str, job_id: str) -> None:
    try:
        uuid.UUID(receipt_id)
    except (ValueError, TypeError, AttributeError):
        raise BildFehler(409, "image_receipt_conflict")
    besitzer = await conn.fetchval(
        "SELECT consumed_job_id FROM privacy_image_receipts WHERE receipt_id = $1::uuid", receipt_id)
    if besitzer != job_id:
        raise BildFehler(409, "image_receipt_conflict")


async def receipt_fuer_job(conn, job_id: str) -> Optional[Dict[str, Any]]:
    row = await conn.fetchrow(
        "SELECT receipt_id::text AS receipt_id, sha256, mime, principal, tenant FROM privacy_image_receipts "
        "WHERE consumed_job_id = $1", job_id)
    return dict(row) if row else None
