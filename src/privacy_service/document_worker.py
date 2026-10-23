"""One conversion per process; invoked only by the document subreaper."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import time

from .document_process import write_json


async def convert(directory: Path) -> dict:
    started = time.monotonic()
    from . import document_execution
    document_execution.managed = True
    document_execution.provider_directory = directory
    from .adapters import build_default_chain
    from .image_describer import describe_images, append_descriptions_to_markdown
    from .image_triage import plan_image_descriptions

    request = json.loads((directory / "request.json").read_text())
    fields = request["fields"]
    source = (directory / "source").read_bytes()
    if request.get("repair_actualtext") and (
        source.startswith(b"%PDF") or request["filename"].lower().endswith(".pdf")
        or "pdf" in (request["mime_type"] or "").lower()
    ):
        from src.privacy.pdf_actualtext_repair import repair_actualtext_glyphs
        repaired = repair_actualtext_glyphs(source)
        if repaired.hint:
            # Value-free diagnostic; the converter result carries the warning.
            repair_warning = True
        else:
            repair_warning = False
        source = repaired.pdf
    else:
        repair_warning = False
    result = build_default_chain().convert(source, request["filename"], request["mime_type"])
    if repair_warning:
        result.metadata["actualtext_repair_warning"] = True
    body = result.to_response()
    if str(fields.get("describe_images", "")).lower() in ("1", "true", "yes", "on") and result.images:
        selected, skipped, triage = await plan_image_descriptions(
            result.images, triage_prompt=fields.get("triage_prompt") or "",
        )
        descriptions = await describe_images(
            selected, context=request["filename"], describe_prompt=fields.get("describe_prompt") or "",
        )
        for name, label in skipped.items():
            descriptions[name] = f"_[nicht detailliert analysiert — {label}]_"
        body["markdown"] = append_descriptions_to_markdown(body["markdown"], descriptions)
        body["image_descriptions"] = descriptions
        if triage:
            body["described_count"] = len(selected)
            body["triage"] = triage
    body["conversion_time_seconds"] = round(time.monotonic() - started, 2)
    return body


def main(directory: Path) -> None:
    from .document_converter import DocumentTooLargeError, UnsupportedFormatError
    try:
        result = asyncio.run(convert(directory))
        envelope = {"status_code": 200, "result": result}
    except Exception as exc:
        status = 413 if isinstance(exc, DocumentTooLargeError) else 415 if isinstance(exc, UnsupportedFormatError) else 500
        # Adapter messages may contain document data. Only the type crosses the
        # durable error boundary, never a repr/traceback of the source content.
        envelope = {"status_code": status, "error": type(exc).__name__}
    write_json(directory / "result.json", envelope)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
