"""
Unit tests for the adapter chain.

The deterministic adapters are already covered through
``test_document_converter.py`` (per-format unit tests on the underlying
``convert_*_bytes`` functions). These tests focus on the chain routing and its
fail-loud behaviour for unknown formats. There is no AI/self-call fallback any
more: image/scan/plan PDFs are handled by the render→Vision cascade inside
``convert_pdf_bytes`` and unknown formats fail loud with HTTP 415.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.privacy_service.adapters import (  # noqa: E402
    AdapterChain,
    AdapterError,
    BaseAdapter,
    CsvAdapter,
    HtmlAdapter,
    PdfAdapter,
    build_default_chain,
)
from src.privacy_service.document_converter import (  # noqa: E402
    ConversionResult,
    UnsupportedFormatError,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _StubAdapter(BaseAdapter):
    """Adapter test double — accepts a configurable can_handle / convert."""

    def __init__(
        self,
        name: str,
        handles: bool,
        result: Optional[ConversionResult] = None,
        raise_error: Optional[Exception] = None,
    ):
        self.name = name
        self._handles = handles
        self._result = result
        self._raise = raise_error
        self.calls = 0

    def can_handle(self, fmt, mime, filename) -> bool:
        return self._handles

    def convert(self, content, filename, mime) -> ConversionResult:
        self.calls += 1
        if self._raise is not None:
            raise self._raise
        assert self._result is not None
        return self._result


# ---------------------------------------------------------------------------
# can_handle dispatch
# ---------------------------------------------------------------------------


def test_pdf_adapter_can_handle_only_pdf():
    a = PdfAdapter()
    assert a.can_handle("pdf", None, "x.pdf") is True
    assert a.can_handle("docx", None, "x.docx") is False


def test_csv_adapter_can_handle_only_csv():
    a = CsvAdapter()
    assert a.can_handle("csv", "text/csv", "x.csv") is True
    assert a.can_handle("xlsx", None, "x.xlsx") is False


def test_html_adapter_can_handle_only_html():
    a = HtmlAdapter()
    assert a.can_handle("html", None, "x.html") is True
    assert a.can_handle("pdf", None, "x.pdf") is False


# ---------------------------------------------------------------------------
# AdapterChain routing
# ---------------------------------------------------------------------------


def _ok_result(name: str) -> ConversionResult:
    return ConversionResult(markdown=f"md-{name}", fmt=name, metadata={"adapter": name})


def test_chain_picks_first_matching_adapter():
    a1 = _StubAdapter("a1", handles=False)
    a2 = _StubAdapter("a2", handles=True, result=_ok_result("a2"))
    a3 = _StubAdapter("a3", handles=True, result=_ok_result("a3"))
    chain = AdapterChain([a1, a2, a3])
    result = chain.convert(b"some content", "x.csv", "text/csv")
    assert result.metadata["adapter"] == "a2"
    assert a2.calls == 1
    assert a3.calls == 0  # never tried — a2 succeeded first


def test_chain_falls_through_on_failure_to_next_handler():
    boom = _StubAdapter("first", handles=True, raise_error=AdapterError("first", "boom"))
    ok = _StubAdapter("second", handles=True, result=_ok_result("second"))
    chain = AdapterChain([boom, ok])
    result = chain.convert(b"data", "x.csv", "text/csv")
    assert result.metadata["adapter"] == "second"
    assert boom.calls == 1
    assert ok.calls == 1


def test_chain_empty_content_raises():
    chain = AdapterChain([_StubAdapter("only", handles=True, result=_ok_result("only"))])
    with pytest.raises(RuntimeError, match="Empty file"):
        chain.convert(b"", "anything.csv")


def test_chain_no_adapter_matches_raises_unsupported():
    chain = AdapterChain([_StubAdapter("none", handles=False)])
    with pytest.raises(UnsupportedFormatError):
        chain.convert(b"data", "anything.xyz")


def test_chain_records_attempted_adapters_in_metadata():
    failing = _StubAdapter("flaky", handles=True, raise_error=AdapterError("flaky", "nope"))
    winner = _StubAdapter("winner", handles=True, result=_ok_result("winner"))
    chain = AdapterChain([failing, winner])
    result = chain.convert(b"data", "x.csv", "text/csv")
    assert result.metadata["attempted_adapters"] == ["flaky", "winner"]


# ---------------------------------------------------------------------------
# Default chain: deterministic-only, no AI fallback
# ---------------------------------------------------------------------------


def test_build_default_chain_is_deterministic_only():
    chain = build_default_chain()
    names = [a.name for a in chain.adapters]
    assert "ai-fallback" not in names
    assert "pdf" in names and "csv" in names and "html" in names
    # Image is the last deterministic adapter — nothing catch-all after it.
    assert names[-1] == "image"


def test_chain_unknown_extension_fails_loud():
    """An unknown format is no longer AI-guessed — it fails loud (→ HTTP 415)."""
    chain = build_default_chain()
    with pytest.raises(UnsupportedFormatError):
        chain.convert(b"hello world\nmore lines", "mystery.xyz", mime_type_hint=None)


def test_chain_prefers_deterministic_adapter_for_csv():
    """CSV files hit the CSV adapter directly."""
    pytest.importorskip("pandas")
    pytest.importorskip("tabulate")

    chain = build_default_chain()
    result = chain.convert(b"a,b\n1,2\n", "table.csv", "text/csv")
    assert result.fmt == "csv"
    assert result.metadata["adapter"] == "csv"


def test_chain_mime_hint_overrides_extension():
    """A mismatched extension (``.bin``) is routed by the explicit MIME hint."""
    pytest.importorskip("pandas")
    pytest.importorskip("tabulate")

    chain = build_default_chain()
    result = chain.convert(b"a,b\n1,2\n", "upload.bin", mime_type_hint="text/csv")
    assert result.fmt == "csv"
    assert result.metadata["adapter"] == "csv"
