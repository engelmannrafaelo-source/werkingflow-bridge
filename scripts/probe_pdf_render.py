"""Offline PDF render probe: no model/network calls, no customer output.

Run with --pdf PATH (repeatable) for numeric per-page measurements. Synthetic
checks and killed mutations run without a PDF argument. Requires Pillow and
pypdfium2. Docling/OCR is substituted; the real render/encoding code is used.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import base64
import contextlib
import io
import json
import random
import sys
import os
import socket
import subprocess


def no_network(*args, **kwargs):
    raise RuntimeError("Offline probe: network access forbidden")


socket.socket.connect = no_network
socket.create_connection = no_network
socket.getaddrinfo = no_network
import time
import types
from pathlib import Path
from unittest.mock import patch

import pypdfium2 as pdfium
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.privacy_service import document_converter as dc
from src.privacy_service.image_encoding import image_data_url
from src.privacy_service import image_encoding


def png(image):
    out = io.BytesIO()
    image.save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode("ascii")


def blank_pdf(*sizes):
    doc = pdfium.PdfDocument.new()
    try:
        for width, height in sizes:
            page = doc.new_page(width, height)
            page.close()
        out = io.BytesIO()
        doc.save(out)
        return out.getvalue()
    finally:
        doc.close()


def refuse(call, *fragments):
    try:
        call()
    except dc.DocumentTooLargeError as exc:
        assert all(str(part) in str(exc) for part in fragments), str(exc)
    else:
        raise AssertionError("Expected a loud DocumentTooLargeError")


@contextlib.contextmanager
def conversion_config():
    # Never inspect process configuration or load a model during this probe.
    with contextlib.ExitStack() as stack:
        for name, value in {
            "_docling_max_pages": 600,
            "_docling_batch_pages": 25,
            "_render_max_bytes": 384 * 2**20,
            "_docling_images_scale": 1.0,
        }.items():
            stack.enter_context(patch.object(dc, name, return_value=value))
        yield


SMALL = (595, 842)
LARGE = (4564, 2551)


def check_pages():
    raw = blank_pdf(SMALL, LARGE, (1684, 1191), (2384, 1684))
    rendered = dc._render_pdf_pages(raw, [0, 1, 2, 3])
    assert set(rendered) == {"page-001.png", "page-002.jpg", "page-003.png", "page-004.jpg"}
    for name, b64 in rendered.items():
        assert len(b64) < dc.PDF_IMAGE_MAX_B64_BYTES
        with Image.open(io.BytesIO(base64.b64decode(b64))) as image:
            assert image.format == ("JPEG" if name.endswith("jpg") else "PNG")
            assert max(image.size) <= 1600


def check_boundary():
    # One character below the base64 length refuses the JPEG even when its
    # BINARY size is under that limit. Equality also refuses (strict bound).
    with Image.new("RGB", (64, 64), "white") as image, patch.object(dc, "PDF_IMAGE_JPEG_QUALITIES", (85,)):
        b64 = dc._encode_vision_jpeg(image, "Seite 7")
        for bound in (len(b64) - 1, len(b64)):
            with patch.object(dc, "PDF_IMAGE_MAX_B64_BYTES", bound):
                refuse(lambda: dc._encode_vision_jpeg(image, "Seite 7"), "Seite 7", bound, len(b64))


def check_pathological():
    with Image.frombytes("RGB", (1600, 1600), random.Random(42).randbytes(1600 * 1600 * 3)) as image:
        encoded = dc._encode_vision_jpeg(image, "Seite 9")
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as reduced:
            assert 900 <= max(reduced.size) < 1600
            details = dict(before_px=[1600, 1600], after_px=list(reduced.size), after_b64_bytes=len(encoded))
        with patch.object(dc, "PDF_IMAGE_MAX_B64_BYTES", 1000):
            refuse(lambda: dc._encode_vision_jpeg(image, "Seite 9"), "Seite 9", "55", 1000, "900 px")
        # Real PDF rendering must propagate the refusal; no successful subset.
        out = io.BytesIO()
        image.save(out, format="PDF", resolution=24, quality=100)
        with patch.object(dc, "PDF_IMAGE_MAX_B64_BYTES", 1000):
            refuse(lambda: dc._render_pdf_pages(out.getvalue(), [0]), "Seite 1", "55", "900 px")
        details["refusal_limit"] = 1000
        details["refusal_edge"] = 900
        return details


def check_quality_steps():
    with Image.frombytes("RGB", (400, 400), random.Random(7).randbytes(400 * 400 * 3)) as image:
        candidates = {}
        for q in dc.PDF_IMAGE_JPEG_QUALITIES:
            with patch.object(dc, "PDF_IMAGE_JPEG_QUALITIES", (q,)):
                candidates[q] = dc._encode_vision_jpeg(image, "synthetic")
        for q in (75, 65, 55):
            with patch.object(dc, "PDF_IMAGE_MAX_B64_BYTES", len(candidates[q]) + 1):
                assert dc._encode_vision_jpeg(image, "synthetic") == candidates[q]


def check_budget():
    raw = blank_pdf(LARGE, LARGE)
    size = sum(map(len, dc._render_pdf_pages(raw, [0, 1]).values()))
    assert len(dc._render_pdf_pages(raw, [0, 1], max_total_bytes=size + 100, already_bytes=100)) == 2
    refuse(lambda: dc._render_pdf_pages(raw, [0, 1], max_total_bytes=size + 99, already_bytes=100), "Seite 2", "PDF_RENDER_MAX_MB")


def check_figures():
    # High-entropy PNG above 700 KiB, JPEG fits without changing dimensions.
    with Image.frombytes("RGB", (600, 600), random.Random(11).randbytes(600 * 600 * 3)) as image:
        big = png(image)
    with Image.new("RGB", (24, 24), "white") as image:
        small = png(image)
    old, new = "image_000000_probe.png", "image_000000_probe.jpg"
    pages = [{"page_no": 1, "markdown": f"![Image](paged_artifacts/{old})"}]
    meta = [{"name": old, "page": 1, "caption": "synthetic"}]
    result, md = dc._prepare_pdf_figures(
        {old: big, "small.png": small}, f"![Image](output_artifacts/{old})", pages, meta
    )
    assert len(big) >= dc.PDF_IMAGE_MAX_B64_BYTES > len(result[new])
    assert result["small.png"] == small
    assert old not in md and new in md
    assert old not in pages[0]["markdown"] and new in pages[0]["markdown"]
    assert meta[0]["name"] == new
    assert image_data_url(result[new]).startswith("data:image/jpeg;")
    refuse(lambda: dc._prepare_pdf_figures({old: big, new: small}, "", None, None), "bereits belegt")
    with conversion_config(), patch.object(dc, "_pdf_page_geometry", return_value=[(100, 842)]), patch.object(
        dc, "_docling_convert_pdf", return_value=(
            f"![Image]({old})", 1, {old: big},
            [{"page_no": 1, "markdown": f"![Image]({old})"}],
            [{"name": old, "page": 1, "caption": "synthetic"}], []
        )
    ):
        md, metadata, images = dc.convert_pdf_bytes(b"synthetic")
        assert set(images) == {new} and new in md and old not in md
        assert new in metadata["page_markdowns"][0]["markdown"]
        assert metadata["figures"][0]["name"] == new


def check_conversion():
    raw = blank_pdf(SMALL, LARGE)
    with conversion_config(), patch.object(dc, "_docling_convert_pdf", return_value=(
        "synthetic", 2, {}, [{"page_no": 1, "markdown": "synthetic"}], None, [1, 2]
    )):
        md, meta, images = dc.convert_pdf_bytes(raw)
        assert "page-001.png, page-002.jpg" in md
        assert meta["docling_failed_pages"] == [1, 2]
        assert meta["rendered_page_numbers"] == [1, 2]
        assert meta["rendered_page_count"] == 2 == len(images)
    with conversion_config(), patch.object(dc, "_docling_convert_pdf", side_effect=RuntimeError("synthetic")):
        md, meta, images = dc.convert_pdf_bytes(raw)
        assert all(f"]({name})" in md for name in images)
        assert meta["docling_parsed"] is False
    # A figure size refusal cannot become a successful full-page fallback.
    with conversion_config(), patch.object(dc, "_docling_convert_pdf", side_effect=dc.DocumentTooLargeError("synthetic budget")):
        refuse(lambda: dc.convert_pdf_bytes(raw), "synthetic budget")
    # Text-rich A2/A4 pages must not acquire new whole-page rendering.
    with conversion_config(), patch.object(dc, "_pdf_page_geometry", return_value=[(100, 1684), (100, 842)]), patch.object(
        dc, "_docling_convert_pdf", return_value=("synthetic", 2, {}, None, None, [])
    ):
        _, meta, images = dc.convert_pdf_bytes(raw)
        assert not images and meta["rendered_page_count"] == 0
    # Re-encoding may increase a JPEG figure's size; check the final total
    # even when no page is rendered (the renderer returns early for []).
    with conversion_config(), patch.object(dc, "_pdf_page_geometry", return_value=[(100, 842)]), patch.object(
        dc, "_docling_convert_pdf", return_value=("synthetic", 1, {"figure": "x"}, None, None, [])
    ), patch.object(dc, "_prepare_pdf_figures", return_value=({"figure": "x" * 101}, "synthetic")), patch.object(
        dc, "_render_max_bytes", return_value=100
    ):
        refuse(lambda: dc.convert_pdf_bytes(raw), "PDF_RENDER_MAX_MB")


def actual_function(path, name, namespace):
    # Exercise the actual async payload builders without importing provider
    # clients, credentials, model registry or application configuration.
    tree = ast.parse((ROOT / path).read_text())
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)  # noqa: S102 — local source, never document input
    return namespace[name]


def check_mime():
    sent = []

    class Provider:
        async def analyze(self, messages, **kwargs):
            sent.append(messages)
            return types.SimpleNamespace(content='{"1":{"relevant":true}}', stop_reason="end_turn")

    fn = actual_function("src/privacy_service/image_describer.py", "_describe_one", {
        "image_data_url": image_data_url, "_SYSTEM_PROMPT": "synthetic", "_USER_PROMPT": "synthetic",
        "record_vision_funding": lambda *args: None,
    })
    for b64, mime in [("/9j/AAAA", "jpeg"), ("iVBORw0KGgo=", "png")]:
        asyncio.run(fn(Provider(), b64, "synthetic", ""))
        assert sent[-1][0]["content"][1]["image_url"]["url"].startswith(f"data:image/{mime};")
    fn = actual_function("src/privacy_service/image_triage.py", "_triage_one_batch", {
        "image_data_url": image_data_url, "_downscale_b64_png": lambda b64, **kw: b64,
        "_TRIAGE_THUMB_EDGE": 384, "_batch_instruction": lambda n: "synthetic",
        "_parse_verdict": json.loads,
    })
    asyncio.run(fn(Provider(), [("synthetic.jpg", "/9j/AAAA")], "synthetic", "synthetic"))
    assert sent[-1][0]["content"][2]["image_url"]["url"].startswith("data:image/jpeg;")


def mutations():
    variants = [
        (image_encoding, "binary_instead_of_base64", "if len(b64) < limit:", "if len(base64.b64decode(b64)) < limit:", check_boundary),
        (image_encoding, "allows_equal_limit", "if len(b64) < limit:", "if len(b64) <= limit:", check_boundary),
        (dc, "drops_last_page", "enumerate(page_indices, start=1)", "enumerate(page_indices[:-1], start=1)", check_pages),
        (dc, "ignores_response_budget", "if max_total_bytes is not None and total_bytes > max_total_bytes:", "if False:", check_budget),
        (dc, "stale_page_reference", 'page["markdown"] = renamed(page["markdown"])', 'page["markdown"] = page["markdown"]', check_figures),
        (dc, "stale_figure_metadata", 'figure["name"] = renames.get(figure["name"], figure["name"])', 'figure["name"] = figure["name"]', check_figures),
    ]
    outcomes = {}
    for target, label, old, new, check in variants:
        source = Path(target.__file__).read_text()
        assert source.count(old) == 1, label
        # Replace only function objects; don't mutate files or runtime services.
        tree = ast.parse(source.replace(old, new))
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        ns = dict(vars(target))
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)] + funcs, type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "mutation", "exec"), ns)  # noqa: S102 — fixed mutations of local source
        with contextlib.ExitStack() as stack:
            for node in funcs:
                stack.enter_context(patch.object(target, node.name, ns[node.name]))
            try:
                check()
            except AssertionError:
                outcomes[label] = "RED"
            else:
                raise AssertionError(f"Mutation survived: {label}")
    return outcomes


def measurements(paths, baseline_ref):
    baseline = None
    if baseline_ref:
        source = subprocess.check_output(
            ["git", "show", f"{baseline_ref}:src/privacy_service/document_converter.py"], cwd=ROOT, text=True
        )
        baseline = types.ModuleType("pdf_probe_baseline")
        sys.modules[baseline.__name__] = baseline
        exec(compile(source, "baseline", "exec"), vars(baseline))
    rows = []
    for sample, path in enumerate(paths, 1):
        raw = Path(path).read_bytes()
        doc = pdfium.PdfDocument(raw)
        try:
            for idx in range(len(doc)):
                page = doc[idx]
                try:
                    size = page.get_size()
                    bitmap = page.render(scale=min(1600 / max(size), 4))
                    try:
                        with bitmap.to_pil() as image:
                            original_png = png(image)
                            before_size = image.size
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                row = {"sample": sample, "page": idx + 1, "pages": len(doc),
                       "width_pt": size[0], "height_pt": size[1],
                       "png_b64_bytes": len(original_png), "before_px": before_size}
                old_b64 = None
                if baseline:
                    try:
                        old_b64 = next(iter(baseline._render_pdf_pages(raw, [idx]).values()))
                        row["before_b64_bytes"] = len(old_b64)
                    except baseline.DocumentTooLargeError as exc:
                        row["before_refused"] = str(exc)
                start = time.perf_counter()
                attempts = []
                save = Image.Image.save
                def record_save(image, fp, *args, **kwargs):
                    if kwargs.get("format") == "JPEG":
                        attempts.append({"edge": max(image.size), "quality": kwargs["quality"]})
                    return save(image, fp, *args, **kwargs)
                try:
                    with patch.object(Image.Image, "save", record_save):
                        result = dc._render_pdf_pages(raw, [idx])
                except dc.DocumentTooLargeError as exc:
                    row.update(status=413, error=str(exc), attempts=attempts)
                    rows.append(row)
                    continue
                name, b64 = next(iter(result.items()))
                assert len(b64) < dc._pdf_image_max_b64_bytes()
                if name.endswith(".png"):
                    assert b64 == original_png
                with Image.open(io.BytesIO(base64.b64decode(b64))) as image:
                    after_size = image.size
                row.update(status=200, after_b64_bytes=len(b64), after_px=after_size,
                           byte_identical_to_baseline=b64 == old_b64 if old_b64 else None,
                           edge_retained_percent=round(100 * max(after_size) / max(before_size), 2),
                           pixel_area_retained_percent=round(100 * after_size[0] * after_size[1] / (before_size[0] * before_size[1]), 2),
                           jpeg_quality=attempts[-1]["quality"] if attempts else None,
                           attempts=attempts, seconds=round(time.perf_counter() - start, 4))
                rows.append(row)
        finally:
            doc.close()
    return rows


def check_config():
    for raw, expected in [("", 400000), (" 123456 ", 123456)]:
        with patch.dict(os.environ, PDF_IMAGE_MAX_B64_BYTES=raw):
            assert dc._pdf_image_max_b64_bytes() == expected
    for raw in ("0", "-1", "abc", "1.5"):
        with patch.dict(os.environ, PDF_IMAGE_MAX_B64_BYTES=raw):
            try:
                dc._pdf_image_max_b64_bytes()
            except dc.ServiceConfigError:
                pass
            else:
                raise AssertionError("Invalid limit accepted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", action="append", default=[])
    parser.add_argument("--baseline-ref")
    args = parser.parse_args()
    os.environ.pop("PDF_IMAGE_MAX_B64_BYTES", None)
    checks = [check_config, check_pages, check_boundary, check_quality_steps, check_pathological, check_budget, check_figures, check_conversion, check_mime]
    details = {check.__name__: check() for check in checks}
    print(json.dumps({"checks": {check.__name__: "PASS" for check in checks}, "details": {k: v for k, v in details.items() if v is not None}, "mutations": mutations(), "measurements": measurements(args.pdf, args.baseline_ref)}, indent=2))


if __name__ == "__main__":
    main()
