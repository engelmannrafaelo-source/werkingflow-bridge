"""
Universal Document Converter for the Privacy Service.

Converts PDF / DOCX / PPTX / XLSX / XLS / CSV / HTML / MSG / EML / images into
Markdown that can be fed into the Smart-Anonymize pipeline or returned directly.

Each adapter is a synchronous function returning ``(markdown, metadata, images)``.
There is no shared dispatcher — three independent production entry points call
these adapters directly: ``/convert-pdf`` calls ``convert_pdf_bytes`` itself;
``/document/convert`` and ``/document/convert-and-anonymize`` go through
``adapters.AdapterChain``, which also calls the ``convert_*_bytes`` functions
directly; PPTX routes through LibreOffice → ``convert_pdf_bytes``, while Word
uses LibreOffice HTML to retain paragraph/list/break structure. Anything
that must hold for every real caller — like the mojibake repair below — is
therefore wired directly onto the adapter functions via the
``@_mojibake_repaired`` decorator, not onto a dispatcher some callers skip.

Design constraints (per task brief):
- Fail loud on unknown formats — never silently fall back to a different adapter.
- Reuse the existing Docling PDF pipeline for PDF and for LibreOffice-converted
  Office documents so the OCR/table behaviour stays consistent.
- DOCX/DOC go through LibreOffice → HTML → Markdown. PPTX with very complex
  layouts logs a warning but does not fail.
"""

from __future__ import annotations

import base64
import functools
import io
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("privacy-service.document-converter")


# ----------------------------------------------------------------------------
# Format detection
# ----------------------------------------------------------------------------

# Extension → canonical format token.
_EXTENSION_FORMATS: Dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".docm": "docx",
    ".dotx": "docx",
    ".dotm": "docx",
    ".doc": "docx",
    ".pptx": "pptx",
    ".ppt": "pptx",
    ".xlsx": "xlsx",
    ".xls": "xlsx",
    ".csv": "csv",
    ".html": "html",
    ".htm": "html",
    ".msg": "msg",
    ".eml": "eml",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".tif": "image",
    ".tiff": "image",
    ".webp": "image",
}

_MIME_FORMATS: Dict[str, str] = {
    "application/pdf": "pdf",
    "application/msword": "docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-word.document.macroenabled.12": "docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template": "docx",
    "application/vnd.ms-word.template.macroenabled.12": "docx",
    "application/vnd.ms-powerpoint": "pptx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-excel": "xlsx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "text/csv": "csv",
    "application/csv": "csv",
    "text/html": "html",
    "application/vnd.ms-outlook": "msg",
    "message/rfc822": "eml",
    "image/png": "image",
    "image/jpeg": "image",
    "image/tiff": "image",
    "image/webp": "image",
}


class UnsupportedFormatError(ValueError):
    """Raised when a file extension/MIME type cannot be mapped to an adapter."""


def detect_format(filename: str, mime_type_hint: Optional[str] = None) -> str:
    """Determine the canonical format token for a file.

    Priority: explicit MIME hint > filename extension. Raises
    ``UnsupportedFormatError`` if neither maps to a known adapter.
    """
    if mime_type_hint:
        normalized = mime_type_hint.split(";", 1)[0].strip().lower()
        if normalized in _MIME_FORMATS:
            return _MIME_FORMATS[normalized]

    ext = os.path.splitext(filename or "")[1].lower()
    if ext in _EXTENSION_FORMATS:
        return _EXTENSION_FORMATS[ext]

    raise UnsupportedFormatError(
        f"Unsupported document type: filename={filename!r}, mime={mime_type_hint!r}"
    )


# ----------------------------------------------------------------------------
# Result container
# ----------------------------------------------------------------------------


@dataclass
class ConversionResult:
    markdown: str
    fmt: str
    metadata: Dict[str, Any]
    images: Optional[Dict[str, str]] = None  # base64-encoded PNGs (PDF path only)

    def to_response(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "success": True,
            "format": self.fmt,
            "markdown": self.markdown,
            "metadata": self.metadata,
        }
        if self.images:
            out["images"] = self.images
            out["image_count"] = len(self.images)
        return out


# ----------------------------------------------------------------------------
# Mojibake repair (UTF-8 bytes wrongly decoded as cp1252/Latin-1)
# ----------------------------------------------------------------------------
#
# Some upstream PDF/Office generators write UTF-8 bytes into the document but
# treat them as a single-byte codepage, so every non-ASCII character comes out
# as a short run of Latin-1-ish characters: "Prüfung" becomes "PrÃ¼fung",
# "Gebäude" becomes "GebÃ¤ude", "gemäß" becomes "gemÃ¤ÃŸ". Verified against a
# real report (pruefbericht_owa.pdf) with three independent extractors
# (Docling, poppler pdftotext, PyMuPDF) all producing the identical
# corruption — the bad glyphs are baked into the source file, not an
# extraction bug on our side.
#
# Repair works per whitespace-delimited token:
#   1. Re-encode the token as cp1252, then decode the result as UTF-8, both
#      strict. That is the exact inverse of the mistake that produced the
#      mojibake, so it only succeeds when the token really is misdecoded
#      UTF-8. Genuine single-byte Latin-1 text ("ü", "ä", "ß", accented
#      names, …) is essentially never a valid UTF-8 continuation sequence on
#      its own, so it fails the round-trip and is left untouched — this is
#      what keeps something like "São Paulo" safe.
#   2. Repairs are only applied to the document once at least
#      MOJIBAKE_MIN_OCCURRENCES tokens independently round-trip. A single
#      coincidental hit is left alone; systemic corruption (the actual
#      failure mode here) clears this easily.
#
# Applied via the ``@_mojibake_repaired`` decorator directly on every
# ``convert_*_bytes`` adapter below — the real production entry points
# (``/convert-pdf``, and the ``AdapterChain`` behind ``/document/convert`` and
# ``/document/convert-and-anonymize``) all call these functions directly, so
# decorating them guarantees the repair applies everywhere a document can
# actually be converted. ``convert_pptx_bytes`` carries no
# decorator of its own — it delegates to the (already decorated)
# ``convert_pdf_bytes`` via LibreOffice → PDF → Docling, so they inherit the
# repair for free.

# Minimum number of independently-round-tripping tokens required before any
# repair is applied to a text. Keeps a single unlucky token (rare, but not
# provably impossible for the cp1252→UTF-8 round-trip) from rewriting a
# document that isn't actually mojibake.
MOJIBAKE_MIN_OCCURRENCES = 3

_WHITESPACE_TOKEN_RE = re.compile(r"\S+")


def _cp1252_roundtrip_repair(token: str) -> Optional[str]:
    """Return the UTF-8-recovered token, or None if the repair doesn't apply.

    Fails closed: any encode/decode error — including a token containing
    characters outside cp1252's range — means "leave it alone", never a
    partial repair.
    """
    if token.isascii():
        return None
    try:
        candidate = token.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    return candidate if candidate != token else None


