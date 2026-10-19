"""Gate against the R1–R3 failure class: one odd element must not fail a Word file.

Every probe of the independent reviews goes through the real
``/document/convert``. Only real dangers (no valid OOXML package, expanded
size budget) may answer 4xx — then without starting Writer. Everything else
must return text, with local notices where an element could not be kept.
The reviewer's sizes for the bomb probes are generated here instead of
being committed. Soll per probe: ``tests/fixtures/docx/korpus/README.md``.

R5 adds the R4 Codex probes (c01–c12) and a stricter ledger: unique
sentinel texts must appear exactly once in the text a CommonMark renderer
shows — no silent loss, no duplication, no literal text turned into markup.

R6 adds the R5 Codex probes (d01–d12 and the generated image path
variants) and an independent occurrence ledger: the multiset of sentinel
texts read from the source package itself must equal the multiset in the
rendered text, so the same text elsewhere cannot stand in for a lost one.
"""

import binascii
import io
import re
import shutil
import struct
import zipfile
import zlib
from collections import Counter
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from docx import Document
from lxml import etree

KORPUS = Path(__file__).parents[1] / "fixtures/docx/korpus"
MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Real dangers: status and reason. Nothing else may be rejected.
DANGER = {
    "bombe.docx": (413, "expanded size"),
    "h17_luegengroesse.docx": (415, "Bad CRC-32"),
    "h20a_unterbudget_unreferenziert.docx": (413, "expanded size"),
    "h20b_unterbudget_bild.docx": (413, "expanded size"),
    "riesig.docx": (413, "expanded size"),
    "falsch_pdf.docx": (415, "Kein Word-Dokument"),
    "falsch_zufall.docx": (415, "Kein Word-Dokument"),
    "falsch_zip.docx": (415, "Kein Word-Dokument"),
}

