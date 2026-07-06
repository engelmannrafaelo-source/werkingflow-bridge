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
from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

from src.privacy_service.document_converter import (
    convert_pdf_bytes,
    UnsupportedFormatError,
)
from src.privacy_service.adapters import (
    AdapterChain,
    AdapterError,
    build_default_chain,
)
from src.privacy_service.image_describer import (
    describe_images,
    append_descriptions_to_markdown,
)


def _truthy(value: Any) -> bool:
    """Parse a multipart form flag (string) into a bool. Default-safe (None→False)."""
    return str(value).strip().lower() in ("1", "true", "yes", "on") if value is not None else False

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


@app.on_event("startup")
async def _warmup_privacy_models() -> None:
    """Pre-load Presidio/spaCy at boot so the first real smart-anonymize doesn't
    pay the ~50s cold-start.

    The Presidio analyzer is lazy-built on first use (PrivacyMiddleware.anonymizer).
    On a cold uvicorn worker that first call takes ~50s, which overran caller
    timeouts → the worker returned 500 → nginx mapped it to 502 (observed
    intermittently in the "Was die KI sieht" preview, 2026-06-23).

    Runs once per uvicorn worker. Non-blocking (thread executor) so the worker is
    immediately ready for the /health probe; the models warm within ~1 min, well
    inside the container's 240s healthcheck start_period. Fail-soft: on any error
    we simply fall back to the existing lazy-load on first request.
    """
    import asyncio

    def _load() -> None:
        try:
            mw = get_middleware()
            mw.anonymize_messages(
                [{"role": "user", "content": "Warmup: Max Mustermann, Wien."}],
                privacy_mode="full",
            )
            logger.info("[warmup] Presidio/spaCy pre-warmed at startup")
        except Exception as e:  # noqa: BLE001 — fail-soft; lazy-load stays the fallback
            logger.warning(f"[warmup] privacy preload failed (lazy-load on demand): {e}")

    asyncio.get_running_loop().run_in_executor(None, _load)


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
    """Smart anonymization: Presidio + local Flair NER, fully deterministic.

    Runs entirely inside this container — no cloud calls. (The former
    cloud-Haiku refinement stage was removed 2026-07-03.)
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
        want_descriptions = _truthy(form.get("describe_images"))
        describe_prompt = form.get("describe_prompt") or ""
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

        loop = asyncio.get_event_loop()
        markdown, metadata, images = await loop.run_in_executor(
            None, convert_pdf_bytes, pdf_bytes
        )
        page_count = metadata.get("pages")

        # Opt-in image descriptions (see /document/convert + image_describer).
        image_descriptions = None
        if want_descriptions and images:
            image_descriptions = await describe_images(
                images, context=filename, describe_prompt=describe_prompt
            )
            markdown = append_descriptions_to_markdown(markdown, image_descriptions)

        conversion_time = _time.time() - t_start

        logger.info(
            f"PDF conversion complete: {filename} -> "
            f"{len(markdown)} chars, {len(images)} images, "
            f"described={want_descriptions} {conversion_time:.1f}s"
        )

        return {
            "status": "success",
            "markdown": markdown,
            "images": images if images else None,
            "image_count": len(images),
            "image_descriptions": image_descriptions,
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


# ======================== Universal Document Conversion ========================
#
# Routes any supported office/email/spreadsheet/HTML/image document to Markdown.
# Reuses the Docling PDF pipeline for PDF and Office (via LibreOffice → PDF).
# Apps that historically used /convert-pdf can either keep that endpoint or
# migrate to /document/convert which auto-routes by MIME/extension.

DOCUMENT_MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB — same cap as /convert-pdf


# Adapter chain is built lazily so unit-test imports of ``app`` do not
# eagerly construct heavy adapters and so test code can monkey-patch
# ``_get_chain`` to inject a stub.
_DOCUMENT_CHAIN: Optional[AdapterChain] = None


def _get_chain() -> AdapterChain:
    global _DOCUMENT_CHAIN
    if _DOCUMENT_CHAIN is None:
        ai_disabled = os.getenv("DISABLE_AI_FALLBACK", "").lower() in ("1", "true", "yes")
        _DOCUMENT_CHAIN = build_default_chain(enable_ai_fallback=not ai_disabled)
    return _DOCUMENT_CHAIN


async def _read_uploaded_file(request: Request) -> Tuple[bytes, str, Optional[str], Dict[str, Any]]:
    """Extract ``file`` (and optional form fields) from a multipart request.

    Returns ``(content_bytes, filename, mime_type_hint, extra_form_fields)``.
    Raises ``HTTPException`` on missing/empty/oversized files so handlers can
    surface a 4xx response cleanly.
    """
    form = await request.form()
    file = form.get("file")
    if not file or not hasattr(file, "read"):
        raise HTTPException(
            status_code=400,
            detail="No file uploaded. Send the document as multipart/form-data with field name 'file'.",
        )
    filename = getattr(file, "filename", "upload.bin") or "upload.bin"
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(content) > DOCUMENT_MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=(
                f"File too large: {len(content) / 1024 / 1024:.1f} MB. "
                f"Maximum: {DOCUMENT_MAX_FILE_SIZE / 1024 / 1024:.0f} MB."
            ),
        )

    # Pull a few well-known optional fields out so handlers can read them
    # without re-parsing the multipart form.
    mime_hint = form.get("mime_type_hint") or getattr(file, "content_type", None)
    extras: Dict[str, Any] = {
        "language": form.get("language"),
        "privacy_mode": form.get("privacy_mode"),
        "context_hint": form.get("context_hint"),
        "describe_images": form.get("describe_images"),
        "describe_prompt": form.get("describe_prompt"),
    }
    return content, filename, mime_hint, extras


@app.post("/document/convert")
async def document_convert_endpoint(request: Request):
    """Convert any supported document type to Markdown.

    Multipart form fields:
    - ``file`` (required): the document to convert.
    - ``mime_type_hint`` (optional): explicit MIME type override.

    Response: ``{success, format, markdown, metadata, images?, image_count?}``.
    Returns 415 on unknown formats and 500 on adapter failures (fail-loud).
    """
    content, filename, mime_hint, extras = await _read_uploaded_file(request)
    want_descriptions = _truthy(extras.get("describe_images"))
    describe_prompt = extras.get("describe_prompt") or ""

    t_start = _time.time()
    loop = asyncio.get_event_loop()
    chain = _get_chain()

    try:
        result = await loop.run_in_executor(
            None, chain.convert, content, filename, mime_hint
        )
    except UnsupportedFormatError as e:
        logger.warning(f"document/convert unsupported: {e}")
        raise HTTPException(status_code=415, detail=str(e))
    except AdapterError as e:
        logger.error(f"document/convert adapter error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    except RuntimeError as e:
        logger.error(f"document/convert chain error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

    body = result.to_response()

    # Opt-in: run every extracted image through Vision and append the descriptions
    # to the Markdown, so the converted document is self-contained without the
    # raw images. Off by default (callers opt in via describe_images=true) so
    # existing consumers stay byte-for-byte unchanged.
    if want_descriptions and result.images:
        descriptions = await describe_images(
            result.images, context=filename, describe_prompt=describe_prompt
        )
        body["markdown"] = append_descriptions_to_markdown(body["markdown"], descriptions)
        body["image_descriptions"] = descriptions

    duration = _time.time() - t_start
    logger.info(
        f"document/convert done: {filename} -> format={result.fmt} "
        f"adapter={result.metadata.get('adapter')} "
        f"chars={len(body['markdown'])} images={len(result.images or {})} "
        f"described={want_descriptions} ({duration:.2f}s)"
    )

    body["conversion_time_seconds"] = round(duration, 2)
    return body


@app.post("/document/convert-and-anonymize")
async def document_convert_and_anonymize_endpoint(request: Request):
    """Atomically convert + smart-anonymize a document.

    Multipart form fields:
    - ``file`` (required)
    - ``mime_type_hint`` (optional)
    - ``language`` (optional, default ``de``)
    - ``privacy_mode`` (optional, ``smart`` (default) or ``basic``)
    - ``context_hint`` (optional, passed to smart-anonymize for better decisions)

    Response: ``{success, format, anonymized_markdown, mapping, metadata, ...}``.
    The mapping is *not* persisted by the Bridge — apps store it themselves so
    they can re-deanonymize later without leaking PII back into Bridge state.
    """
    content, filename, mime_hint, extras = await _read_uploaded_file(request)

    language = (extras.get("language") or "de").lower()
    privacy_mode = (extras.get("privacy_mode") or "smart").lower()
    context_hint = extras.get("context_hint")
    if privacy_mode not in ("smart", "basic"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid privacy_mode: {privacy_mode!r}. Expected 'smart' or 'basic'.",
        )

    t_start = _time.time()
    loop = asyncio.get_event_loop()
    chain = _get_chain()

    # Step 1 — convert via adapter chain (deterministic adapters first, AI fallback last)
    try:
        conversion = await loop.run_in_executor(
            None, chain.convert, content, filename, mime_hint
        )
    except UnsupportedFormatError as e:
        logger.warning(f"convert-and-anonymize unsupported: {e}")
        raise HTTPException(status_code=415, detail=str(e))
    except AdapterError as e:
        logger.error(f"convert-and-anonymize adapter error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
    except RuntimeError as e:
        logger.error(f"convert-and-anonymize convert step failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

    convert_time = _time.time() - t_start

    # Step 2 — anonymize
    if privacy_mode == "smart":
        from src.privacy.smart_anonymizer import smart_anonymize

        try:
            anon = await smart_anonymize(
                text=conversion.markdown,
                language=language,
                context_hint=context_hint,
            )
        except Exception as e:
            logger.error(f"smart_anonymize failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Anonymization failed: {e}")
        anonymized_text = anon.get("smart_anonymized_text") or conversion.markdown
        mapping = anon.get("mapping") or {}
        detected_entities = anon.get("detected_entities") or []
        restored_entities = anon.get("restored_entities") or []
    else:
        # Basic mode: Presidio-only, no AI refinement.
        middleware = get_middleware()
        anon_messages, mapping = middleware.anonymize_messages(
            [{"role": "user", "content": conversion.markdown}],
            privacy_mode="full",
        )
        anonymized_text = (
            anon_messages[0].get("content", conversion.markdown)
            if anon_messages
            else conversion.markdown
        )
        detected_entities = []
        restored_entities = []

    duration = _time.time() - t_start
    logger.info(
        f"convert-and-anonymize done: {filename} format={conversion.fmt} "
        f"convert={convert_time:.2f}s total={duration:.2f}s "
        f"entities_kept={len(mapping)}"
    )

    return {
        "success": True,
        "format": conversion.fmt,
        "anonymized_markdown": anonymized_text,
        "mapping": mapping,
        "detected_entities": detected_entities,
        "restored_entities": restored_entities,
        "metadata": conversion.metadata,
        "privacy_mode": privacy_mode,
        "language": language,
        "convert_time_seconds": round(convert_time, 2),
        "total_time_seconds": round(duration, 2),
    }


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

    # Step 3: Convert via AI (parallel per page)
    import asyncio

    async def _convert_page(i: int, page_html: str) -> str:
        logger.info(f"[SemanticConverter] Processing page {i + 1}/{len(pages)} ({len(page_html) / 1024:.0f} KB)")
        return await _call_ai_for_conversion(page_html)

    tasks = [_convert_page(i, ph) for i, ph in enumerate(pages)]
    converted_parts = list(await asyncio.gather(*tasks))

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


@app.post("/convert-html-to-docx")
async def convert_html_to_docx_endpoint(request: Request):
    """Convert HTML → DOCX via ConvertAPI Microsoft Word 15 engine.

    Accepts JSON body: { "html": "<html>…</html>", "filename": "input.html" (optional) }.
    Returns DOCX as base64 string + raw byte length + ConvertAPI cost (seconds).

    Centralizing the ConvertAPI call here means apps no longer need their own
    CONVERTAPI_SECRET — only this service does.
    """
    import httpx

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Invalid JSON body."},
        )

    html = body.get("html")
    if not isinstance(html, str) or not html.strip():
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Missing or empty 'html' field."},
        )

    filename = body.get("filename") or "input.html"
    if not isinstance(filename, str):
        filename = "input.html"

    MAX_HTML_BYTES = 50 * 1024 * 1024  # 50 MB
    html_bytes = html.encode("utf-8")
    if len(html_bytes) > MAX_HTML_BYTES:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": f"HTML too large: {len(html_bytes) / 1024 / 1024:.1f} MB. Maximum: 50 MB."},
        )

    convert_api_secret = os.getenv("CONVERTAPI_SECRET")
    if not convert_api_secret:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "CONVERTAPI_SECRET not configured on privacy-pdf-service."},
        )

    logger.info(f"[HTML→DOCX] Starting conversion ({len(html_bytes) / 1024:.1f} KB)")
    t_start = _time.time()

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                "https://v2.convertapi.com/convert/html/to/docx",
                headers={"Authorization": f"Bearer {convert_api_secret}"},
                files={"File": (filename, html_bytes, "text/html")},
                data={"StoreFile": "true"},
            )

        if resp.status_code != 200:
            logger.error(f"[HTML→DOCX] ConvertAPI error {resp.status_code}: {resp.text[:500]}")
            return JSONResponse(
                status_code=502,
                content={"status": "error", "error": f"ConvertAPI failed with status {resp.status_code}: {resp.text[:200]}"},
            )

        result = resp.json()
        cost = float(result.get("ConversionCost", 0))
        files_arr = result.get("Files", [])
        if not files_arr:
            return JSONResponse(
                status_code=502,
                content={"status": "error", "error": "ConvertAPI returned no DOCX files."},
            )

        file_entry = files_arr[0]
        if file_entry.get("FileData"):
            docx_bytes = base64.b64decode(file_entry["FileData"])
        elif file_entry.get("Url"):
            async with httpx.AsyncClient(timeout=120.0) as client:
                docx_resp = await client.get(file_entry["Url"])
            if docx_resp.status_code != 200:
                return JSONResponse(
                    status_code=502,
                    content={"status": "error", "error": f"DOCX download failed: {docx_resp.status_code}"},
                )
            docx_bytes = docx_resp.content
        else:
            return JSONResponse(
                status_code=502,
                content={"status": "error", "error": "ConvertAPI response has neither Url nor FileData."},
            )

    except httpx.TimeoutException:
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": "ConvertAPI request timed out."},
        )
    except Exception as e:
        logger.error(f"[HTML→DOCX] ConvertAPI call failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": f"ConvertAPI call failed: {str(e)}"},
        )

    duration = _time.time() - t_start
    logger.info(f"[HTML→DOCX] Done: {len(docx_bytes)} bytes DOCX in {duration:.1f}s, cost={cost}s")

    return JSONResponse(content={
        "status": "success",
        "docx_base64": base64.b64encode(docx_bytes).decode("ascii"),
        "size_bytes": len(docx_bytes),
        "cost": cost,
    })


@app.post("/convert-docx-to-html")
async def convert_docx_to_html_endpoint(request: Request):
    """Convert DOCX → editable HTML via ConvertAPI (Word import for the workspace).

    Reverse of /convert-html-to-docx, same ConvertAPI engine, so an uploaded Word
    document round-trips back into the TinyMCE editor with styles/tables/images
    preserved. Images + CSS are embedded so the result is a single self-contained
    HTML string (no external asset files to host).

    Accepts JSON body: { "docx_base64": "…", "filename": "input.docx" (optional) }.
    Returns the HTML string + ConvertAPI cost (seconds).
    """
    import httpx

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Invalid JSON body."},
        )

    docx_b64 = body.get("docx_base64")
    if not isinstance(docx_b64, str) or not docx_b64.strip():
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Missing or empty 'docx_base64' field."},
        )

    try:
        docx_bytes = base64.b64decode(docx_b64)
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "'docx_base64' is not valid base64."},
        )

    filename = body.get("filename") or "input.docx"
    if not isinstance(filename, str):
        filename = "input.docx"

    MAX_DOCX_BYTES = 50 * 1024 * 1024  # 50 MB
    if len(docx_bytes) > MAX_DOCX_BYTES:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": f"DOCX too large: {len(docx_bytes) / 1024 / 1024:.1f} MB. Maximum: 50 MB."},
        )

    convert_api_secret = os.getenv("CONVERTAPI_SECRET")
    if not convert_api_secret:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "CONVERTAPI_SECRET not configured on privacy-pdf-service."},
        )

    logger.info(f"[DOCX→HTML] Starting conversion ({len(docx_bytes) / 1024:.1f} KB)")
    t_start = _time.time()

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                "https://v2.convertapi.com/convert/docx/to/html",
                headers={"Authorization": f"Bearer {convert_api_secret}"},
                files={"File": (filename, docx_bytes, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
                # EmbedImages/EmbedCss → one self-contained HTML (data-URI images, inline
                # styles) so TinyMCE gets editable rich text without external asset files.
                data={"EmbedImages": "true", "EmbedCss": "true", "StoreFile": "true"},
            )

        if resp.status_code != 200:
            logger.error(f"[DOCX→HTML] ConvertAPI error {resp.status_code}: {resp.text[:500]}")
            return JSONResponse(
                status_code=502,
                content={"status": "error", "error": f"ConvertAPI failed with status {resp.status_code}: {resp.text[:200]}"},
            )

        result = resp.json()
        cost = float(result.get("ConversionCost", 0))
        files_arr = result.get("Files", [])
        if not files_arr:
            return JSONResponse(
                status_code=502,
                content={"status": "error", "error": "ConvertAPI returned no HTML files."},
            )

        file_entry = files_arr[0]
        if file_entry.get("FileData"):
            html_bytes = base64.b64decode(file_entry["FileData"])
        elif file_entry.get("Url"):
            async with httpx.AsyncClient(timeout=120.0) as client:
                html_resp = await client.get(file_entry["Url"])
            if html_resp.status_code != 200:
                return JSONResponse(
                    status_code=502,
                    content={"status": "error", "error": f"HTML download failed: {html_resp.status_code}"},
                )
            html_bytes = html_resp.content
        else:
            return JSONResponse(
                status_code=502,
                content={"status": "error", "error": "ConvertAPI response has neither Url nor FileData."},
            )

    except httpx.TimeoutException:
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": "ConvertAPI request timed out."},
        )
    except Exception as e:
        logger.error(f"[DOCX→HTML] ConvertAPI call failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": f"ConvertAPI call failed: {str(e)}"},
        )

    duration = _time.time() - t_start
    html_str = html_bytes.decode("utf-8", errors="replace")
    logger.info(f"[DOCX→HTML] Done: {len(html_str)} chars HTML in {duration:.1f}s, cost={cost}s")

    return JSONResponse(content={
        "status": "success",
        "html": html_str,
        "cost": cost,
    })


@app.post("/convert-html-to-pdf")
async def convert_html_to_pdf_endpoint(request: Request):
    """Render HTML → PDF via headless Chromium (Playwright).

    Accepts JSON body: { "html": "<html>…</html>", "filename": "input.html" (optional) }.
    Returns PDF as base64 + byte length.

    Uses the SAME Chromium print path as the engelmann local renderer
    (prefer_css_page_size + print_background + margin 0): the document's own
    @page rules fully control page size/margins, so a self-contained Muster
    with a full-bleed cover (@page :first { margin: 0 }) renders 1:1. This is
    the shared server-side PDF renderer for all report apps — no per-app
    Chromium bundling needed (Vercel-Lambdas können kein Chromium tragen).
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Invalid JSON body."},
        )

    html = body.get("html")
    if not isinstance(html, str) or not html.strip():
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Missing or empty 'html' field."},
        )

    MAX_HTML_BYTES = 50 * 1024 * 1024  # 50 MB
    html_bytes = html.encode("utf-8")
    if len(html_bytes) > MAX_HTML_BYTES:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": f"HTML too large: {len(html_bytes) / 1024 / 1024:.1f} MB. Maximum: 50 MB."},
        )

    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        logger.error(f"[HTML→PDF] Playwright import failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "Playwright/Chromium not installed on privacy-pdf-service."},
        )

    logger.info(f"[HTML→PDF] Starting Chromium render ({len(html_bytes) / 1024:.1f} KB)")
    t_start = _time.time()

    try:
        async with async_playwright() as pw:
            # --no-sandbox + --disable-gpu: Container läuft als non-root ohne GPU
            # (gleiche Args wie der engelmann-puppeteer-Renderer).
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            try:
                page = await browser.new_page()
                # networkidle: auf @font-face/@import-Fonts + Bilder warten, sonst
                # druckt der erste Versuch mit Fallback-Font.
                await page.set_content(html, wait_until="networkidle")
                pdf_bytes = await page.pdf(
                    format="A4",
                    print_background=True,
                    prefer_css_page_size=True,
                    margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
                )
            finally:
                await browser.close()
    except Exception as e:
        logger.error(f"[HTML→PDF] Chromium render failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": f"PDF render failed: {str(e)}"},
        )

    duration = _time.time() - t_start
    logger.info(f"[HTML→PDF] Done: {len(pdf_bytes)} bytes PDF in {duration:.1f}s")

    return JSONResponse(content={
        "status": "success",
        "pdf_base64": base64.b64encode(pdf_bytes).decode("ascii"),
        "size_bytes": len(pdf_bytes),
        "cost": 0,
    })


