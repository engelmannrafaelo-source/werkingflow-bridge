"""Binary envelope and executor for the durable document conversion job.

Cancellation drains an already submitted conversion: the remote converter uses
threads and offers no stop acknowledgement. Never report that it stopped merely
because our HTTP socket closed. The job heartbeat stays alive while draining.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
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


async def document_executor(payload, attribution, report_progress) -> dict[str, Any]:
    from src.error_contract import JobFailure
    from src.privacy_client import privacy_timeout
    from src.jobs import store_client
    from src.jobs.executors import (
        SELF_BASE_URL, _build_headers, _http_error,
        _raise_if_dependency_unavailable,
    )
    from src.jobs.registry import laufender_job
    from src.jobs.selbstaufruf import JOB_CANCEL_POLL_S, JobLaufAbgebrochen

    try:
        document = DocumentPayload.model_validate(payload)
    except ValidationError:
        raise JobFailure(
            "Invalid document-convert envelope", retryable=False,
        ) from None
    headers = _build_headers(attribution)
    headers.pop("Content-Type", None)  # httpx owns the multipart boundary
    job_id = laufender_job()
    await report_progress({"phase": "document-convert"})
    async with httpx.AsyncClient(timeout=privacy_timeout(None)) as client:
        task = asyncio.create_task(client.post(
            f"{SELF_BASE_URL}/v1/document/convert",
            headers=headers,
            files={"file": (
                document.filename, base64.b64decode(document.content_base64),
                document.content_type,
            )},
            data=document.fields,
        ))
        cancel_requested = False
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=JOB_CANCEL_POLL_S)
                if done:
                    break
                if job_id and not cancel_requested:
                    try:
                        state = await store_client.cancel_state(job_id)
                    except Exception as exc:
                        # Store unavailability is not permission to abandon a
                        # conversion whose remote outcome is still unknown.
                        logger.warning(
                            "document job cancel poll unavailable (%s)",
                            type(exc).__name__,
                        )
                        continue
                    if state and state.get("cancel_requested"):
                        cancel_requested = True
                        try:
                            await report_progress({
                                "phase": "cancel-requested-draining-conversion",
                                "cancel_requested": True,
                            })
                        except Exception as exc:
                            logger.warning(
                                "document job cancel progress unavailable (%s)",
                                type(exc).__name__,
                            )
            response = await task
        except httpx.TransportError as exc:
            # Remote execution may still exist. L6 will add attempt-level
            # reconciliation; a blind re-submit cannot promise exactly-once.
            raise JobFailure(
                f"document conversion transport lost ({type(exc).__name__}); "
                "remote execution outcome unknown", retryable=False,
            ) from exc
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    _raise_if_dependency_unavailable(response, "/v1/document/convert")
    if response.status_code >= 300:
        raise _http_error(response, f"document conversion HTTP {response.status_code}", None)
    try:
        result = response.json()
    except ValueError:
        raise JobFailure("document conversion returned non-JSON", retryable=False) from None
    if not isinstance(result, dict):
        raise JobFailure("document conversion returned no object", retryable=False)
    if result.get("status") == "error":
        # An upstream error body can contain the uploaded document.
        raise JobFailure("document conversion returned status=error", retryable=False)
    if result.get("success") is not True or not isinstance(result.get("markdown"), str):
        raise JobFailure("document conversion returned no successful Markdown result", retryable=False)
    if cancel_requested:
        # The response proves the remote conversion ended. Its activity/provider
        # accounting belongs to that completed call; no second billing here.
        raise JobLaufAbgebrochen({
            "ledger": "vom_dokumentlauf", "mode": "drained", "remote_finished": True,
        })
    return result