def repair_mojibake(text: str) -> str:
    """Conservatively repair cp1252-decoded-as-UTF-8 mojibake in ``text``.

    See the module section header above for the two guards (lossless
    round-trip + minimum occurrence count). Returns ``text`` unchanged unless
    both are satisfied.
    """
    if not text or text.isascii():
        return text

    repairs: Dict[Tuple[int, int], str] = {}
    for m in _WHITESPACE_TOKEN_RE.finditer(text):
        repaired = _cp1252_roundtrip_repair(m.group())
        if repaired is not None:
            repairs[(m.start(), m.end())] = repaired

    if len(repairs) < MOJIBAKE_MIN_OCCURRENCES:
        return text

    out: List[str] = []
    cursor = 0
    for (start, end), repaired in repairs.items():
        out.append(text[cursor:start])
        out.append(repaired)
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _mojibake_repaired(fn):
    """Decorator: repair mojibake in a ``convert_*_bytes`` adapter's output.

    Post-processes the returned ``(markdown, metadata, images)`` triple only —
    repairs ``markdown`` and every ``metadata["page_markdowns"][i]["markdown"]``
    entry (selective anonymization reads those per-page texts, not just the
    flat markdown). Never wraps the conversion call itself in try/except: an
    adapter failure propagates unchanged, so this can't turn a genuine
    conversion error into a silent partial result.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        markdown, metadata, images = fn(*args, **kwargs)
        markdown = repair_mojibake(markdown)
        metadata = dict(metadata or {})
        page_markdowns = metadata.get("page_markdowns")
        if page_markdowns:
            metadata["page_markdowns"] = [
                {**page, "markdown": repair_mojibake(page["markdown"])}
                if isinstance(page, dict) and isinstance(page.get("markdown"), str)
                else page
                for page in page_markdowns
            ]
        return markdown, metadata, images

    return wrapper


# ----------------------------------------------------------------------------
# PDF (Docling text + per-page render→Vision cascade)
# ----------------------------------------------------------------------------
#
# A PDF is not one thing: it can be a digital text document, a scanned image, a
# large-format CAD/architectural plan, a photo, or any mix of those per page.
# Docling extracts text/tables well but chokes on pages that carry no real text
# layer or embed enormous (e.g. 132-megapixel) rasters — the latter previously
# blew up the downstream Vision call.
#
# The cascade, per page:
#   1. Docling runs over the whole document for rich text/table Markdown.
#   2. A page that has (almost) no native text layer, OR is an oversized
#      large-format drawing, OR belongs to a document Docling could not parse at
#      all, is RENDERED to a down-scaled PNG and added to the ``images`` dict.
#   3. Every image (Docling-extracted figures AND rendered pages) is pushed
#      through a uniform down-scale guard so no giant raster ever reaches Vision.
# The rendered pages ride the exact same ``images`` dict that the endpoint hands
# to ``describe_images(...)`` — so a plan/scan page becomes a Vision description
# instead of a 415/timeout.

# Long edge (px) a rendered page image is scaled to. ~1600px is the value the
# whole render→Vision path was validated with on a real large-format plan:
# small enough to avoid the giant-raster blow-up, detailed enough for Vision to
# read zoning/hull/PV off the drawing.
RENDER_PAGE_MAX_EDGE = 1600

# Hard down-scale ceiling applied to EVERY image (Docling figures included).
# Anything whose long edge exceeds this is resized before it can reach Vision —
# this is the guard against the 132-MP figure that previously timed out.
MAX_IMAGE_EDGE = 2000

# Render scale for Docling's figure crops (see _docling_convert_pdf). Default 1.0
# keeps the historic output byte-identical; the GPU host opts in via env, so the
# switch (and its rollback) is one compose line on the one host both bridges share.
DOCLING_IMAGES_SCALE_DEFAULT = 1.0
DOCLING_IMAGES_SCALE_MAX = 4.0


class ServiceConfigError(ValueError):
    """A service env value is unusable — a deployment error, answered with HTTP 500.

    Never treated as an adapter failure (that would end as 415 "unsupported")
    and never swallowed into the Docling render-all fallback.
    """


def _docling_images_scale() -> float:
    """``DOCLING_IMAGES_SCALE`` as float; fail loud on anything unusable."""
    raw = os.getenv("DOCLING_IMAGES_SCALE", "").strip()
    if not raw:
        return DOCLING_IMAGES_SCALE_DEFAULT
    try:
        value = float(raw)
    except ValueError as e:
        raise ServiceConfigError(f"DOCLING_IMAGES_SCALE={raw!r} ist keine Zahl") from e
    if not (1.0 <= value <= DOCLING_IMAGES_SCALE_MAX):
        raise ServiceConfigError(
            f"DOCLING_IMAGES_SCALE={value} liegt ausserhalb 1.0..{DOCLING_IMAGES_SCALE_MAX}"
        )
    return value


# Docling keeps every page's raster (at 1.0 AND at ``images_scale``) until the
# whole conversion ends whenever ``generate_picture_images`` is on
# (``keep_images`` in docling's StandardPdfPipeline), so one ``convert`` over the
# whole PDF grows linearly with the page count. A 1393-page PDF took the shared
# GPU document service (Dev AND Prod, 16 GiB) down by cgroup OOM on 12.10.2026.
# Above DOCLING_BATCH_PAGES pages the PDF is converted in page_range batches and
# the documents are concatenated; at or below it the single-pass path is the
# unchanged historic one (byte-identical output).
# Measured 13.10.2026 (CPU, synthetic A4 pages with one photo each): at
# images_scale 3 Docling holds 14.3 MB of rasters per page until the end and
# peaks ~62 MB per page in flight; batched, the peak stays flat over the whole
# document. 25 pages ≈ 1.6 GB per conversion, two concurrent conversions
# (BRIDGE_MAX_CONCURRENT_CONVERSIONS) ≈ 3.1 GB on top of the models.
DOCLING_BATCH_PAGES_DEFAULT = 25
# Hard cap: above this a PDF is refused (HTTP 413) before Docling runs. Batching
# bounds Docling's memory, not the run time: at ~0.45 s/page (measured CPU
# 2.7 s/page, GPU OCR ~6x faster — not measured on the GPU host) 600 pages fit
# the main caller's 300 s whole-document timeout; a longer run keeps a shared
# conversion slot busy after its caller has already given up.
DOCLING_MAX_PAGES_DEFAULT = 600


class DocumentTooLargeError(ValueError):
    """The document exceeds a hard size limit — callers map this to HTTP 413.

    Never retried by another adapter and never answered with a partial result:
    the message names the limit and how to get under it.
    """


def _positive_int_env(name: str, default: int) -> int:
    """``name`` as int >= 1; unset/empty → ``default``; anything else fails loud."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ServiceConfigError(f"{name}={raw!r} ist keine ganze Zahl") from e
    if value < 1:
        raise ServiceConfigError(f"{name}={value} muss mindestens 1 sein")
    return value


def _docling_batch_pages() -> int:
    """Pages per Docling ``convert`` call (``DOCLING_BATCH_PAGES``)."""
    return _positive_int_env("DOCLING_BATCH_PAGES", DOCLING_BATCH_PAGES_DEFAULT)


def _docling_max_pages() -> int:
    """Largest PDF page count the service accepts (``DOCLING_MAX_PAGES``)."""
    return _positive_int_env("DOCLING_MAX_PAGES", DOCLING_MAX_PAGES_DEFAULT)


# Every image of one response — Docling's figure crops AND rendered whole pages
# (scan pages, large-format plans, every page of a PDF Docling could not read) —
# is held as base64 until the response is built; the JSON response and the
# Bridge worker's re-serialisation copy it several times more. The PDF's size
# does not bound them: one image XObject reused on every page keeps the file
# small while every page renders anew (1.6 MB PDF, 600 pages of noise ≈ 3.8 GB
# base64, OOM under 2 GB) or yields fresh figure crops (4 shared photos per page
# at DOCLING_IMAGES_SCALE=3 ≈ 1.55 MB per page), measured 13.10.2026. Above this
# budget the conversion is refused with 413 and the reason. A plain grey scan
# renders to ~0.5 MB per page, so 600 such pages stay well inside the default.
PDF_RENDER_MAX_MB_DEFAULT = 384


def _render_max_bytes() -> int:
    """Budget for all images of one PDF response (``PDF_RENDER_MAX_MB``), in bytes."""
    return _positive_int_env("PDF_RENDER_MAX_MB", PDF_RENDER_MAX_MB_DEFAULT) * 2**20

# A page with fewer than this many native (non-OCR) text-layer characters is
# treated as image/scan-only and rendered whole for Vision.
MIN_PAGE_TEXT_CHARS = 40

# A page whose long edge exceeds this (points; 72pt = 1 inch) is a large-format
# drawing/plan (bigger than A2). Such pages are rendered whole even when they
# carry a text layer, because the drawing's gestalt (zoning, envelope, symbols)
# only reads off the full page — Docling's fragmented figure extraction loses it.
OVERSIZE_PAGE_LONG_EDGE_PT = 1700


def _pdf_page_geometry(pdf_bytes: bytes) -> List[Tuple[int, float]]:
    """Return ``[(native_text_chars, long_edge_pt), ...]`` per page (0-indexed).

    Uses pypdfium2 (a Docling dependency, always present) to read each page's
    NATIVE text layer — not OCR — so the render decision is independent of
    Docling's OCR behaviour. Fail loud if the PDF cannot be opened at all
    (genuinely corrupt input), which callers map to HTTP 500.
    """
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(pdf_bytes)
    except Exception as e:  # noqa: BLE001 — genuinely unreadable PDF, fail loud
        raise RuntimeError(f"Could not open PDF for page inspection: {e}") from e

    geometry: List[Tuple[int, float]] = []
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            try:
                width_pt, height_pt = page.get_size()
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range() or ""
                finally:
                    textpage.close()
            finally:
                page.close()
            geometry.append((len(text.strip()), float(max(width_pt, height_pt))))
    finally:
        pdf.close()
    return geometry


