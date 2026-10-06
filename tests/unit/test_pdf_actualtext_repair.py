"""ActualText-Reparatur vor Docling (Vorzeichen-Befund 06.10.2026).

Port der TS-Tests aus werking-report
(``tests/unit/services/pdf-actualtext-reparatur.test.ts``) plus:
  - Paritaet am echten Muehl-Auszug gegen die TS-reparierte Datei (byte-identisch),
  - Verdrahtung im Worker-Proxy ``_proxy_document_endpoint`` (nur PDF, nur mit Flag).

Das synthetische Fixture ist ein erfundenes PDF in der Bauart, die Chromium/Skia
fuer Inter als Type3-Font schreibt — kein Kundentext.
"""

from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.privacy.pdf_actualtext_repair import (
    _PdfReader,
    _ref_number,
    _value,
    repair_actualtext_glyphs,
)

# Echte Proben (local-storage auf dem Dev-Server). Nicht ins Repo kopiert:
# Energy-Export-Auszug eines Kundenobjekts (Objektname, Adresse).
_SAMPLE_DIR = Path("/root/projekte/local-storage/vorzeichen-alle-tools-20261006")
_SAMPLE = _SAMPLE_DIR / "muehl-auszug.pdf"
_SAMPLE_TS_REPAIRED = _SAMPLE_DIR / "muehl-auszug-repariert.pdf"
#: Glyph-Anzahl, die die TS-Fassung (repariereActualTextGlyphen) auf dem Auszug meldet.
_SAMPLE_TS_COUNT = 32


def _hex(text: str) -> str:
    return "<" + text.encode("latin1").hex().upper() + ">"


def build_fixture_pdf(with_actualtext: bool = True) -> bytes:
    """Minimal-PDF mit einer Type3-Schrift wie Skia sie schreibt (1:1 aus dem TS-Test)."""
    to_unicode = "\n".join([
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<00> <FF>",
        "endcodespacerange",
        "6 beginbfchar",
        "<01> <0000>",
        "<02> <0000>",
        "<03> <0000>",
        "<81> <2265>",
        "<82> <0394>",
        "<B0> <00B0>",
        "endbfchar",
        "1 beginbfrange",
        "<20> <7E> <0020>",
        "endbfrange",
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
    ])
    # „Soletemperatur ≥ −1,5 °C, Minimum –8,6 °C, Δ −15 K, Abweichung -2"
    content_raw = "\n".join([
        "BT",
        "/F1 12 Tf",
        "1 0 0 1 40 700 Tm",
        f"{_hex('Soletemperatur ')} Tj <81> Tj {_hex(' ')} Tj",
        "/Span <</ActualText <FEFF2212>>> BDC <01> Tj EMC",
        f"{_hex('1,5 ')} Tj <B0> Tj {_hex('C, Minimum ')} Tj",
        "/Span<</ActualText <FEFF2013> >> BDC",
        "<02> Tj",
        "EMC",
        f"{_hex('8,6 ')} Tj <B0> Tj {_hex('C, ')} Tj <82> Tj {_hex(' ')} Tj",
        "/Span <</ActualText <FEFF2212>>> BDC [<01>] TJ EMC",
        f"{_hex('15 K, Abweichung ')} Tj",
        "/Span <</ActualText (-)>> BDC <03> Tj EMC",
        f"{_hex('2')} Tj",
        "ET",
    ])
    content = content_raw if with_actualtext else re.sub(r"/Span\s*<<.*?>>\s*BDC|EMC", "", content_raw)

    names = [f"/g{i:02x}" for i in range(256)]
    char_procs = " ".join(f"{n} 7 0 R" for n in names)
    widths = " ".join("500" for _ in range(256))
    tu_data = zlib.compress(to_unicode.encode("latin1"))
    content_data = zlib.compress(content.encode("latin1"))
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        (f"<< /Length {len(content_data)} /Filter /FlateDecode >>", content_data),
        "<< /Type /Font /Subtype /Type3 /FontBBox [0 0 500 700] /FontMatrix [0.001 0 0 0.001 0 0] "
        f"/CharProcs << {char_procs} >> /Encoding << /Type /Encoding /Differences [0 {' '.join(names)}] >> "
        f"/FirstChar 0 /LastChar 255 /Widths [{widths}] /ToUnicode 6 0 R /Resources << >> >>",
        (f"<< /Length {len(tu_data)} /Filter /FlateDecode >>", tu_data),
        ("<< /Length 9 >>", b"500 0 d0\n"),
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(objects):
        offsets.append(len(out))
        if isinstance(obj, str):
            out += f"{i + 1} 0 obj\n{obj}\nendobj\n".encode("latin1")
        else:
            d, data = obj
            out += f"{i + 1} 0 obj\n{d}\nstream\n".encode("latin1") + data + b"\nendstream\nendobj\n"
    xref = f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    for off in offsets:
        xref += f"{off:010d} 00000 n \n"
    xref += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{len(out)}\n%%EOF\n"
    out += xref.encode("latin1")
    return bytes(out)