# Content the reviews named per probe; every listed string must survive.
EXPECT = {
    "voll.docx": [
        "Absatz EINS.",
        "EINGEFUEGTERTEXT",
        "KOPFZEILENTEXT",
        "FUSSZEILENTEXT",
        "FUSSNOTENTEXT",
        "TEXTFELDINHALT",
        'colspan="2"><p>QUERVERBUNDEN',
    ],
    "voll.doc": ["Absatz EINS.", "QUERVERBUNDEN", "nicht gegen das Original prüfbar"],
    "spaces.docx": ["**Fett** Text danach.", "Vorher *kursiv* und weiter.", "5*3 = 15"],
    "t_sym.docx": ["20°C"],
    "t_nbh.docx": ["WP‑1"],
    "tab_feld.docx": ["|"],
    "t_pagefeld.docx": ["|"],
    "tab_mehrabsatz.docx": ["Zeile a Zeile b"],
    "tab_delrow.docx": ["| Z0 | W0 |", "| Z2 | W2 |"],
    "h01_kopf_mehrfach.docx": [
        "KD-QUER",
        "KE-QUER",
        "Kopfzeile (erste Seite)",
        "Brieftext.",
    ],
    "h01b_kopf_gerade_ohne_tabelle.docx": ["Gerade", "Kopfzeile (gerade Seiten)"],
    "h02_textfeld_tabelle.docx": ['colspan="2"><p>B-QUER', "TB-A", "TB-B"],
    "h03_sdt_zeile.docx": ['colspan="2"><p>S-QUER', "S22"],
    "h05_fussnote_endnote.docx": [
        "FN **FETT** und *KURSIV* Wert",  # Word formatting as Markdown
        "FN FETT und KURSIV Wert 5*3.",  # the literal asterisk as shown
        "ENDNOTENTEXT",
    ],
    "h06_kommentar_tabelle.docx": ['colspan="2"><p>KQUER'],
    "h07_rtl_emoji.docx": ["مرحبا", "👨‍👩‍👧", "🇦🇹", "𠜎", 'colspan="2"><p>E-QUER'],
    "h10_lange_tabelle.docx": [],
    "h11_lange_tabelle_verbunden.docx": [],
    "h12_liste_ueber_tabelle.docx": ["Punkt eins", "L-QUER", "innen b", "Punkt drei"],
    "h13_makro.docm": ["Makros (VBA)", "MAKROTEXT", 'colspan="2"><p>D-QUER'],
    "h15_wingdings.docx": ["Symbol aus Wingdings"],
    "h16_gridafter.docx": ['colspan="2"><p>G-QUER', "G21"],
    "h19_pixelbombe.docx": [
        "[Eingebettetes Bild zu groß (30000×30000 Pixel), nicht geladen]"
    ],
    "h19b_pixelbombe_60k.docx": [
        "[Eingebettetes Bild zu groß (60000×60000 Pixel), nicht geladen]"
    ],
    "h21_excel_ole.docx": [
        "| EXCELZELLE | 42 |",
        "Eingebettetes Objekt (Excel.Sheet.12)",
    ],
    "h21b_excel_ole_word.docx": [
        "| EXCELZELLE | 42 |",
        "Eingebettetes Objekt (Excel.Sheet.12)",
    ],
    "h24_zwei_angrenzende_tabellen.docx": ["| Einheit | kWh/m2a |", 'colspan="2"><p>Q'],
    "c05_platzhalter_literal.docx": ["WORDTABLEPLACEHOLDER0END", "QXMERGEDQZ"],
    "c09_textfeld_tabelle.docx": ['colspan="2"><p>B-QUER', "TB-A", "TB-B", "B22"],
    "c10_bild_paketabsolut.docx": [
        "[Eingebettetes Bild zu groß (30000×30000 Pixel), nicht geladen]"
    ],
    "c11_hauptteil_verlegt_bild.docx": [
        "[Eingebettetes Bild zu groß (30000×30000 Pixel), nicht geladen]"
    ],
    # voll.docx with its main document moved from word/ to content/
    "c12_hauptteil_verlegt_privat.docx": [
        "Absatz EINS.",
        "EINGEFUEGTERTEXT",
        "KOPFZEILENTEXT",
        "FUSSZEILENTEXT",
        "FUSSNOTENTEXT",
    ],
}
FORBIDDEN = {
    "voll.docx": ["VERSTECKTERTEXT", "KOMMENTARTEXT", "GELOESCHTERTEXT", "javascript:"],
    "hidden_style.docx": ["VERSTECKTABSATZ"],
    "tab_delrow.docx": ["GELOESCHT", "ZEILEWEG", "|  |  |"],
    "tab_delrow2.docx": ["GELOESCHT", "ZEILEWEG"],
    "h06_kommentar_tabelle.docx": ["KOMMENTAR"],
    "spaces.docx": ["FettText", "Vorherkursiv"],
    "c12_hauptteil_verlegt_privat.docx": [
        "VERSTECKTERTEXT",
        "KOMMENTARTEXT",
        "GELOESCHTERTEXT",
        "example.invalid",
        "javascript:",
    ],
}