def _render_pdf_pages(
    pdf_bytes: bytes,
    page_indices: List[int],
    max_edge: int = RENDER_PAGE_MAX_EDGE,
    *,
    max_total_bytes: Optional[int] = None,
    already_bytes: int = 0,
    why: str = "",
) -> Dict[str, str]:
    """Render the given 0-indexed pages to down-scaled PNGs.

    Returns ``{"page-NNN.png": base64-png}`` (1-indexed in the filename so it
    reads naturally in the appended descriptions). The render scale is chosen so
    the long edge lands at ``max_edge`` px, which both avoids the giant-raster
    blow-up and keeps CAD detail legible for Vision.

    ``max_total_bytes`` bounds the summed base64 size of the response's images,
    ``already_bytes`` of which Docling's figures already use: one page over it
    raises ``DocumentTooLargeError`` (413) naming the budget, the page reached
    and ``why`` these pages are rendered — never a truncated set of pages.
    """
    if not page_indices:
        return {}

    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(pdf_bytes)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Could not open PDF for page rendering: {e}") from e

    rendered: Dict[str, str] = {}
    page_bytes = 0
    try:
        for done, idx in enumerate(page_indices, start=1):
            page = pdf[idx]
            try:
                width_pt, height_pt = page.get_size()
                long_edge_pt = max(width_pt, height_pt) or 1.0
                # pypdfium2 renders at ``scale`` px per point (scale=1 → 72 DPI).
                scale = min(max_edge / long_edge_pt, 4.0)
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil()
            finally:
                page.close()
            if pil_image.mode not in ("RGB", "L"):
                pil_image = pil_image.convert("RGB")
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            page_bytes += len(b64)
            total_bytes = already_bytes + page_bytes
            if max_total_bytes is not None and total_bytes > max_total_bytes:
                budget_mb = max_total_bytes / 2**20
                per_page_mb = page_bytes / done / 2**20
                room_mb = max(0.0, budget_mb - already_bytes / 2**20)
                figures_note = (
                    f" (davon {already_bytes / 2**20:.0f} MB Docling-Abbildungen)"
                    if already_bytes
                    else ""
                )
                raise DocumentTooLargeError(
                    f"Die Bilder dieser Antwort ueberschreiten die Obergrenze von "
                    f"{budget_mb:.0f} MB (PDF_RENDER_MAX_MB): nach {done} von "
                    f"{len(page_indices)} als Bild gerenderten Seiten (bis Seite {idx + 1}) "
                    f"bereits {total_bytes / 2**20:.0f} MB{figures_note}, im Mittel "
                    f"{per_page_mb:.1f} MB je gerenderter Seite. Gerendert wird, weil "
                    f"{why or 'diese Seiten keine verwertbare Textebene haben'}. "
                    f"Bitte in Teile mit hoechstens {max(1, int(room_mb / per_page_mb))} "
                    f"solchen Seiten zerlegen und einzeln umwandeln."
                )
            rendered[f"page-{idx + 1:03d}.png"] = b64
    finally:
        pdf.close()
    return rendered


def _downscale_b64_png(b64: str, max_edge: int = MAX_IMAGE_EDGE) -> str:
    """Down-scale a base64 image if its long edge exceeds ``max_edge``.

    Idempotent for already-small images (returned unchanged). This is the guard
    that stops a Docling-extracted 132-MP figure from ever reaching Vision.
    Fail-soft: if the bytes cannot be parsed as an image we return the original
    (the caller's Vision step surfaces any genuine problem loudly).
    """
    from PIL import Image

    # Disable Pillow's DecompressionBomb guard for THIS decode: the whole point
    # here is to shrink an oversized figure, so a huge (>178-MP) raster must be
    # decodable — otherwise Pillow would raise and we'd hand the giant image
    # straight to Vision. Input is already size-capped upstream (100 MB PDF).
    Image.MAX_IMAGE_PIXELS = None

    try:
        raw = base64.b64decode(b64)
        with Image.open(io.BytesIO(raw)) as img:
            if max(img.size) <= max_edge:
                return b64
            ratio = max_edge / max(img.size)
            new_size = (max(1, round(img.width * ratio)), max(1, round(img.height * ratio)))
            resized = img.resize(new_size, Image.LANCZOS)
            if resized.mode not in ("RGB", "L"):
                resized = resized.convert("RGB")
            buf = io.BytesIO()
            resized.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:  # noqa: BLE001 — never let the guard itself break conversion
        logger.warning(f"[pdf] image down-scale skipped (unparseable image): {e}")
        return b64


# Caption search window below a figure, in PDF points (top-left origin). A caption
# may start slightly INSIDE the picture box (Docling's crop includes a margin), so
# the gap may be a little negative.
FIGURE_CAPTION_MAX_GAP_PT = 40.0
FIGURE_CAPTION_MIN_GAP_PT = -12.0
# The positional fallback only takes short caption-like blocks: a heading or a
# body paragraph that happens to follow a logo is not that picture's caption
# (Muehl-Bericht 07.10.2026: logo -> "Inhaltsverzeichnis").
FIGURE_CAPTION_MAX_CHARS = 160
FIGURE_CAPTION_LABELS = {"caption", "text", "paragraph"}


def _figure_meta(document: Any) -> List[Dict[str, Any]]:
    """Page and caption for every picture, in the order of the exported names.

    The markdown export numbers pictures ``image_{i:06d}_<hash>`` in
    ``iterate_items`` order; this walks the same order, so entry ``i`` belongs to
    the figure whose name starts with ``image_{i:06d}_``.

    Why not the text after ``![Image](…)`` in the markdown: on a two-column photo
    page Docling's reading order interleaves captions and pictures (Weidenhof-Akt
    07.10.2026: "Foto 04" stood after the 6th picture, "Foto 05" nowhere), so the
    check numbered photos "Bild 1..16" and the report blamed the wrong photo.
    Order: a caption Docling linked to the picture; else a caption-labelled child
    of the picture; else the text block directly below it on the same page.
    ``caption`` is None when none is found — the caller must not guess.
    """
    from docling_core.types.doc import PictureItem, TextItem

    pictures: List[Any] = []
    texts: List[Any] = []
    for item, _level in document.iterate_items():
        if isinstance(item, PictureItem):
            pictures.append(item)
        elif isinstance(item, TextItem) and item.prov:
            texts.append(item)

    def box(prov: Any) -> Any:
        page = document.pages.get(prov.page_no)
        if page is None or page.size is None:
            return None
        return prov.bbox.to_top_left_origin(page.size.height)

    meta: List[Dict[str, Any]] = []
    for pic in pictures:
        prov = pic.prov[0] if pic.prov else None
        page_no = prov.page_no if prov else None
        caption = (pic.caption_text(document) or "").strip()
        if not caption:
            for child in pic.children or []:
                item = child.resolve(document)
                if getattr(item, "label", None) == "caption" and getattr(item, "text", "").strip():
                    caption = item.text.strip()
                    break
        if not caption and prov is not None:
            pbox = box(prov)
            best = None
            for text in texts:
                if text.prov[0].page_no != page_no or not text.text.strip():
                    continue
                if str(getattr(text.label, "value", text.label)) not in FIGURE_CAPTION_LABELS:
                    continue
                if len(text.text.strip()) > FIGURE_CAPTION_MAX_CHARS:
                    continue
                tbox = box(text.prov[0])
                if pbox is None or tbox is None:
                    continue
                overlap = min(pbox.r, tbox.r) - max(pbox.l, tbox.l)
                if overlap < 0.5 * min(pbox.r - pbox.l, tbox.r - tbox.l):
                    continue
                gap = tbox.t - pbox.b
                if FIGURE_CAPTION_MIN_GAP_PT <= gap <= FIGURE_CAPTION_MAX_GAP_PT and (
                    best is None or abs(gap) < best[0]
                ):
                    best = (abs(gap), text.text.strip())
            if best:
                caption = best[1]
        meta.append({"index": len(meta), "page": page_no, "caption": caption or None})
    return meta


