"""R2 regressions: real OOXML, Writer export, and actual vision wire bytes."""

import base64
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from PIL import Image

from src.privacy_service.document_converter import convert_docx_bytes
from src.privacy_service.image_format import image_mime, resize_word_image
from src.privacy_service.word_safety import sanitize_docx

FIXTURE = Path(__file__).parents[1] / "fixtures/docx/edge_cases.docx"


def changed_source(**replacements):
    out = io.BytesIO()
    with zipfile.ZipFile(FIXTURE) as src, zipfile.ZipFile(out, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == "word/document.xml":
                text = data.decode()
                for old, new in replacements.items():
                    text = text.replace(old, new)
                data = text.encode()
            dst.writestr(name, data)
    return out.getvalue()


@pytest.fixture(scope="module")
def converted():
    source = changed_source(
        **{
            "Absatz EINS.": "Absatz EINS. 1.5 kWh z0-0",
            "javascript:alert(1)": "javascript:alert(1)",
        }
    )
    return convert_docx_bytes(source)


def test_private_content_absent_and_insertions_present(converted):
    markdown, _, _ = converted
    for secret in ("VERSTECKTERTEXT", "KOMMENTARTEXT", "GELOESCHTERTEXT"):
        assert secret not in markdown
    assert "EINGEFUEGTERTEXT" in markdown
    assert "Kommentierter Satz." in markdown


def test_numbers_links_pages_and_document_parts(converted):
    markdown, meta, _ = converted
    assert "1.5 kWh z0-0" in markdown
    assert "javascript:" not in markdown
    assert "JSLINK" in markdown
    assert "[HTTPLINK](http://evil.invalid/track?a=1)" in markdown
    assert meta["pages"] is None
    assert meta["page_count_status"] == "unavailable"
    assert "Keine Seitenangabe" in meta["page_count_note"]
    for text in ("KOPFZEILENTEXT", "FUSSZEILENTEXT", "FUSSNOTENTEXT", "TEXTFELDINHALT"):
        assert markdown.count(text) == 1


def test_merged_and_nested_cells_retain_source_geometry(converted):
    markdown, _, _ = converted
    table = BeautifulSoup(markdown, "html.parser").find("table")
    rows = table.find_all("tr", recursive=False)
    row2 = rows[1].find_all("td", recursive=False)
    assert row2[0].get_text() == "QUERVERBUNDEN"
    assert row2[0]["colspan"] == "2"
    assert row2[1].get_text() == "C2"
    assert row2[1].get("colspan", "1") == "1"
    assert rows[2].find("td")["rowspan"] == "2"
    assert [c.get_text() for c in rows[3].find_all("td")] == ["B4", "C4 | mit Pipe"]
    assert "INNEN-A" in rows[2].find("table").get_text()
    assert "Verschachtelte Tabelle" in markdown
    assert "</table>\n\nEnde." in markdown


def test_external_and_embedded_images(converted):
    markdown, _, images = converted
    assert "Extern verlinktes Bild nicht geladen" in markdown
    formats = set()
    for name, b64 in images.items():
        mime = image_mime(b64)
        formats.add(mime)
        assert name.endswith(
            {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif"}[mime]
        )
        assert f"]({name})" in markdown
        with Image.open(io.BytesIO(base64.b64decode(b64))) as image:
            assert max(image.size) <= 2000
    assert formats == {"image/png", "image/jpeg", "image/gif"}


@pytest.mark.parametrize("flag", ["vanish", "webHidden"])
def test_style_inheritance_and_explicit_visibility(flag):
    from lxml import etree

    from src.privacy_service.word_safety import W

    raw = changed_source(
        **{
            "<w:vanish/>": '<w:rStyle w:val="Child"/>',
        }
    )
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as src, zipfile.ZipFile(out, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
        dst.writestr(
            "word/styles.xml",
            f'''<w:styles xmlns:w="{W}">
        <w:style w:styleId="Parent"><w:rPr><w:{flag}/></w:rPr></w:style>
        <w:style w:styleId="Child"><w:basedOn w:val="Parent"/></w:style>
        </w:styles>''',
        )
    with zipfile.ZipFile(io.BytesIO(sanitize_docx(out.getvalue()))) as result:
        assert b"VERSTECKTERTEXT" not in result.read("word/document.xml")
        assert b"KOMMENTARTEXT" not in result.read("word/comments.xml")
        etree.fromstring(result.read("word/document.xml"))


@pytest.mark.parametrize(
    "scheme",
    [
        "javascript:alert(1)",
        "data:text/html,evil",
        "file:///etc/hosts",
        "vbscript:evil",
    ],
)
def test_unsafe_links_become_text(scheme, monkeypatch):
    from tests.unit.test_docx_paragraphs import _export_html

    _export_html(monkeypatch, f'<body><p><a href="{scheme}">Caption</a></p></body>')
    markdown, _, _ = convert_docx_bytes((Path(__file__).parents[1] / "fixtures/docx/paragraphs.docx").read_bytes())
    assert markdown == "Caption"


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "GIF"])
@pytest.mark.parametrize("edge", [10, 2200])
def test_resize_keeps_image_type(fmt, edge):
    out = io.BytesIO()
    Image.new("RGB", (edge, 10), "red").save(out, format=fmt)
    raw, ext = resize_word_image(out.getvalue(), 1000)
    assert ext == {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif"}[fmt]
    with Image.open(io.BytesIO(raw)) as image:
        assert image.format == fmt
        assert max(image.size) <= 1000


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "GIF"])
async def test_description_and_triage_send_actual_mime(fmt):
    from src.privacy_service.image_describer import _describe_one
    from src.privacy_service.image_triage import _triage_one_batch

    out = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(out, format=fmt)
    b64 = base64.b64encode(out.getvalue()).decode()
    calls = []

    class Provider:
        async def analyze(self, messages, **kwargs):
            calls.append(messages)
            return SimpleNamespace(content='{"1":{"relevant":true,"label":"photo"}}')

    await _describe_one(Provider(), b64, "test", "")
    await _triage_one_batch(Provider(), [("image", b64)], "test", "")
    expected = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif"}[fmt]
    for messages in calls:
        block = next(b for b in messages[0]["content"] if b["type"] == "image_url")
        assert block["image_url"]["url"].startswith(f"data:{expected};base64,")