# R5 ledger: every sentinel exactly once in the rendered text.
_HEAD = [f"QXHEAD{k}{i}QZ" for i in (0, 1) for k in ("ER", "MERGE", "NEST", "END")]
SENTINELS = {
    "c01_fussnote_tabelle_nach_absatz.docx": [f"QXNOTE{i}QZ" for i in range(4)],
    "c02_fussnote_tabelle_vor_absatz.docx": [f"QXNOTE{i}QZ" for i in range(4)],
    "c03_kopf_mehrdeutig.docx": [
        "QXHEADERAMBODYQZ",
        *[f"QXAMHEAD{i}QZ" for i in range(4)],
    ],
    "c04_vmerge_folgezelle_text.docx": [
        "QXVMSTARTQZ",
        "QXCONTINUATIONQZ",
        *[f"QXVMCELL{i}QZ" for i in range(4)],
    ],
    "c05_platzhalter_literal.docx": ["QXBEFOREQZ", "QXAFTERQZ", "QXMERGEDQZ"],
    "c06_lesezeichen_kollision.docx": [
        "QXSTARTQZ",
        "QXENDQZ",
        *[f"QXCELL{i}QZ" for i in range(4)],
    ],
    "c07_verschachtelt_verbunden.docx": [
        "QXBEFOREQZ",
        "QXOUTERQZ",
        "QXPARENTQZ",
        *[f"QXINNER{i}QZ" for i in range(4)],
        "QXAFTERNESTQZ",
        "QXNEIGHBOURQZ",
        "QXAFTERQZ",
    ],
    "c08_kopf_varianten_verschachtelt.docx": ["QXBODYHEADERQZ", *_HEAD],
}
# Literal source text that must stay visible text (not markup) once rendered.
RENDERED = {
    "spaces.docx": ["<tag> & Co."],
    "c05_platzhalter_literal.docx": ["QXBEFOREQZ WORDTABLEPLACEHOLDER0END QXAFTERQZ"],
    "c06_lesezeichen_kollision.docx": [
        'QXSTARTQZ <a name="wqdeadbeef_t1r0c0"> QXENDQZ'
    ],
}
# Probes whose every source text run must be visible verbatim once rendered.
LITERAL = {
    "d09_markup_literal_kontexte.docx",
    "d10_markdown_literal.docx",
    "e05_literal_contexts_new.docx",
    "e06_literal_notes.docx",
    "e07_literal_split_runs.docx",
}
# Image targets of the R5 Codex review (worker1/own.py), generated from c10:
# (target spelling, member name, required notice).
_TOO_BIG = "Eingebettetes Bild zu groß (30000×30000 Pixel)"
IMAGE_PATHS = {
    "p1_punktsegmente_fragment.docx": (
        "../word/media/./image1.png#piece",
        "word/media/image1.png",
        _TOO_BIG,
    ),
    "p2_paketabsolut_kodiert.docx": (
        "/word/media/%69mage1.png",
        "word/media/image1.png",
        _TOO_BIG,
    ),
    "p3_prozent_leerzeichen.docx": (
        "media/space%20image.png",
        "word/media/space image.png",
        _TOO_BIG,
    ),
    "p4_prozent_raute.docx": (
        "media/hash%23image.png",
        "word/media/hash#image.png",
        _TOO_BIG,
    ),
    "p5_prozentzeichen_im_namen.docx": (
        "media/literal%2520image.png",
        "word/media/literal%20image.png",
        _TOO_BIG,
    ),
    "p6_backslash.docx": ("media\\image1.png", "word/media/image1.png", _TOO_BIG),
    "p7_ausserhalb.docx": (
        "../../outside.png",
        "word/media/image1.png",
        "Eingebettetes Bild nicht im Dokument gefunden",
    ),
}
# Required notices (prefixes) — a loss repaired from the source is never silent.
NOTICES = {
    "d01_endnote_tabelle_eindeutig.docx": ["Endnoten: 4 Absätze"],
    "d02_endnote_tabelle_wiederholt.docx": ["Endnoten: 4 Absätze"],
    "d03_fussnote_tabelle_wie_haupttext.docx": ["Fußnoten: 4 Absätze"],
    "d04_endnote_tabelle_wie_haupttext.docx": ["Endnoten: 4 Absätze"],
    "d05_fusszeile_standard_mehrdeutig.docx": [
        "Tabelle ohne Spaltenzuordnung ausgegeben"
    ],
    "d06_fusszeile_gerade_mehrdeutig.docx": ["Fußzeile (gerade Seiten)"],
    "d07_folgezelle_verschachtelt.docx": ["Text einer vertikal verbundenen Folgezelle"],
    **{name: [notice] for name, (_, _, notice) in IMAGE_PATHS.items()},
    "c01_fussnote_tabelle_nach_absatz.docx": ["Fußnoten: 4 Absätze"],
    "c02_fussnote_tabelle_vor_absatz.docx": ["Fußnoten: 4 Absätze"],
    "c03_kopf_mehrdeutig.docx": ["Tabelle ohne Spaltenzuordnung ausgegeben"],
    "c04_vmerge_folgezelle_text.docx": ["Text einer vertikal verbundenen Folgezelle"],
    "c10_bild_paketabsolut.docx": ["Eingebettetes Bild zu groß"],
    "c11_hauptteil_verlegt_bild.docx": ["Eingebettetes Bild zu groß"],
}
# Notices that must not appear (e.g. a present part reported missing).
NO_NOTICES = {
    "c03_kopf_mehrdeutig.docx": ["Kopfzeile", "im Export nicht enthalten"],
    "c06_lesezeichen_kollision.docx": ["im Export nicht enthalten"],
    "c07_verschachtelt_verbunden.docx": ["im Export nicht enthalten"],
    "d05_fusszeile_standard_mehrdeutig.docx": ["Fußzeile", "im Export"],
    **{
        name: ["im Export"]
        for name in (
            "d08_platzhalter_zwei_ziehungen.docx",
            "d09_markup_literal_kontexte.docx",
            "d10_markdown_literal.docx",
            "d11_textfeld_in_zelle.docx",
            "d12_utf16_hauptteil.docx",
        )
    },
}

