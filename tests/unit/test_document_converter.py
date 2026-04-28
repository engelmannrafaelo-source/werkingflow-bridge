"""
Unit tests for the universal document converter.

These tests cover the format-detection layer plus the lightweight adapters
(CSV / HTML / EML / XLSX / MSG). Heavy adapters (PDF, DOCX, PPTX, image) need
Docling and/or LibreOffice and are smoke-tested at the container level rather
than here.

Tests are written defensively: each adapter test ``importorskip``s its
runtime dependency so the suite degrades to skipped tests on a base
checkout instead of failing.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.privacy_service.document_converter import (  # noqa: E402
    UnsupportedFormatError,
    convert_document_sync,
    convert_csv_bytes,
    convert_eml_bytes,
    convert_html_bytes,
    convert_xlsx_bytes,
    detect_format,
)


# ----------------------------------------------------------------------------
# Format detection
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("report.pdf", "pdf"),
        ("Quote.DOCX", "docx"),
        ("slides.pptx", "pptx"),
        ("data.xlsx", "xlsx"),
        ("legacy.xls", "xlsx"),
        ("table.csv", "csv"),
        ("page.HTML", "html"),
        ("notice.htm", "html"),
        ("mail.msg", "msg"),
        ("mail.eml", "eml"),
        ("photo.PNG", "image"),
        ("scan.jpeg", "image"),
    ],
)
def test_detect_format_by_extension(filename, expected):
    assert detect_format(filename) == expected


def test_detect_format_mime_hint_overrides_extension():
    # Filename has wrong extension but MIME says PDF — MIME wins.
    assert detect_format("upload.bin", mime_type_hint="application/pdf") == "pdf"


def test_detect_format_handles_charset_suffix():
    # Real-world MIME types often look like ``text/csv; charset=utf-8``.
    assert detect_format("file", mime_type_hint="text/csv; charset=utf-8") == "csv"


def test_detect_format_unknown_raises():
    with pytest.raises(UnsupportedFormatError):
        detect_format("mystery.xyz")


def test_detect_format_no_extension_no_mime_raises():
    with pytest.raises(UnsupportedFormatError):
        detect_format("LICENSE")


# ----------------------------------------------------------------------------
# CSV adapter
# ----------------------------------------------------------------------------


def test_convert_csv_basic():
    pytest.importorskip("pandas")
    pytest.importorskip("tabulate")
    csv = b"name,city\nAlice,Wien\nBob,Graz\n"
    markdown, meta, images = convert_csv_bytes(csv)
    assert "Alice" in markdown
    assert "Bob" in markdown
    assert "|" in markdown  # Markdown table pipe
    assert meta["rows"] == 2
    assert meta["columns"] == ["name", "city"]
    assert images == {}


def test_convert_csv_semicolon_delimiter():
    """pandas' Python-engine sniffer should detect semicolons."""
    pytest.importorskip("pandas")
    pytest.importorskip("tabulate")
    csv = b"name;city\nAlice;Wien\nBob;Graz\n"
    markdown, meta, _ = convert_csv_bytes(csv)
    assert meta["rows"] == 2
    assert meta["columns"] == ["name", "city"]
    assert "Alice" in markdown


def test_convert_csv_latin1_encoding():
    pytest.importorskip("pandas")
    pytest.importorskip("tabulate")
    # ``Müller`` in cp1252.
    csv = "name,city\nMüller,Wien\n".encode("cp1252")
    markdown, meta, _ = convert_csv_bytes(csv)
    assert "Müller" in markdown
    assert meta["rows"] == 1


# ----------------------------------------------------------------------------
# HTML adapter
# ----------------------------------------------------------------------------


def test_convert_html_basic():
    pytest.importorskip("markdownify")
    html = b"<html><body><h1>Title</h1><p>Hello <b>world</b>.</p></body></html>"
    markdown, meta, images = convert_html_bytes(html)
    assert "Title" in markdown
    assert "Hello" in markdown
    assert meta["original_size_bytes"] == len(html)
    assert images == {}


def test_convert_html_uses_atx_headings():
    pytest.importorskip("markdownify")
    html = b"<h2>Subtitle</h2>"
    markdown, _, _ = convert_html_bytes(html)
    assert markdown.strip().startswith("##")


# ----------------------------------------------------------------------------
# EML adapter (stdlib only)
# ----------------------------------------------------------------------------


SAMPLE_EML = (
    b"From: Alice <alice@example.com>\r\n"
    b"To: Bob <bob@example.com>\r\n"
    b"Subject: Hello\r\n"
    b"Date: Mon, 1 Jan 2024 12:00:00 +0000\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Hi Bob, this is a test email.\r\n"
)


