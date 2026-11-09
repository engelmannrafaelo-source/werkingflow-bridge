"""Media types for the converter's PNG/JPEG payloads, independent of filenames."""


def image_data_url(b64: str) -> str:
    """JPEG's FF D8 FF signature is /9j/ in base64; legacy PNG is the default.

    Detect the actual payload: a resized JPEG figure or triage thumbnail may
    have been re-encoded as PNG while retaining its original dictionary key.
    """
    mime = "image/jpeg" if b64.startswith("/9j/") else "image/png"
    return f"data:{mime};base64,{b64}"
