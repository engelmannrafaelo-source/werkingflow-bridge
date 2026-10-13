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
        dc, "_docling_convert_pdf", lambda pdf_bytes: ("# ok", 3, {}, None, None, [])
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
    """Mirrors Docling 2.x: a page that fails keeps an EMPTY entry in
    ``result.document.pages`` (``_add_failed_pages_to_document``) and is missing
    only from ``result.pages``, the pages the pipeline actually converted."""

    def __init__(
        self, *, partial_at=None, raise_at=None, fail_page=None, foreign_at=None,
        doc_missing=None,
    ):
        self.calls = []
        self.partial_at, self.raise_at, self.fail_page = partial_at, raise_at, fail_page
        self.foreign_at, self.doc_missing = foreign_at, doc_missing

    def convert(self, path, page_range=None):
        from docling.datamodel.base_models import ConversionStatus

        self.calls.append(page_range)
        start, end = page_range
        if self.raise_at == start:
            raise RuntimeError("layout model exploded")
        doc = _page_doc(start, end)
        converted = list(range(start, end + 1))
        if self.fail_page and start <= self.fail_page <= end:
            # Its content is gone, its PageItem stays; not in result.pages.
            for item in [*doc.texts, *doc.pictures]:
                if item.prov and item.prov[0].page_no == self.fail_page:
                    doc.delete_items(node_items=[item])
            converted.remove(self.fail_page)
        if self.doc_missing and start <= self.doc_missing <= end:
            del doc.pages[self.doc_missing]
        if self.foreign_at == start:
            from docling_core.types.doc import Size

            doc.add_page(page_no=end + 1, size=Size(width=595, height=842))
        status = ConversionStatus.PARTIAL_SUCCESS if self.partial_at == start else ConversionStatus.SUCCESS
        errors = []
        if status != ConversionStatus.SUCCESS:
            errors = [SimpleNamespace(error_message=f"Page {self.fail_page}: boom")]
        return SimpleNamespace(
            document=doc,
            status=status,
            errors=errors,
            pages=[SimpleNamespace(page_no=p) for p in converted],
        )


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
    doc, failed = dc._docling_document(conv, "x.pdf", _pdf_bytes_with(monkeypatch, pages))
    assert conv.calls == expected and failed == []
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
            return _FakeConverter().convert(path, page_range=(1, 5))

    conv = _Single()
    doc, failed = dc._docling_document(conv, "x.pdf", _pdf_bytes_with(monkeypatch, 5))
    assert sorted(doc.pages) == [1, 2, 3, 4, 5] and failed == []
    assert conv.calls == [{}]  # no page_range at all — exactly the old call


