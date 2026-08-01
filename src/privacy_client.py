"""
Privacy Service Client — HTTP client for the external privacy-pdf-service.

Used by lightweight workers (Containers 1-3) to delegate anonymization
to the heavy privacy-pdf-service (Container 4).

De-anonymization (simple string replace) runs LOCALLY in the worker
to avoid HTTP latency per streaming chunk.
"""

import os
import re
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, List, Optional, Any, Tuple

import httpx

logger = logging.getLogger(__name__)

# Internal Docker service URL
PRIVACY_SERVICE_URL = os.getenv("PRIVACY_SERVICE_URL", "http://privacy-service:8100")

# How long to wait for the TCP connection to the privacy service to be
# ESTABLISHED — deliberately separate from the (deliberately long) read
# timeouts the callers pass in.
#
# Why this exists (2026-08-01 outage): every caller used to pass a scalar
# `timeout=600.0`, and httpx applies a scalar to ALL four phases — connect,
# read, write, pool. So an unreachable privacy service did not fail at "600s
# of work", it fell through to the kernel: an unanswered SYN retries for
# tcp_syn_retries=6 (~63s of backoff + a final ~64s wait) and only then
# raises ConnectError — a measured 133.5-136.4s per request, every request.
# During that window the request occupies a worker slot, nginx sees a 500 and
# relabels it as "bridge at capacity", and a deploy smoke reads the whole
# thing as a broken endpoint. The dependency being DOWN and the dependency
# being SLOW are different failures and must fail on different clocks.
#
# 10s is far above any healthy path (the local container answers /health in
# ~2ms, the Tailscale-remote GPU host in ~50ms) and far below the kernel's
# ~127s, so an unreachable service now fails loud and fast while a genuinely
# slow inference call keeps its full read budget.
PRIVACY_CONNECT_TIMEOUT_S = float(os.getenv("PRIVACY_SERVICE_CONNECT_TIMEOUT_S", "10"))

# Default read budget for short control calls (/status, /health).
PRIVACY_DEFAULT_READ_TIMEOUT_S = 30.0


def privacy_timeout(read_s: float = PRIVACY_DEFAULT_READ_TIMEOUT_S) -> httpx.Timeout:
    """Timeout for a privacy-service call: short connect, caller-chosen read.

    Pass this instead of a bare float wherever a long timeout is needed, so
    the long budget applies to WAITING FOR AN ANSWER, never to finding the
    server. `read_s` also covers write (large document uploads) and pool
    (the privacy service is single-worker, so queueing for a free connection
    is legitimate work, not a fault).
    """
    return httpx.Timeout(
        connect=PRIVACY_CONNECT_TIMEOUT_S,
        read=read_s,
        write=read_s,
        pool=read_s,
    )