def _docling_convert_pdf(
    pdf_bytes: bytes,
) -> Tuple[
    str,
    Optional[int],
    Dict[str, str],
    Optional[List[Dict[str, Any]]],
    Optional[List[Dict[str, Any]]],
    List[int],
]:
    """Run Docling over a PDF → (markdown, page_count, figures, page_markdowns, figure_meta, failed_pages).

    This is the text/table extraction half of the cascade. Raises on any Docling
    failure so the caller can fall back to a full page render. ``failed_pages``
    (1-based) are single pages Docling could not convert inside an otherwise
    converted batch (see _docling_document); the caller renders them.
    """
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        AcceleratorOptions,
        AcceleratorDevice,
    )
    from docling.datamodel.base_models import InputFormat
    from docling_core.types.doc.base import ImageRefMode

    pipeline_options = PdfPipelineOptions()
    pipeline_options.generate_picture_images = True
    # Docling crops figures from a page raster rendered at ``images_scale``
    # (1.0 = 72 DPI). At the default a photo printed 8 cm wide comes out
    # ~230 px — too small for Vision to read a gauge or a nameplate, so it
    # guesses (Weidenhof-Akt 05.10.2026: 0/4 photo findings, invented devices).
    # MAX_IMAGE_EDGE still caps every figure afterwards.
    pipeline_options.images_scale = _docling_images_scale()
    pipeline_options.generate_table_images = False
    pipeline_options.do_ocr = True
    # Device folgt PRIVACY_DEVICE (gleiche Semantik wie Flair, siehe
    # privacy/flair_recognizer._resolve_device): auto = CUDA falls vorhanden,
    # cuda = Pflicht (fail loud unten via torch-Check im Recognizer — Docling
    # selbst probt bei AUTO ebenfalls CUDA→CPU), cpu = erzwungen. Auf den
    # CPU-Hosts ändert sich nichts (kein CUDA → CPU wie bisher); auf dem
    # GPU-Host ist Docling-OCR der größte Hebel (~6-8x pro Seite, siehe
    # local-storage/research-gpu-privacy-optimierung-20260728.md).
    _privacy_device = os.getenv("PRIVACY_DEVICE", "auto").strip().lower() or "auto"
    if _privacy_device == "cpu":
        _docling_device = AcceleratorDevice.CPU
    elif _privacy_device == "cuda":
        _docling_device = AcceleratorDevice.CUDA
    else:
        _docling_device = AcceleratorDevice.AUTO
    pipeline_options.accelerator_options = AcceleratorOptions(
        num_threads=int(os.getenv("DOCLING_THREADS", "1")),
        device=_docling_device,
    )

    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_pdf:
        tmp_pdf.write(pdf_bytes)
        tmp_pdf_path = tmp_pdf.name

    try:
        document, failed_pages = _docling_document(converter, tmp_pdf_path, pdf_bytes)

        with tempfile.TemporaryDirectory() as tmp_dir:
            md_path = os.path.join(tmp_dir, "output.md")
            document.save_as_markdown(md_path, image_mode=ImageRefMode.REFERENCED)

            with open(md_path, "r") as f:
                markdown = f.read()

            figures: Dict[str, str] = {}
            artifacts_dir = os.path.join(tmp_dir, "output_artifacts")
            if os.path.exists(artifacts_dir):
                for img_name in os.listdir(artifacts_dir):
                    if img_name.lower().endswith((".png", ".jpg", ".jpeg")):
                        img_path = os.path.join(artifacts_dir, img_name)
                        with open(img_path, "rb") as img_f:
                            figures[img_name] = base64.b64encode(img_f.read()).decode("utf-8")

            for img_name in figures:
                markdown = markdown.replace(os.path.join(artifacts_dir, img_name), img_name)

            page_count = len(document.pages) if hasattr(document, "pages") else None

            # Optional per-page split for callers that need page-faithful text
            # (e.g. selective anonymization: user picks the PII pages, only
            # those run through smart-anonymize). A SECOND export with an
            # explicit page-break token keeps the flat `markdown` above
            # byte-identical to what existing callers have always received,
            # while the token split preserves the exact same content ordering
            # (cross-page tables etc.) as the flat export — unlike N separate
            # per-page exports, which can duplicate page-spanning items.
            # Fail-safe by absence: any doubt (blank pages make the split
            # count disagree with page_count, token collision, export error)
            # → no page_markdowns, callers fall back to whole-document
            # handling. Never a wrong page map.
            page_markdowns: Optional[List[Dict[str, Any]]] = None
            try:
                paged_path = os.path.join(tmp_dir, "paged.md")
                document.save_as_markdown(
                    paged_path,
                    image_mode=ImageRefMode.REFERENCED,
                    page_break_placeholder=PAGE_BREAK_TOKEN,
                )
                with open(paged_path, "r") as f:
                    paged = f.read()
                paged_artifacts = os.path.join(tmp_dir, "paged_artifacts")
                if os.path.exists(paged_artifacts):
                    for img_name in os.listdir(paged_artifacts):
                        paged = paged.replace(
                            os.path.join(paged_artifacts, img_name), img_name
                        )
                # A failed page stays in the document as an empty page, so the
                # numbering holds; like any blank page it emits no page break,
                # and the count check below omits the map rather than shift it.
                page_markdowns = _split_paged_markdown(paged, page_count)
                if page_markdowns is None:
                    logger.warning(
                        f"[pdf] page_markdowns omitted: page-break split does not "
                        f"match page_count={page_count} (blank pages or token "
                        f"collision) — callers fall back to whole-document handling"
                    )
            except Exception as e:  # noqa: BLE001 — enrichment only, never fatal
                page_markdowns = None
                logger.warning(f"[pdf] page_markdowns omitted (paged export failed): {e}")

            # Page + caption per figure (see _figure_meta). Enrichment only: a
            # failure, or a count that does not match the exported figures,
            # yields None — never a caption on the wrong photo.
            figure_meta: Optional[List[Dict[str, Any]]] = None
            try:
                raw_meta = _figure_meta(document)
                by_index = {
                    int(name[len("image_"):len("image_") + 6]): name
                    for name in figures
                    if re.match(r"^image_\d{6}_", name)
                }
                if len(raw_meta) == len(figures) == len(by_index):
                    figure_meta = [
                        {"name": by_index[m["index"]], "page": m["page"], "caption": m["caption"]}
                        for m in raw_meta
                        if m["index"] in by_index
                    ]
                    if len(figure_meta) != len(raw_meta):
                        figure_meta = None
                if figure_meta is None:
                    logger.warning(
                        f"[pdf] figure_meta omitted: {len(raw_meta)} pictures vs "
                        f"{len(figures)} exported figures"
                    )
            except Exception as e:  # noqa: BLE001 — enrichment only, never fatal
                figure_meta = None
                logger.warning(f"[pdf] figure_meta omitted: {e}")

            return markdown, page_count, figures, page_markdowns, figure_meta, failed_pages
    finally:
        os.unlink(tmp_pdf_path)


def _checked_failed_pages(result: Any, start: int, end: int, where: str) -> List[int]:
    """Pages of ``start..end`` (1-based) Docling failed on, from one ``convert`` result.

    Docling 2.x keeps a failed page in ``result.document.pages`` as an EMPTY
    ``PageItem`` (``_add_failed_pages_to_document`` preserves the numbering) and
    drops it only from ``result.pages``, the pipeline's list of pages it
    actually converted (``Page.page_no`` is 1-based there too). So the
    document's pages must be exactly the range, and the failed pages are the
    range minus ``result.pages``. Raises, naming ``where``, for anything that
    cannot be pinned to single pages; never returns a silent partial result.
    """
    from docling.datamodel.base_models import ConversionStatus

    expected = set(range(start, end + 1))
    errors = "; ".join(str(err.error_message) for err in result.errors or [])
    doc_pages = set(result.document.pages)
    if doc_pages - expected:
        extra = sorted(doc_pages - expected)
        raise RuntimeError(f"{where}: Docling lieferte fremde Seiten {extra[:3]}…{extra[-3:]}")
    if expected - doc_pages:
        raise RuntimeError(
            f"{where}: im Docling-Dokument fehlen die Seiten {sorted(expected - doc_pages)}"
        )
    converted = {p.page_no for p in result.pages}
    if converted - expected:
        extra = sorted(converted - expected)
        raise RuntimeError(f"{where}: Docling wandelte fremde Seiten um {extra[:3]}…{extra[-3:]}")
    failed = sorted(expected - converted)
    if result.status == ConversionStatus.SUCCESS:
        if failed:
            raise RuntimeError(
                f"{where}: Docling meldet Erfolg, hat aber die Seiten {failed} nicht umgewandelt"
            )
        return []
    if result.status == ConversionStatus.PARTIAL_SUCCESS:
        if not failed:
            raise RuntimeError(
                f"{where}: Docling meldet Teilerfolg, hat aber jede Seite umgewandelt; "
                f"der Fehler laesst sich keiner Seite zuordnen"
                f"{' (' + errors + ')' if errors else ''}"
            )
        logger.warning(
            f"[pdf] {where}: Docling konnte die Seiten {failed} nicht umwandeln "
            f"({errors or 'ohne Meldung'}) — sie werden als Bild gerendert"
        )
        return failed
    raise RuntimeError(
        f"{where} nicht umgewandelt (Status {result.status.value}"
        f"{': ' + errors if errors else ''})"
    )