def test_explicit_hidden_false_stays_visible():
    content = changed_source(**{"<w:vanish/>": '<w:vanish w:val="false"/>'})
    with zipfile.ZipFile(io.BytesIO(sanitize_docx(content))) as result:
        assert b"VERSTECKTERTEXT" in result.read("word/document.xml")


def test_external_image_relationship_removed_before_writer():
    with zipfile.ZipFile(io.BytesIO(sanitize_docx(FIXTURE.read_bytes()))) as result:
        assert b"example.invalid/x.png" not in result.read(
            "word/_rels/document.xml.rels"
        )
        assert b"Extern verlinktes Bild nicht geladen" in result.read(
            "word/document.xml"
        )


def test_simple_table_escapes_literal_pipe(monkeypatch):
    from tests.unit.test_docx_paragraphs import _export_html

    _export_html(
        monkeypatch,
        "<body><table><tr><td>Label</td><td>Value</td></tr>"
        "<tr><td>A | B</td><td>1.5</td></tr></table></body>",
    )
    markdown, _, _ = convert_docx_bytes((Path(__file__).parents[1] / "fixtures/docx/paragraphs.docx").read_bytes())
    assert r"| A \| B | 1.5 |" in markdown


def test_changed_cell_count_is_loud():
    from src.privacy_service.word_safety import restore_table_geometry

    soup = BeautifulSoup("<table><tr><td>different</td></tr></table>", "html.parser")
    with pytest.raises(ValueError, match="table count changed"):
        restore_table_geometry(soup, FIXTURE.read_bytes())


def test_ambiguous_tables_fail_instead_of_guessing_geometry(converted):
    from copy import deepcopy

    from lxml import etree

    from src.privacy_service.word_safety import NS, W, restore_table_geometry

    output = io.BytesIO()
    with zipfile.ZipFile(FIXTURE) as source, zipfile.ZipFile(output, "w") as target:
        for name in source.namelist():
            raw = source.read(name)
            if name == "word/document.xml":
                root = etree.fromstring(raw)
                table = root.find(".//w:tbl", NS)
                duplicate = deepcopy(table)
                duplicate.find(".//w:gridSpan", NS).set(f"{{{W}}}val", "1")
                table.getparent().append(duplicate)
                raw = etree.tostring(root)
            target.writestr(name, raw)
    soup = BeautifulSoup(converted[0], "html.parser")
    with pytest.raises(ValueError, match="Ambiguous"):
        restore_table_geometry(soup, output.getvalue())
