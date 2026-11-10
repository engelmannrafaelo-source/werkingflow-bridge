"""Offline Abo image boundary probe; no CLI, models, credentials or network.

Run: python3 scripts/probe_abo_image.py
Uses synthetic images and real production preparation/worker entry points.
"""
import socket


def no_network(*args, **kwargs):
    raise RuntimeError("Offline probe: network access forbidden")


socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.socket.sendto = no_network
socket.create_connection = no_network
socket.getaddrinfo = no_network

import asyncio
import base64
import io
import json
import logging
import os
import random
import resource
import struct
import subprocess
import time
import zlib
import sys
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from fastapi import HTTPException
from src.providers import subscription_vision as sv
from src import docconvert_worker_vision as worker
from src.privacy_service import document_converter as pdf
from src.privacy_service import image_encoding as enc


def png(image):
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def block(raw):
    return {"source": {"type": "base64", "data": base64.b64encode(raw).decode("ascii")}}


def samples():
    small = png(Image.new("RGB", (32, 24), "white"))
    # Reproducible ~458 KB real pixel data, no padding/ancillary chunk trick.
    image = Image.new("RGB", (1490, 1090), "white")
    noise = Image.frombytes("RGB", (1479, 101), random.Random(42).randbytes(1479 * 101 * 3))
    image.paste(noise)
    big = png(image)
    assert 457000 < len(big) < 459000, len(big)
    return small, big


def check_images(small, big):
    assert sv.bilder_pruefen([block(small)]) == [(small, "image/png", "png")]
    result = sv.bilder_pruefen([block(big)])[0]
    raw, mime, ext = result
    assert len(base64.b64encode(raw)) < 400000, "5 MiB limit survived"
    assert mime == "image/jpeg" and ext == "jpg" and raw.startswith(b"\xff\xd8\xff")
    directory, paths = sv._dateien_schreiben([result])
    try:
        assert paths[0].suffix == ".jpg" and paths[0].read_bytes() == raw
    finally:
        sv._aufraeumen(directory)
    return {"input_bytes": len(big), "input_b64": len(block(big)["source"]["data"]),
            "output_b64": len(base64.b64encode(raw)), "before_px": [1490, 1090]}


def check_refusal():
    raw = png(Image.frombytes("RGB", (1600, 1600), random.Random(9).randbytes(1600 * 1600 * 3)))
    prepared = sv.bild_vorbereiten(block(raw)["source"]["data"], "Bild 7 (rauschen.png)")
    assert len(base64.b64encode(prepared[0])) < 400000
    with Image.open(io.BytesIO(prepared[0])) as reduced:
        assert 900 <= max(reduced.size) < 1600
    with patch.dict(os.environ, ABO_VISION_MAX_IMAGE_B64="1000"):
        try:
            sv.bild_vorbereiten(block(raw)["source"]["data"], "Bild 7 (rauschen.png)")
        except HTTPException as exc:
            assert exc.status_code == 413
            message = exc.detail["message"]
            for fragment in ("Bild 7", "rauschen.png", "1000", "900 px", "55", "Byte Base64"):
                assert fragment in message, message
            return {"status": exc.status_code, "message": message}
        raise AssertionError("Noise did not fail loud")


def check_config_and_boundary(small):
    data = block(small)["source"]["data"]
    with patch.dict(os.environ, ABO_VISION_MAX_IMAGE_B64=str(len(data))):
        # At equality PNG must enter the cascade (may either fit JPEG or refuse).
        with patch.object(enc, "encode_vision_jpeg", side_effect=enc.ImageTooLargeError("boundary")) as call:
            try:
                sv.bild_vorbereiten(data, "Bild 1")
            except HTTPException as exc:
                assert exc.status_code == 413
            assert call.call_count == 1
    for value in ("0", "-1", "no", "1.5"):
        with patch.dict(os.environ, ABO_VISION_MAX_IMAGE_B64=value):
            try:
                sv.bild_vorbereiten(data, "Bild 1")
            except HTTPException as exc:
                assert exc.status_code == 500
            else:
                raise AssertionError("Invalid config accepted")