def _picture_b64_bytes(document: Any) -> int:
    """Size of the figure crops Docling embedded in ``document`` (base64 data URIs)."""
    return sum(
        len(str(pic.image.uri))
        for pic in getattr(document, "pictures", None) or []
        if getattr(pic, "image", None) is not None
    )


def _check_figure_budget(figure_bytes: int, last_page: int, total_pages: int) -> None:
    """413 once Docling's figure crops alone exceed the response image budget."""
    budget = _render_max_bytes()
    if figure_bytes <= budget:
        return
    per_page_mb = figure_bytes / last_page / 2**20
    raise DocumentTooLargeError(
        f"Die Abbildungen, die Docling aus diesem PDF schneidet, ueberschreiten die "
        f"Obergrenze von {budget / 2**20:.0f} MB fuer die Bilder einer Antwort "
        f"(PDF_RENDER_MAX_MB): bis Seite {last_page} von {total_pages} bereits "
        f"{figure_bytes / 2**20:.0f} MB, im Mittel {per_page_mb:.1f} MB je Seite. "
        f"Bitte in Teile mit hoechstens {max(1, int(budget / 2**20 / per_page_mb))} "
        f"Seiten zerlegen und einzeln umwandeln."
    )


def _docling_document(converter: Any, pdf_path: str, pdf_bytes: bytes) -> Tuple[Any, List[int]]:
    """Docling's document for the whole PDF — one pass, or concatenated batches.

    Returns ``(document, failed_pages)``; ``failed_pages`` (1-based) are single
    pages Docling could not convert (PARTIAL_SUCCESS, see _checked_failed_pages).
    They stay in the document as empty pages; the caller renders and names them.

    Up to ``DOCLING_BATCH_PAGES`` pages: the historic single ``converter.convert``
    (same call, same output on success). Above it: one
    ``convert(page_range=…)`` per batch, so Docling never holds more than one
    batch of page rasters, then ``DoclingDocument.concatenate`` — a single export
    of the merged document keeps the figure numbering, the page-break split and
    the figure pages exactly as one export of the whole PDF would.

    Both paths check every result the same way. A batch that raises, a status
    other than SUCCESS/PARTIAL_SUCCESS, foreign or missing pages, or a
    PARTIAL_SUCCESS that names no page raise, naming the batch — the caller's
    contract (render every page for Vision, ``docling_parsed: false``) applies.

    Docling's figure crops count against the response image budget
    (``PDF_RENDER_MAX_MB``) after every batch: over it, ``DocumentTooLargeError``
    (413) — before the next batch adds more.
    """
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        total_pages = len(pdf)
    finally:
        pdf.close()
    batch_pages = _docling_batch_pages()
    if total_pages <= batch_pages:
        result = converter.convert(pdf_path)
        failed = _checked_failed_pages(result, 1, total_pages, f"Docling ({total_pages} Seiten)")
        _check_figure_budget(_picture_b64_bytes(result.document), total_pages, total_pages)
        return result.document, failed

    from docling_core.types.doc import DoclingDocument

    parts: List[Any] = []
    failed_pages: List[int] = []
    figure_bytes = 0
    for start in range(1, total_pages + 1, batch_pages):
        end = min(total_pages, start + batch_pages - 1)
        where = f"Docling-Stapel Seiten {start}-{end} von {total_pages}"
        try:
            result = converter.convert(pdf_path, page_range=(start, end))
        except Exception as e:  # noqa: BLE001 — re-raised with the batch named
            raise RuntimeError(f"{where} gescheitert: {e}") from e
        failed_pages.extend(_checked_failed_pages(result, start, end, where))
        figure_bytes += _picture_b64_bytes(result.document)
        _check_figure_budget(figure_bytes, end, total_pages)
        parts.append(result.document)
        # The ConversionResult holds this batch's page rasters; drop it before
        # the next batch so they never pile up across batches.
        del result
        logger.info(f"[pdf] {where} umgewandelt")

    document = DoclingDocument.concatenate(parts)
    if sorted(document.pages) != list(range(1, total_pages + 1)):
        raise RuntimeError(
            f"Docling-Stapel: zusammengefuegtes Dokument hat {len(document.pages)} "
            f"statt {total_pages} Seiten"
        )
    return document, failed_pages


# Unusual fixed token: a real document containing it would corrupt the split —
# which the page_count check below turns into a safe omission, not a wrong map.
PAGE_BREAK_TOKEN = "<!-- DOCLING-PAGE-BREAK-7f3acb1e -->"


def _split_paged_markdown(
    paged: str, page_count: Optional[int]
) -> Optional[List[Dict[str, Any]]]:
    """Split a page-break-token export into `[{page_no, markdown}, …]`.

    Returns None unless the split produces EXACTLY `page_count` parts: Docling
    only emits the token between pages that carry content, so a blank page
    makes `parts` shorter than `page_count` and every later page_no would be
    wrong. Absence is the documented fallback contract (full-document
    handling); a silently wrong page map would misdirect selective
    anonymization onto the wrong pages.
    """
    if page_count is None or page_count < 1:
        return None
    parts = paged.split(PAGE_BREAK_TOKEN)
    if len(parts) != page_count:
        return None
    return [
        {"page_no": i + 1, "markdown": part.strip("\n")}
        for i, part in enumerate(parts)
    ]


def _render_only_markdown(rendered_pages: Dict[str, str]) -> str:
    """Markdown body for a PDF that has no usable text layer at all.

    References each rendered page image by filename so the endpoint's
    ``append_descriptions_to_markdown`` attaches the Vision descriptions in the
    right place — the document becomes self-contained from the images alone.
    """
    lines = [
        "<!-- PDF ohne verwertbare Textebene: jede Seite als Bild gerendert "
        "und per Vision beschrieben. -->",
        "",
    ]
    for name in rendered_pages:
        lines.append(f"![{name}]({name})")
    return "\n".join(lines)


