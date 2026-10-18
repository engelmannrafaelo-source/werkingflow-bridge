"""Text-only material contract, shared verbatim at the provider boundaries.

Encoded runs above 256 characters are not admitted as prose. Shorter encodings
are decoded to check file signatures. This is a second line, not a filesystem
sandbox: original documents must never be staged in an agent workspace.
"""

import base64
import binascii
import re

_MAGIC = (b"%PDF-", b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"PK\x03\x04")
_DATA = re.compile(r"""data:[^,\s<>]+,[^\s<>"')]+""", re.I)
_RUN = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{16,}={0,2}")
_HEX = re.compile(r"(?<![a-fA-F0-9])(?:[a-fA-F0-9]{2}[ \t]*){8,}")
_XXD = re.compile(r"^\s*[0-9a-fA-F]{7,16}:\s*((?:(?:[0-9a-fA-F]{2}){1,4}\s+)+)", re.M)


def binary_text_reason(text: str) -> str | None:
    if _DATA.search(text):
        return "Data-URL mit Nutzlast"
    if "%PDF-" in text or any(ord(c) < 32 and c not in "\r\n\t\f" for c in text):
        return "Rohdokument oder binaere Steuerzeichen"
    if "\u0089PNG" in text or "\ufffdPNG" in text:
        return "Rohbild"
    # xxd has offsets/ASCII columns: check its byte column independently.
    dumps = _XXD.findall(text)
    if dumps:
        raw = bytes.fromhex("".join(dumps))
        if len(raw) >= 128 or any(m in raw for m in _MAGIC):
            return "Hexdump"
    for match in _HEX.finditer(text):
        value = re.sub(r"\s", "", match.group())
        raw = bytes.fromhex(value)
        if len(value) >= 256 or any(m in raw for m in _MAGIC):
            return "Hex-kodierte Binaerdaten"
    # Join only base64-shaped wrapped lines, never arbitrary prose words.
    joined = re.sub(r"(?<=[A-Za-z0-9+/=_-])\r?\n(?=[A-Za-z0-9+/_-]{16})", "", text)
    for match in _RUN.finditer(joined):
        value = match.group()
        try:
            raw = base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            )
        except (binascii.Error, ValueError):
            continue
        if (
            len(value) >= 256
            or any(m in raw for m in _MAGIC)
            or (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP")
        ):
            return "Base64-kodierte Binaerdaten"
    return None


def require_text_bytes(data: bytes) -> str:
    text = data.decode("utf-8", errors="strict")
    reason = binary_text_reason(text)
    if reason:
        raise ValueError(reason)
    return text
