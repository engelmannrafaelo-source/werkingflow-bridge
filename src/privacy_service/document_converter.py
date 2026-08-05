"""
Universal Document Converter for the Privacy Service.

Converts PDF / DOCX / PPTX / XLSX / XLS / CSV / HTML / MSG / EML / images into
Markdown that can be fed into the Smart-Anonymize pipeline or returned directly.

Each adapter is a synchronous function returning ``(markdown, metadata, images)``.
There is no shared dispatcher — three independent production entry points call
these adapters directly: ``/convert-pdf`` calls ``convert_pdf_bytes`` itself;
``/document/convert`` and ``/document/convert-and-anonymize`` go through
``adapters.AdapterChain``, which also calls the ``convert_*_bytes`` functions
directly; DOCX/PPTX route through LibreOffice → ``convert_pdf_bytes``. Anything
that must hold for every real caller — like the mojibake repair below — is
therefore wired directly onto the adapter functions via the
``@_mojibake_repaired`` decorator, not onto a dispatcher some callers skip.

Design constraints (per task brief):
- Fail loud on unknown formats — never silently fall back to a different adapter.
- Reuse the existing Docling PDF pipeline for PDF and for LibreOffice-converted
  Office documents so the OCR/table behaviour stays consistent.
- DOCX/PPTX go through LibreOffice → PDF → Docling. PPTX with very complex
  layouts logs a warning but does not fail.
"""

from __future__ import annotations

import base64
import functools
import io
import logging
import os
import re
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
# actually be converted. ``convert_docx_bytes``/``convert_pptx_bytes`` carry no
# decorator of their own — they delegate to the (already decorated)
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
    pdf_bytes: bytes, page_indices: List[int], max_edge: int = RENDER_PAGE_MAX_EDGE
) -> Dict[str, str]:
    """Render the given 0-indexed pages to down-scaled PNGs.

    Returns ``{"page-NNN.png": base64-png}`` (1-indexed in the filename so it
    reads naturally in the appended descriptions). The render scale is chosen so
    the long edge lands at ``max_edge`` px, which both avoids the giant-raster
    blow-up and keeps CAD detail legible for Vision.
    """
    if not page_indices:
        return {}

    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(pdf_bytes)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Could not open PDF for page rendering: {e}") from e

    rendered: Dict[str, str] = {}
    try:
        for idx in page_indices:
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
            rendered[f"page-{idx + 1:03d}.png"] = base64.b64encode(
                buf.getvalue()
            ).decode("utf-8")
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


def _docling_convert_pdf(
    pdf_bytes: bytes,
) -> Tuple[str, Optional[int], Dict[str, str], Optional[List[Dict[str, Any]]]]:
    """Run Docling over a PDF → (markdown, page_count, figures, page_markdowns).

    This is the text/table extraction half of the cascade. Raises on any Docling
    failure so the caller can fall back to a full page render.
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
        result = converter.convert(tmp_pdf_path)

        with tempfile.TemporaryDirectory() as tmp_dir:
            md_path = os.path.join(tmp_dir, "output.md")
            result.document.save_as_markdown(md_path, image_mode=ImageRefMode.REFERENCED)

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

            page_count = (
                len(result.document.pages) if hasattr(result.document, "pages") else None
            )

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
                result.document.save_as_markdown(
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

            return markdown, page_count, figures, page_markdowns
    finally:
        os.unlink(tmp_pdf_path)


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

    # 1. Docling for the text/table Markdown + figure extraction (defensive:
    #    a Docling failure is not fatal — we render every page instead).
    docling_markdown = ""
    docling_page_count: Optional[int] = None
    figures: Dict[str, str] = {}
    page_markdowns: Optional[List[Dict[str, Any]]] = None
    docling_ok = False
    try:
        docling_markdown, docling_page_count, figures, page_markdowns = _docling_convert_pdf(pdf_bytes)
        docling_ok = True
    except Exception as e:  # noqa: BLE001 — fall back to full page render
        logger.warning(
            f"[pdf] Docling could not parse the document ({e}); "
            f"rendering all {total_pages} page(s) for Vision instead."
        )

    # 2. Decide which pages to render whole.
    if docling_ok:
        pages_to_render = [
            i
            for i, (text_chars, long_edge_pt) in enumerate(geometry)
            if text_chars < MIN_PAGE_TEXT_CHARS
            or long_edge_pt > OVERSIZE_PAGE_LONG_EDGE_PT
        ]
    else:
        pages_to_render = list(range(total_pages))

    rendered_pages = _render_pdf_pages(pdf_bytes, pages_to_render)

    # 3. Uniform down-scale guard over ALL images (figures + rendered pages).
    images: Dict[str, str] = {
        name: _downscale_b64_png(b64) for name, b64 in figures.items()
    }
    images.update(rendered_pages)  # rendered pages are already within the cap

    # 4. Markdown: Docling's text if we have it, else a render-only body that
    #    references the page images (fail loud only if we have literally nothing).
    if docling_ok:
        markdown = docling_markdown
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
    return markdown, metadata, images


# ----------------------------------------------------------------------------
# LibreOffice helper (used by DOCX, PPTX, XLS legacy fallback)
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


def convert_docx_bytes(content: bytes) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    """DOCX/DOC → Markdown via LibreOffice → PDF → Docling."""
    return _convert_via_libreoffice(content, ".docx")


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
    """Image → Markdown via Docling (best-effort).

    Docling supports images as ``InputFormat.IMAGE`` in recent versions.
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
