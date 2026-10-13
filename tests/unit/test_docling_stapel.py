"""Docling page batching (DOCLING_BATCH_PAGES) and the hard page cap (DOCLING_MAX_PAGES).

Background: one Docling ``convert`` over a 1393-page PDF took the shared GPU
document service (Dev AND Prod) down by OOM on 12.10.2026 — Docling keeps every
page raster until the whole conversion ends. Large PDFs are now converted in
``page_range`` batches and concatenated; above a hard cap they are refused (413).

Three layers, each ``importorskip``-guarded so a base checkout skips rather than
fails:

1. Cap + wiring (no Docling): the refusal happens before Docling and before any
   page render, survives the adapter chain and reaches the client as 413.
2. Batch logic against a fake converter (docling-core only): page ranges, order,
   page numbers, PARTIAL_SUCCESS / missing pages / raising batch fail loud.
3. Real Docling over synthetic PDFs (no customer data): batched output equals the
   single-pass output — markdown, figures, page_markdowns, figure pages.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.privacy_service import document_converter as dc  # noqa: E402
from src.privacy_service.document_converter import (  # noqa: E402
    DocumentTooLargeError,
    convert_pdf_bytes,
)


def _fake_geometry(pages: int):
    return lambda pdf_bytes: [(500, 842.0)] * pages


def _synthetic_pdf(pages: int, *, blank: tuple = (), figures: bool = True) -> bytes:
    """A4 pages with a numbered heading, body lines and (optionally) one photo-like
    picture + caption each. ``blank`` holds 1-based page numbers left empty."""
    pytest.importorskip("reportlab")
    from PIL import Image, ImageDraw
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    for p in range(1, pages + 1):
        if p not in blank:
            c.setFont("Helvetica-Bold", 16)
            c.drawString(60, h - 70, f"Abschnitt {p}: Messbericht Seite {p}")
            c.setFont("Helvetica", 11)
            for i in range(4):
                c.drawString(60, h - 100 - 16 * i, f"Seite {p} Zeile {i + 1}: Wert {100 + p + i} Watt.")
            if figures:
                im = Image.new("RGB", (600, 400), (20 * p % 256, 90, 160))
                d = ImageDraw.Draw(im)
                d.ellipse((50 + 10 * p, 50, 300 + 10 * p, 300), fill=(240, 200 - 5 * p, 40))
                d.rectangle((350, 100 - p, 550, 350), fill=(30, 30 + 7 * p, 30))
                img = io.BytesIO()
                im.save(img, "PNG")
                img.seek(0)
                c.drawImage(ImageReader(img), 60, h - 400, width=300, height=200)
                c.drawString(60, h - 415, f"Foto {p}: Heizkessel Seite {p}")
        c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. Cap and wiring — no Docling needed
# ---------------------------------------------------------------------------


def test_cap_refuses_before_docling_and_render(monkeypatch):
    monkeypatch.setenv("DOCLING_MAX_PAGES", "3")
    monkeypatch.setattr(dc, "_pdf_page_geometry", _fake_geometry(4))

    def _must_not_run(*a, **k):
        raise AssertionError("Docling/render must not run for a refused PDF")

    monkeypatch.setattr(dc, "_docling_convert_pdf", _must_not_run)
    monkeypatch.setattr(dc, "_render_pdf_pages", _must_not_run)

    with pytest.raises(DocumentTooLargeError) as exc:
        convert_pdf_bytes(b"%PDF-fake")
    msg = str(exc.value)
    assert "4 Seiten" in msg and "3 Seiten" in msg and "DOCLING_MAX_PAGES" in msg


def test_cap_boundary_is_inclusive(monkeypatch):
    monkeypatch.setenv("DOCLING_MAX_PAGES", "3")
    monkeypatch.setattr(dc, "_pdf_page_geometry", _fake_geometry(3))
    monkeypatch.setattr(
        dc, "_docling_convert_pdf", lambda pdf_bytes: ("# ok", 3, {}, None, None)
    )
    markdown, meta, images = convert_pdf_bytes(b"%PDF-fake")
    assert markdown == "# ok" and meta["pages"] == 3 and meta["docling_parsed"] is True


def test_default_cap_is_documented_constant(monkeypatch):
    monkeypatch.delenv("DOCLING_MAX_PAGES", raising=False)
    monkeypatch.delenv("DOCLING_BATCH_PAGES", raising=False)
    assert dc._docling_max_pages() == dc.DOCLING_MAX_PAGES_DEFAULT
    assert dc._docling_batch_pages() == dc.DOCLING_BATCH_PAGES_DEFAULT
    assert dc.DOCLING_BATCH_PAGES_DEFAULT < dc.DOCLING_MAX_PAGES_DEFAULT


@pytest.mark.parametrize("name", ["DOCLING_MAX_PAGES", "DOCLING_BATCH_PAGES"])
@pytest.mark.parametrize("raw", ["0", "-5", "abc", "2.5"])
def test_bad_config_fails_loud_not_as_docling_fallback(monkeypatch, name, raw):
    """A bad value must surface as an error, not be swallowed into the
    'Docling failed → render every page' fallback."""
    monkeypatch.setenv(name, raw)
    monkeypatch.setattr(dc, "_pdf_page_geometry", _fake_geometry(2))
    monkeypatch.setattr(
        dc, "_render_pdf_pages", lambda *a, **k: pytest.fail("fallback render ran")
    )
    with pytest.raises(ValueError, match=name):
        convert_pdf_bytes(b"%PDF-fake")


def test_chain_does_not_turn_refusal_into_415(monkeypatch):
    from src.privacy_service.adapters import build_default_chain

    monkeypatch.setenv("DOCLING_MAX_PAGES", "2")
    monkeypatch.setattr(dc, "_pdf_page_geometry", _fake_geometry(5))
    with pytest.raises(DocumentTooLargeError):
        build_default_chain().convert(b"%PDF-fake", "gross.pdf", "application/pdf")


@pytest.fixture()
def client(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from src.privacy_service import app as app_mod

    monkeypatch.setattr(app_mod, "_DOCUMENT_CHAIN", None)
    monkeypatch.setenv("DOCLING_MAX_PAGES", "2")
    monkeypatch.setattr(dc, "_pdf_page_geometry", _fake_geometry(5))
    return TestClient(app_mod.app)


@pytest.mark.parametrize("path", ["/document/convert", "/document/convert-and-anonymize"])
def test_endpoints_answer_413_with_reason(client, path):
    resp = client.post(path, files={"file": ("gross.pdf", b"%PDF-fake", "application/pdf")})
    assert resp.status_code == 413, resp.text
    assert "5 Seiten" in resp.json()["detail"]


def test_legacy_convert_pdf_answers_413_with_reason(client):
    resp = client.post("/convert-pdf", files={"file": ("gross.pdf", b"%PDF-fake", "application/pdf")})
    assert resp.status_code == 413, resp.text
    body = resp.json()
    assert body["status"] == "error" and "5 Seiten" in body["error"]


# ---------------------------------------------------------------------------
# 2. Batch logic against a fake converter (docling-core, no models)
# ---------------------------------------------------------------------------


def _page_doc(start: int, end: int):
    """A DoclingDocument with pages start..end, one heading + one picture per page."""
    from docling_core.types.doc import (
        BoundingBox,
        DoclingDocument,
        ProvenanceItem,
        Size,
    )
    from docling_core.types.doc.labels import DocItemLabel

    doc = DoclingDocument(name=f"teil-{start}-{end}")
    for p in range(start, end + 1):
        doc.add_page(page_no=p, size=Size(width=595, height=842))
        prov = ProvenanceItem(
            page_no=p, bbox=BoundingBox(l=60, t=70, r=400, b=90), charspan=(0, 10)
        )
        doc.add_text(label=DocItemLabel.TEXT, text=f"Text Seite {p}", prov=prov)
        doc.add_picture(
            prov=ProvenanceItem(
                page_no=p, bbox=BoundingBox(l=60, t=100, r=360, b=300), charspan=(0, 0)
            )
        )
    return doc


class _FakeConverter:
    def __init__(self, *, partial_at=None, raise_at=None, drop_page=None):
        self.calls = []
        self.partial_at, self.raise_at, self.drop_page = partial_at, raise_at, drop_page

    def convert(self, path, page_range=None):
        from docling.datamodel.base_models import ConversionStatus

        self.calls.append(page_range)
        start, end = page_range
        if self.raise_at == start:
            raise RuntimeError("layout model exploded")
        doc = _page_doc(start, end)
        if self.drop_page and start <= self.drop_page <= end:
            del doc.pages[self.drop_page]
        status = ConversionStatus.PARTIAL_SUCCESS if self.partial_at == start else ConversionStatus.SUCCESS
        errors = []
        if status != ConversionStatus.SUCCESS:
            errors = [SimpleNamespace(error_message="Page 5 failed to parse.")]
        return SimpleNamespace(document=doc, status=status, errors=errors)


def _pdf_bytes_with(monkeypatch, pages: int) -> bytes:
    pdfium = pytest.importorskip("pypdfium2")

    class _Doc:
        def __init__(self, data):
            pass

        def __len__(self):
            return pages

        def close(self):
            pass

    monkeypatch.setattr(pdfium, "PdfDocument", _Doc)
    return b"%PDF-fake"


@pytest.mark.parametrize(
    "pages,batch,expected",
    [
        (7, 3, [(1, 3), (4, 6), (7, 7)]),
        (6, 3, [(1, 3), (4, 6)]),
        (7, 1, [(p, p) for p in range(1, 8)]),
        (4, 2, [(1, 2), (3, 4)]),
    ],
)
def test_batches_cover_every_page_in_order(monkeypatch, pages, batch, expected):
    pytest.importorskip("docling")
    monkeypatch.setenv("DOCLING_BATCH_PAGES", str(batch))
    conv = _FakeConverter()
    doc = dc._docling_document(conv, "x.pdf", _pdf_bytes_with(monkeypatch, pages))
    assert conv.calls == expected
    assert sorted(doc.pages) == list(range(1, pages + 1))
    texts = [t.text for t in doc.texts]
    assert texts == [f"Text Seite {p}" for p in range(1, pages + 1)]
    # Every picture keeps the page it came from.
    assert [pic.prov[0].page_no for pic in doc.pictures] == list(range(1, pages + 1))


def test_at_or_below_batch_size_is_the_single_historic_call(monkeypatch):
    pytest.importorskip("docling")
    monkeypatch.setenv("DOCLING_BATCH_PAGES", "5")

    class _Single:
        calls = []

        def convert(self, path, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(document="DOC")

    conv = _Single()
    assert dc._docling_document(conv, "x.pdf", _pdf_bytes_with(monkeypatch, 5)) == "DOC"
    assert conv.calls == [{}]  # no page_range at all — exactly the old call


@pytest.mark.parametrize(
    "kwargs,needle",
    [
        ({"raise_at": 4}, "Seiten 4-6 von 7 gescheitert: layout model exploded"),
        ({"partial_at": 4}, "Seiten 4-6 von 7 nur teilweise umgewandelt"),
        ({"drop_page": 5}, "Seiten 4-6 von 7: Docling lieferte"),
    ],
)
def test_failing_batch_fails_the_document_named(monkeypatch, kwargs, needle):
    pytest.importorskip("docling")
    monkeypatch.setenv("DOCLING_BATCH_PAGES", "3")
    conv = _FakeConverter(**kwargs)
    with pytest.raises(RuntimeError) as exc:
        dc._docling_document(conv, "x.pdf", _pdf_bytes_with(monkeypatch, 7))
    assert needle in str(exc.value)
    assert conv.calls[-1] == (4, 6)  # stopped at the failing batch, nothing after it


def test_failing_batch_takes_the_documented_render_all_fallback(monkeypatch):
    """Contract of convert_pdf_bytes: Docling failure → every page rendered for
    Vision, docling_parsed False, no Docling text and no page_markdowns — never
    the batches that did succeed as a silent partial document."""
    pytest.importorskip("docling")
    pytest.importorskip("pypdfium2")
    pdf = _synthetic_pdf(4, figures=False)
    monkeypatch.setenv("DOCLING_BATCH_PAGES", "2")

    def _batch_two_fails(converter, path, pdf_bytes):
        raise RuntimeError("Docling-Stapel Seiten 3-4 von 4 gescheitert: boom")

    monkeypatch.setattr(dc, "_docling_document", _batch_two_fails)
    markdown, meta, images = convert_pdf_bytes(pdf)
    assert meta["docling_parsed"] is False
    assert meta["rendered_page_numbers"] == [1, 2, 3, 4]
    assert "page_markdowns" not in meta and "figures" not in meta
    assert set(images) == {f"page-00{p}.png" for p in range(1, 5)}


# ---------------------------------------------------------------------------
# 3. Real Docling over synthetic PDFs (slow: loads the layout/OCR models)
# ---------------------------------------------------------------------------


def _docling_run(monkeypatch, pdf: bytes, batch: int):
    monkeypatch.setenv("DOCLING_BATCH_PAGES", str(batch))
    monkeypatch.setenv("PRIVACY_DEVICE", "cpu")
    return dc._docling_convert_pdf(pdf)


@pytest.mark.slow
def test_real_docling_batched_equals_single_pass(monkeypatch):
    pytest.importorskip("docling")
    pdf = _synthetic_pdf(5)
    single = _docling_run(monkeypatch, pdf, 100)
    md, pages, figures, page_mds, fig_meta = single
    assert pages == 5 and len(figures) == 5
    assert [m["markdown"].splitlines()[0] for m in page_mds] == [
        f"## Abschnitt {p}: Messbericht Seite {p}" for p in range(1, 6)
    ]
    assert [m["page"] for m in fig_meta] == [1, 2, 3, 4, 5]
    for batch in (1, 2, 3, 4):
        assert _docling_run(monkeypatch, pdf, batch) == single, f"batch={batch}"


@pytest.mark.slow
def test_real_docling_blank_page_inside_a_batch(monkeypatch):
    """A blank page keeps its number; page_markdowns follow the existing rule
    (omitted when the page-break split cannot be proven) — same as single pass."""
    pytest.importorskip("docling")
    pdf = _synthetic_pdf(4, blank=(3,), figures=False)
    single = _docling_run(monkeypatch, pdf, 100)
    assert single[1] == 4
    assert _docling_run(monkeypatch, pdf, 2) == single
    assert _docling_run(monkeypatch, pdf, 3) == single
