"""
Privacy & PDF Service — Standalone FastAPI application.

Runs as Container 4 in the Bridge architecture.
Hosts all heavy NLP dependencies (Presidio, spaCy, Docling, PyTorch).
Internal-only: not exposed to nginx/public internet.
"""

import os
import re
import json
import asyncio
import logging
import base64
import tempfile
import time as _time
from typing import Dict, List, Optional, Any, Tuple

from pydantic import BaseModel, Field
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import JSONResponse

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("privacy-service")


# ======================== Request/Response Models ========================

class AnonymizeRequest(BaseModel):
    messages: List[Dict[str, Any]]
    privacy_mode: Optional[str] = "full"


class AnonymizeResponse(BaseModel):
    messages: List[Dict[str, Any]]
    mapping: Dict[str, str]


class DeanonymizeRequest(BaseModel):
    text: str
    mapping: Dict[str, str]


class DeanonymizeResponse(BaseModel):
    text: str


class SmartAnonymizeServiceRequest(BaseModel):
    text: str
    language: Optional[str] = "de"
    context_hint: Optional[str] = None
    prefix: Optional[str] = None


class StatusResponse(BaseModel):
    enabled: bool
    available: bool
    language: str
    supported_entities: List[str]


class ConvertSemanticHtmlRequest(BaseModel):
    html: str


class ConvertSemanticHtmlResponse(BaseModel):
    status: str
    html: str
    pages: int
    converted: bool


# ======================== App Setup ========================

app = FastAPI(title="Privacy & PDF Service", version="1.0.0")

# Active request counter for graceful shutdown
_active_requests = 0
_active_requests_lock = asyncio.Lock()


@app.middleware("http")
async def track_active_requests(request: Request, call_next):
    global _active_requests
    if request.url.path not in ("/health", "/ready", "/status"):
        async with _active_requests_lock:
            _active_requests += 1
        try:
            response = await call_next(request)
            return response
        finally:
            async with _active_requests_lock:
                _active_requests -= 1
    else:
        return await call_next(request)


# Lazy-initialized middleware singleton
_privacy_middleware = None


def get_middleware():
    global _privacy_middleware
    if _privacy_middleware is None:
        from src.privacy.middleware import PrivacyMiddleware
        _privacy_middleware = PrivacyMiddleware(enabled=True)
    return _privacy_middleware


# ======================== Privacy Endpoints ========================

@app.post("/anonymize", response_model=AnonymizeResponse)
async def anonymize_endpoint(req: AnonymizeRequest):
    """Anonymize messages using Presidio. Returns anonymized messages + mapping."""
    middleware = get_middleware()
    anon_messages, mapping = middleware.anonymize_messages(
        req.messages, privacy_mode=req.privacy_mode
    )
    return AnonymizeResponse(messages=anon_messages, mapping=mapping)


@app.post("/deanonymize", response_model=DeanonymizeResponse)
async def deanonymize_endpoint(req: DeanonymizeRequest):
    """De-anonymize text (simple string replace). Mainly for non-streaming use."""
    middleware = get_middleware()
    result = middleware.deanonymize_response(req.text, req.mapping)
    return DeanonymizeResponse(text=result)


@app.post("/smart-anonymize")
async def smart_anonymize_service_endpoint(req: SmartAnonymizeServiceRequest):
    """Smart anonymization: Presidio + AI refinement.

    The AI refinement step calls BACK to the workers via BRIDGE_SELF_URL.
    """
    from src.privacy.smart_anonymizer import smart_anonymize
    result = await smart_anonymize(
        text=req.text,
        language=req.language or "de",
        context_hint=req.context_hint,
        prefix=req.prefix,
    )
    return result


# ======================== PDF Conversion Endpoint ========================