def test_convert_eml_plain():
    markdown, meta, _ = convert_eml_bytes(SAMPLE_EML)
    assert "# Hello" in markdown
    assert "From:" in markdown and "alice@example.com" in markdown
    assert "Hi Bob, this is a test email." in markdown
    assert meta["subject"] == "Hello"
    assert "alice@example.com" in (meta["from"] or "")


SAMPLE_EML_HTML = (
    b"From: Alice <alice@example.com>\r\n"
    b"To: Bob <bob@example.com>\r\n"
    b"Subject: HTML Email\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n"
    b"\r\n"
    b"<html><body><p>Hello <b>HTML</b> world</p></body></html>\r\n"
)


def test_convert_eml_html_falls_back_to_markdown():
    pytest.importorskip("markdownify")
    markdown, meta, _ = convert_eml_bytes(SAMPLE_EML_HTML)
    assert meta["subject"] == "HTML Email"
    # Markdownify keeps the bold formatting and the literal word.
    assert "HTML" in markdown


# ----------------------------------------------------------------------------
# XLSX adapter (uses openpyxl + pandas)
# ----------------------------------------------------------------------------


def _build_sample_xlsx() -> bytes:
    pytest.importorskip("openpyxl")
    from openpyxl import Workbook

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Customers"
    ws1.append(["name", "city"])
    ws1.append(["Alice", "Wien"])
    ws1.append(["Bob", "Graz"])

    ws2 = wb.create_sheet("Orders")
    ws2.append(["order_id", "amount"])
    ws2.append([1001, 49.99])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_convert_xlsx_two_sheets():
    pytest.importorskip("openpyxl")
    pytest.importorskip("pandas")
    pytest.importorskip("tabulate")

    content = _build_sample_xlsx()
    markdown, meta, images = convert_xlsx_bytes(content)

    assert "## Customers" in markdown
    assert "## Orders" in markdown
    assert "Alice" in markdown
    assert "1001" in markdown
    assert meta["sheet_count"] == 2
    sheet_names = {s["name"] for s in meta["sheets"]}
    assert sheet_names == {"Customers", "Orders"}
    customers = next(s for s in meta["sheets"] if s["name"] == "Customers")
    assert customers["rows"] == 2
    assert customers["columns"] == ["name", "city"]
    assert images == {}


# ----------------------------------------------------------------------------
# Dispatcher: end-to-end + edge cases
# ----------------------------------------------------------------------------


def test_dispatcher_routes_csv():
    pytest.importorskip("pandas")
    pytest.importorskip("tabulate")
    result = convert_document_sync(b"a,b\n1,2\n", "tiny.csv")
    assert result.fmt == "csv"
    assert result.metadata["rows"] == 1
    assert result.metadata["filename"] == "tiny.csv"
    assert result.metadata["original_size_bytes"] == len(b"a,b\n1,2\n")


def test_dispatcher_routes_eml():
    result = convert_document_sync(SAMPLE_EML, "sample.eml")
    assert result.fmt == "eml"
    assert "Hello" in result.markdown


def test_dispatcher_unknown_format_raises():
    with pytest.raises(UnsupportedFormatError):
        convert_document_sync(b"some bytes", "mystery.xyz")


def test_dispatcher_empty_file_raises():
    with pytest.raises(RuntimeError):
        convert_document_sync(b"", "anything.csv")


def test_dispatcher_mime_hint_overrides_extension():
    pytest.importorskip("pandas")
    pytest.importorskip("tabulate")
    # ``.bin`` has no extension mapping; MIME hint forces CSV adapter.
    result = convert_document_sync(b"a,b\n1,2\n", "tiny.bin", mime_type_hint="text/csv")
    assert result.fmt == "csv"


# ----------------------------------------------------------------------------
# MSG adapter (extract-msg) — only run if dependency + sample available
# ----------------------------------------------------------------------------


def test_convert_msg_smoke():
    """Smoke-test the MSG adapter only if extract-msg is installed.

    extract-msg can read its own sample tests so we generate a minimal MSG by
    using the package's compose helpers when available; otherwise skip.
    """
    extract_msg = pytest.importorskip("extract_msg")

    # Find an example MSG bundled with extract-msg's tests so we don't have to
    # ship binaries in this repo. If not present, skip — pytest will still cover
    # the dispatcher routing through detect_format earlier.
    pkg_dir = os.path.dirname(extract_msg.__file__)
    candidates = []
    for root, _dirs, files in os.walk(pkg_dir):
        for fname in files:
            if fname.lower().endswith(".msg"):
                candidates.append(os.path.join(root, fname))
    if not candidates:
        pytest.skip("No bundled .msg sample in extract-msg package")

    with open(candidates[0], "rb") as f:
        content = f.read()
    from src.privacy_service.document_converter import convert_msg_bytes

    markdown, meta, _ = convert_msg_bytes(content)
    assert isinstance(markdown, str) and len(markdown) > 0
    # Subject may be empty for some samples — just assert key is present.
    assert "subject" in meta