def test_single_pass_names_a_failed_page_like_the_batch_path(monkeypatch):
    """Was silent before: the single pass accepted PARTIAL_SUCCESS as is."""
    pytest.importorskip("docling")
    monkeypatch.setenv("DOCLING_BATCH_PAGES", "25")

    class _Single:
        def convert(self, path, **kwargs):
            assert kwargs == {}
            return _FakeConverter(partial_at=1, fail_page=2).convert(path, page_range=(1, 5))

    doc, failed = dc._docling_document(_Single(), "x.pdf", _pdf_bytes_with(monkeypatch, 5))
    assert failed == [2] and sorted(doc.pages) == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    "kwargs,needle",
    [
        ({"raise_at": 4}, "Seiten 4-6 von 7 gescheitert: layout model exploded"),
        (
            {"partial_at": 4},
            "Seiten 4-6 von 7: Docling meldet Teilerfolg, hat aber jede Seite umgewandelt",
        ),
        (
            {"fail_page": 5},
            "Seiten 4-6 von 7: Docling meldet Erfolg, hat aber die Seiten [5] nicht umgewandelt",
        ),
        ({"doc_missing": 5}, "Seiten 4-6 von 7: im Docling-Dokument fehlen die Seiten [5]"),
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
    md, pages, figures, page_mds, fig_meta, failed = single
    assert failed == []
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


# ---------------------------------------------------------------------------
# 4. Nachtrag R1: page-local PARTIAL_SUCCESS, render budget, config errors
# ---------------------------------------------------------------------------


def test_partial_batch_names_the_failed_page_and_goes_on(monkeypatch):
    """One page Docling fails on (PARTIAL_SUCCESS) is named — the other batches
    still run and keep their Docling text; no render-all fallback. The page
    stays in the document (empty), as in Docling."""
    pytest.importorskip("docling")
    monkeypatch.setenv("DOCLING_BATCH_PAGES", "3")
    conv = _FakeConverter(partial_at=4, fail_page=5)
    doc, failed = dc._docling_document(conv, "x.pdf", _pdf_bytes_with(monkeypatch, 7))
    assert conv.calls == [(1, 3), (4, 6), (7, 7)]
    assert failed == [5]
    assert sorted(doc.pages) == [1, 2, 3, 4, 5, 6, 7]
    assert [t.text for t in doc.texts] == [f"Text Seite {p}" for p in (1, 2, 3, 4, 6, 7)]
    assert [pic.prov[0].page_no for pic in doc.pictures] == [1, 2, 3, 4, 6, 7]


def test_foreign_page_in_a_batch_fails_the_document(monkeypatch):
    pytest.importorskip("docling")
    monkeypatch.setenv("DOCLING_BATCH_PAGES", "3")
    with pytest.raises(RuntimeError, match="Seiten 4-6 von 7: Docling lieferte fremde Seiten"):
        dc._docling_document(
            _FakeConverter(foreign_at=4), "x.pdf", _pdf_bytes_with(monkeypatch, 7)
        )


def _patch_docling_converter(monkeypatch, fake):
    import docling.document_converter as ddc

    monkeypatch.setattr(ddc, "DocumentConverter", lambda *a, **k: fake)


def test_partial_page_end_to_end_is_rendered_and_named(monkeypatch):
    """convert_pdf_bytes over a real 7-page PDF, Docling (fake) fails page 5:
    Docling text for the other pages, page 5 rendered for Vision, named in
    metadata and visibly in the markdown, page count 7. The page map is omitted
    by the existing count check (the empty page emits no page break)."""
    pytest.importorskip("docling")
    pytest.importorskip("pypdfium2")
    pdf = _synthetic_pdf(7, figures=False)
    monkeypatch.setenv("DOCLING_BATCH_PAGES", "3")
    _patch_docling_converter(monkeypatch, _FakeConverter(partial_at=4, fail_page=5))

    markdown, meta, images = convert_pdf_bytes(pdf)

    assert meta["docling_parsed"] is True
    assert meta["docling_failed_pages"] == [5]
    assert meta["rendered_page_numbers"] == [5]
    assert meta["pages"] == 7
    assert "page_markdowns" not in meta
    assert "page-005.png" in images
    assert "Text Seite 4" in markdown and "Text Seite 6" in markdown
    assert "Text Seite 5" not in markdown
    assert "Docling konnte die Seite 5 nicht in Text umwandeln. Sie liegt als gerendertes Bild bei (page-005.png)." in markdown
    assert "page-005.png" in markdown


def test_partial_naming_no_page_still_takes_the_render_all_path(monkeypatch):
    pytest.importorskip("docling")
    pytest.importorskip("pypdfium2")
    pdf = _synthetic_pdf(4, figures=False)
    monkeypatch.setenv("DOCLING_BATCH_PAGES", "2")
    _patch_docling_converter(monkeypatch, _FakeConverter(partial_at=3))
    markdown, meta, images = convert_pdf_bytes(pdf)
    assert meta["docling_parsed"] is False
    assert meta["rendered_page_numbers"] == [1, 2, 3, 4]
    assert "docling_failed_pages" not in meta


def _noise_pdf(pages: int) -> bytes:
    """Every page shows the SAME full-page noise image (one shared XObject):
    a small PDF whose pages render to large PNGs."""
    pytest.importorskip("reportlab")
    import random

    from PIL import Image
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    rnd = random.Random(1)
    im = Image.frombytes("RGB", (400, 566), bytes(rnd.getrandbits(8) for _ in range(400 * 566 * 3)))
    img = io.BytesIO()
    im.save(img, "JPEG", quality=60)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    w, h = A4
    for _ in range(pages):
        c.drawImage(ImageReader(io.BytesIO(img.getvalue())), 0, 0, width=w, height=h)
        c.showPage()
    c.save()
    return buf.getvalue()


def _count_renders(monkeypatch):
    import pypdfium2 as pdfium

    seen = []
    real = pdfium.PdfPage.render

    def _spy(self, *a, **k):
        seen.append(1)
        return real(self, *a, **k)

    monkeypatch.setattr(pdfium.PdfPage, "render", _spy)
    return seen


def test_render_budget_refuses_with_413_reason_and_stops_rendering(monkeypatch):
    """Render-all fallback over pages that render large: refused once the
    budget is crossed, with the reason — not after rendering every page."""
    pytest.importorskip("pypdfium2")
    pdf = _noise_pdf(12)
    monkeypatch.setenv("PDF_RENDER_MAX_MB", "4")
    monkeypatch.setattr(
        dc, "_docling_convert_pdf", lambda b: (_ for _ in ()).throw(RuntimeError("docling kaputt"))
    )
    rendered = _count_renders(monkeypatch)
    with pytest.raises(DocumentTooLargeError) as exc:
        convert_pdf_bytes(pdf)
    msg = str(exc.value)
    assert "PDF_RENDER_MAX_MB" in msg and "4 MB" in msg
    assert "Docling das Dokument nicht lesen konnte" in msg
    assert "von 12 als Bild gerenderten Seiten" in msg
    assert 1 <= len(rendered) < 12


def test_render_budget_also_guards_the_scan_path(monkeypatch):
    """Docling succeeds but no page has a text layer (scan PDF): same budget."""
    pytest.importorskip("pypdfium2")
    pdf = _noise_pdf(12)
    monkeypatch.setenv("PDF_RENDER_MAX_MB", "4")
    monkeypatch.setattr(dc, "_docling_convert_pdf", lambda b: ("", 12, {}, None, None, []))
    with pytest.raises(DocumentTooLargeError, match="keine verwertbare Textebene"):
        convert_pdf_bytes(pdf)


def test_within_render_budget_the_contract_is_unchanged(monkeypatch):
    pytest.importorskip("pypdfium2")
    pdf = _noise_pdf(3)
    monkeypatch.setenv("PDF_RENDER_MAX_MB", "64")
    monkeypatch.setattr(
        dc, "_docling_convert_pdf", lambda b: (_ for _ in ()).throw(RuntimeError("docling kaputt"))
    )
    markdown, meta, images = convert_pdf_bytes(pdf)
    assert meta["docling_parsed"] is False
    assert meta["rendered_page_numbers"] == [1, 2, 3]
    assert meta["rendered_page_count"] == 3
    assert set(images) == {"page-001.png", "page-002.png", "page-003.png"}


def test_render_budget_default_is_documented_constant(monkeypatch):
    monkeypatch.delenv("PDF_RENDER_MAX_MB", raising=False)
    assert dc._render_max_bytes() == dc.PDF_RENDER_MAX_MB_DEFAULT * 2**20


@pytest.mark.parametrize(
    "name,raw",
    [("PDF_RENDER_MAX_MB", "0"), ("PDF_RENDER_MAX_MB", "viel"), ("DOCLING_IMAGES_SCALE", "9"),
     ("DOCLING_IMAGES_SCALE", "abc")],
)
def test_bad_config_is_a_config_error_before_any_work(monkeypatch, name, raw):
    """Also DOCLING_IMAGES_SCALE: read inside the Docling try it used to be
    swallowed into the render-all fallback."""
    monkeypatch.setenv(name, raw)
    monkeypatch.setattr(dc, "_pdf_page_geometry", _fake_geometry(2))
    monkeypatch.setattr(dc, "_docling_convert_pdf", lambda b: pytest.fail("Docling ran"))
    monkeypatch.setattr(dc, "_render_pdf_pages", lambda *a, **k: pytest.fail("render ran"))
    with pytest.raises(dc.ServiceConfigError, match=name):
        convert_pdf_bytes(b"%PDF-fake")


def test_chain_does_not_turn_config_error_into_415(monkeypatch):
    from src.privacy_service.adapters import build_default_chain

    monkeypatch.setenv("DOCLING_BATCH_PAGES", "0")
    monkeypatch.setattr(dc, "_pdf_page_geometry", _fake_geometry(2))
    with pytest.raises(dc.ServiceConfigError):
        build_default_chain().convert(b"%PDF-fake", "a.pdf", "application/pdf")


@pytest.fixture()
def bad_config_client(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient

    from src.privacy_service import app as app_mod

    monkeypatch.setattr(app_mod, "_DOCUMENT_CHAIN", None)
    monkeypatch.setenv("DOCLING_BATCH_PAGES", "0")
    monkeypatch.setattr(dc, "_pdf_page_geometry", _fake_geometry(2))
    return TestClient(app_mod.app)


@pytest.mark.parametrize("path", ["/document/convert", "/document/convert-and-anonymize"])
def test_endpoints_answer_500_on_bad_config(bad_config_client, path):
    resp = bad_config_client.post(path, files={"file": ("a.pdf", b"%PDF-fake", "application/pdf")})
    assert resp.status_code == 500, resp.text
    assert "Konfigurationsfehler" in resp.json()["detail"]
    assert "DOCLING_BATCH_PAGES=0" in resp.json()["detail"]


def test_legacy_convert_pdf_answers_500_on_bad_config(bad_config_client):
    resp = bad_config_client.post("/convert-pdf", files={"file": ("a.pdf", b"%PDF-fake", "application/pdf")})
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "error" and "DOCLING_BATCH_PAGES=0" in body["error"]


# ---------------------------------------------------------------------------
# 5. Nachtrag R2: real Docling page failure, figures count against the budget
# ---------------------------------------------------------------------------


def _fail_real_docling_pages(monkeypatch, pages):
    """Make Docling's own PageAssembleModel raise for these 1-based pages —
    Docling then reports PARTIAL_SUCCESS exactly as on a real page error."""
    from docling.models.stages.page_assemble.page_assemble_model import (
        PageAssembleModel,
    )

    orig = PageAssembleModel.__call__

    def _call(self, conv_res, page_batch):
        batch = list(page_batch)
        if any(p.page_no in pages for p in batch):
            raise RuntimeError(f"Testfehler auf Seite {[p.page_no for p in batch]}")
        yield from orig(self, conv_res, batch)

    monkeypatch.setattr(PageAssembleModel, "__call__", _call)


@pytest.mark.slow
@pytest.mark.parametrize("batch", [3, 1000])
def test_real_docling_failed_page_is_rendered_and_named(monkeypatch, batch):
    """Real Docling 2.x, one page failing, batched (3) and single pass (1000):
    Docling text for every other page, page 5 rendered and named, no
    render-all fallback, nothing silent."""
    pytest.importorskip("docling")
    pytest.importorskip("pypdfium2")
    pdf = _synthetic_pdf(7, figures=False)
    monkeypatch.setenv("DOCLING_BATCH_PAGES", str(batch))
    monkeypatch.setenv("PRIVACY_DEVICE", "cpu")
    _fail_real_docling_pages(monkeypatch, {5})

    markdown, meta, images = convert_pdf_bytes(pdf)

    assert meta["docling_parsed"] is True
    assert meta["docling_failed_pages"] == [5]
    assert meta["rendered_page_numbers"] == [5]
    assert meta["pages"] == 7
    assert "page-005.png" in images
    for p in (1, 2, 3, 4, 6, 7):
        assert f"Messbericht Seite {p}" in markdown
    assert "Messbericht Seite 5" not in markdown
    assert "Docling konnte die Seite 5 nicht in Text umwandeln" in markdown


def test_figure_budget_stops_the_batches(monkeypatch):
    """Figure crops count after every batch: over the budget → 413 before the
    next batch, naming the figures."""
    pytest.importorskip("docling")
    monkeypatch.setenv("DOCLING_BATCH_PAGES", "1")
    monkeypatch.setenv("PDF_RENDER_MAX_MB", "5")
    monkeypatch.setattr(dc, "_picture_b64_bytes", lambda doc: 2 * 2**20)
    conv = _FakeConverter()
    with pytest.raises(DocumentTooLargeError, match="Abbildungen, die Docling") as exc:
        dc._docling_document(conv, "x.pdf", _pdf_bytes_with(monkeypatch, 7))
    assert conv.calls == [(1, 1), (2, 2), (3, 3)]
    assert "bis Seite 3 von 7 bereits 6 MB" in str(exc.value)


def test_picture_bytes_are_the_embedded_data_uris():
    doc = SimpleNamespace(pictures=[
        SimpleNamespace(image=SimpleNamespace(uri="data:image/png;base64," + "A" * 100)),
        SimpleNamespace(image=None),
    ])
    assert dc._picture_b64_bytes(doc) == len("data:image/png;base64,") + 100


def test_figure_refusal_inside_docling_is_not_a_render_all_fallback(monkeypatch):
    monkeypatch.setattr(dc, "_pdf_page_geometry", _fake_geometry(3))

    def _too_many_figures(pdf_bytes):
        raise DocumentTooLargeError("Abbildungen zu gross")

    monkeypatch.setattr(dc, "_docling_convert_pdf", _too_many_figures)
    monkeypatch.setattr(dc, "_render_pdf_pages", lambda *a, **k: pytest.fail("render ran"))
    with pytest.raises(DocumentTooLargeError, match="Abbildungen zu gross"):
        convert_pdf_bytes(b"%PDF-fake")


def test_figures_and_rendered_pages_share_one_budget(monkeypatch):
    """3 MB of figures + scan pages: refused once both together cross 4 MB."""
    pytest.importorskip("pypdfium2")
    pdf = _noise_pdf(12)
    monkeypatch.setenv("PDF_RENDER_MAX_MB", "4")
    figures = {"image_000000_x.png": "A" * (3 * 2**20)}
    monkeypatch.setattr(dc, "_docling_convert_pdf", lambda b: ("", 12, figures, None, None, []))
    with pytest.raises(DocumentTooLargeError) as exc:
        convert_pdf_bytes(pdf)
    msg = str(exc.value)
    assert "davon 3 MB Docling-Abbildungen" in msg and "4 MB" in msg


def test_figures_alone_over_budget_refuse_before_rendering(monkeypatch):
    monkeypatch.setenv("PDF_RENDER_MAX_MB", "1")
    monkeypatch.setattr(dc, "_pdf_page_geometry", _fake_geometry(2))
    figures = {"image_000000_x.png": "A" * (2 * 2**20)}
    monkeypatch.setattr(dc, "_docling_convert_pdf", lambda b: ("# x", 2, figures, None, None, []))
    monkeypatch.setattr(dc, "_render_pdf_pages", lambda *a, **k: pytest.fail("render ran"))
    with pytest.raises(DocumentTooLargeError, match="Abbildungen, die Docling"):
        convert_pdf_bytes(b"%PDF-fake")