@app.post("/convert-pdf")
async def convert_pdf_service_endpoint(request: Request):
    """Convert PDF to Markdown + extract images using Docling.

    Accepts multipart/form-data with PDF file upload.
    Max file size: 100 MB.
    """
    MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB

    try:
        form = await request.form()
        file = form.get("file")
        if not file:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "error": "No file uploaded."},
            )

        filename = getattr(file, "filename", "upload.pdf") or "upload.pdf"
        if not filename.lower().endswith(".pdf"):
            return JSONResponse(
                status_code=400,
                content={"status": "error", "error": f"Invalid file type: {filename}"},
            )

        pdf_bytes = await file.read()
        original_size = len(pdf_bytes)

        if original_size == 0:
            return {"status": "error", "error": "Uploaded file is empty."}
        if original_size > MAX_FILE_SIZE:
            return {
                "status": "error",
                "error": f"File too large: {original_size / 1024 / 1024:.1f} MB. Maximum: 100 MB.",
            }

        logger.info(f"PDF conversion started: {filename} ({original_size / 1024:.1f} KB)")

        t_start = _time.time()

        def _convert_pdf():
            """Synchronous Docling conversion — runs in executor."""
            from docling.document_converter import DocumentConverter, PdfFormatOption
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.datamodel.base_models import InputFormat
            from docling_core.types.doc.base import ImageRefMode

            pipeline_options = PdfPipelineOptions()
            pipeline_options.generate_picture_images = True
            pipeline_options.generate_table_images = False
            pipeline_options.do_ocr = True

            converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
                }
            )

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_pdf:
                tmp_pdf.write(pdf_bytes)
                tmp_pdf_path = tmp_pdf.name

            try:
                result = converter.convert(tmp_pdf_path)

                with tempfile.TemporaryDirectory() as tmp_dir:
                    md_path = os.path.join(tmp_dir, "output.md")
                    result.document.save_as_markdown(
                        md_path, image_mode=ImageRefMode.REFERENCED
                    )

                    with open(md_path, "r") as f:
                        markdown = f.read()

                    images = {}
                    artifacts_dir = os.path.join(tmp_dir, "output_artifacts")
                    if os.path.exists(artifacts_dir):
                        for img_name in os.listdir(artifacts_dir):
                            if img_name.lower().endswith((".png", ".jpg", ".jpeg")):
                                img_path = os.path.join(artifacts_dir, img_name)
                                with open(img_path, "rb") as img_f:
                                    images[img_name] = base64.b64encode(
                                        img_f.read()
                                    ).decode("utf-8")

                    for img_name in images:
                        markdown = markdown.replace(
                            os.path.join(artifacts_dir, img_name), img_name
                        )

                    page_count = (
                        len(result.document.pages)
                        if hasattr(result.document, "pages")
                        else None
                    )
                    return markdown, images, page_count

            finally:
                os.unlink(tmp_pdf_path)

        loop = asyncio.get_event_loop()
        markdown, images, page_count = await loop.run_in_executor(None, _convert_pdf)
        conversion_time = _time.time() - t_start

        logger.info(
            f"PDF conversion complete: {filename} -> "
            f"{len(markdown)} chars, {len(images)} images, "
            f"{conversion_time:.1f}s"
        )

        return {
            "status": "success",
            "markdown": markdown,
            "images": images if images else None,
            "image_count": len(images),
            "pages": page_count,
            "original_size_bytes": original_size,
            "markdown_size_bytes": len(markdown.encode("utf-8")),
            "conversion_time_seconds": round(conversion_time, 2),
        }

    except ImportError as e:
        logger.error(f"Docling not installed: {e}")
        return {
            "status": "error",
            "error": "Docling is not installed on this service.",
        }
    except Exception as e:
        logger.error(f"PDF conversion failed: {e}", exc_info=True)
        return {"status": "error", "error": f"PDF conversion failed: {str(e)}"}


# ======================== Semantic HTML Conversion ========================

# Direct Anthropic API — bypasses Bridge workers to avoid rate-limit contention
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
HAIKU_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

