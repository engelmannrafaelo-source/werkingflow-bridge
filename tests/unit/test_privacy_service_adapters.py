"""
Unit tests for the adapter chain and the AI fallback adapter.

The deterministic adapters are already covered through
``test_document_converter.py`` (per-format unit tests on the underlying
``convert_*_bytes`` functions). These tests focus on the chain + the
self-healing AI fallback path with a mocked Bridge self-call.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.privacy_service.adapters import (  # noqa: E402
    AdapterChain,
    AdapterError,
    AiFallbackAdapter,
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


def test_ai_fallback_can_handle_anything():
    a = AiFallbackAdapter()
    assert a.can_handle("pdf", None, "x.pdf") is True
    assert a.can_handle("unknown", None, "x.xyz") is True
    assert a.can_handle("", None, "") is True


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


def test_build_default_chain_includes_ai_fallback_by_default():
    chain = build_default_chain()
    names = [a.name for a in chain.adapters]
    assert names[-1] == "ai-fallback"
    assert "pdf" in names and "csv" in names and "html" in names


def test_build_default_chain_can_disable_ai_fallback():
    chain = build_default_chain(enable_ai_fallback=False)
    names = [a.name for a in chain.adapters]
    assert "ai-fallback" not in names


# ---------------------------------------------------------------------------
# AiFallbackAdapter — happy path with mocked Bridge
# ---------------------------------------------------------------------------


class _MockResponse:
    def __init__(self, status_code: int, body: Dict[str, Any], text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text or str(body)

    def json(self) -> Dict[str, Any]:
        return self._body


class _MockClientCtx:
    """Stand-in for ``httpx.Client`` context manager."""

    def __init__(self, resp_factory):
        self._resp_factory = resp_factory
        self.last_url: Optional[str] = None
        self.last_headers: Optional[Dict[str, str]] = None
        self.last_payload: Optional[Dict[str, Any]] = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):  # noqa: A002 — match httpx api
        self.last_url = url
        self.last_headers = headers
        self.last_payload = json
        return self._resp_factory()


def _patch_httpx_client(monkeypatch, resp_factory) -> _MockClientCtx:
    ctx = _MockClientCtx(resp_factory)
    import src.privacy_service.adapters as adapters_mod

    monkeypatch.setattr(adapters_mod.httpx, "Client", lambda *a, **kw: ctx)
    return ctx


def test_ai_fallback_returns_markdown(monkeypatch):
    body = {
        "choices": [{"message": {"content": "# Hi\n\nHello world."}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 6},
    }
    ctx = _patch_httpx_client(monkeypatch, lambda: _MockResponse(200, body))

    adapter = AiFallbackAdapter(bridge_url="http://test/v1/chat/completions", api_key="K")
    result = adapter.convert(b"some plain text", "weird.dat", mime="application/octet-stream")
    assert result.fmt == "ai-fallback"
    assert "Hello world" in result.markdown
    assert result.metadata["adapter"] == "ai-fallback"
    assert "AI fallback" in result.metadata["warning"]
    assert result.metadata["ai_input_tokens"] == 10
    assert ctx.last_url == "http://test/v1/chat/completions"
    assert ctx.last_headers["Authorization"] == "Bearer K"
    assert ctx.last_payload["model"]
    assert "weird.dat" in ctx.last_payload["messages"][0]["content"]


def test_ai_fallback_strips_code_fences(monkeypatch):
    body = {
        "choices": [{"message": {"content": "```markdown\n# Title\n\nBody.\n```"}}],
        "usage": {},
    }
    _patch_httpx_client(monkeypatch, lambda: _MockResponse(200, body))
    adapter = AiFallbackAdapter(bridge_url="http://x", api_key=None)
    result = adapter.convert(b"text", "f.txt", mime="text/plain")
    assert result.markdown.startswith("# Title")
    assert "```" not in result.markdown


def test_ai_fallback_unconvertible_token_raises(monkeypatch):
    body = {
        "choices": [{"message": {"content": "<<UNCONVERTIBLE>>"}}],
        "usage": {},
    }
    _patch_httpx_client(monkeypatch, lambda: _MockResponse(200, body))
    adapter = AiFallbackAdapter(bridge_url="http://x", api_key=None)
    with pytest.raises(AdapterError, match="unconvertible"):
        adapter.convert(b"some text", "secret.dat", mime=None)


def test_ai_fallback_http_error_raises(monkeypatch):
    _patch_httpx_client(
        monkeypatch, lambda: _MockResponse(500, {}, text="upstream boom")
    )
    adapter = AiFallbackAdapter(bridge_url="http://x", api_key=None)
    with pytest.raises(AdapterError, match="HTTP 500"):
        adapter.convert(b"text", "f.txt", mime=None)


def test_ai_fallback_truncates_large_input(monkeypatch):
    payload_seen: Dict[str, Any] = {}

    def resp_factory():
        return _MockResponse(
            200,
            {"choices": [{"message": {"content": "ok"}}], "usage": {}},
        )

    ctx = _patch_httpx_client(monkeypatch, resp_factory)

    big = ("a" * (AiFallbackAdapter.MAX_TEXT_CHARS + 5000)).encode("utf-8")
    adapter = AiFallbackAdapter(bridge_url="http://x", api_key=None)
    result = adapter.convert(big, "huge.txt", mime="text/plain")
    assert result.metadata["truncated"] is True
    assert result.metadata["input_chars"] == AiFallbackAdapter.MAX_TEXT_CHARS + 5000
    # Extract the content actually sent inside the <file_content> tag and check
    # it was truncated to the configured cap.
    sent_prompt = ctx.last_payload["messages"][0]["content"]
    sent_block = sent_prompt.split("<file_content>\n", 1)[1].split("\n</file_content>", 1)[0]
    assert len(sent_block) == AiFallbackAdapter.MAX_TEXT_CHARS


def test_ai_fallback_binary_blob_fails(monkeypatch):
    """Random binary content (lots of NULs) is not text-decodable → fail loud."""
    # Build a payload that decodes as text in latin-1 (so try_decode returns
    # a string) but has so many control chars _looks_binary trips.
    binary = bytes([0x00, 0x01, 0x02, 0x03] * 200)
    adapter = AiFallbackAdapter(bridge_url="http://x", api_key=None)
    with pytest.raises(AdapterError, match="binary"):
        adapter.convert(binary, "blob.bin", mime="application/octet-stream")


# ---------------------------------------------------------------------------
# AdapterChain integration with AiFallbackAdapter
# ---------------------------------------------------------------------------


def test_chain_uses_ai_fallback_for_unknown_extension(monkeypatch):
    body = {
        "choices": [{"message": {"content": "# Recovered\n\nText here."}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 4},
    }
    _patch_httpx_client(monkeypatch, lambda: _MockResponse(200, body))

    chain = build_default_chain(enable_ai_fallback=True)
    result = chain.convert(b"hello world\nmore lines", "mystery.xyz", mime_type_hint=None)
    assert result.fmt == "ai-fallback"
    assert result.metadata["adapter"] == "ai-fallback"
    assert "Recovered" in result.markdown


def test_chain_prefers_deterministic_over_ai(monkeypatch):
    """CSV files must hit the CSV adapter — never burn AI tokens."""
    pytest.importorskip("pandas")
    pytest.importorskip("tabulate")

    # Patch httpx.Client to raise if accidentally called — proves AI is NOT used.
    class _Fail:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            raise AssertionError("AI fallback should not be called for CSV input")

    import src.privacy_service.adapters as adapters_mod

    monkeypatch.setattr(adapters_mod.httpx, "Client", lambda *a, **kw: _Fail())

    chain = build_default_chain(enable_ai_fallback=True)
    result = chain.convert(b"a,b\n1,2\n", "table.csv", "text/csv")
    assert result.fmt == "csv"
    assert result.metadata["adapter"] == "csv"
