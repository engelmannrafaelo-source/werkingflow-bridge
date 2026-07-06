"""
Adapter chain for the universal document conversion endpoint.

Each adapter declares whether it ``can_handle`` a given (format, MIME, filename)
tuple and, if so, ``convert``s the bytes into a :class:`ConversionResult`.

The :class:`AdapterChain` walks adapters in order, deterministic-first. There is
no AI/self-call fallback: PDFs that carry no text layer (scans, plans, photos)
are handled inside :func:`convert_pdf_bytes` via a per-page render→Vision
cascade, and a genuinely unknown format fails loud with HTTP 415 — matching the
service's "never silently fall back to a different adapter" design constraint.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import List, Optional

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
# Chain
# ----------------------------------------------------------------------------


class AdapterChain:
    """Iterate adapters in order; first matching ``can_handle`` that succeeds wins.

    Deterministic adapters are checked first (cheap, free). If none can convert
    the file, an :class:`UnsupportedFormatError` is raised so the endpoint
    returns HTTP 415 — the service never silently AI-guesses an unknown format.
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


def build_default_chain() -> AdapterChain:
    """Build the production adapter chain (deterministic adapters, no AI guess).

    Order matters — deterministic adapters first (cheap). Image/scan/plan PDFs
    are covered by the render→Vision cascade inside :func:`convert_pdf_bytes`,
    so no catch-all AI fallback is needed; unknown formats fail loud (HTTP 415).
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
    return AdapterChain(adapters)
