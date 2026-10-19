"""Word R6: every source occurrence is accounted for by its own provenance;
collision checks read XML in its declared encoding; literal Markdown stays
literal for the reader."""

import html
import io
import re
import zipfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from lxml import etree

from src.privacy_service import document_converter as dc
from src.privacy_service import word_provenance as wp
from src.privacy_service import word_safety as ws
from src.privacy_service.document_converter import convert_docx_bytes
from src.privacy_service.word_provenance import (
    Provenance,
    _Stamper,
    apply_provenance,
    new_provenance,
    record_text,
)

KORPUS = Path(__file__).parents[1] / "fixtures/docx/korpus"
W = wp.W
MC = wp.MC
WNS = f'xmlns:w="{W}"'
NO_NOTES = {"footnote": set(), "endnote": set()}


def run(text):
    return f"<w:r><w:t xml:space='preserve'>{text}</w:t></w:r>"


def para(*texts):
    return "<w:p>" + "".join(run(t) for t in texts) + "</w:p>"


class Source:
    """Text ledger of hand-written parts; ``stamp`` gives paragraph bookmarks."""

    def __init__(self, stamp=True):
        self.provenance = Provenance("wqtest_")
        self.stamper = _Stamper(self.provenance) if stamp else None

    def body(self, xml):
        xml = f"<w:document {WNS}><w:body>{xml}</w:body></w:document>"
        root = etree.fromstring(xml)
        record_text(
            self.provenance, "word/document.xml", root, [], NO_NOTES, self.stamper
        )
        return self

    def footnotes(self, notes):
        xml = "".join(f'<w:footnote w:id="{i}">{n}</w:footnote>' for i, n in notes)
        root = etree.fromstring(f"<w:footnotes {WNS}>{xml}</w:footnotes>")
        references = {"footnote": {str(i) for i, _ in notes}, "endnote": set()}
        record_text(
            self.provenance, "word/footnotes.xml", root, [], references, self.stamper
        )
        return self

    def mark(self, index):
        """Writer's export of the bookmark of the n-th recorded paragraph."""
        keys = [p.key for t in self.provenance.texts for p in t.paragraphs]
        return f'<a name="wqtest_{keys[index]}"></a>'

    def apply(self, html):
        soup = BeautifulSoup(f"<body>{html}</body>", "html.parser")
        notices = apply_provenance(soup, soup.body, self.provenance)
        return soup, notices


def count(soup, text):
    return soup.body.get_text().count(text)


# --------------------------------------------------------------------------
# Occurrence ledger (Codex R5, Nachbesserung 1)
# --------------------------------------------------------------------------


def test_same_text_in_the_body_does_not_stand_in_for_lost_note_cells():
    cells = "".join(
        f"<w:tc>{para('QXSHAREDQZ')}</w:tc>" for _ in range(2)
    )
    table = f"<w:tbl><w:tr>{cells}</w:tr><w:tr>{cells}</w:tr></w:tbl>"
    source = Source().body(para("QXSHAREDQZ")).footnotes([(1, para("FN") + table)])
    # Writer exports body and note text, but not the table inside the note.
    soup, notices = source.apply(
        f"<p>{source.mark(0)}QXSHAREDQZ</p>"
        f'<div id="sdfootnote1"><p>{source.mark(1)}1 FN</p></div>'
    )
    assert count(soup, "QXSHAREDQZ") == 5
    assert notices == [
        "Fußnoten: 4 Absätze im Export nicht enthalten, aus der Quelle angehängt"
    ]
    # Inserted next to the note it belongs to, not at the end of the body.
    note = soup.find("div", id="sdfootnote1")
    assert note.get_text().count("QXSHAREDQZ") == 4


@pytest.mark.parametrize("stamp", [True, False])
def test_repeated_paragraphs_are_counted_per_occurrence(stamp):
    source = Source(stamp).body(para("QXREPEATQZ") * 2)
    first = source.mark(0) if stamp else ""
    soup, notices = source.apply(f"<p>{first}QXREPEATQZ</p>")
    assert count(soup, "QXREPEATQZ") == 2
    assert notices == [
        "Dokumenttext: 1 Absatz im Export nicht enthalten, aus der Quelle angehängt"
    ]