def _cmap_of_font(pdf: bytes, font_nr: int) -> str:
    """ToUnicode-CMap eines Fonts, so wie ein Leser ihn sieht (letzte Definition gewinnt)."""
    reader = _PdfReader(pdf)
    font = reader.obj(font_nr)
    assert font is not None
    cmap_nr = _ref_number(_value(font.dict, "ToUnicode"))
    assert cmap_nr is not None
    text = reader.stream_text(reader.obj(cmap_nr))
    assert text is not None
    return text


def _map_code(cmap: str, code: str) -> str | None:
    m = re.search(rf"<{code}>\s*<([0-9A-Fa-f]+)>", cmap, re.IGNORECASE)
    return m.group(1).upper() if m else None


# ---------------------------------------------------------------------------
# Synthetisch (Port der TS-Faelle)
# ---------------------------------------------------------------------------

def test_original_fixture_maps_sign_glyphs_to_null():
    """Ausgangsbefund: das CMap traegt fuer Minus/Strich/Bindestrich U+0000."""
    cmap = _cmap_of_font(build_fixture_pdf(), 5)
    assert _map_code(cmap, "01") == "0000"
    assert _map_code(cmap, "02") == "0000"
    assert _map_code(cmap, "03") == "0000"


def test_repair_writes_actualtext_values_into_tounicode():
    result = repair_actualtext_glyphs(build_fixture_pdf())
    assert result.repaired_glyphs == 3
    assert result.hint is None

    cmap = _cmap_of_font(result.pdf, 5)
    assert _map_code(cmap, "01") == "2212"  # Minus
    assert _map_code(cmap, "02") == "2013"  # Halbgeviertstrich
    assert _map_code(cmap, "03") == "002D"  # Bindestrich (latin1-ActualText)
    assert "<0000>" not in cmap
    # Unbeteiligte Eintraege bleiben stehen.
    assert _map_code(cmap, "81") == "2265"
    assert _map_code(cmap, "B0") == "00B0"


def test_repair_only_appends_original_bytes_untouched():
    original = build_fixture_pdf()
    result = repair_actualtext_glyphs(original)
    assert result.pdf[: len(original)] == original
    assert len(result.pdf) > len(original)
    tail = result.pdf[len(original):].decode("latin1")
    assert "6 0 obj" in tail
    assert "/Prev " in tail and tail.rstrip().endswith("%%EOF")
    # Neue Xref zeigt exakt auf das angehaengte Objekt.
    xref_off = int(re.findall(r"startxref\s+(\d+)", result.pdf.decode("latin1"))[-1])
    assert result.pdf[xref_off:xref_off + 4] == b"xref"
    entry = re.search(r"xref\n6 1\n(\d{10}) 00000 n", tail)
    assert entry is not None
    assert result.pdf[int(entry.group(1)):].startswith(b"6 0 obj")