SEMANTIC_CONVERTER_SYSTEM_PROMPT = """# HTML-Strukturkonverter fuer Dokumenten-Templates

## Kontext

Du erhaeltst HTML aus einer automatischen PDF-zu-HTML-Konvertierung. Dieses HTML verwendet
absolute Positionierung (position: absolute, left/top in rem) fuer jedes Element. Die visuelle
Darstellung ist pixelperfekt, aber die Struktur ist flach — alle Elemente liegen als Geschwister
in einem page-Container.

Das HTML enthaelt mehrere <div class="page">-Bloecke. Jede Seite hat dieselbe Grundstruktur.

## Aufgabe

Wandle dieses pixel-positionierte HTML in semantisches HTML mit CSS Flexbox um. Das Ergebnis
soll die gleiche visuelle Struktur ausdruecken, aber mit logischer DOM-Hierarchie statt absoluter
Koordinaten.

## Zonen-Erkennung ueber Y-Koordinaten

Die top-Werte in rem verraten die Dokumentzonen:
- Elemente mit top < 15rem gehoeren zum Seitenkopf (Logo, Firmeninfo, Kontaktdaten)
- Elemente mit top zwischen 15rem und 73rem gehoeren zum Hauptinhalt
- Elemente mit top > 73rem gehoeren zur Fusszeile

Innerhalb jeder Zone:
- Elemente mit aehnlichem top-Wert (Differenz < 3rem) stehen nebeneinander → flexbox row
- Elemente mit aufsteigendem top-Wert stehen untereinander → normaler Fluss

## CSS-Klassen als semantische Hinweise

Die bestehenden CSS-Klassen zeigen die Textart:
- .title → Hauptueberschrift (wird h1)
- .heading-1 → Kapitelueberschrift (wird h2)
- .heading-2 → Unterueberschrift (wird h3)
- .body-text → Fliesstext (wird p)
- .list-paragraph → Listenpunkt (wird li in ul/ol)
- .table-paragraph → Tabellentext
- .textbox → Umrandeter Bereich (wird div mit border)

## Ausgabe-Format

Ein HTML-Fragment bestehend aus:
1. Ein <style>-Tag mit allen CSS-Regeln (Flexbox, keine absolute Positionierung)
2. Pro Seite ein <div class="page">
3. Innerhalb jeder Seite: <header>, <main>, <footer> Bereiche
4. Semantische Tags: h1, h2, h3, p, table, ul, ol, figure

## Erhaltung

- Bild-Platzhalter wie src="{{IMG_0}}" muessen 1:1 erhalten bleiben (werden spaeter durch echte Bilder ersetzt)
- Alle SVG-Elemente 1:1 erhalten (koennen in header oder footer als Deko-Grafik stehen)
- Farben, Schriftarten und Schriftgroessen aus dem CSS-Block und inline-Styles uebernehmen
- Text-Inhalte 1:1 uebernehmen, auch gekuerzte Texte mit "[...]"
- Kein <!DOCTYPE>, <html>, <head>, <body> — nur <style> + <div class="page">-Bloecke

Antworte ausschliesslich mit dem konvertierten HTML. Keine Erklaerungen, kein Markdown."""

PAGE_SPLIT_PATTERN = re.compile(r'(<div\s+class="page"[^>]*>)', re.IGNORECASE)
BASE64_SRC_PATTERN = re.compile(r'src="(data:image/[^"]+)"')
IMG_PLACEHOLDER_PATTERN = re.compile(r'\{\{IMG_\d+\}\}')
PAGE_SIZE_THRESHOLD = 100 * 1024  # 100 KB