def test_one_export_occurrence_serves_one_source_paragraph():
    source = Source().body(para("QXAQZ") + para("QXAQZ") + para("QXBQZ"))
    # Writer kept the first and the third bookmark; the second paragraph's
    # text is not in its own region, and the first one's text may not count.
    soup, notices = source.apply(
        f"<p>{source.mark(0)}QXAQZ</p><p>{source.mark(2)}QXBQZ</p>"
    )
    assert count(soup, "QXAQZ") == 2 and count(soup, "QXBQZ") == 1
    assert len(notices) == 1
    # The lost one is inserted right after its predecessor, before QXBQZ.
    text = soup.body.get_text()
    assert text.index("QXAQZ", text.index("QXAQZ") + 1) < text.index("QXBQZ")


@pytest.mark.parametrize("stamp", [True, False])
def test_incomplete_paragraph_replaces_its_own_block_once(stamp):
    source = Source(stamp).body(
        "<w:p><w:r><w:t>QXKEPTQZ</w:t><w:br/><w:t>QXLOSTQZ</w:t></w:r></w:p>"
    )
    first = source.mark(0) if stamp else ""
    soup, notices = source.apply(f"<p>{first}QXKEPTQZ</p>")
    assert count(soup, "QXKEPTQZ") == 1 and count(soup, "QXLOSTQZ") == 1
    assert notices == [
        "Dokumenttext: 1 Absatz im Export unvollständig, aus der Quelle ersetzt"
    ]
    assert "Absatz im Export unvollständig" in soup.p.get_text()


def test_incomplete_paragraph_in_a_shared_block_is_inserted_whole_and_named():
    source = Source().body(
        "<w:p><w:r><w:t>QXKEPTQZ</w:t><w:br/><w:t>QXLOSTQZ</w:t></w:r></w:p>"
        + para("QXNEXTQZ")
    )
    # Writer merged both paragraphs into one block: rewriting it would touch
    # the other paragraph, so the whole source paragraph is inserted, named.
    soup, notices = source.apply(
        f"<p>{source.mark(0)}QXKEPTQZ {source.mark(1)}QXNEXTQZ</p>"
    )
    assert count(soup, "QXLOSTQZ") == 1 and count(soup, "QXNEXTQZ") == 1
    assert notices == [
        "Dokumenttext: 1 Absatz im Export unvollständig, vollständig aus der Quelle"
        " angehängt"
    ]
    assert "Absatz im Export unvollständig, vollständig" in soup.body.get_text()


def test_text_box_text_does_not_make_its_anchor_incomplete():
    box = (
        "<w:r><w:drawing><w:txbxContent>"
        + para("QXBOXQZ")
        + "</w:txbxContent></w:drawing></w:r>"
    )
    source = Source().body(f"<w:p>{run('QXVORQZ')}{box}{run('QXNACHQZ')}</w:p>")
    keys = [p.key for p in source.provenance.texts[0].paragraphs]
    assert source.provenance.texts[0].paragraphs[1].parent == keys[0]
    soup, notices = source.apply(
        f"<p>{source.mark(0)}QXVORQZ<span><p>{source.mark(1)}QXBOXQZ</p></span>"
        "QXNACHQZ</p>"
    )
    assert notices == []


def test_text_box_cannot_borrow_its_anchors_text():
    box = (
        "<w:r><w:drawing><w:txbxContent>"
        + para("QXSAMEQZ")
        + "</w:txbxContent></w:drawing></w:r>"
    )
    source = Source().body(f"<w:p>{run('QXSAMEQZ')}{box}</w:p>")
    # Writer exported the anchor text and the box bookmark, but not the box text.
    soup, notices = source.apply(
        f"<p>{source.mark(0)}QXSAMEQZ<span><p>{source.mark(1)}</p></span></p>"
    )
    assert count(soup, "QXSAMEQZ") == 2
    assert len(notices) == 1