def test_pdf_without_actualtext_is_returned_unchanged_same_object():
    without = build_fixture_pdf(with_actualtext=False)
    result = repair_actualtext_glyphs(without)
    assert result.repaired_glyphs == 0
    assert result.pdf is without
    assert result.hint is None


def test_garbage_never_raises_and_comes_back_unchanged():
    junk = b"kein pdf"
    result = repair_actualtext_glyphs(junk)
    assert result.pdf is junk
    assert result.repaired_glyphs == 0


def test_empty_input_does_not_raise():
    result = repair_actualtext_glyphs(b"")
    assert result.repaired_glyphs == 0
    assert result.pdf == b""


def test_encrypted_pdf_is_skipped_with_hint():
    pdf = build_fixture_pdf().replace(b"/Root 1 0 R >>", b"/Root 1 0 R /Encrypt 99 0 R >>")
    result = repair_actualtext_glyphs(pdf)
    assert result.repaired_glyphs == 0
    assert result.pdf is pdf
    assert result.hint and "verschluesselt" in result.hint


def test_xref_stream_pdf_is_skipped_with_hint():
    pdf = build_fixture_pdf()
    # startxref auf ein Ziel ohne "xref"-Tabelle biegen (wie bei Xref-Streams).
    pdf = re.sub(rb"startxref\n\d+\n", b"startxref\n0\n", pdf)
    result = repair_actualtext_glyphs(pdf)
    assert result.repaired_glyphs == 0
    assert result.pdf is pdf
    assert result.hint and "Xref-Stream" in result.hint


def test_already_mapped_code_is_not_overwritten():
    """Traegt das CMap schon einen echten Wert, bleibt er (nur <0000>/fehlend wird gefuellt)."""
    pdf = build_fixture_pdf()
    once = repair_actualtext_glyphs(pdf)
    twice = repair_actualtext_glyphs(once.pdf)
    assert twice.repaired_glyphs == 0
    assert twice.pdf is once.pdf


# ---------------------------------------------------------------------------
# Paritaet mit der TS-Fassung am echten Muehl-Auszug
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _SAMPLE.exists(), reason="Muehl-Probe nur auf dem Dev-Server vorhanden")
def test_parity_with_ts_on_muehl_sample():
    original = _SAMPLE.read_bytes()
    result = repair_actualtext_glyphs(original)
    assert result.hint is None
    assert result.repaired_glyphs == _SAMPLE_TS_COUNT
    if _SAMPLE_TS_REPAIRED.exists():
        # Die TS-Fassung haengt exakt dieselben Bytes an.
        assert result.pdf == _SAMPLE_TS_REPAIRED.read_bytes()

    # Jede Glyphe, die nur ueber ActualText lesbar war, hat jetzt einen
    # Unicode-Wert: ein zweiter Lauf findet nichts mehr zu reparieren.
    # (<0000>-Eintraege fuer Glyphen OHNE ActualText-Span bleiben bewusst stehen.)
    second = repair_actualtext_glyphs(result.pdf)
    assert second.repaired_glyphs == 0 and second.hint is None
    assert _null_entries(result.pdf) < _null_entries(original)


def _null_entries(pdf: bytes) -> int:
    """Anzahl <code> <0000>-Eintraege in allen ToUnicode-CMaps, die ein Leser sieht."""
    reader = _PdfReader(pdf)
    seen = set()
    total = 0
    for nr in reader.numbers:
        o = reader.obj(nr)
        if o is None or "/ToUnicode" not in o.dict:
            continue
        cmap_nr = _ref_number(_value(o.dict, "ToUnicode"))
        if cmap_nr is None or cmap_nr in seen:
            continue
        seen.add(cmap_nr)
        cmap = reader.stream_text(reader.obj(cmap_nr))
        if cmap:
            total += len(re.findall(r"<[0-9A-Fa-f]+>\s*<0000>", cmap))
    return total