@_mojibake_repaired
def convert_pdf_bytes(pdf_bytes: bytes) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    """Convert PDF bytes → (markdown, metadata, images).

    Docling handles the text/table layer; pages that are image/scan-only or
    large-format drawings (and every page of a PDF Docling cannot parse) are
    rendered to down-scaled PNGs and returned in ``images`` so the endpoint's
    Vision pass describes them. Every returned image passes a hard down-scale
    guard, so a giant embedded raster never reaches Vision.
    """
    geometry = _pdf_page_geometry(pdf_bytes)
    total_pages = len(geometry)
    # Before any Docling or render work, outside the Docling try below: a refused
    # document must never fall back to rendering all of its pages instead.
    # Every env value is read here so a bad one fails loud (ServiceConfigError →
    # 500), never inside the Docling try as a render-all fallback.
    max_pages = _docling_max_pages()
    batch_pages = _docling_batch_pages()
    render_max_bytes = _render_max_bytes()
    _docling_images_scale()
    if total_pages > max_pages:
        raise DocumentTooLargeError(
            f"PDF hat {total_pages} Seiten, die Obergrenze dieses Dienstes ist "
            f"{max_pages} Seiten (DOCLING_MAX_PAGES). Bitte in Teile mit hoechstens "
            f"{max_pages} Seiten zerlegen und einzeln umwandeln."
        )
    if total_pages > batch_pages:
        logger.info(
            f"[pdf] {total_pages} Seiten: Docling in Stapeln zu {batch_pages} Seiten "
            f"(DOCLING_BATCH_PAGES)"
        )

    # 1. Docling for the text/table Markdown + figure extraction (defensive:
    #    a Docling failure is not fatal — we render every page instead).
    docling_markdown = ""
    docling_page_count: Optional[int] = None
    figures: Dict[str, str] = {}
    page_markdowns: Optional[List[Dict[str, Any]]] = None
    figure_meta: Optional[List[Dict[str, Any]]] = None
    docling_failed_pages: List[int] = []
    docling_ok = False
    try:
        (
            docling_markdown,
            docling_page_count,
            figures,
            page_markdowns,
            figure_meta,
            docling_failed_pages,
        ) = _docling_convert_pdf(pdf_bytes)
        docling_ok = True
    except DocumentTooLargeError:
        raise  # a refusal (figure budget) is the answer, never a render-all fallback
    except Exception as e:  # noqa: BLE001 — fall back to full page render
        logger.warning(
            f"[pdf] Docling could not parse the document ({e}); "
            f"rendering all {total_pages} page(s) for Vision instead."
        )

    # 2. Decide which pages to render whole. A page Docling failed on inside a
    #    converted batch is rendered like a scan page (its text is not in the
    #    markdown; Vision describes the image).
    if docling_ok:
        failed = {p - 1 for p in docling_failed_pages}
        pages_to_render = [
            i
            for i, (text_chars, long_edge_pt) in enumerate(geometry)
            if text_chars < MIN_PAGE_TEXT_CHARS
            or long_edge_pt > OVERSIZE_PAGE_LONG_EDGE_PT
            or i in failed
        ]
        why = (
            "diese Seiten keine verwertbare Textebene haben (Scan) oder Grossformat sind"
        )
    else:
        pages_to_render = list(range(total_pages))
        why = "Docling das Dokument nicht lesen konnte und deshalb jede Seite gerendert wird"

    figure_bytes = sum(len(v) for v in figures.values())
    if figure_bytes > render_max_bytes:
        _check_figure_budget(figure_bytes, total_pages, total_pages)
    rendered_pages = _render_pdf_pages(
        pdf_bytes,
        pages_to_render,
        max_total_bytes=render_max_bytes,
        already_bytes=figure_bytes,
        why=why,
    )

    # 3. Uniform down-scale guard over ALL images (figures + rendered pages).
    images: Dict[str, str] = {
        name: _downscale_b64_png(b64) for name, b64 in figures.items()
    }
    images.update(rendered_pages)  # rendered pages are already within the cap

    # 4. Markdown: Docling's text if we have it, else a render-only body that
    #    references the page images (fail loud only if we have literally nothing).
    if docling_ok:
        markdown = docling_markdown
        if docling_failed_pages:
            names = ", ".join(f"page-{p:03d}.png" for p in docling_failed_pages)
            plural = len(docling_failed_pages) > 1
            markdown = (
                f"{markdown.rstrip()}\n\n> **Hinweis:** Docling konnte "
                f"{'die Seiten' if plural else 'die Seite'} "
                f"{', '.join(str(p) for p in docling_failed_pages)} nicht in Text "
                f"umwandeln. {'Sie liegen als gerenderte Bilder' if plural else 'Sie liegt als gerendertes Bild'} "
                f"bei ({names}).\n"
            )
    elif rendered_pages:
        markdown = _render_only_markdown(rendered_pages)
    else:
        raise RuntimeError(
            "PDF could not be parsed by Docling and no pages could be rendered — "
            "the file is unreadable."
        )

    metadata: Dict[str, Any] = {
        "pages": docling_page_count if docling_page_count is not None else total_pages,
        "docling_parsed": docling_ok,
        "rendered_page_numbers": [i + 1 for i in pages_to_render],
        "rendered_page_count": len(rendered_pages),
    }
    # Page-faithful per-page markdown (selective anonymization). Only present
    # on the Docling text-layer path AND when the page split is provably
    # correct — absence is the contract, callers fall back to full-document
    # handling. Named page_markdowns because `pages` (the count) is taken.
    if docling_ok and page_markdowns:
        metadata["page_markdowns"] = page_markdowns
    # [{name, page, caption}] per Docling figure (rendered whole pages are not
    # figures and have no entry). Absent when the mapping is not provably right.
    if docling_ok and figure_meta:
        metadata["figures"] = figure_meta
    # Pages Docling could not convert inside a converted batch; they are also in
    # rendered_page_numbers. Absent when there are none.
    if docling_failed_pages:
        metadata["docling_failed_pages"] = docling_failed_pages
    return markdown, metadata, images


# ----------------------------------------------------------------------------
# LibreOffice PDF helper (used by PPTX and the XLS legacy fallback)
# ----------------------------------------------------------------------------


def _libreoffice_to_pdf(input_path: str, output_dir: str, timeout: int = 300) -> str:
    """Run headless LibreOffice and return the produced PDF path.

    Raises ``RuntimeError`` with stderr if conversion fails.
    """
    if not shutil.which("libreoffice") and not shutil.which("soffice"):
        raise RuntimeError(
            "LibreOffice is not installed in this container — cannot convert Office documents."
        )
    binary = "libreoffice" if shutil.which("libreoffice") else "soffice"
    proc = subprocess.run(
        [
            binary,
            "--headless",
            "--norestore",
            "--nologo",
            "--nofirststartwizard",
            "--convert-to",
            "pdf",
            "--outdir",
            output_dir,
            input_path,
        ],
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"LibreOffice exited {proc.returncode}: "
            f"{proc.stderr.decode('utf-8', errors='replace')[:500]}"
        )

    pdf_name = os.path.splitext(os.path.basename(input_path))[0] + ".pdf"
    pdf_path = os.path.join(output_dir, pdf_name)
    if not os.path.exists(pdf_path):
        raise RuntimeError(
            f"LibreOffice produced no PDF (stdout: "
            f"{proc.stdout.decode('utf-8', errors='replace')[:300]}, "
            f"stderr: {proc.stderr.decode('utf-8', errors='replace')[:300]})"
        )
    return pdf_path


def _convert_via_libreoffice(content: bytes, suffix: str) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    """Generic Office → PDF → Docling pipeline."""
    with tempfile.TemporaryDirectory() as work_dir:
        in_path = os.path.join(work_dir, f"input{suffix}")
        with open(in_path, "wb") as f:
            f.write(content)
        pdf_path = _libreoffice_to_pdf(in_path, work_dir)
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
    return convert_pdf_bytes(pdf_bytes)


# Document text is text: wherever CommonMark (or GFM tables/strikethrough)
# would read a character as syntax, it is backslash-escaped. Inline syntax
# anywhere in a line: emphasis, code spans, pipes, raw HTML, autolinks,
# entities, the backslash itself, and ``]`` — no link, image or reference
# definition can close without it, so ``[`` stays as written.
_INLINE_SYNTAX = re.compile(r"[\\`*_\]~|]|<(?=[A-Za-z/!?])|&(?=#?[0-9A-Za-z]+;)")
# Block syntax only where a line starts: heading, quote, list item, rule or
# setext underline. Any text node may start a rendered line.
_LINE_START = re.compile(
    r"^(\s*)(?:(#{1,6}(?=\s|$)|>|[-+](?=\s|$)|[=-]+\s*$)|(\d{1,9})([.)])(?=\s|$))"
)


def _literal_text(text: str) -> str:
    """Escape Word text so a Markdown reader shows exactly these characters."""
    text = _INLINE_SYNTAX.sub(r"\\\g<0>", text)
    return _LINE_START.sub(
        lambda m: m.group(1)
        + (f"\\{m.group(2)}" if m.group(2) else f"{m.group(3)}\\{m.group(4)}"),
        text,
    )


