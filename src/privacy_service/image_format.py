"""Determine image wire MIME from bytes, never a filename or an assumption."""

import base64
import io


def image_mime(b64: str) -> str:
    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(b64, validate=True))) as image:
        mime = Image.MIME.get(image.format)
        if mime not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            raise ValueError(f"Unsupported vision image format: {image.format}")
        return mime


def resize_word_image(raw: bytes, max_edge: int) -> tuple[bytes, str]:
    """Keep JPEG/PNG/GIF identity even when resizing; reject unknown formats."""
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as image:
        fmt = image.format
        extension = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif"}.get(fmt)
        if extension is None:
            raise ValueError(f"Unsupported Word image format: {fmt}")
        if max(image.size) <= max_edge:
            return raw, extension
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format=fmt)
        return output.getvalue(), extension