def _replace_base64_with_placeholders(html: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Replace base64 data:-URLs with {{IMG_N}} placeholders to reduce size for AI."""
    placeholders: List[Tuple[str, str]] = []
    index = 0

    def replacer(match: re.Match) -> str:
        nonlocal index
        data_uri = match.group(1)
        placeholder = f"{{{{IMG_{index}}}}}"
        placeholders.append((placeholder, data_uri))
        index += 1
        return f'src="{placeholder}"'

    result = BASE64_SRC_PATTERN.sub(replacer, html)
    return result, placeholders


def _restore_placeholders(html: str, placeholders: List[Tuple[str, str]]) -> str:
    """Restore {{IMG_N}} placeholders back to original base64 data:-URLs."""
    result = html
    for placeholder, original_src in placeholders:
        result = result.replace(placeholder, original_src)
    return result


def _split_pages(html: str) -> List[str]:
    """Split HTML into per-page chunks at <div class="page"> boundaries."""
    parts = PAGE_SPLIT_PATTERN.split(html)
    if len(parts) <= 1:
        return [html]

    # parts[0] = preamble (style block etc.), parts[1] = first <div class="page">, parts[2] = content, ...
    preamble = parts[0]
    pages = []
    for i in range(1, len(parts), 2):
        tag = parts[i]
        content = parts[i + 1] if i + 1 < len(parts) else ""
        pages.append(preamble + tag + content)
    return pages


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences if AI wrapped output."""
    text = text.strip()
    if text.startswith("```html"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


async def _call_ai_for_conversion(html_chunk: str, max_retries: int = 4) -> str:
    """Call Anthropic API directly for HTML conversion (bypasses Bridge workers)."""
    import asyncio
    import httpx

    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set — required for semantic HTML conversion")

    request_body = {
        "model": HAIKU_MODEL,
        "max_tokens": 16000,
        "system": SEMANTIC_CONVERTER_SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": html_chunk},
        ],
    }

    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }

    last_error = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(ANTHROPIC_API_URL, headers=headers, json=request_body)

            if response.status_code == 200:
                result = response.json()
                content = result.get("content", [{}])[0].get("text", "")
                return _strip_markdown_fences(content)

            if response.status_code == 429:
                retry_after = int(response.headers.get("retry-after", 30))
                logger.warning(f"[SemanticConverter] Rate limited, waiting {retry_after}s")
                await asyncio.sleep(retry_after)
                continue

            last_error = f"Anthropic API returned {response.status_code}: {response.text[:300]}"
            logger.warning(
                f"[SemanticConverter] AI call failed (attempt {attempt + 1}/{max_retries}): {last_error}"
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            last_error = f"Anthropic API connection error: {e}"
            logger.warning(
                f"[SemanticConverter] AI call failed (attempt {attempt + 1}/{max_retries}): {last_error}"
            )

        if attempt < max_retries - 1:
            wait = 15 * (attempt + 1)
            logger.info(f"[SemanticConverter] Retrying in {wait}s...")
            await asyncio.sleep(wait)

    raise RuntimeError(last_error)


async def _convert_pixel_to_semantic_html(pixel_html: str) -> Tuple[str, int, bool]:
    """Internal: convert pixel-positioned HTML to semantic Flexbox HTML.

    Returns (semantic_html, page_count, converted).
    Reused by /convert-semantic-html and /convert-pdf-to-semantic-html.
    """
    # Skip if already semantic (few absolute positions)
    absolute_count = len(re.findall(r'position:\s*absolute', pixel_html))
    if absolute_count < 5:
        logger.info(
            f"[SemanticConverter] Already semantic ({absolute_count} absolute positions), skipping"
        )
        return pixel_html, 1, False

    logger.info(
        f"[SemanticConverter] Converting pixel HTML "
        f"({len(pixel_html) / 1024:.0f} KB, {absolute_count} absolute positions)"
    )
    t0 = _time.time()

    # Step 1: Replace base64 images with placeholders
    small_html, placeholders = _replace_base64_with_placeholders(pixel_html)
    logger.info(
        f"[SemanticConverter] {len(placeholders)} images replaced "
        f"({len(pixel_html) / 1024:.0f} KB → {len(small_html) / 1024:.0f} KB)"
    )

    # Step 2: Split into pages if too large
    if len(small_html) > PAGE_SIZE_THRESHOLD:
        pages = _split_pages(small_html)
        logger.info(f"[SemanticConverter] Large HTML, processing {len(pages)} pages separately")
    else:
        pages = [small_html]

    # Step 3: Convert via AI (per page or whole)
    converted_parts = []
    for i, page_html in enumerate(pages):
        logger.info(f"[SemanticConverter] Processing page {i + 1}/{len(pages)} ({len(page_html) / 1024:.0f} KB)")
        result = await _call_ai_for_conversion(page_html)
        converted_parts.append(result)

    semantic_html = "\n".join(converted_parts)

    # Step 4: Restore placeholders
    semantic_html = _restore_placeholders(semantic_html, placeholders)

    # Step 5: Validate
    if "<style" not in semantic_html or len(semantic_html) < 500:
        raise RuntimeError(
            f"Conversion output invalid ({len(semantic_html)} chars, "
            f"has <style>: {'<style' in semantic_html})"
        )

    remaining = len(IMG_PLACEHOLDER_PATTERN.findall(semantic_html))
    if remaining > 0:
        raise RuntimeError(f"{remaining} image placeholders not restored in output")

    duration = _time.time() - t0
    logger.info(
        f"[SemanticConverter] Done: {len(pixel_html) / 1024:.0f} KB → "
        f"{len(semantic_html) / 1024:.0f} KB ({duration:.1f}s, "
        f"{len(placeholders)} images restored, {len(pages)} pages)"
    )

    return semantic_html, len(pages), True


FINAL_IMAGE_PATTERN = re.compile(r'src="(data:image/(png|jpeg|gif|svg\+xml);base64,([^"]+))"')


def _extract_images_from_html(html: str) -> Tuple[str, List[Dict[str, str]]]:
    """Extract base64 images from HTML, replace with {{IMAGE_N}} placeholders.

    Returns (cleaned_html, images_array).
    """
    images: List[Dict[str, str]] = []
    index = 0

    def replacer(match: re.Match) -> str:
        nonlocal index
        mime_type = match.group(2)
        b64_data = match.group(3)
        ext = "png"
        if "jpeg" in mime_type:
            ext = "jpg"
        elif "gif" in mime_type:
            ext = "gif"
        elif "svg" in mime_type:
            ext = "svg"
        images.append({
            "index": index,
            "data": b64_data,
            "contentType": f"image/{mime_type}",
            "filename": f"image_{index}.{ext}",
        })
        placeholder = f'src="{{{{IMAGE_{index}}}}}"'
        index += 1
        return placeholder

    cleaned_html = FINAL_IMAGE_PATTERN.sub(replacer, html)
    return cleaned_html, images


@app.post("/convert-pdf-to-semantic-html")
async def convert_pdf_to_semantic_html_endpoint(file: UploadFile = File(...)):
    """Full pipeline: PDF → pixel HTML (ConvertAPI) → semantic HTML → images extracted.

    Accepts multipart/form-data with a PDF file.
    Returns semantic HTML with {{IMAGE_N}} placeholders and images as separate array.
    """
    import httpx

    # Validate file
    filename = file.filename or "upload.pdf"
    if not filename.lower().endswith(".pdf"):
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": f"Invalid file type: {filename}. Expected PDF."},
        )

    pdf_bytes = await file.read()
    if len(pdf_bytes) == 0:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Uploaded file is empty."},
        )

    MAX_FILE_SIZE = 100 * 1024 * 1024
    if len(pdf_bytes) > MAX_FILE_SIZE:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": f"File too large: {len(pdf_bytes) / 1024 / 1024:.1f} MB. Maximum: 100 MB."},
        )

    logger.info(f"[PDF→SemanticHTML] Starting pipeline for {filename} ({len(pdf_bytes) / 1024:.1f} KB)")
    t_start = _time.time()

    # --- Step 1: PDF → pixel-perfect HTML via ConvertAPI ---
    convert_api_secret = os.getenv("CONVERTAPI_SECRET")
    if not convert_api_secret:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "CONVERTAPI_SECRET not configured."},
        )

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                "https://v2.convertapi.com/convert/pdf/to/html",
                headers={"Authorization": f"Bearer {convert_api_secret}"},
                files={"File": (filename, pdf_bytes, "application/pdf")},
                data={"StoreFile": "true"},
            )

        if resp.status_code != 200:
            logger.error(f"[PDF→SemanticHTML] ConvertAPI error {resp.status_code}: {resp.text[:500]}")
            return JSONResponse(
                status_code=502,
                content={"status": "error", "error": f"ConvertAPI failed with status {resp.status_code}: {resp.text[:200]}"},
            )

        convert_result = resp.json()
        files = convert_result.get("Files", [])
        if not files:
            return JSONResponse(
                status_code=502,
                content={"status": "error", "error": "ConvertAPI returned no files."},
            )

        # Get HTML content — either download from URL or decode FileData
        file_entry = files[0]
        if file_entry.get("Url"):
            async with httpx.AsyncClient(timeout=120.0) as client:
                html_resp = await client.get(file_entry["Url"])
            pixel_html = html_resp.text
        elif file_entry.get("FileData"):
            pixel_html = base64.b64decode(file_entry["FileData"]).decode("utf-8")
        else:
            return JSONResponse(
                status_code=502,
                content={"status": "error", "error": "ConvertAPI response has neither Url nor FileData."},
            )

        page_count_hint = len(files)
        logger.info(f"[PDF→SemanticHTML] ConvertAPI done: {len(pixel_html) / 1024:.0f} KB HTML")

    except httpx.TimeoutException:
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": "ConvertAPI request timed out."},
        )
    except Exception as e:
        logger.error(f"[PDF→SemanticHTML] ConvertAPI call failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": f"ConvertAPI call failed: {str(e)}"},
        )

    # --- Step 2: pixel HTML → semantic Flexbox HTML ---
    try:
        semantic_html, page_count, converted = await _convert_pixel_to_semantic_html(pixel_html)
    except RuntimeError as e:
        logger.error(f"[PDF→SemanticHTML] Semantic conversion failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": f"Semantic HTML conversion failed: {str(e)}"},
        )

    # --- Step 3: Extract base64 images into separate array ---
    cleaned_html, images = _extract_images_from_html(semantic_html)

    duration = _time.time() - t_start
    logger.info(
        f"[PDF→SemanticHTML] Pipeline complete: {filename} → "
        f"{len(cleaned_html) / 1024:.0f} KB HTML, {len(images)} images, "
        f"{page_count} pages ({duration:.1f}s)"
    )

    return {
        "status": "success",
        "html": cleaned_html,
        "images": images,
        "pages": page_count,
        "converted": converted,
    }