def test_writer_showing_the_fallback_branch_counts_as_present():
    alternatives = (
        f'<mc:AlternateContent xmlns:mc="{MC}">'
        f"<mc:Choice><w:txbxContent>{para('QXBOXQZ')}</w:txbxContent></mc:Choice>"
        f"<mc:Fallback><w:txbxContent>{para('QXBOXQZ')}</w:txbxContent>"
        "</mc:Fallback></mc:AlternateContent>"
    )
    source = Source().body(f"<w:p>{run('QXANKERQZ')}<w:r>{alternatives}</w:r></w:p>")
    soup, notices = source.apply(
        f"<p>{source.mark(0)}QXANKERQZ<span>{source.mark(2)}QXBOXQZ</span></p>"
    )
    assert notices == [] and count(soup, "QXBOXQZ") == 1
    # Neither branch exported: the shown branch (mc:Choice) is inserted once.
    soup, notices = source.apply(f"<p>{source.mark(0)}QXANKERQZ</p>")
    assert count(soup, "QXBOXQZ") == 1 and len(notices) == 1


# --------------------------------------------------------------------------
# Collision check in the declared encoding (Codex R5, Nachbesserung 2)
# --------------------------------------------------------------------------


def _part(encoding, name):
    return (
        f'<?xml version="1.0" encoding="{encoding}"?><w:hdr {WNS}><w:p>'
        f'<w:bookmarkStart w:id="92" w:name="{name}"/></w:p></w:hdr>'
    ).encode(encoding)


@pytest.mark.parametrize(
    "encoding,name",
    [
        ("utf-16", "wqaaaaaaaa_t9r2c3"),
        ("utf-16", "wq&#97;aaaaaaa_t9r2c3"),
        ("utf-8", "wq&#97;&#97;aaaaaa_t9r2c3"),
        ("utf-16", "x-wqaaaaaaaa_-x"),
    ],
)
def test_prefix_collision_is_checked_in_the_declared_encoding(
    encoding, name, monkeypatch
):
    tokens = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(wp.secrets, "token_hex", lambda n: next(tokens))
    provenance = new_provenance({"word/header1.xml": _part(encoding, name)})
    assert provenance.prefix == "wqbbbbbbbb_"
    assert provenance.next_id == 93


def test_prefix_in_utf16_text_is_a_collision(monkeypatch):
    tokens = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(wp.secrets, "token_hex", lambda n: next(tokens))
    raw = (
        f'<?xml version="1.0" encoding="utf-16"?><w:document {WNS}><w:body><w:p>'
        "<w:r><w:t>Text wqaaaaaaaa_ im Absatz</w:t></w:r></w:p></w:body></w:document>"
    ).encode("utf-16")
    assert new_provenance({"word/document.xml": raw}).prefix == "wqbbbbbbbb_"


def test_utf16_package_gets_unique_provenance_names(monkeypatch):
    tokens = iter(["aaaaaaaa", "bbbbbbbb"])
    monkeypatch.setattr(wp.secrets, "token_hex", lambda n: next(tokens))
    prepared = ws.prepare_word((KORPUS / "d12_utf16_hauptteil.docx").read_bytes())
    assert prepared.provenance.prefix == "wqbbbbbbbb_"
    names = []
    with zipfile.ZipFile(io.BytesIO(prepared.content)) as package:
        for member in package.namelist():
            if member.endswith(".xml"):
                root = etree.fromstring(package.read(member))
                names += root.xpath("//w:bookmarkStart/@w:name", namespaces=wp.NS)
    ours = [n for n in names if n.startswith(prepared.provenance.prefix)]
    assert ours and len(ours) == len(set(ours))
    assert not [n for n in names if n not in ours and n.startswith("wqbbbbbbbb_")]