def check_worker(big):
    captured = []
    pool = types.ModuleType("src.providers.credit_pool")
    pool.enabled = lambda: True
    pool.PoolUnavailable = type("PoolUnavailable", (Exception,), {})
    fp = types.ModuleType("src.providers.funding_policy")
    fp.subscription_fallback_in_context = lambda: True
    fp.fallback_eligible = lambda request: True
    fp.activate_subscription_fallback = lambda *args: None
    fp.CAUSE_POOL_UNAVAILABLE = "synthetic"
    describer = types.ModuleType("src.privacy_service.image_describer")
    describer.append_descriptions_to_markdown = lambda *args: None
    describer.describe_images = lambda *args: None
    triage = types.ModuleType("src.privacy_service.image_triage")
    triage._triage_batch_size = lambda: 20
    triage.plan_image_descriptions = lambda *args: None
    triage.should_triage = lambda figures: False
    def split(images):
        captured.append(images)
        return images, {}
    triage.split_figures_and_pages = split
    modules = {m.__name__: m for m in (pool, fp, describer, triage)}
    payload = {"images": {"probe.png": block(big)["source"]["data"]}}
    with patch.dict(sys.modules, modules):
        # Real preparation + preflight, deliberately expired BEFORE model calls.
        status, _, _ = asyncio.run(worker.describe_in_worker(payload, {}, "synthetic", 0))
        assert status == 504
        assert len(captured[-1]["probe.png"]) < 400000
        assert enc.image_data_url(captured[-1]["probe.png"]).startswith("data:image/jpeg;")
        fp.subscription_fallback_in_context = lambda: False
        captured.clear()
        status, _, _ = asyncio.run(worker.describe_in_worker(payload, {}, "synthetic", 0))
        assert status == 504 and captured[-1] == payload["images"]  # Pool unchanged.
        real = worker._beschreiben
        async def retry(*args, **kwargs):
            if not kwargs["abo"]:
                return worker._POOL_KANN_NICHT_ZAHLEN, None, None
            return await real(*args, **kwargs)
        with patch.object(worker, "_beschreiben", retry):
            status, _, _ = asyncio.run(worker.describe_in_worker(payload, {}, "synthetic", 0, request=object()))
        assert status == 504 and len(captured[-1]["probe.png"]) < 400000
        fp.subscription_fallback_in_context = lambda: True
        with patch.dict(os.environ, ABO_VISION_MAX_IMAGE_B64="1000"):
            status, body, _ = asyncio.run(worker.describe_in_worker(payload, {}, "synthetic", 0))
        assert status == 413 and "probe.png" in body["error"] and "900 px" in body["error"]


def check_pdf_shared():
    with Image.frombytes("RGB", (400, 400), random.Random(3).randbytes(400 * 400 * 3)) as image:
        assert pdf._encode_vision_jpeg(image, "Seite 1") == enc.encode_vision_jpeg(
            image, "Seite 1", limit=400000, limit_name="PDF_IMAGE_MAX_B64_BYTES")
        with patch.dict(os.environ, PDF_IMAGE_MAX_B64_BYTES="1000"):
            try:
                pdf._encode_vision_jpeg(image, "Seite 1")
            except pdf.DocumentTooLargeError:
                pass
            else:
                raise AssertionError("PDF refusal lost")



