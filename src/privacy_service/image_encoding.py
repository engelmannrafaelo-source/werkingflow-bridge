"""Shared JPEG size cascade and payload-derived PNG/JPEG media types."""


import base64
import io
import logging

logger = logging.getLogger(__name__)


def image_data_url(b64: str) -> str:
    """JPEG's FF D8 FF signature is /9j/ in base64; legacy PNG is the default.

    Detect the actual payload: a resized JPEG figure or triage thumbnail may
    have been re-encoded as PNG while retaining its original dictionary key.
    """
    mime = "image/jpeg" if b64.startswith("/9j/") else "image/png"
    return f"data:{mime};base64,{b64}"


class ImageTooLargeError(ValueError):
    """No JPEG below the strict base64 bound at the minimum edge."""


def encode_vision_jpeg(
    image, label: str, *, limit: int, limit_name: str,
    qualities=(85, 75, 65, 55), min_edge: int = 900,
) -> str:
    """Try quality then edge reductions, or refuse the entire conversion with 413.

    Never return the last oversized attempt or quietly omit an image. The
    label identifies the page/figure without including document contents.
    """
    from PIL import Image

    rgb = image.convert("RGB")
    original_edge = encoded_edge = max(rgb.size)
    try:
        while True:
            for quality in qualities:
                with io.BytesIO() as buf:
                    rgb.save(buf, format="JPEG", quality=quality)
                    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                encoded_edge = max(rgb.size)
                if len(b64) < limit:
                    return b64
            edge = max(rgb.size)
            if edge <= min_edge:
                break
            next_edge = max(min_edge, int(edge * 0.85))
            ratio = next_edge / edge
            resized = rgb.resize(
                (max(1, round(rgb.width * ratio)), max(1, round(rgb.height * ratio))),
                Image.Resampling.LANCZOS,
            )
            rgb.close()
            rgb = resized
        raise ImageTooLargeError(
            f"{label}: JPEG bei Qualitaet {quality} hat {len(b64)} Byte Base64; "
            f"erforderlich sind weniger als {limit} Byte "
            f"({limit_name}), letzte Kantenlaenge {edge} px. "
            f"Bitte den Bildbereich aufteilen und erneut umwandeln. "
            f"Keine Bilder dieser Antwort wurden ausgeliefert."
        )
    finally:
        if "b64" in locals():
            logger.info(
                "[image] %s: JPEG edge %d -> %d px; quality=%d; base64_bytes=%d; limit=%d",
                label, original_edge, encoded_edge, quality, len(b64), limit,
            )
        rgb.close()