def test_stamper_never_issues_a_name_twice():
    provenance = Provenance("wqtest_")
    stamper = _Stamper(provenance)
    paragraph = etree.fromstring(f"<w:p {WNS}/>")
    stamper.bookmark(paragraph, "s1")
    with pytest.raises(RuntimeError):
        stamper.bookmark(paragraph, "s1")


# --------------------------------------------------------------------------
# Literal Markdown stays literal (Codex R5, Nachbesserung 3)
# --------------------------------------------------------------------------

LITERALS = [
    "*QXSTARSQZ*",
    "**QXDOUBLEQZ**",
    "[QXLINKQZ](https://example.invalid/)",
    "`QXCODEQZ`",
    "_QXUNDERQZ_ und __QXDUNDERQZ__",
    "~~QXSTRIKEQZ~~",
    "# QXHEADQZ",
    "> QXQUOTEQZ",
    "- QXDASHQZ",
    "+ QXPLUSQZ",
    "1. QXNUMQZ",
    "2) QXPARENQZ",
    "---",
    "a | b QXPIPEQZ",
    "C:\\Pfad\\*QXBACKQZ*",
    "![QXIMGQZ](bild.png)",
    "[QXREFQZ]: https://example.invalid/",
    "<b>QXTAGQZ</b> &amp; &#65;",
    "<https://example.invalid/> QXAUTOQZ",
]


def _shown(markdown):
    markdown_it = pytest.importorskip("markdown_it")
    html = markdown_it.MarkdownIt("commonmark").enable("table").render(markdown)
    return BeautifulSoup(html, "html.parser")


def test_literal_markdown_is_shown_verbatim(monkeypatch):
    from tests.unit.test_docx_paragraphs import _export_html

    paragraphs = "".join(f"<p>{html.escape(t)}</p>" for t in LITERALS)
    lines = "Titel<br>===<br>- Punkt<br>1. Zahl"
    cell = "<table><tr><td>*QXCELLQZ* | x</td><td>`y`</td></tr></table>"
    _export_html(monkeypatch, f"<body>{paragraphs}<p>{lines}</p>{cell}</body>")
    markdown, _, _ = convert_docx_bytes((KORPUS / "spaces.docx").read_bytes())
    shown = _shown(markdown)
    blocks = [p.get_text() for p in shown.find_all("p")]
    for literal in LITERALS:
        assert literal in blocks, (literal, markdown)
    assert not shown.find(["h1", "h2", "ul", "ol", "blockquote", "hr", "em"])
    assert not shown.find(["strong", "code", "a", "img", "del"])
    assert "Titel\n===\n- Punkt\n1. Zahl" in blocks
    cells = [c.get_text() for c in shown.find_all(["td", "th"])]
    assert cells == ["*QXCELLQZ* | x", "`y`"]


def test_literal_text_escapes_only_where_markdown_reads_syntax():
    assert dc._literal_text("2026-10-19 und 5.3 kWh") == "2026-10-19 und 5.3 kWh"
    assert dc._literal_text("- Punkt") == "\\- Punkt"
    assert dc._literal_text("12. Mai") == "12\\. Mai"


# A5 with two colliding draws and two merged tables, through real Writer.
def test_placeholder_is_redrawn_twice(monkeypatch):
    tokens = iter(["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"])
    drawn = []

    def token_hex(n):
        drawn.append(next(tokens))
        return drawn[-1]

    monkeypatch.setattr(dc, "secrets", SimpleNamespace(token_hex=token_hex))
    raw = (KORPUS / "d08_platzhalter_zwei_ziehungen.docx").read_bytes()
    markdown, _, _ = convert_docx_bytes(raw)
    shown = _shown(markdown).get_text()
    assert "QXPLACEQZ aaaaaaaaaaaa bbbbbbbbbbbb WORDTABLEaaaaaaaaaaaaN0END" in shown
    assert Counter(re.findall(r"QX[A-Z0-9]+?QZ", shown)) == Counter(
        ["QXPLACEQZ", "QXCOMPLEX0QZ", "QXCOMPLEX1QZ"]
    )
    assert drawn == ["aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"]
