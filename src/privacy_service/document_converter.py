"""
Universal Document Converter for the Privacy Service.

Converts PDF / DOCX / PPTX / XLSX / XLS / CSV / HTML / MSG / EML / images into
Markdown that can be fed into the Smart-Anonymize pipeline or returned directly.

Each adapter is a synchronous function returning ``(markdown, metadata)``.
The dispatcher ``convert_document_sync`` routes by file extension or explicit
``mime_type_hint`` and is intended to be invoked from an async endpoint via
``loop.run_in_executor``.

Design constraints (per task brief):
- Fail loud on unknown formats — never silently fall back to a different adapter.
- Reuse the existing Docling PDF pipeline for PDF and for LibreOffice-converted
  Office documents so the OCR/table behaviour stays consistent.
- DOCX/PPTX go through LibreOffice → PDF → Docling. PPTX with very complex
  layouts logs a warning but does not fail.
"""

from __future__ import annotations

import base64
import io
import logging
import os
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
# PDF (Docling) — extracted from app.py so other adapters can reuse it.
# ----------------------------------------------------------------------------


def convert_pdf_bytes(pdf_bytes: bytes) -> Tuple[str, Dict[str, Any], Dict[str, str]]:
    """Convert PDF bytes → (markdown, metadata, images).

    Uses the same Docling pipeline as the legacy ``/convert-pdf`` endpoint.
    Returned images are a ``{filename: base64-png}`` dict referenced from the
    Markdown via plain filenames (Docling's REFERENCED image mode after
    rewrite).
    """
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.datamodel.base_models import InputFormat
    from docling_core.types.doc.base import ImageRefMode

    pipeline_options = PdfPipelineOptions()
    pipeline_options.generate_picture_images = True
    pipeline_options.generate_table_images = False
    pipeline_options.do_ocr = True

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

            images: Dict[str, str] = {}
            artifacts_dir = os.path.join(tmp_dir, "output_artifacts")
            if os.path.exists(artifacts_dir):
                for img_name in os.listdir(artifacts_dir):
                    if img_name.lower().endswith((".png", ".jpg", ".jpeg")):
                        img_path = os.path.join(artifacts_dir, img_name)
                        with open(img_path, "rb") as img_f:
                            images[img_name] = base64.b64encode(img_f.read()).decode("utf-8")

            for img_name in images:
                markdown = markdown.replace(os.path.join(artifacts_dir, img_name), img_name)

            page_count = (
                len(result.document.pages) if hasattr(result.document, "pages") else None
            )
            return markdown, {"pages": page_count}, images
    finally:
        os.unlink(tmp_pdf_path)


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


# ----------------------------------------------------------------------------
# Public dispatcher
# ----------------------------------------------------------------------------


def convert_document_sync(
    content: bytes,
    filename: str,
    mime_type_hint: Optional[str] = None,
) -> ConversionResult:
    """Route a document to the appropriate adapter and return a ``ConversionResult``.

    Raises ``UnsupportedFormatError`` for unknown formats and ``RuntimeError``
    for adapter failures (callers map these to HTTP 415 / 500).
    """
    if not content:
        raise RuntimeError("Empty file.")

    fmt = detect_format(filename, mime_type_hint)
    logger.info(f"convert_document: filename={filename!r} format={fmt} size={len(content)}")

    if fmt == "pdf":
        markdown, metadata, images = convert_pdf_bytes(content)
    elif fmt == "docx":
        markdown, metadata, images = convert_docx_bytes(content)
    elif fmt == "pptx":
        markdown, metadata, images = convert_pptx_bytes(content)
    elif fmt == "xlsx":
        markdown, metadata, images = convert_xlsx_bytes(content)
    elif fmt == "csv":
        markdown, metadata, images = convert_csv_bytes(content)
    elif fmt == "html":
        markdown, metadata, images = convert_html_bytes(content)
    elif fmt == "msg":
        markdown, metadata, images = convert_msg_bytes(content)
    elif fmt == "eml":
        markdown, metadata, images = convert_eml_bytes(content)
    elif fmt == "image":
        ext = os.path.splitext(filename or "")[1].lower() or ".png"
        markdown, metadata, images = convert_image_bytes(content, suffix=ext)
    else:  # pragma: no cover — detect_format already validates this
        raise UnsupportedFormatError(f"No adapter for format token {fmt!r}")

    metadata = dict(metadata or {})
    metadata["original_size_bytes"] = len(content)
    metadata["filename"] = filename

    return ConversionResult(
        markdown=markdown,
        fmt=fmt,
        metadata=metadata,
        images=images or None,
    )
