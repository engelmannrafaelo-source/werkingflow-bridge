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



class UnprocessableWordImage(ValueError):
    """An embedded picture has no available raster decoder."""


def resize_word_image(raw: bytes, max_edge: int) -> tuple[bytes, str]:
    """Preserve supported wire formats; rasterize other Pillow formats to PNG."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(raw)) as image:
            fmt = image.format
            extension = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif",
                         "WEBP": ".webp"}.get(fmt)
            if extension is not None and max(image.size) <= max_edge:
                image.load()
                return raw, extension
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            if extension is None:
                image = image.convert("RGBA")
                fmt, extension = "PNG", ".png"
            output = io.BytesIO()
            image.save(output, format=fmt)
            return output.getvalue(), extension
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise UnprocessableWordImage(f"Bild nicht verarbeitbar: {exc}") from exc