@app.post("/convert-html-to-screenshot")
async def convert_html_to_screenshot_endpoint(request: Request):
    """Render HTML → PNG-Screenshot via headless Chromium (Playwright).

    Accepts JSON body: { "html": "…", "width": 1440 (optional), "full_page": true (optional) }.
    Returns PNG as base64 + byte length.

    Gleiche Render-Engine wie /convert-html-to-pdf, aber Screen-Viewport statt
    A4-Druckseite: zeigt den Report so, wie der User ihn am Bildschirm sieht
    (Screen-CSS, viewport-Breite) — nicht die paginierte Druckansicht. Damit
    kann ein Editor-Agent das gerenderte Ergebnis selbst betrachten und gezielt
    nachbessern. Shared server-side Renderer — kein per-App Chromium nötig.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Invalid JSON body."},
        )

    html = body.get("html")
    if not isinstance(html, str) or not html.strip():
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Missing or empty 'html' field."},
        )

    width = body.get("width", 1440)
    if not isinstance(width, int) or isinstance(width, bool) or width < 320 or width > 3840:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "'width' must be an integer in [320, 3840]."},
        )

    full_page = body.get("full_page", True)
    if not isinstance(full_page, bool):
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "'full_page' must be a boolean."},
        )

    MAX_HTML_BYTES = 50 * 1024 * 1024  # 50 MB
    html_bytes = html.encode("utf-8")
    if len(html_bytes) > MAX_HTML_BYTES:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": f"HTML too large: {len(html_bytes) / 1024 / 1024:.1f} MB. Maximum: 50 MB."},
        )

    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        logger.error(f"[HTML→PNG] Playwright import failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "Playwright/Chromium not installed on privacy-pdf-service."},
        )

    logger.info(f"[HTML→PNG] Starting Chromium render ({len(html_bytes) / 1024:.1f} KB, width={width}, full_page={full_page})")
    t_start = _time.time()

    try:
        async with async_playwright() as pw:
            # Gleiche Container-Args wie der PDF-Renderer (non-root, keine GPU).
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            try:
                # Screen-Viewport: der Report rendert mit seiner Bildschirm-Breite.
                # height=900 ist nur initial — full_page erfasst die gesamte Höhe.
                page = await browser.new_page(viewport={"width": width, "height": 900})
                # networkidle: auf @font-face/@import-Fonts + Bilder warten, sonst
                # screenshot mit Fallback-Font / fehlenden Bildern.
                await page.set_content(html, wait_until="networkidle")
                png_bytes = await page.screenshot(full_page=full_page, type="png")
            finally:
                await browser.close()
    except Exception as e:
        logger.error(f"[HTML→PNG] Chromium render failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": f"Screenshot render failed: {str(e)}"},
        )

    duration = _time.time() - t_start
    logger.info(f"[HTML→PNG] Done: {len(png_bytes)} bytes PNG in {duration:.1f}s")

    return JSONResponse(content={
        "status": "success",
        "png_base64": base64.b64encode(png_bytes).decode("ascii"),
        "size_bytes": len(png_bytes),
        "cost": 0,
    })


@app.post("/convert-pdf-to-html-direct")
async def convert_pdf_to_html_direct_endpoint(request: Request):
    """Convert PDF → HTML (pixel-perfect, direct) via ConvertAPI.

    Accepts JSON body: { "pdf_base64": "<base64-encoded PDF>", "filename": "input.pdf" (optional) }.
    Returns the raw HTML (with absolute positioning + per-character spans + inline base64 images)
    plus ConvertAPI cost (seconds).

    This is the raw pixel-positioned conversion. For semantic flexbox HTML use
    /convert-pdf-to-semantic-html (chains direct PDF→HTML + AI restructure).
    """
    import httpx

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Invalid JSON body."},
        )

    pdf_b64 = body.get("pdf_base64")
    if not isinstance(pdf_b64, str) or not pdf_b64.strip():
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Missing or empty 'pdf_base64' field."},
        )

    filename = body.get("filename") or "input.pdf"
    if not isinstance(filename, str):
        filename = "input.pdf"

    try:
        pdf_bytes = base64.b64decode(pdf_b64, validate=True)
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Invalid base64 in 'pdf_base64'."},
        )

    MAX_PDF_BYTES = 50 * 1024 * 1024  # 50 MB
    if len(pdf_bytes) > MAX_PDF_BYTES:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": f"PDF too large: {len(pdf_bytes) / 1024 / 1024:.1f} MB. Maximum: 50 MB."},
        )

    convert_api_secret = os.getenv("CONVERTAPI_SECRET")
    if not convert_api_secret:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "CONVERTAPI_SECRET not configured on privacy-pdf-service."},
        )

    logger.info(f"[PDF→HTML] Starting conversion ({len(pdf_bytes) / 1024:.1f} KB)")
    t_start = _time.time()

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            resp = await client.post(
                "https://v2.convertapi.com/convert/pdf/to/html",
                headers={"Authorization": f"Bearer {convert_api_secret}"},
                files={"File": (filename, pdf_bytes, "application/pdf")},
                data={"StoreFile": "true"},
            )

        if resp.status_code != 200:
            logger.error(f"[PDF→HTML] ConvertAPI error {resp.status_code}: {resp.text[:500]}")
            return JSONResponse(
                status_code=502,
                content={"status": "error", "error": f"ConvertAPI failed with status {resp.status_code}: {resp.text[:200]}"},
            )

        result = resp.json()
        cost = float(result.get("ConversionCost", 0))
        files_arr = result.get("Files", [])
        if not files_arr:
            return JSONResponse(
                status_code=502,
                content={"status": "error", "error": "ConvertAPI returned no HTML files."},
            )

        html_entry = next(
            (f for f in files_arr if f.get("FileName", "").lower().endswith((".html", ".htm"))),
            files_arr[0],
        )

        if html_entry.get("FileData"):
            html_bytes = base64.b64decode(html_entry["FileData"])
        elif html_entry.get("Url"):
            async with httpx.AsyncClient(timeout=120.0) as client:
                html_resp = await client.get(html_entry["Url"])
            if html_resp.status_code != 200:
                return JSONResponse(
                    status_code=502,
                    content={"status": "error", "error": f"HTML download failed: {html_resp.status_code}"},
                )
            html_bytes = html_resp.content
        else:
            return JSONResponse(
                status_code=502,
                content={"status": "error", "error": "ConvertAPI response has neither Url nor FileData."},
            )

        html_str = html_bytes.decode("utf-8", errors="replace")

    except httpx.TimeoutException:
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": "ConvertAPI request timed out."},
        )
    except Exception as e:
        logger.error(f"[PDF→HTML] ConvertAPI call failed: {e}", exc_info=True)
        return JSONResponse(
            status_code=502,
            content={"status": "error", "error": f"ConvertAPI call failed: {str(e)}"},
        )

    duration = _time.time() - t_start
    logger.info(f"[PDF→HTML] Done: {len(html_str)} chars HTML in {duration:.1f}s, cost={cost}s")

    return JSONResponse(content={
        "status": "success",
        "html": html_str,
        "cost": cost,
    })


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