# Probes whose regional ledger is knowingly not balanced, with the reason.
REGIONS_OPEN: dict[str, str] = {}

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_SENTINEL = re.compile(r"QX[A-Z0-9]+?QZ")


def source_runs(raw: bytes) -> list[str]:
    """Stored text runs of every XML part, read from the source package itself
    (mc:Fallback copies excluded: Word shows one branch)."""
    runs = []
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        for name in package.namelist():
            if not name.endswith(".xml"):
                continue
            root = etree.fromstring(package.read(name))
            for node in root.iter(f"{{{_W}}}t"):
                if not any(a.tag.endswith("}Fallback") for a in node.iterancestors()):
                    runs.append(node.text or "")
    return runs


_PARTS = {
    f"{{{_W}}}document": "Haupttext",
    f"{{{_W}}}hdr": "Kopfzeile",
    f"{{{_W}}}ftr": "Fußzeile",
    f"{{{_W}}}footnotes": "Fußnote",
    f"{{{_W}}}endnotes": "Endnote",
}


def source_regions(raw: bytes) -> Counter:
    """Sentinels per region, read from the source package itself: the part
    (body, header, footer, footnotes, endnotes) and inside it text box or
    table cell. Independent of the converter: it only reads the XML."""
    found: Counter = Counter()
    with zipfile.ZipFile(io.BytesIO(raw)) as package:
        for name in package.namelist():
            if not name.endswith(".xml"):
                continue
            root = etree.fromstring(package.read(name))
            if root.tag not in _PARTS:
                continue
            for node in root.iter(f"{{{_W}}}t"):
                tags = [a.tag for a in node.iterancestors()]
                if any(t.endswith("}Fallback") for t in tags):
                    continue
                region = _PARTS[root.tag]
                if f"{{{_W}}}txbxContent" in tags:
                    region += "/Textfeld"
                elif f"{{{_W}}}tc" in tags:
                    region += "/Zelle"
                for sentinel in _SENTINEL.findall(node.text or ""):
                    found[(region, sentinel)] += 1
    return found


def output_regions(body) -> Counter:
    """Sentinels per region of the reconciled export, by where they stand."""
    found: Counter = Counter()
    for node in body.find_all(string=True):
        region = "Haupttext"
        inner = ""
        for parent in node.parents:
            name, ident = parent.name, parent.get("id") or ""
            if not inner and (
                name in {"td", "th"} or parent.get("data-word-region") == "Zelle"
            ):
                inner = "/Zelle"
            if name in {"span", "div"} and ident.startswith("Frame"):
                inner = "/Textfeld"
            if name == "div" and parent.get("title") in {"header", "footer"}:
                region = "Kopfzeile" if parent["title"] == "header" else "Fußzeile"
            if name == "div" and re.match(r"sdfootnote\d+$", ident):
                region = "Fußnote"
            if name == "div" and re.match(r"sdendnote\d+$", ident):
                region = "Endnote"
        for sentinel in _SENTINEL.findall(str(node)):
            found[(region + inner, sentinel)] += 1
    return found