# ---------------------------------------------------------------------------
# Verdrahtung im Worker-Proxy (src/main.py::_proxy_document_endpoint)
# ---------------------------------------------------------------------------

def _import_main():
    for mod in [
        "claude_code_sdk",
        "claude_code_sdk._errors",
        "claude_code_sdk._internal",
        "claude_code_sdk._internal.client",
        "src.identity.routes",
        "src.db.client",
    ]:
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()
    import src.main  # noqa: E402

    return src.main


class _FakeTrackCall:
    async def __aenter__(self):
        return 0

    async def __aexit__(self, *exc_info):
        return False


async def _forwarded_bytes(monkeypatch, *, flag, content, filename, content_type):
    main = _import_main()
    if flag is None:
        monkeypatch.delenv("BRIDGE_PDF_ACTUALTEXT_REPAIR", raising=False)
    else:
        monkeypatch.setenv("BRIDGE_PDF_ACTUALTEXT_REPAIR", flag)

    upload = MagicMock()
    upload.filename = filename
    upload.content_type = content_type
    upload.read = AsyncMock(return_value=content)
    form = MagicMock()
    form.get = MagicMock(side_effect=lambda k, d=None: upload if k == "file" else d)
    form.multi_items = MagicMock(return_value=[("file", upload), ("language", "de")])
    request = MagicMock()
    request.headers = {}
    request.form = AsyncMock(return_value=form)

    response = MagicMock()
    response.status_code = 200
    response.json = MagicMock(return_value={"success": True, "markdown": "x"})
    pc = MagicMock()
    pc.post = AsyncMock(return_value=response)
    pc.track_call = MagicMock(return_value=_FakeTrackCall())

    repair_spy = MagicMock(wraps=repair_actualtext_glyphs)
    with (
        patch("src.main.get_privacy_client", return_value=pc),
        patch("src.main._record_document_call_metrics"),
        patch("src.privacy.pdf_actualtext_repair.repair_actualtext_glyphs", repair_spy),
    ):
        resp = await main._proxy_document_endpoint(
            request, "/document/convert", 60.0, agent_id="dokument-konvertierung",
        )
    assert resp.status_code == 200
    files = pc.post.call_args.kwargs["files"]
    assert files["file"][0] == filename
    assert pc.post.call_args.kwargs["data"] == {"language": "de"}
    return files["file"][1], repair_spy.call_count


@pytest.mark.asyncio
async def test_proxy_repairs_pdf_when_flag_true(monkeypatch):
    pdf = build_fixture_pdf()
    sent, calls = await _forwarded_bytes(
        monkeypatch, flag="true", content=pdf, filename="a.pdf", content_type="application/pdf",
    )
    assert calls == 1
    assert sent != pdf and sent.startswith(pdf)
    assert _map_code(_cmap_of_font(sent, 5), "01") == "2212"


@pytest.mark.asyncio
async def test_proxy_detects_pdf_by_magic_bytes(monkeypatch):
    pdf = build_fixture_pdf()
    sent, calls = await _forwarded_bytes(
        monkeypatch, flag="true", content=pdf, filename="upload.bin",
        content_type="application/octet-stream",
    )
    assert calls == 1
    assert sent != pdf


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, "", "false", "1", "yes"])
async def test_proxy_does_not_repair_without_flag(monkeypatch, flag):
    pdf = build_fixture_pdf()
    sent, calls = await _forwarded_bytes(
        monkeypatch, flag=flag, content=pdf, filename="a.pdf", content_type="application/pdf",
    )
    assert calls == 0
    assert sent is pdf


@pytest.mark.asyncio
async def test_proxy_does_not_touch_non_pdf(monkeypatch):
    csv = b"a,b\n1,2\n"
    sent, calls = await _forwarded_bytes(
        monkeypatch, flag="true", content=csv, filename="t.csv", content_type="text/csv",
    )
    assert calls == 0
    assert sent is csv