def check_pixel_guard():
    # Build valid 100 MP PNG scanlines incrementally, never allocate a 100 MP image.
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    compressor = zlib.compressobj()
    parts = []
    row = b"\0" + b"\xff" * (10000 * 3)
    for _ in range(10000):
        parts.append(compressor.compress(row))
    parts.append(compressor.flush())
    raw = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", 10000, 10000, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", b"".join(parts)) + chunk(b"IEND", b""))
    data = block(raw)["source"]["data"]
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    start = time.perf_counter()
    # Any attempted pixel load fails this probe, independent of timing/RSS.
    with patch.object(Image.Image, "load", side_effect=AssertionError("Pixel decode attempted")):
        try:
            sv.bild_vorbereiten(data, "Bild 100MP")
        except HTTPException as exc:
            assert exc.status_code == 413
            for fragment in ("10000x10000", "100000000", "25000000"):
                assert fragment in exc.detail["message"]
        else:
            raise AssertionError("100 MP accepted")
    return {"status": 413, "input_b64": len(data), "decode": "not called",
            "elapsed_s": round(time.perf_counter() - start, 6),
            "maxrss_before_kib": before,
            "maxrss_after_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}



def check_pixel_boundary():
    results = {}
    for size in (5000, 6000):
        with Image.new("RGB", (size, size), "white") as image:
            # Real pixels force the accepted image through the JPEG cascade.
            with Image.frombytes("RGB", (400, 400), random.Random(25).randbytes(400 * 400 * 3)) as noise:
                image.paste(noise)
            data = block(png(image))["source"]["data"]
        assert len(data) >= 400000
        if size == 5000:
            raw, mime, ext = sv.bild_vorbereiten(data, "Bild 25MP")
            assert mime == "image/jpeg" and ext == "jpg" and raw.startswith(b"\xff\xd8\xff")
            assert len(base64.b64encode(raw)) < 400000
            results["5000x5000"] = {"status": "ok", "mime": mime}
        else:
            with patch.object(Image.Image, "load", side_effect=AssertionError("Pixel decode attempted")):
                try:
                    sv.bild_vorbereiten(data, "Bild 36MP")
                except HTTPException as exc:
                    assert exc.status_code == 413
                    assert exc.detail["error"] == "abo_vision_image_too_large"
                    for fragment in ("6000x6000", "36000000", "25000000"):
                        assert fragment in exc.detail["message"]
                    results["6000x6000"] = {"status": 413, "error": exc.detail["error"],
                                           "decode": "not called"}
                else:
                    raise AssertionError("36 MP accepted")
    return results


def check_high_depth():
    # A real 16-bit gradient; old RGBA conversion yielded mean ~254.
    import array
    values = array.array("H", (x * 257 for y in range(64) for x in range(256)))
    if sys.byteorder != "little":
        values.byteswap()
    raw = png(Image.frombytes("I;16", (256, 64), values.tobytes()))
    for limit in ("400000", "1000"):
        with patch.dict(os.environ, ABO_VISION_MAX_IMAGE_B64=limit):
            try:
                sv.bild_vorbereiten(block(raw)["source"]["data"], "16-Bit-Verlauf")
            except HTTPException as exc:
                assert exc.status_code == 415
                assert "8-Bit" in exc.detail["message"]
            else:
                raise AssertionError("16-bit gradient silently converted/passed")
    return {"status": 415, "output_mean": None, "silent_white_output": False}


def check_early_guards(small):
    data = block(small)["source"]["data"]
    with patch.dict(os.environ, ABO_VISION_MAX_IMAGE_PIXELS="767"):
        try:
            sv.bild_vorbereiten(data, "small")
        except HTTPException as exc:
            assert exc.status_code == 413 and "767" in exc.detail["message"]
        else:
            raise AssertionError("Pixel override ignored")
    with patch.object(sv, "ABO_VISION_MAX_TOTAL_B64", 1), patch.object(
        sv, "bild_vorbereiten", side_effect=AssertionError("encoding before total guard")
    ):
        try:
            sv.bilder_pruefen([block(small)])
        except HTTPException as exc:
            assert exc.status_code == 413
    try:
        sv.bild_vorbereiten(block(b"\x89PNG\r\n\x1a\ninvalid")["source"]["data"], "broken")
    except HTTPException as exc:
        assert exc.status_code == 415
    else:
        raise AssertionError("Broken image accepted")

def main():
    os.environ.pop("ABO_VISION_MAX_IMAGE_B64", None)
    os.environ.pop("PDF_IMAGE_MAX_B64_BYTES", None)
    os.environ.pop("ABO_VISION_MAX_IMAGE_PIXELS", None)
    if "--pixel-child" in sys.argv:
        print(json.dumps(check_pixel_guard()))
        return
    if "--boundary-child" in sys.argv:
        print(json.dumps(check_pixel_boundary()))
        return
    logging.basicConfig(level=logging.INFO)
    pixel_guard = json.loads(subprocess.check_output(
        [sys.executable, __file__, "--pixel-child"], text=True))
    pixel_boundary = json.loads(subprocess.check_output(
        [sys.executable, __file__, "--boundary-child"], text=True))
    high_depth = check_high_depth()
    small, big = samples()
    check_early_guards(small)
    result = check_images(small, big)
    refusal = check_refusal()
    check_config_and_boundary(small)
    check_worker(big)
    check_pdf_shared()
    with patch.object(sv, "ABO_VISION_MAX_IMAGE_B64", 5 * 2**20):
        try:
            check_images(small, big)
        except AssertionError:
            mutation = "RED (killed)"
        else:
            raise AssertionError("Mutation survived: 5 MiB remains")
    print(json.dumps({"checks": "PASS", "pixel_guard": pixel_guard, "pixel_boundary": pixel_boundary, "high_depth": high_depth, "small_png": "byte-identical", "image": result,
                      "refusal": refusal, "worker_abo_pool_retry": "PASS",
                      "pdf_shared_cascade": "PASS", "mutation_5_mib": mutation}, indent=2))


if __name__ == "__main__":
    main()