class PrivacyServiceClient:
    """
    HTTP client for privacy-service. Handles anonymization via HTTP
    and de-anonymization locally (no Presidio/spaCy needed).
    """

    def __init__(self):
        self.base_url = PRIVACY_SERVICE_URL
        self.enabled = os.getenv("PRIVACY_ENABLED", "false").lower() in ("true", "1", "yes", "on")
        self.language = os.getenv("PRIVACY_LANGUAGE", "de")
        self._client: Optional[httpx.AsyncClient] = None
        # In-process concurrency gauge for calls into the (single-worker,
        # UVICORN_WORKERS=1) privacy-pdf-service container — the actual
        # capacity bottleneck of the document/anonymize pipeline. Scoped to
        # THIS worker process only: with multiple bridge workers sharing one
        # privacy-service container, true global concurrency is >= this
        # value. Treat it as a per-worker lower bound, not an exact count.
        self.active_calls = 0

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=privacy_timeout(),
            )
        return self._client

    @asynccontextmanager
    async def track_call(self) -> AsyncIterator[int]:
        """Observe concurrency around a privacy-service call.

        Yields the number of calls already in flight on THIS worker when
        this call started (0 = no overlap seen). Callers feed this into
        PromptMetricsCollector.record(concurrent_calls_at_start=...) so the
        capacity endpoint can show a busy-rate for the privacy-service
        bottleneck. Purely observational — never rejects or throttles.
        """
        concurrent_before = self.active_calls
        self.active_calls += 1
        try:
            yield concurrent_before
        finally:
            self.active_calls -= 1

    @property
    def is_available(self) -> bool:
        return self.enabled

    def should_anonymize(self, privacy_mode: Optional[str] = None) -> bool:
        if not self.enabled:
            return False
        if privacy_mode == "none":
            return False
        return True

    # ===== REMOTE: Anonymization (requires Presidio/spaCy) =====

    async def anonymize_messages(
        self,
        messages: List[Dict[str, Any]],
        privacy_mode: Optional[str] = "full",
    ) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
        """
        Anonymize messages via HTTP call to privacy-service.

        Returns:
            Tuple of (anonymized_messages, mapping)
        """
        if not self.should_anonymize(privacy_mode):
            return messages, {}

        try:
            client = await self._get_client()
            response = await client.post("/anonymize", json={
                "messages": messages,
                "privacy_mode": privacy_mode,
            })
            response.raise_for_status()
            data = response.json()
            return data["messages"], data["mapping"]

        except Exception as e:
            # Fail CLOSED, never open: if anonymization was requested but cannot
            # be verified, the request must NOT proceed to the LLM with raw
            # content. A privacy-service outage becomes a loud, visible request
            # failure — never a silent PII leak. Availability must never trump
            # data protection here. (should_anonymize() already returned early
            # above for the legitimate "privacy off" config, so reaching this
            # except means anonymization was expected and genuinely failed.)
            logger.error(f"Privacy service anonymize failed: {e}", exc_info=True)
            raise RuntimeError(
                "Anonymization failed while privacy is enabled — refusing to "
                f"forward raw content to the LLM: {e}"
            ) from e

    # ===== LOCAL: De-anonymization (pure string replace, no NLP) =====

    @staticmethod
    def deanonymize_response(content: str, mapping: Optional[Dict[str, str]] = None) -> str:
        """
        De-anonymize response content using mapping.
        Runs LOCALLY — no HTTP call, no Presidio needed.
        Just sorted string replacement (longest placeholders first).
        """
        if not content or not mapping:
            return content

        result = content
        sorted_placeholders = sorted(mapping.keys(), key=len, reverse=True)
        for placeholder in sorted_placeholders:
            original = mapping[placeholder]
            result = result.replace(placeholder, original)
        return result

    @staticmethod
    def deanonymize_streaming_chunk(
        chunk: str,
        buffer: str,
        mapping: Optional[Dict[str, str]] = None,
    ) -> Tuple[str, str]:
        """
        De-anonymize a streaming chunk with buffering for split placeholders.
        Runs LOCALLY — no HTTP call needed.

        Handles the case where ANON_XXX placeholders are split across chunks.
        Returns (text_to_yield, new_buffer).
        """
        if not mapping:
            return chunk, ""

        combined = buffer + chunk

        # Pattern to detect partial ANON_ placeholders at end of text
        partial_pattern = r'ANON_[A-Z_]*\d*$'
        match = re.search(partial_pattern, combined)

        if match:
            potential_placeholder = match.group(0)
            if potential_placeholder in mapping:
                # Complete placeholder — de-anonymize everything
                result = PrivacyServiceClient.deanonymize_response(combined, mapping)
                return result, ""
            else:
                # Partial — buffer it
                safe_text = combined[:match.start()]
                new_buffer = combined[match.start():]
                if safe_text:
                    safe_text = PrivacyServiceClient.deanonymize_response(safe_text, mapping)
                return safe_text, new_buffer
        else:
            result = PrivacyServiceClient.deanonymize_response(combined, mapping)
            return result, ""

    @staticmethod
    def flush_streaming_buffer(
        buffer: str,
        mapping: Optional[Dict[str, str]] = None,
    ) -> str:
        """Flush remaining buffer at end of stream. Local operation."""
        if not buffer:
            return ""
        if not mapping:
            return buffer
        return PrivacyServiceClient.deanonymize_response(buffer, mapping)

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# Singleton
_privacy_client: Optional[PrivacyServiceClient] = None


def get_privacy_client() -> PrivacyServiceClient:
    global _privacy_client
    if _privacy_client is None:
        _privacy_client = PrivacyServiceClient()
    return _privacy_client