@_mojibake_repaired
def convert_docx_bytes(content: bytes) -> tuple[str, dict[str, Any], dict[str, str]]:
    """DOCX/DOC → HTML → Markdown, retaining Word's block boundaries.

    PDF layout extraction cannot distinguish w:p from a wrapped line or w:br.
    Writer's HTML export retains that distinction as p versus br, including
    headings, lists and tables. Other Office formats keep the PDF pipeline.
    """
    import zipfile
    from pathlib import Path
    from urllib.parse import unquote, urlsplit

    from bs4 import BeautifulSoup, NavigableString
    from markdownify import markdownify

    from .image_format import UnprocessableWordImage, resize_word_image
    from .word_provenance import apply_provenance
    from .word_safety import (
        EXTERNAL_IMAGE_NOTICE,
        MACRO_NOTICE,
        prepare_word,
        safe_link,
    )

    binary = shutil.which("libreoffice") or shutil.which("soffice")
    if not binary:
        raise RuntimeError(
            "LibreOffice is not installed — cannot convert Word documents."
        )
    with tempfile.TemporaryDirectory() as work_dir:
        root = Path(work_dir)
        if not zipfile.is_zipfile(io.BytesIO(content)) and content.startswith(
            bytes.fromhex("d0cf11e0a1b11ae1")
        ):
            legacy = root / "legacy.doc"
            legacy.write_bytes(content)
            normalized = subprocess.run(
                [
                    binary,
                    f"-env:UserInstallation={(root / 'normalize-profile').as_uri()}",
                    "--headless",
                    "--norestore",
                    "--nologo",
                    "--nofirststartwizard",
                    "--convert-to",
                    "docx",
                    "--outdir",
                    work_dir,
                    str(legacy),
                ],
                capture_output=True,
                timeout=300,
                check=False,
            )
            if normalized.returncode or not (root / "legacy.docx").is_file():
                raise RuntimeError("LibreOffice legacy Word normalization failed")
            word_content = (root / "legacy.docx").read_bytes()
            legacy_doc = True
        else:
            word_content = content
            legacy_doc = False
        prepared = prepare_word(word_content)
        (root / "input.docx").write_bytes(prepared.content)
        # A private profile prevents concurrent conversions attaching to another
        # Writer process and returning before their own output exists.
        proc = subprocess.run(
            [
                binary,
                f"-env:UserInstallation={(root / 'profile').as_uri()}",
                "--headless",
                "--norestore",
                "--nologo",
                "--nofirststartwizard",
                "--convert-to",
                "html:HTML (StarWriter)",
                "--outdir",
                work_dir,
                str(root / "input.docx"),
            ],
            capture_output=True,
            timeout=300,
            check=False,
        )
        output = root / "input.html"
        if proc.returncode != 0 or not output.is_file():
            raise RuntimeError(
                f"LibreOffice Word HTML conversion failed (exit {proc.returncode}): "
                f"{proc.stderr.decode('utf-8', errors='replace')[:500]} "
                f"{proc.stdout.decode('utf-8', errors='replace')[:300]}"
            )
        # Writer exports UTF-8; do not let an optional charset detector guess
        # a different encoding for short text (including repairable mojibake).
        soup = BeautifulSoup(output.read_text(encoding="utf-8"), "html.parser")
        if soup.body is None:
            raise RuntimeError("LibreOffice Word HTML has no body.")
        # Geometry comes from the source cell each HTML cell names, never
        # from Writer's counting; a doubtful table stays local and visible.
        notices = apply_provenance(soup, soup.body, prepared.provenance)
        if prepared.provenance.macros_removed:
            note = soup.new_tag("p")
            note.string = MACRO_NOTICE
            soup.body.insert(0, note)
        # Writer pretty-prints its HTML. Those source newlines are whitespace,
        # not Word breaks; only <br> must become Markdown's hard line break.
        for node in list(soup.body.find_all(string=True)):
            if node.find_parent("pre"):
                continue
            text = re.sub(r"\s+", " ", str(node))
            block = node.parent.name in {
                "p",
                "li",
                "td",
                "th",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
            }
            if (block and node.previous_sibling is None) or getattr(
                node.previous_sibling, "name", None
            ) == "br":
                text = text.lstrip()
            if (block and node.next_sibling is None) or getattr(
                node.next_sibling, "name", None
            ) == "br":
                text = text.rstrip()
            if not text or (
                not text.strip()
                and node.parent.name
                in {
                    "body",
                    "table",
                    "thead",
                    "tbody",
                    "tfoot",
                    "tr",
                    "ul",
                    "ol",
                }
            ):
                node.extract()
            else:
                node.replace_with(text)
        for node in soup.body.find_all(["script", "style"]):
            node.decompose()
        for link in soup.body.find_all("a", href=True):
            if not safe_link(link["href"]):
                link.unwrap()
        images: dict[str, str] = {}
        for image in soup.body.find_all("img"):
            source = unquote(image.get("src", ""))
            if urlsplit(source).scheme or source.startswith("//"):
                image.replace_with(EXTERNAL_IMAGE_NOTICE)
                continue
            path = (root / source).resolve()
            if not source or path.parent != root.resolve() or not path.is_file():
                raise RuntimeError(
                    f"Word image is not a local export artifact: {source!r}"
                )
            try:
                raw, extension = resize_word_image(path.read_bytes(), MAX_IMAGE_EDGE)
            except UnprocessableWordImage as exc:
                logger.warning("Word image omitted with visible notice: %s", exc)
                image.replace_with("[Eingebettetes Bild nicht verarbeitbar]")
                continue
            name = path.stem + extension
            images[name] = base64.b64encode(raw).decode("ascii")
            image["src"] = name

        # Pipe tables cannot represent spans/nesting. Keep these as sanitized
        # HTML, including their exact cell geometry, and say so explicitly.
        html_tables = {}
        # A per-document token: ordinary document text can never collide.
        document_text = soup.body.get_text()
        token = secrets.token_hex(6)
        while token in document_text:
            token = secrets.token_hex(6)
        for table in list(soup.body.find_all("table")):
            if table.find_parent("table") is not None:
                continue
            complex_table = (
                table.find("table") is not None
                or table.find(
                    lambda tag: (
                        tag.name in {"td", "th"}
                        and (
                            int(tag.get("colspan", 1)) > 1
                            or int(tag.get("rowspan", 1)) > 1
                        )
                    )
                )
                is not None
            )
            if complex_table:
                for tag in [table, *table.find_all(True)]:
                    tag.attrs = {
                        k: v
                        for k, v in tag.attrs.items()
                        if (tag.name in {"td", "th"} and k in {"rowspan", "colspan"})
                        or (tag.name == "a" and k == "href")
                        or (tag.name == "img" and k in {"src", "alt"})
                    }
                for anchor in table.find_all("a", href=False):
                    anchor.unwrap()  # Writer's bookmark anchors, now without a name
                marker = f"WORDTABLE{token}N{len(html_tables)}END"
                notice = (
                    "Verschachtelte Tabelle: als HTML mit Zellstruktur erhalten."
                    if table.find("table")
                    else "Verbundene Tabellenzellen: als HTML mit Zellstruktur "
                    "erhalten."
                )
                if legacy_doc:
                    # The geometry source is LibreOffice's own .doc reading;
                    # there is no independent original to check it against.
                    notice += (
                        " Zellverbindungen stammen aus der .doc-Umwandlung"
                        " und sind nicht gegen das Original prüfbar."
                    )
                html_tables[marker] = "\n\n" + notice + "\n\n" + str(table) + "\n\n"
                table.replace_with(marker)
            else:
                for line_break in table.find_all("br"):
                    line_break.replace_with(" ")
                for column in table.find_all(["col", "colgroup"]):
                    column.decompose()
                first_row = table.find("tr")
                if first_row is not None and not first_row.find("th"):
                    for cell in first_row.find_all("td", recursive=False):
                        cell.name = "th"
        # Text that merely looks like Markdown or HTML (``*x*``, ``[a](b)``,
        # ``<a name=…>``, ``&amp;``) must stay text: escaped here, in one place,
        # so markdownify's own escaping is off. The HTML tables above are
        # already serialized with entities and are not touched here.
        for node in list(soup.body.find_all(string=True)):
            if type(node) is not NavigableString or node.find_parent(
                ["pre", "code", "kbd", "samp"]
            ):
                continue
            escaped = _literal_text(str(node))
            if escaped != str(node):
                node.replace_with(escaped)
        markdown = markdownify(
            str(soup.body),
            heading_style="ATX",
            escape_misc=False,
            escape_asterisks=False,
            escape_underscores=False,
        ).strip()
        for marker, table_html in html_tables.items():
            markdown = markdown.replace(marker, table_html)
    return (
        markdown,
        {
            "original_size_bytes": len(content),
            "pages": None,
            "page_count_status": "unavailable",
            "page_count_note": (
                "Keine Seitenangabe: Word wurde ohne Seitenlayout konvertiert."
            ),
            "word_notices": notices,
        },
        images,
    )


def convert_pptx_bytes(content: bytes) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    """PPTX/PPT → Markdown via LibreOffice → PDF → Docling.

    Complex layouts may render imperfectly; we log but do not fail.
    """
    try:
        return _convert_via_libreoffice(content, ".pptx")
    except RuntimeError as e:
        logger.warning(f"PPTX conversion produced runtime error (best-effort): {e}")
        raise


