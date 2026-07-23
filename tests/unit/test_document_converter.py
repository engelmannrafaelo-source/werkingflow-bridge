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

import base64  # noqa: E402
import io  # noqa: E402

from src.privacy_service import document_converter as dc  # noqa: E402
from src.privacy_service.document_converter import (  # noqa: E402
    MAX_IMAGE_EDGE,
    RENDER_PAGE_MAX_EDGE,
    UnsupportedFormatError,
    convert_document_sync,
    convert_csv_bytes,
    convert_eml_bytes,
    convert_html_bytes,
    convert_pdf_bytes,
    convert_xlsx_bytes,
    detect_format,
)

# Real large-format CAD plan (~4564pt long edge, 132-MP embedded rasters). This
# is exactly the file that used to 415→ai-fallback→401 before the render→Vision
# cascade. Tests that touch it importorskip pypdfium2/Pillow so a base checkout
# degrades to skipped rather than failing.
BAUPLAN_PDF = Path(
    "/root/projekte/local-storage/test-state/test-data/werking-energy/"
    "kurt-real/context/19_036_BGN_VA_2020-07-21_gross.pdf"
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


# ----------------------------------------------------------------------------
# PDF render→Vision cascade
# ----------------------------------------------------------------------------


def _png_b64(width: int, height: int) -> str:
    """Build a plain base64 PNG of the given pixel size (test figure)."""
    pytest.importorskip("PIL")
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    img = Image.new("RGB", (width, height), (180, 160, 140))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _decoded_size(b64: str):
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = None
    with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
        return im.size


def test_downscale_guard_shrinks_giant_figure():
    """A 132-MP figure (the thing that timed out Vision) is shrunk under the cap."""
    pytest.importorskip("PIL")
    giant = _png_b64(12000, 11000)  # 132 MP
    shrunk = dc._downscale_b64_png(giant)
    w, h = _decoded_size(shrunk)
    assert max(w, h) <= MAX_IMAGE_EDGE
    assert (w, h) != (12000, 11000)


def test_downscale_guard_is_idempotent_for_small_images():
    pytest.importorskip("PIL")
    small = _png_b64(800, 600)
    assert dc._downscale_b64_png(small) == small


def test_pdf_geometry_reads_native_text_and_size():
    pytest.importorskip("pypdfium2")
    if not BAUPLAN_PDF.exists():
        pytest.skip("Bauplan reference PDF not present in this environment")
    geometry = dc._pdf_page_geometry(BAUPLAN_PDF.read_bytes())
    assert len(geometry) == 2
    # Real text layer (CAD labels) AND large-format dimensions.
    assert geometry[0][0] > 1000  # native text chars
    assert geometry[0][1] > dc.OVERSIZE_PAGE_LONG_EDGE_PT  # oversized long edge


def test_render_pages_within_edge_cap():
    pytest.importorskip("pypdfium2")
    pytest.importorskip("PIL")
    if not BAUPLAN_PDF.exists():
        pytest.skip("Bauplan reference PDF not present in this environment")
    rendered = dc._render_pdf_pages(BAUPLAN_PDF.read_bytes(), [0, 1])
    assert set(rendered) == {"page-001.png", "page-002.png"}
    for b64 in rendered.values():
        w, h = _decoded_size(b64)
        assert max(w, h) <= RENDER_PAGE_MAX_EDGE


def test_convert_pdf_oversized_plan_renders_pages(monkeypatch):
    """Docling parses the plan's text, but oversized pages are still rendered
    whole for Vision AND any giant extracted figure is down-scaled."""
    pytest.importorskip("pypdfium2")
    pytest.importorskip("PIL")
    if not BAUPLAN_PDF.exists():
        pytest.skip("Bauplan reference PDF not present in this environment")

    giant_figure = _png_b64(12000, 11000)

    def _fake_docling(pdf_bytes):
        return "# Plan text\n\nSTAHLBETON …", 2, {"figure-1.png": giant_figure}

    monkeypatch.setattr(dc, "_docling_convert_pdf", _fake_docling)

    markdown, meta, images = convert_pdf_bytes(BAUPLAN_PDF.read_bytes())

    assert meta["docling_parsed"] is True
    assert "Plan text" in markdown  # docling text preserved
    # Both oversized pages rendered whole …
    assert "page-001.png" in images and "page-002.png" in images
    assert meta["rendered_page_numbers"] == [1, 2]
    # … and the 132-MP figure was down-scaled by the guard.
    fw, fh = _decoded_size(images["figure-1.png"])
    assert max(fw, fh) <= MAX_IMAGE_EDGE


def test_convert_pdf_docling_failure_renders_all_pages(monkeypatch):
    """When Docling cannot parse the PDF at all, every page is rendered for
    Vision and the markdown references those page images (no 415/crash)."""
    pytest.importorskip("pypdfium2")
    pytest.importorskip("PIL")
    if not BAUPLAN_PDF.exists():
        pytest.skip("Bauplan reference PDF not present in this environment")

    def _boom(pdf_bytes):
        raise RuntimeError("docling exploded on this file")

    monkeypatch.setattr(dc, "_docling_convert_pdf", _boom)

    markdown, meta, images = convert_pdf_bytes(BAUPLAN_PDF.read_bytes())

    assert meta["docling_parsed"] is False
    assert set(images) == {"page-001.png", "page-002.png"}
    # Render-only markdown references each page so descriptions attach cleanly.
    assert "page-001.png" in markdown and "page-002.png" in markdown


# ---------------------------------------------------------------------------
# _split_paged_markdown (per-page markdown for selective anonymization)
# ---------------------------------------------------------------------------

from src.privacy_service.document_converter import (  # noqa: E402
    PAGE_BREAK_TOKEN,
    _split_paged_markdown,
)


class TestSplitPagedMarkdown:
    def test_happy_path_three_pages(self):
        paged = f"Seite eins\n{PAGE_BREAK_TOKEN}\nSeite zwei\n{PAGE_BREAK_TOKEN}\nSeite drei"
        result = _split_paged_markdown(paged, 3)
        assert result == [
            {"page_no": 1, "markdown": "Seite eins"},
            {"page_no": 2, "markdown": "Seite zwei"},
            {"page_no": 3, "markdown": "Seite drei"},
        ]

    def test_single_page_document(self):
        result = _split_paged_markdown("Nur eine Seite", 1)
        assert result == [{"page_no": 1, "markdown": "Nur eine Seite"}]

    def test_blank_page_mismatch_returns_none(self):
        # Docling emits the token only between content-bearing pages: a blank
        # page yields fewer parts than page_count. A wrong page map would
        # misdirect selective anonymization — absence is the contract.
        paged = f"Seite eins\n{PAGE_BREAK_TOKEN}\nSeite vier"
        assert _split_paged_markdown(paged, 3) is None

    def test_token_collision_returns_none(self):
        # A document that CONTAINS the token corrupts the split → count
        # mismatch → safe omission instead of a wrong page map.
        paged = (
            f"Text {PAGE_BREAK_TOKEN} mitten im Inhalt\n"
            f"{PAGE_BREAK_TOKEN}\nSeite zwei"
        )
        assert _split_paged_markdown(paged, 2) is None

    def test_page_count_none_returns_none(self):
        assert _split_paged_markdown("Inhalt", None) is None

    def test_page_count_zero_returns_none(self):
        assert _split_paged_markdown("", 0) is None

    def test_inner_newlines_preserved_outer_stripped(self):
        paged = f"\nZeile A\n\nZeile B\n{PAGE_BREAK_TOKEN}\nZeile C\n"
        result = _split_paged_markdown(paged, 2)
        assert result == [
            {"page_no": 1, "markdown": "Zeile A\n\nZeile B"},
            {"page_no": 2, "markdown": "Zeile C"},
        ]
