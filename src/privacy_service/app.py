"""
Privacy & PDF Service — Standalone FastAPI application.

Runs as Container 4 in the Bridge architecture.
Hosts all heavy NLP dependencies (Presidio, spaCy, Docling, PyTorch).
Internal-only: not exposed to nginx/public internet.
"""

import os
import json
import asyncio
import logging
import base64
import tempfile
import time as _time
from typing import Dict, List, Optional, Any

from pydantic import BaseModel, Field
from fastapi import FastAPI, Request
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


# ======================== App Setup ========================

app = FastAPI(title="Privacy & PDF Service", version="1.0.0")

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


# ======================== Health & Status ========================

@app.get("/health")
async def health():
    return {
        "status": "healthy",
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
