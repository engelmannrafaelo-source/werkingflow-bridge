"""Durable document envelope and fenced remote reconciliation runner."""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
from datetime import datetime, timezone
import hashlib
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, field_validator


logger = logging.getLogger(__name__)


class DocumentPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: StrictStr = Field(min_length=1)
    content_base64: StrictStr = Field(min_length=1)
    content_type: StrictStr = "application/octet-stream"
    fields: dict[str, StrictStr] = Field(default_factory=dict)
    deadline_at: StrictStr | None = None

    @field_validator("deadline_at")
    @classmethod
    def deadline(cls, value):
        if value is not None:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                raise ValueError("deadline_at requires a timezone")
            return parsed.astimezone(timezone.utc).isoformat()
        return value

    @field_validator("filename", "content_type")
    @classmethod
    def header_value(cls, value: str) -> str:
        if any(c in value for c in ("\r", "\n", "\x00")):
            raise ValueError("invalid multipart header value")
        return value

    @field_validator("content_base64")
    @classmethod
    def encoded_bytes(cls, value: str) -> str:
        try:
            content = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("content_base64 must be standard base64") from None
        if not content:
            raise ValueError("document is empty")
        return value

    @field_validator("fields")
    @classmethod
    def form_fields(cls, value: dict[str, str]) -> dict[str, str]:
        if "file" in value:
            raise ValueError("fields.file conflicts with the uploaded document")
        return value


async def document_executor(payload, attribution, report_progress):
    # Registry dispatches this kind through run_document_job. There is no
    # unfenced/synchronous fallback when either service lacks the new protocol.
    raise RuntimeError("document-convert requires the fenced document runner")