@app.post("/convert-semantic-html", response_model=ConvertSemanticHtmlResponse)
async def convert_semantic_html_endpoint(req: ConvertSemanticHtmlRequest):
    """Convert pixel-positioned HTML (from ConvertAPI PDF→HTML) to semantic Flexbox HTML.

    Port of SemanticHtmlConverter.ts logic:
    1. Replace base64 images with {{IMG_N}} placeholders
    2. If HTML > 100KB, split by <div class="page"> and process per-page
    3. Send to AI (Haiku 4.5) for structural transformation
    4. Restore image placeholders
    5. Validate output
    """
    semantic_html, page_count, converted = await _convert_pixel_to_semantic_html(req.html)
    return ConvertSemanticHtmlResponse(
        status="success",
        html=semantic_html,
        pages=page_count,
        converted=converted,
    )


# ======================== Health & Status ========================

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "privacy-pdf-service",
    }


@app.get("/ready")
async def ready():
    """Readiness endpoint for graceful rebuild. Returns active request count."""
    return {
        "ready_for_shutdown": _active_requests == 0,
        "active_requests": _active_requests,
        "service": "privacy-pdf-service",
    }


@app.get("/status", response_model=StatusResponse)
async def status():
    middleware = get_middleware()
    return StatusResponse(
        enabled=middleware.enabled,
        available=middleware.is_available() if middleware.enabled else False,
        language=middleware.language,
        supported_entities=(
            middleware.anonymizer.SUPPORTED_ENTITIES if middleware.enabled else []
        ),
    )