# ----------------------------------------------------------------------------
# Spreadsheet adapters
# ----------------------------------------------------------------------------


def _df_to_markdown(df, max_rows: Optional[int] = None) -> str:
    """Render a pandas DataFrame to a Markdown table.

    Truncates rows above ``max_rows`` so a giant sheet does not blow up the
    response; we surface the truncation in metadata.
    """
    if max_rows is not None and len(df) > max_rows:
        truncated = df.head(max_rows)
        return truncated.to_markdown(index=False) + f"\n\n_(truncated: showing {max_rows} of {len(df)} rows)_"
    return df.to_markdown(index=False)


@_mojibake_repaired
def convert_xlsx_bytes(content: bytes, max_rows_per_sheet: int = 5000) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    """XLSX/XLS → Markdown (one ``## SheetName`` heading per sheet)."""
    import pandas as pd

    buf = io.BytesIO(content)
    # ``engine=None`` lets pandas auto-pick openpyxl (xlsx) or xlrd (xls).
    try:
        xls = pd.ExcelFile(buf)
    except Exception as e:
        raise RuntimeError(f"Could not open spreadsheet: {e}") from e

    sheets_meta: List[Dict[str, Any]] = []
    parts: List[str] = []
    for sheet_name in xls.sheet_names:
        df = pd.read_excel(xls, sheet_name=sheet_name, dtype=object)
        df = df.fillna("")
        parts.append(f"## {sheet_name}\n\n{_df_to_markdown(df, max_rows=max_rows_per_sheet)}\n")
        sheets_meta.append(
            {
                "name": sheet_name,
                "rows": int(len(df)),
                "columns": [str(c) for c in df.columns],
                "truncated": bool(len(df) > max_rows_per_sheet),
            }
        )
    markdown = "\n".join(parts)
    return markdown, {"sheets": sheets_meta, "sheet_count": len(sheets_meta)}, {}


@_mojibake_repaired
def convert_csv_bytes(content: bytes) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    """CSV → Markdown table. Auto-detects encoding (utf-8 → cp1252 fallback)."""
    import pandas as pd

    text: Optional[str] = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise RuntimeError("Could not decode CSV with any of utf-8/cp1252/latin-1.")

    # ``sep=None`` triggers pandas' Python-engine sniffer for delimiter.
    df = pd.read_csv(io.StringIO(text), sep=None, engine="python", dtype=object).fillna("")
    markdown = _df_to_markdown(df, max_rows=10000)
    return (
        markdown,
        {"rows": int(len(df)), "columns": [str(c) for c in df.columns]},
        {},
    )


# ----------------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------------


@_mojibake_repaired
def convert_html_bytes(content: bytes) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    """HTML → Markdown via the ``markdownify`` library."""
    from markdownify import markdownify as html_to_md

    text: Optional[str] = None
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise RuntimeError("Could not decode HTML.")

    markdown = html_to_md(text, heading_style="ATX")
    return markdown, {"original_size_bytes": len(content)}, {}


# ----------------------------------------------------------------------------
# Email adapters: MSG (Outlook binary) and EML (RFC822)
# ----------------------------------------------------------------------------


def _format_email_markdown(
    subject: str,
    sender: str,
    to: str,
    date: str,
    body: str,
    attachments: List[Dict[str, Any]],
) -> str:
    parts = [
        f"# {subject or '(no subject)'}",
        "",
        f"**From:** {sender or '(unknown)'}",
        f"**To:** {to or '(unknown)'}",
        f"**Date:** {date or '(unknown)'}",
    ]
    if attachments:
        names = ", ".join(a.get("filename") or "(unnamed)" for a in attachments)
        parts.append(f"**Attachments:** {names}")
    parts.extend(["", "---", "", body or ""])
    return "\n".join(parts)


@_mojibake_repaired
def convert_msg_bytes(content: bytes) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    """Outlook .msg → Markdown."""
    import extract_msg

    with tempfile.NamedTemporaryFile(suffix=".msg", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        msg = extract_msg.Message(tmp_path)
        try:
            body_text = msg.body
            html_body = msg.htmlBody
            if (not body_text or not str(body_text).strip()) and html_body:
                from markdownify import markdownify as html_to_md

                if isinstance(html_body, bytes):
                    html_body = html_body.decode("utf-8", errors="replace")
                body_text = html_to_md(html_body, heading_style="ATX")
            if isinstance(body_text, bytes):
                body_text = body_text.decode("utf-8", errors="replace")
            body_text = body_text or ""

            attachments_meta: List[Dict[str, Any]] = []
            for att in (msg.attachments or []):
                size = 0
                try:
                    data = att.data
                    if data is not None:
                        size = len(data) if isinstance(data, (bytes, bytearray)) else 0
                except Exception:
                    size = 0
                attachments_meta.append(
                    {
                        "filename": att.longFilename or att.shortFilename or "(unnamed)",
                        "size_bytes": size,
                    }
                )

            metadata = {
                "subject": msg.subject,
                "from": str(msg.sender) if msg.sender else None,
                "to": str(msg.to) if msg.to else None,
                "date": str(msg.date) if msg.date else None,
                "attachments": attachments_meta,
            }
            markdown = _format_email_markdown(
                subject=msg.subject or "",
                sender=str(msg.sender) if msg.sender else "",
                to=str(msg.to) if msg.to else "",
                date=str(msg.date) if msg.date else "",
                body=body_text,
                attachments=attachments_meta,
            )
            return markdown, metadata, {}
        finally:
            try:
                msg.close()
            except Exception:
                pass
    finally:
        os.unlink(tmp_path)


@_mojibake_repaired
def convert_eml_bytes(content: bytes) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    """RFC822 .eml → Markdown using stdlib ``email`` (no extra dep)."""
    import email
    from email import policy

    msg = email.message_from_bytes(content, policy=policy.default)

    body_text = ""
    if msg.is_multipart():
        # Prefer text/plain, fall back to text/html → markdownify.
        plain_part = None
        html_part = None
        for part in msg.walk():
            ct = part.get_content_type()
            disposition = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disposition:
                continue
            if ct == "text/plain" and plain_part is None:
                plain_part = part
            elif ct == "text/html" and html_part is None:
                html_part = part
        if plain_part is not None:
            body_text = plain_part.get_content() or ""
        elif html_part is not None:
            from markdownify import markdownify as html_to_md

            body_text = html_to_md(html_part.get_content() or "", heading_style="ATX")
    else:
        ct = msg.get_content_type()
        raw_body = msg.get_content() or ""
        if ct == "text/html":
            from markdownify import markdownify as html_to_md

            body_text = html_to_md(raw_body, heading_style="ATX")
        else:
            body_text = raw_body

    attachments_meta: List[Dict[str, Any]] = []
    for part in msg.iter_attachments():
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            payload = b""
        attachments_meta.append(
            {
                "filename": part.get_filename() or "(unnamed)",
                "size_bytes": len(payload),
                "content_type": part.get_content_type(),
            }
        )

    subject = msg.get("Subject", "") or ""
    sender = msg.get("From", "") or ""
    to = msg.get("To", "") or ""
    date = msg.get("Date", "") or ""

    markdown = _format_email_markdown(
        subject=subject, sender=sender, to=to, date=date,
        body=body_text, attachments=attachments_meta,
    )
    metadata = {
        "subject": subject or None,
        "from": sender or None,
        "to": to or None,
        "date": date or None,
        "attachments": attachments_meta,
    }
    return markdown, metadata, {}


# ----------------------------------------------------------------------------
# Image (Docling vision OCR — optional)
# ----------------------------------------------------------------------------


@_mojibake_repaired
def convert_image_bytes(content: bytes, suffix: str = ".png") -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    """Image → Markdown via Docling using InputFormat.IMAGE (best-effort).

    If the running Docling build lacks that input format, fail loud.
    """
    try:
        from docling.document_converter import DocumentConverter
        from docling.datamodel.base_models import InputFormat  # noqa: F401  (sanity import)
    except ImportError as e:
        raise RuntimeError(f"Docling not installed: {e}") from e

    converter = DocumentConverter()  # default options OK for images

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        result = converter.convert(tmp_path)
        markdown = result.document.export_to_markdown()
        return markdown, {"image_format": suffix.lstrip(".")}, {}
    except Exception as e:
        raise RuntimeError(f"Image OCR failed: {e}") from e
    finally:
        os.unlink(tmp_path)
