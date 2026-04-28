"""
Adapter chain for the universal document conversion endpoint.

Each adapter declares whether it ``can_handle`` a given (format, MIME, filename)
tuple and, if so, ``convert``s the bytes into a :class:`ConversionResult`.

The :class:`AdapterChain` walks adapters in order. Deterministic adapters come
first (cheap, free) — :class:`AiFallbackAdapter` is the catch-all last entry
that calls the Bridge's own ``/v1/chat/completions`` to handle exotic formats
that none of the deterministic adapters can parse.

This keeps a single ``/v1/document/convert`` endpoint with a clean fallback
policy instead of two separate endpoints (deterministic vs AI).
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import List, Optional

import httpx

from .document_converter import (
    ConversionResult,
    UnsupportedFormatError,
    convert_csv_bytes,
    convert_docx_bytes,
    convert_eml_bytes,
    convert_html_bytes,
    convert_image_bytes,
    convert_msg_bytes,
    convert_pdf_bytes,
    convert_pptx_bytes,
    convert_xlsx_bytes,
    detect_format,
)

logger = logging.getLogger("privacy-service.adapters")


class AdapterError(RuntimeError):
    """Raised by an adapter when conversion fails. Carries the adapter name."""

    def __init__(self, adapter_name: str, message: str, *, cause: Optional[BaseException] = None):
        super().__init__(f"[{adapter_name}] {message}")
        self.adapter_name = adapter_name
        self.cause = cause


# ----------------------------------------------------------------------------
# Base
# ----------------------------------------------------------------------------


class BaseAdapter(ABC):
    """Abstract base — convert one (or one family of) document type(s) to Markdown."""

    name: str = "base"

    @abstractmethod
    def can_handle(self, fmt: str, mime: Optional[str], filename: str) -> bool:
        """Cheap check: would this adapter attempt to convert the file?"""

    @abstractmethod
    def convert(self, content: bytes, filename: str, mime: Optional[str]) -> ConversionResult:
        """Run the conversion. Raise :class:`AdapterError` (or subclass) on failure."""


def _wrap_legacy(
    fn,
    fmt_label: str,
    content: bytes,
    filename: str,
    adapter_name: str,
    *,
    image_kind: bool = False,
) -> ConversionResult:
    """Call a legacy ``convert_*_bytes`` function and assemble a ConversionResult."""
    if image_kind:
        ext = os.path.splitext(filename or "")[1].lower() or ".png"
        markdown, metadata, images = fn(content, suffix=ext)
    else:
        markdown, metadata, images = fn(content)
    metadata = dict(metadata or {})
    metadata["adapter"] = adapter_name
    metadata.setdefault("original_size_bytes", len(content))
    metadata.setdefault("filename", filename)
    return ConversionResult(
        markdown=markdown,
        fmt=fmt_label,
        metadata=metadata,
        images=images or None,
    )


# ----------------------------------------------------------------------------
# Deterministic adapters (thin wrappers around the legacy functions)
# ----------------------------------------------------------------------------


class _LegacyAdapter(BaseAdapter):
    """Internal helper — DRY wrapper for the deterministic format adapters."""

    fmt_token: str = ""
    image_kind: bool = False

    def __init__(self, fn):
        self._fn = fn

    def can_handle(self, fmt, mime, filename) -> bool:
        return fmt == self.fmt_token

    def convert(self, content, filename, mime) -> ConversionResult:
        try:
            return _wrap_legacy(
                self._fn,
                self.fmt_token,
                content,
                filename,
                self.name,
                image_kind=self.image_kind,
            )
        except Exception as e:
            raise AdapterError(self.name, str(e), cause=e) from e


class PdfAdapter(_LegacyAdapter):
    name = "pdf"
    fmt_token = "pdf"

    def __init__(self):
        super().__init__(convert_pdf_bytes)


class DocxAdapter(_LegacyAdapter):
    name = "docx"
    fmt_token = "docx"

    def __init__(self):
        super().__init__(convert_docx_bytes)


class PptxAdapter(_LegacyAdapter):
    name = "pptx"
    fmt_token = "pptx"

    def __init__(self):
        super().__init__(convert_pptx_bytes)


class XlsxAdapter(_LegacyAdapter):
    name = "xlsx"
    fmt_token = "xlsx"

    def __init__(self):
        super().__init__(convert_xlsx_bytes)


class CsvAdapter(_LegacyAdapter):
    name = "csv"
    fmt_token = "csv"

    def __init__(self):
        super().__init__(convert_csv_bytes)


class HtmlAdapter(_LegacyAdapter):
    name = "html"
    fmt_token = "html"

    def __init__(self):
        super().__init__(convert_html_bytes)


class MsgAdapter(_LegacyAdapter):
    name = "msg"
    fmt_token = "msg"

    def __init__(self):
        super().__init__(convert_msg_bytes)


class EmlAdapter(_LegacyAdapter):
    name = "eml"
    fmt_token = "eml"

    def __init__(self):
        super().__init__(convert_eml_bytes)


class ImageAdapter(_LegacyAdapter):
    name = "image"
    fmt_token = "image"
    image_kind = True

    def __init__(self):
        super().__init__(convert_image_bytes)


# ----------------------------------------------------------------------------
# AI fallback — calls the Bridge's own /v1/chat/completions
# ----------------------------------------------------------------------------


class AiFallbackAdapter(BaseAdapter):
    """Catch-all adapter that delegates to a Claude model via the Bridge.

    Strategy:
    - Try to text-decode the bytes (utf-8 / utf-16 / cp1252 / latin-1).
    - If decodable: send the text (truncated) to the Bridge's
      ``/v1/chat/completions`` and ask Claude for raw Markdown.
    - If Claude returns the literal token ``<<UNCONVERTIBLE>>``, fail with an
      :class:`AdapterError` so the chain reports HTTP 415 to the caller.
    - If the bytes don't decode as text, fail loudly — we don't try to OCR
      arbitrary binary blobs through this path.

    The fallback only burns AI tokens for files that no deterministic adapter
    could handle, so the standard formats stay free.
    """

    name = "ai-fallback"
    DEFAULT_MODEL = "claude-haiku-4-5-20251001"
    MAX_TEXT_CHARS = 60_000
    UNCONVERTIBLE_TOKEN = "<<UNCONVERTIBLE>>"

    def __init__(
        self,
        bridge_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 120.0,
    ):
        self.bridge_url = bridge_url or os.getenv(
            "BRIDGE_SELF_URL", "http://localhost:8000/v1/chat/completions"
        )
        self.api_key = api_key if api_key is not None else os.getenv("API_KEY")
        self.model = model or os.getenv("AI_FALLBACK_MODEL", self.DEFAULT_MODEL)
        self.timeout = timeout

    def can_handle(self, fmt, mime, filename) -> bool:  # catch-all
        return True

    def convert(self, content: bytes, filename: str, mime: Optional[str]) -> ConversionResult:
        text = self._try_decode(content)
        if text is None:
            raise AdapterError(
                self.name,
                f"Cannot AI-convert binary file {filename!r} (mime={mime!r}): "
                "not text-decodable and no specific adapter handled it.",
            )

        truncated = len(text) > self.MAX_TEXT_CHARS
        sample = text[: self.MAX_TEXT_CHARS] if truncated else text

        prompt = self._build_prompt(filename, mime, sample, truncated)

        body = {
            "model": self.model,
            "max_tokens": 8000,
            "temperature": 0,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(self.bridge_url, headers=headers, json=body)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            raise AdapterError(self.name, f"Bridge self-call failed: {e}", cause=e) from e

        if resp.status_code != 200:
            raise AdapterError(
                self.name,
                f"Bridge self-call returned HTTP {resp.status_code}: {resp.text[:300]}",
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            raise AdapterError(self.name, f"Bridge response was not JSON: {e}") from e

        choices = data.get("choices") or []
        if not choices:
            raise AdapterError(self.name, f"Bridge response had no choices: {data}")

        markdown_raw = (choices[0].get("message") or {}).get("content", "") or ""
        markdown = markdown_raw.strip()

        if not markdown or markdown == self.UNCONVERTIBLE_TOKEN:
            raise AdapterError(
                self.name,
                f"AI declared {filename!r} unconvertible.",
            )

        markdown = self._strip_code_fence(markdown)

        usage = data.get("usage") or {}
        metadata = {
            "adapter": self.name,
            "warning": "converted via AI fallback - quality may vary",
            "ai_model": self.model,
            "ai_input_tokens": usage.get("prompt_tokens"),
            "ai_output_tokens": usage.get("completion_tokens"),
            "truncated": truncated,
            "input_chars": len(text),
            "filename": filename,
            "original_size_bytes": len(content),
        }
        return ConversionResult(
            markdown=markdown,
            fmt="ai-fallback",
            metadata=metadata,
        )

    @staticmethod
    def _build_prompt(filename: str, mime: Optional[str], sample: str, truncated: bool) -> str:
        return (
            "Convert the following file content into clean Markdown.\n"
            f"Filename: {filename}\n"
            f"MIME hint: {mime or '(none)'}\n"
            f"Truncated to first {AiFallbackAdapter.MAX_TEXT_CHARS} chars: {truncated}\n\n"
            "If the content is unintelligible, encrypted, or cannot meaningfully be "
            "rendered as Markdown, respond with EXACTLY this token and nothing else:\n"
            f"{AiFallbackAdapter.UNCONVERTIBLE_TOKEN}\n\n"
            "Otherwise return ONLY the raw Markdown — no preface, no code fences, "
            "no explanation.\n\n"
            "<file_content>\n"
            f"{sample}\n"
            "</file_content>"
        )

    @staticmethod
    def _try_decode(content: bytes) -> Optional[str]:
        for enc in ("utf-8-sig", "utf-8", "utf-16", "cp1252", "latin-1"):
            try:
                text = content.decode(enc)
            except UnicodeDecodeError:
                continue
            if AiFallbackAdapter._looks_binary(text):
                return None
            return text
        return None

    @staticmethod
    def _looks_binary(text: str) -> bool:
        if not text:
            return False
        sample = text[:4000]
        bad = sum(
            1 for c in sample if c not in "\t\n\r\f\v" and ord(c) < 0x20
        )
        return bad / max(len(sample), 1) > 0.05

    @staticmethod
    def _strip_code_fence(markdown: str) -> str:
        if not markdown.startswith("```"):
            return markdown
        lines = markdown.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)


# ----------------------------------------------------------------------------
# Chain
# ----------------------------------------------------------------------------


class AdapterChain:
    """Iterate adapters in order; first matching ``can_handle`` that succeeds wins.

    Deterministic adapters are checked first (cheap, free). The AI fallback —
    if registered — is the catch-all last entry. If even the AI fallback
    fails, an :class:`UnsupportedFormatError` is raised so the endpoint
    returns HTTP 415.
    """

    def __init__(self, adapters: List[BaseAdapter]):
        if not adapters:
            raise ValueError("AdapterChain needs at least one adapter.")
        self.adapters = adapters

    def convert(
        self,
        content: bytes,
        filename: str,
        mime_type_hint: Optional[str] = None,
    ) -> ConversionResult:
        if not content:
            raise RuntimeError("Empty file.")

        try:
            fmt = detect_format(filename, mime_type_hint)
        except UnsupportedFormatError:
            fmt = "unknown"

        logger.info(
            f"adapter_chain: filename={filename!r} fmt={fmt} mime={mime_type_hint!r} "
            f"size={len(content)}"
        )

        last_error: Optional[BaseException] = None
        attempted: List[str] = []
        for adapter in self.adapters:
            if not adapter.can_handle(fmt, mime_type_hint, filename):
                continue
            attempted.append(adapter.name)
            logger.info(f"adapter_chain: trying {adapter.name}")
            try:
                result = adapter.convert(content, filename, mime_type_hint)
            except AdapterError as e:
                logger.warning(f"adapter_chain: {adapter.name} failed: {e.cause or e}")
                last_error = e
                continue
            except Exception as e:
                logger.warning(
                    f"adapter_chain: {adapter.name} raised unexpected: {e}",
                    exc_info=True,
                )
                last_error = e
                continue
            # Success — tag adapter on metadata for observability
            result.metadata = dict(result.metadata or {})
            result.metadata.setdefault("adapter", adapter.name)
            result.metadata.setdefault("attempted_adapters", attempted)
            return result

        raise UnsupportedFormatError(
            f"No adapter could convert {filename!r} (mime={mime_type_hint!r}, "
            f"format={fmt}). Tried: {attempted or ['(none matched)']}. "
            f"Last error: {last_error}"
        )


def build_default_chain(*, enable_ai_fallback: bool = True) -> AdapterChain:
    """Build the production adapter chain.

    Order matters — deterministic adapters first (cheap), AI fallback last.
    """
    adapters: List[BaseAdapter] = [
        PdfAdapter(),
        DocxAdapter(),
        PptxAdapter(),
        XlsxAdapter(),
        CsvAdapter(),
        HtmlAdapter(),
        MsgAdapter(),
        EmlAdapter(),
        ImageAdapter(),
    ]
    if enable_ai_fallback:
        adapters.append(AiFallbackAdapter())
    return AdapterChain(adapters)