def rendered_text(markdown: str) -> str:
    """What a CommonMark reader shows (tables as GFM), as plain text."""
    from bs4 import BeautifulSoup

    markdown_it = pytest.importorskip("markdown_it")
    html = markdown_it.MarkdownIt("commonmark").enable("table").render(markdown)
    return BeautifulSoup(html, "html.parser").get_text(" ")


def _bomb(path, megabytes, referenced):
    """Reviewer probes mk5/mk10: zeros behind a valid Word package."""
    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (40, 40), (9, 9, 9)).save(image, "PNG")
    doc = Document()
    doc.add_paragraph("Vor")
    doc.add_picture(io.BytesIO(image.getvalue()))
    raw = io.BytesIO()
    doc.save(raw)
    source = zipfile.ZipFile(io.BytesIO(raw.getvalue()))
    block = b"\0" * (1 << 20)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            if referenced and info.filename.startswith("word/media/"):
                with target.open(info.filename, "w", force_zip64=True) as writer:
                    writer.write(source.read(info))
                    for _ in range(megabytes):
                        writer.write(block)
            else:
                target.writestr(info.filename, source.read(info))
        if not referenced:
            with target.open("word/media/blob.bin", "w", force_zip64=True) as writer:
                for _ in range(megabytes):
                    writer.write(block)