async def run_document_job(job_id: str, payload: dict, attribution: dict | None,
                           attempt: int | None = None) -> None:
    from src.federation import set_request_origin
    from src.privacy_client import PRIVACY_SERVICE_URL, privacy_auth_headers, privacy_timeout
    from . import store_client

    set_request_origin((attribution or {}).get("bridge_origin"))
    call_id = hashlib.sha256((str((attribution or {}).get("bridge_origin")) + ":" + job_id).encode()).hexdigest()[:32]
    remote_url = f"{PRIVACY_SERVICE_URL}/document/calls/{call_id}"
    failure = None
    document = DocumentPayload.model_validate(payload)
    headers = privacy_auth_headers()
    headers["X-Document-Actualtext-Repair"] = "true" if os.getenv("BRIDGE_PDF_ACTUALTEXT_REPAIR", "").strip().lower() == "true" else "false"
    if document.deadline_at:
        headers["X-Document-Deadline"] = document.deadline_at
    async with httpx.AsyncClient(headers=headers, timeout=privacy_timeout(30)) as client:
        while True:
            try:
                if attempt is None:
                    claim = await store_client.document_state(job_id, 0, "claim")
                    if not claim["owned"]:
                        return
                    attempt = claim["attempt"]
                state = await store_client.document_state(job_id, attempt, "heartbeat")
                if not state["owned"]:
                    return
                expired = document.deadline_at is not None and datetime.now(timezone.utc) >= datetime.fromisoformat(document.deadline_at)
                cancelling = state["cancel_requested"] or expired
                response = await client.get(remote_url)
                if response.status_code == 404:
                    if cancelling:
                        # No request was accepted under this identity. Persist
                        # a remote cancellation tombstone before acknowledging
                        # stop; it fences a delayed PUT from an older worker.
                        response = await client.delete(remote_url)
                    else:
                        response = await client.put(
                            remote_url,
                            files={"file": (document.filename, base64.b64decode(document.content_base64), document.content_type)},
                            data=document.fields,
                        )
                if cancelling and response.status_code < 300:
                    response = await client.delete(remote_url)
                if response.status_code >= 300:
                    # 404/405 means an incompatible service, not permission to
                    # fall back to a fresh synchronous conversion.
                    raise RuntimeError(f"document control HTTP {response.status_code}")
                remote = response.json()
                if remote.get("call_id") != call_id or "status" not in remote:
                    raise RuntimeError("invalid document control identity")
                progress = {
                    "phase": "document-" + remote["status"], "call_id": call_id,
                    "attempt_id": remote.get("attempt_id"), "cancel_requested": cancelling,
                }
                if remote["status"] in ("done", "error", "cancelled", "interrupted"):
                    receipt = remote.get("stop_receipt", {})
                    if not receipt.get("reaped") or not receipt.get("local_stopped"):
                        raise RuntimeError("document remote end lacks stop receipt")
                    progress["stop_receipt"] = receipt
                    if cancelling or remote["status"] == "cancelled":
                        progress["abbruch"] = {
                            "ledger": "unbekannt" if receipt.get("provider_outcome") == "unknown" else "vom_dokumentlauf",
                            "mode": "process-reaped", "remote_finished": receipt.get("provider_outcome") != "unknown",
                            "reason": "deadline_expired" if expired else "user_cancelled",
                        }
                        action = "cancelled"
                        value = {"progress": progress, "stop_receipt": receipt}
                    elif remote["status"] == "done":
                        result = remote["result"]["result"]
                        if result.get("success") is not True or not isinstance(result.get("markdown"), str):
                            raise RuntimeError("document result schema invalid")
                        action = "done"
                        value = {"result": {**result, "call_id": call_id, "attempt_id": remote["attempt_id"]}, "progress": progress}
                    else:
                        action = "error"
                        value = {"error": {
                            "message": "document execution " + remote["status"],
                            "code": "DOCUMENT_REMOTE_" + remote["status"].upper(),
                            "retryable": False, "retry_after_s": None,
                        }, "progress": progress}
                    saved = await store_client.document_state(job_id, attempt, action, value)
                    if saved["owned"] and saved.get("written"):
                        await record_completion(call_id, attribution or {}, action, value)
                    if not saved["owned"] or saved.get("written"):
                        return
                    # Cancel won the database lock against late done. Read its
                    # wish and the SAME retained remote result on the next turn.
                else:
                    saved = await store_client.document_state(job_id, attempt, "progress", progress)
                    if not saved["owned"]:
                        return
                    if remote["status"] == "unknown":
                        raise RuntimeError("document guardian lost; reconciliation required")
                if failure is not None:
                    logger.info("document control recovered")
                    failure = None
            except asyncio.CancelledError:
                # The persistent remote call remains addressable. The next
                # fenced owner reconciles it; never send an unrequested DELETE.
                raise
            except Exception as exc:
                current = (type(exc).__name__, str(exc) if isinstance(exc, RuntimeError) else "")
                if current != failure:
                    logger.error("document reconciliation waiting (%s %s)", *current)
                    failure = current
            await asyncio.sleep(2)


async def record_completion(call_id: str, attribution: dict, action: str, value: dict) -> None:
    """Preserve local document activity/metrics without recording source text."""
    from src.activity.ai_call_writer import persist_ai_call_activity
    from src.activity.providers import PROVIDER_LOCAL
    from src.middleware.prompt_metrics import get_prompt_metrics
    import uuid

    duration = int(value.get("result", {}).get("conversion_time_seconds", 0) * 1000)
    state = "success" if action == "done" else "error"
    try:
        get_prompt_metrics().record(
            app_id=attribution.get("app_id"), agent_id="dokument-konvertierung",
            workflow_id=attribution.get("workflow_id"), model="docling",
            duration_ms=duration, status=state, user_id=attribution.get("user_id"),
        )
        await persist_ai_call_activity(
            provider=PROVIDER_LOCAL, app_id=attribution.get("app_id"),
            user_id=attribution.get("user_id"), agent_id="dokument-konvertierung",
            workflow_id=attribution.get("workflow_id"), model="docling",
            input_tokens=0, output_tokens=0, status=state, duration_ms=duration,
            app_env=attribution.get("app_env"), bridge_origin=attribution.get("bridge_origin"),
            _call_uid=str(uuid.UUID(call_id)),
        )
    except Exception as exc:
        logger.error("document completion tracking failed (%s)", type(exc).__name__)
