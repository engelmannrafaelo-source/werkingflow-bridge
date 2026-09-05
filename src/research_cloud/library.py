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


class LibraryUnavailableError(Exception):
    """The library was switched ON but cannot be used — missing config, or an
    index that does not load.

    Deliberately NOT a LibraryFetchError: that one is the fail-soft, per-call
    kind ("this one document did not load, carry on"). This one is the loud
    kind and aborts the run BEFORE any model tokens are spent.

    Why loud: presence-only gating used to make the two indistinguishable.
    RESEARCH_LIBRARY_ENABLED=true with empty values silently dropped the tools
    from the request (library_enabled() checks presence, not validity), and
    stale credentials advertised tools whose every call failed fail-soft — the
    run still reported success. Production ran that way from the ADR-0009
    worker move until 2026-09-04 without a single error (library_calls = 0 for
    two weeks). A library that is configured-on and does not work must say so,
    never quietly answer from the open web instead (Rafael 2026-09-05).
    """


# Curator convention in index.json: an ``ext-`` id is a catalogue pointer to an
# external portal, NOT a stored full text — there is no docs/<id>.md behind it.
# Every one of these entries also spells it out in its ``note`` ("KATALOG-EINTRAG
# OHNE VOLLTEXT"), but prose in one of eight fields is not something a caller can
# branch on, so the prefix is the machine-readable contract.
EXTERNAL_ENTRY_ID_PREFIX = "ext-"


def entry_has_fulltext(entry: Dict[str, Any]) -> bool:
    """True iff ``library_get`` can actually return a stored full text for this
    index entry."""
    return not str(entry.get("id", "")).startswith(EXTERNAL_ENTRY_ID_PREFIX)


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


async def fetch_library_document(
    doc_id: str, config: LibraryConfig, *, index: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Fetch one document's full text plus its index metadata (for the
    source/title attribution on the resulting search_result block).

    The id is validated against index.json first — only ids the curated
    index actually lists are ever used to build an S3 key, so a
    model-supplied id cannot address arbitrary bucket keys.

    ``index``, if given, is the already-loaded index for this run (the
    executor holds one) — saves a second S3 GET per document and keeps the
    catalogue the model was shown and the lookup that answers it identical.
    """
    if index is None:
        index = await fetch_library_index(config)
    entry = _find_index_entry(index, doc_id)
    if not entry_has_fulltext(entry):
        # Answering this with a bare S3 404 would tell the model "the library
        # is broken" when the truth is "this entry never had a full text".
        # Name the alternative instead, so the run continues on the open web
        # for exactly this source.
        raise LibraryFetchError(
            f"{doc_id!r} is a catalogue entry without a stored full text — "
            f"there is nothing to load. Use its source instead: "
            f"{entry.get('source_url') or entry.get('publisher') or 'no source_url in the index'}"
        )
    key = f"{config.prefix}docs/{doc_id}.md"
    text = await asyncio.to_thread(_get_object_sync, config, key)
    return {"entry": entry, "text": text}


async def load_library_for_run(config: LibraryConfig) -> Optional[Dict[str, Any]]:
    """Resolve the library once per research run, before any model tokens are
    spent. Returns the loaded index, or None when the library is switched off.

    Three outcomes, deliberately distinct:

    * flag off            -> None. The library is not part of this run; the
                             executor offers no library tools. Legitimate.
    * flag on, unusable   -> LibraryUnavailableError. Missing config values, or
                             an index that will not load (typically credentials
                             that were rotated without a re-sync). Loud, and
                             cheap: it happens before the first API call.
    * flag on, usable     -> the parsed index, which the run then both shows to
                             the model (prompt catalogue) and serves its
                             library_index calls from.

    The middle case is the one this function exists for. Everything else in
    this module is fail-soft by design; a library that was switched on and does
    not work is a configuration fault, not a degraded document.
    """
    if not config.enabled:
        return None
    if not config.configured:
        missing = [
            name
            for name, value in (
                ("RESEARCH_LIBRARY_S3_ENDPOINT_URL", config.endpoint_url),
                ("RESEARCH_LIBRARY_S3_BUCKET", config.bucket),
                ("RESEARCH_LIBRARY_S3_ACCESS_KEY_ID", config.access_key_id),
                ("RESEARCH_LIBRARY_S3_SECRET_ACCESS_KEY", config.secret_access_key),
            )
            if not value
        ]
        raise LibraryUnavailableError(
            "RESEARCH_LIBRARY_ENABLED is on, but the library is not configured — "
            f"empty or missing: {', '.join(missing)}"
        )
    try:
        index = await fetch_library_index(config)
    except LibraryFetchError as e:
        raise LibraryUnavailableError(
            f"RESEARCH_LIBRARY_ENABLED is on, but the library index does not load: {e}"
        ) from e
    if not index.get("documents"):
        raise LibraryUnavailableError(
            "RESEARCH_LIBRARY_ENABLED is on, but the library index lists no documents"
        )
    return index