def _lying_size(path, megabytes=1536):
    """Reviewer probe h17: the directory claims 1 KiB for 1.5 GiB of zeros."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    block = b"\0" * (1 << 20)
    crc = 0
    chunks = []
    for _ in range(megabytes):
        chunks.append(compressor.compress(block))
        crc = binascii.crc32(block, crc)
    chunks.append(compressor.flush())
    payload = b"".join(chunks)
    doc = Document()
    doc.add_paragraph("Lügengröße")
    raw = io.BytesIO()
    doc.save(raw)
    data = raw.getvalue()
    name = b"word/media/luege.bin"
    cd_offset = data.find(b"PK\x01\x02")
    end_offset = data.find(b"PK\x05\x06")
    local = (
        struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50,
            20,
            0,
            8,
            0,
            0,
            crc,
            len(payload),
            1024,
            len(name),
            0,
        )
        + name
    )
    central = (
        struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50,
            20,
            20,
            0,
            8,
            0,
            0,
            crc,
            len(payload),
            1024,
            len(name),
            0,
            0,
            0,
            0,
            0,
            cd_offset,
        )
        + name
    )
    (entries,) = struct.unpack("<H", data[end_offset + 10 : end_offset + 12])
    directory = data[cd_offset:end_offset] + central
    end = (
        data[end_offset : end_offset + 8]
        + struct.pack(
            "<HHII",
            entries + 1,
            entries + 1,
            len(directory),
            cd_offset + len(local) + len(payload),
        )
        + data[end_offset + 20 :]
    )
    path.write_bytes(data[:cd_offset] + local + payload + directory + end)


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    root = tmp_path_factory.mktemp("word-bombs")
    _bomb(root / "bombe.docx", 1536, referenced=False)
    _bomb(root / "h20a_unterbudget_unreferenziert.docx", 950, referenced=False)
    _bomb(root / "h20b_unterbudget_bild.docx", 950, referenced=True)
    _lying_size(root / "h17_luegengroesse.docx")
    raw = (KORPUS / "c10_bild_paketabsolut.docx").read_bytes()
    for name, (target, member, _) in IMAGE_PATHS.items():
        with (
            zipfile.ZipFile(io.BytesIO(raw)) as source,
            zipfile.ZipFile(root / name, "w") as package,
        ):
            for info in source.infolist():
                data = source.read(info)
                if info.filename == "word/_rels/document.xml.rels":
                    data = data.replace(b"/word/media/image1.png", target.encode())
                if info.filename == "word/media/image1.png":
                    package.writestr(member, data)
                else:
                    package.writestr(info.filename, data)
    return {p.name: p for p in root.iterdir()}


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from src.privacy_service import app as app_mod
    from src.privacy_service.adapters import build_default_chain

    previous = app_mod._DOCUMENT_CHAIN
    app_mod._DOCUMENT_CHAIN = build_default_chain()
    with TestClient(app_mod.app) as test_client:
        yield test_client
    app_mod._DOCUMENT_CHAIN = previous


GENERATED = [
    "bombe.docx",
    "h17_luegengroesse.docx",
    "h20a_unterbudget_unreferenziert.docx",
    "h20b_unterbudget_bild.docx",
    *IMAGE_PATHS,
]
PROBES = sorted(
    p.name for p in KORPUS.iterdir() if p.suffix in {".docx", ".doc", ".docm", ".dotx"}
)


def test_korpus_is_complete():
    # 48 committed reviewer probes (R1 5, R2 20 without bombe,
    # R3 23 without h17/h20a/h20b)
    # plus 12 of the R4 Codex review (c01–c12), 12 of the R5 one (d01–d12)
    # and the 7 Writer documents of the R6 one (e01–e07).
    assert len(PROBES) == 79
    named = (
        set(EXPECT)
        | set(FORBIDDEN)
        | set(SENTINELS)
        | set(RENDERED)
        | set(NOTICES)
        | LITERAL
    )
    assert named <= set(PROBES) | set(GENERATED)
    assert not set(GENERATED) & set(PROBES)


@pytest.mark.skipif(
    not shutil.which("libreoffice") and not shutil.which("soffice"),
    reason="requires LibreOffice",
)
@pytest.mark.parametrize("name", PROBES + GENERATED)
def test_korpus_probe(name, client, generated, monkeypatch):
    from src.privacy_service import document_converter as dc

    path = generated[name] if name in generated else KORPUS / name
    if name in DANGER:
        monkeypatch.setattr(
            dc.subprocess,
            "run",
            lambda *a, **kw: pytest.fail(f"Writer started for {name}"),
        )
    from src.privacy_service import word_provenance as wp

    reconciled = []
    apply = wp.apply_provenance

    def keep(soup, body, provenance):
        notices = apply(soup, body, provenance)
        reconciled.append(BeautifulSoup(str(body), "html.parser"))
        return notices

    monkeypatch.setattr(wp, "apply_provenance", keep)
    with path.open("rb") as handle:
        response = client.post(
            "/document/convert", files={"file": (name, handle, MIME)}
        )
    if name in DANGER:
        status, reason = DANGER[name]
        assert response.status_code == status, response.text[:300]
        assert reason in response.json()["detail"]
        return
    assert response.status_code == 200, response.text[:500]
    body = response.json()
    markdown = body["markdown"]
    assert markdown.strip()
    shown = " ".join(rendered_text(markdown).split())
    # Named content survives as Markdown (formatting, HTML tables) or as the
    # text a reader is shown (literal characters are escaped in Markdown).
    for text in EXPECT.get(name, []):
        assert text in markdown or text in shown, (name, text)
    for text in FORBIDDEN.get(name, []):
        assert text not in markdown, (name, text)
    notices = body["metadata"]["word_notices"]
    for prefix in NOTICES.get(name, []):
        assert any(n.startswith(prefix) for n in notices), (name, prefix, notices)
    for part in NO_NOTICES.get(name, []):
        assert not any(part in n for n in notices), (name, part, notices)
    for sentinel in SENTINELS.get(name, []):
        assert shown.count(sentinel) == 1, (name, sentinel, shown.count(sentinel))
    for text in RENDERED.get(name, []):
        assert text in shown, (name, text)
    # Occurrence ledger: every source occurrence once, counted independently.
    raw = path.read_bytes()
    runs = source_runs(raw) if zipfile.is_zipfile(io.BytesIO(raw)) else []
    expected = Counter(_SENTINEL.findall(" ".join(runs)))
    assert Counter(_SENTINEL.findall(shown)) == expected, name
    # ... and per region: text found in the wrong part never balances a loss.
    if name in REGIONS_OPEN or not runs or name.endswith(".doc"):
        return
    (html,) = reconciled
    assert output_regions(html) == source_regions(raw), name
    if name in LITERAL:
        visible = "".join(shown.split())
        for run in runs:
            assert "".join(run.split()) in visible, (name, run)
