"""Research-library S3 fetch layer (specs/research-library-tool/DESIGN.md).

A kuratierte, private Dokumentbibliothek on Hetzner S3 that the research-cloud
executor's two client tools (``library_index``, ``library_get`` — see
executor.py) read from directly. Flag-gated off by default
(RESEARCH_LIBRARY_ENABLED); every fetch is fail-soft from the executor's
perspective — this module raises LibraryFetchError, and the executor turns
that into a tool_result(is_error=True) so a broken S3 config degrades the
research run, never aborts it.
"""
from __future__ import annotations

import asyncio
import json
from os import environ
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class LibraryFetchError(Exception):
    """S3 access or index-lookup failure — caught at the executor's tool-call
    boundary and turned into a fail-soft tool_result, never raised further."""


class LibraryConfig(BaseModel):
    """Snapshot of RESEARCH_LIBRARY_* env vars. Loaded once per research run
    (load_library_config) so the tool-availability check (library_enabled)
    and the actual fetches agree on the same config, even if env vars were
    mutated between calls."""

    enabled: bool = False
    endpoint_url: Optional[str] = None
    bucket: Optional[str] = None
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    region: str = "eu-central-1"
    prefix: str = "research-library/"

    @property
    def configured(self) -> bool:
        return bool(self.endpoint_url and self.bucket and self.access_key_id and self.secret_access_key)


def load_library_config() -> LibraryConfig:
    prefix = environ.get("RESEARCH_LIBRARY_S3_PREFIX", "research-library/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return LibraryConfig(
        enabled=environ.get("RESEARCH_LIBRARY_ENABLED", "").strip().lower() in ("1", "true", "yes", "on"),
        endpoint_url=environ.get("RESEARCH_LIBRARY_S3_ENDPOINT_URL") or None,
        bucket=environ.get("RESEARCH_LIBRARY_S3_BUCKET") or None,
        access_key_id=environ.get("RESEARCH_LIBRARY_S3_ACCESS_KEY_ID") or None,
        secret_access_key=environ.get("RESEARCH_LIBRARY_S3_SECRET_ACCESS_KEY") or None,
        region=environ.get("RESEARCH_LIBRARY_S3_REGION", "eu-central-1"),
        prefix=prefix,
    )


def library_enabled(config: LibraryConfig) -> bool:
    """True only when the flag is on AND the S3 target is fully configured —
    a half-configured library (flag on, creds missing) must not advertise the
    tool to the model, since every call would then fail."""
    return config.enabled and config.configured


def _get_s3_client(config: LibraryConfig):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=config.endpoint_url,
        region_name=config.region,
        aws_access_key_id=config.access_key_id,
        aws_secret_access_key=config.secret_access_key,
        config=Config(connect_timeout=10, read_timeout=30, retries={"max_attempts": 2, "mode": "standard"}),
    )


def _get_object_sync(config: LibraryConfig, key: str) -> str:
    # boto3 is synchronous — callers run this via asyncio.to_thread so a slow
    # S3 GET does not park the event loop (same reasoning as
    # bedrock_service.py's _invoke_sync).
    client = _get_s3_client(config)
    try:
        obj = client.get_object(Bucket=config.bucket, Key=key)
        return obj["Body"].read().decode("utf-8")
    except Exception as e:
        raise LibraryFetchError(f"S3 GetObject failed for key {key!r}: {e}") from e


async def fetch_library_index(config: LibraryConfig) -> Dict[str, Any]:
    """Return the parsed index.json — id/title/publisher/jurisdiction/...
    metadata only, no document bodies (those are lazy-loaded per document via
    fetch_library_document)."""
    if not config.configured:
        raise LibraryFetchError("research library not configured (RESEARCH_LIBRARY_S3_* missing)")
    key = f"{config.prefix}index.json"
    raw = await asyncio.to_thread(_get_object_sync, config, key)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise LibraryFetchError(f"index.json at {key!r} is not valid JSON: {e}") from e


def _find_index_entry(index: Dict[str, Any], doc_id: str) -> Dict[str, Any]:
    documents: List[Dict[str, Any]] = index.get("documents", [])
    entry = next((d for d in documents if d.get("id") == doc_id), None)
    if entry is None:
        raise LibraryFetchError(f"unknown library document id: {doc_id!r}")
    return entry


async def fetch_library_document(doc_id: str, config: LibraryConfig) -> Dict[str, Any]:
    """Fetch one document's full text plus its index metadata (for the
    source/title attribution on the resulting search_result block).

    The id is validated against index.json first — only ids the curated
    index actually lists are ever used to build an S3 key, so a
    model-supplied id cannot address arbitrary bucket keys.
    """
    index = await fetch_library_index(config)
    entry = _find_index_entry(index, doc_id)
    key = f"{config.prefix}docs/{doc_id}.md"
    text = await asyncio.to_thread(_get_object_sync, config, key)
    return {"entry": entry, "text": text}
