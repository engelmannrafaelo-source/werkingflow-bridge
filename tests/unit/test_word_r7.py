"""Word R7: every source paragraph is bound to the whole export span between
its own start and end sentinel. Unbound text never counts for any paragraph,
a repair rewrites the whole span, and the ledger balances per region.

The reconcile probes are the Codex R6 probes (reconcile/, reconcile_fields/),
written as Writer exports them with R7 sentinels and then damaged the same
way. The Writer probes (e01–e07) are Codex's own R6 documents."""

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from lxml import etree

from src.privacy_service import word_provenance as wp
from tests.unit.test_word_korpus import output_regions, rendered_text, source_regions
from tests.unit.test_word_r6 import Source, count, para, run

KORPUS = Path(__file__).parents[1] / "fixtures/docx/korpus"
W = wp.W
SPLIT = (
    "<w:p><w:r><w:t>QXKEPTQZ</w:t><w:br/><w:t>QXLOSTQZ</w:t><w:br/>"
    "<w:t>QXTAILQZ</w:t></w:r></w:p>"
)
writer = pytest.mark.skipif(
    not shutil.which("libreoffice") and not shutil.which("soffice"),
    reason="requires LibreOffice",
)


def counts(soup, *texts):
    return {t: count(soup, t) for t in texts}


# --------------------------------------------------------------------------
# A paragraph spread over several export blocks (Codex R6, Nachbesserung 2)
# --------------------------------------------------------------------------


def test_paragraph_over_three_blocks_is_one_occurrence():
    s = Source().body(SPLIT)
    soup, notices = s.apply(
        f"<p>{s.mark(0)}QXKEPTQZ</p><p>QXLOSTQZ</p><p>QXTAILQZ{s.end(0)}</p>"
    )
    assert counts(soup, "QXKEPTQZ", "QXLOSTQZ", "QXTAILQZ") == dict.fromkeys(
        ("QXKEPTQZ", "QXLOSTQZ", "QXTAILQZ"), 1
    )
    assert notices == []


def test_repair_replaces_every_block_of_the_span():
    # reconcile/split_paragraph_partial: the middle fragment is lost, the tail
    # stands in a block of its own. R6 rewrote the first block only: TAIL twice.
    s = Source().body(SPLIT)
    soup, notices = s.apply(f"<p>{s.mark(0)}QXKEPTQZ</p><p>QXTAILQZ{s.end(0)}</p>")
    assert counts(soup, "QXKEPTQZ", "QXLOSTQZ", "QXTAILQZ") == dict.fromkeys(
        ("QXKEPTQZ", "QXLOSTQZ", "QXTAILQZ"), 1
    )
    assert notices == [
        "Dokumenttext: 1 Absatz im Export unvollständig, aus der Quelle ersetzt"
    ]
    # the emptied tail block is gone, the notice stands where the span began
    blocks = soup.find_all("p")
    assert len(blocks) == 1 and "Absatz im Export unvollständig" in blocks[0].text


def test_repair_in_a_shared_block_touches_neither_neighbour_nor_own_rest():
    # reconcile/shared_block_partial: KEPT of the incomplete paragraph and
    # NEXT of another one share a block. R6: KEPT twice.
    s = Source().body(SPLIT + para("QXNEXTQZ"))
    soup, notices = s.apply(
        f"<p>{s.mark(0)}QXKEPTQZ{s.end(0)} {s.span(1, '<i>QXNEXTQZ</i>')}</p>"
    )
    assert counts(soup, "QXKEPTQZ", "QXLOSTQZ", "QXTAILQZ", "QXNEXTQZ") == (
        dict.fromkeys(("QXKEPTQZ", "QXLOSTQZ", "QXTAILQZ", "QXNEXTQZ"), 1)
    )
    assert soup.find("i").text == "QXNEXTQZ"
    assert len(notices) == 1 and "ersetzt" in notices[0]


def test_paragraph_split_over_blocks_shared_with_two_neighbours():
    s = Source().body(para("QXAQZ") + SPLIT + para("QXBQZ"))
    soup, notices = s.apply(
        f"<p>{s.span(0, 'QXAQZ')} {s.mark(1)}QXKEPTQZ</p>"
        f"<p>QXTAILQZ{s.end(1)} {s.span(2, 'QXBQZ')}</p>"
    )
    expected = ("QXAQZ", "QXKEPTQZ", "QXLOSTQZ", "QXTAILQZ", "QXBQZ")
    assert counts(soup, *expected) == dict.fromkeys(expected, 1)
    assert len(notices) == 1
    text = soup.body.get_text()
    assert text.index("QXAQZ") < text.index("QXLOSTQZ") < text.index("QXBQZ")


# --------------------------------------------------------------------------
# Missing or displaced marks never bind foreign text (Codex R6, Nachbesserung 1)
# --------------------------------------------------------------------------


def test_shifted_start_mark_is_no_binding():
    # reconcile/shifted_bookmark_to_next_block: the first paragraph's start
    # mark moved into the next block, its text stays in front. R6: A twice.
    s = Source().body(para("QXAQZ") + para("QXBQZ"))
    soup, notices = s.apply(
        f"<p>QXAQZ{s.end(0)}</p><p>{s.mark(0)}{s.span(1, 'QXBQZ')}</p>"
    )
    assert counts(soup, "QXAQZ", "QXBQZ") == {"QXAQZ": 1, "QXBQZ": 1}
    assert notices == [
        "Dokumenttext: 1 Absatz ohne zuordenbare Herkunft im Export, aus der Quelle"
        " ersetzt"
    ]
    assert "Herkunft im Export nicht zuordenbar" in soup.body.get_text()


@pytest.mark.parametrize("lost", ["start", "end", "both"])
def test_lost_marks_with_intact_text_are_replaced_once_and_named(lost):
    # reconcile/missing_bookmark_with_intact_text, every mark of the first
    # paragraph of the part in turn
    s = Source().body(para("QXAQZ") + para("QXBQZ"))
    first = {
        "start": f"QXAQZ{s.end(0)}",
        "end": f"{s.mark(0)}QXAQZ",
        "both": "QXAQZ",
    }[lost]
    soup, notices = s.apply(f"<p>{first}</p><p>{s.span(1, 'QXBQZ')}</p>")
    assert counts(soup, "QXAQZ", "QXBQZ") == {"QXAQZ": 1, "QXBQZ": 1}
    assert len(notices) == 1 and "ohne zuordenbare Herkunft" in notices[0]


@pytest.mark.parametrize("lost", ["start", "end", "both"])
def test_lost_marks_at_the_end_of_a_part(lost):
    s = Source().body(para("QXAQZ") + para("QXBQZ"))
    last = {
        "start": f"QXBQZ{s.end(1)}",
        "end": f"{s.mark(1)}QXBQZ",
        "both": "QXBQZ",
    }[lost]
    soup, notices = s.apply(
        f"<p>{s.span(0, 'QXAQZ')}</p><p>{last}</p>"
        f'<div id="sdfootnote1"><p>QXBQZ</p></div>'
    )
    # the same text in a note is no occurrence of the body paragraph
    assert counts(soup, "QXAQZ", "QXBQZ") == {"QXAQZ": 1, "QXBQZ": 2}
    assert soup.find("div", id="sdfootnote1").get_text() == "QXBQZ"
    assert len(notices) == 1 and "ohne zuordenbare Herkunft" in notices[0]


def test_lost_paragraph_with_its_marks_is_inserted_at_its_place():
    # reconcile/missing_bookmark_and_text
    s = Source().body(para("QXAQZ") + para("QXBQZ"))
    soup, notices = s.apply(f"<p>{s.span(1, 'QXBQZ')}</p>")
    assert counts(soup, "QXAQZ", "QXBQZ") == {"QXAQZ": 1, "QXBQZ": 1}
    text = soup.body.get_text()
    assert text.index("QXAQZ") < text.index("QXBQZ")
    assert notices == [
        "Dokumenttext: 1 Absatz im Export nicht enthalten, an seiner Stelle aus der"
        " Quelle eingefügt"
    ]


def test_unclaimed_header_text_does_not_stand_in_for_a_lost_note():
    # reconcile/missing_all_note_marks_foreign_unclaimed
    s = Source().body(para("QXSAMEQZ")).footnotes([(1, para("QXSAMEQZ"))])
    soup, notices = s.apply(
        f"<p>{s.span(0, 'QXSAMEQZ')}</p><div title=\"header\"><p>QXSAMEQZ</p></div>"
    )
    assert count(soup, "QXSAMEQZ") == 3
    assert notices == [
        "Fußnoten: 1 Absatz im Export nicht enthalten, aus der Quelle angehängt"
    ]


@pytest.mark.parametrize("field_marks", [True, False])
def test_field_copy_in_the_body_is_kept_and_no_proof_of_the_note(field_marks):
    # reconcile_fields/missing_note_marks_field_copy: a field result with the
    # note's text stays (it is the body's own), the lost note comes back.
    toc = (
        '<w:p><w:fldSimple w:instr="TOC"><w:r><w:t>QXSAMEQZ</w:t></w:r>'
        "</w:fldSimple></w:p>"
    )
    s = Source().body(toc + para("QXBODYQZ")).footnotes([(1, para("QXSAMEQZ"))])
    field_paragraph = s.provenance.texts[0].paragraphs[0]
    assert field_paragraph.units == [] and field_paragraph.key
    copy = s.span(0, "QXSAMEQZ") if field_marks else "QXSAMEQZ"
    soup, notices = s.apply(f"<p>{copy}</p><p>{s.span(1, 'QXBODYQZ')}</p>")
    assert counts(soup, "QXSAMEQZ", "QXBODYQZ") == {"QXSAMEQZ": 2, "QXBODYQZ": 1}
    assert soup.find("p").get_text() == "QXSAMEQZ"  # the field copy, untouched
    assert notices == [
        "Fußnoten: 1 Absatz im Export nicht enthalten, aus der Quelle angehängt"
    ]


def test_markless_notes_do_not_take_text_of_another_part():
    s = (
        Source()
        .body(para("QXBODYQZ"))
        .footnotes([(1, para("QXNOTEAQZ") + para("QXNOTEBQZ"))])
    )
    # Both marks of both note paragraphs are gone, their text with them;
    # the same text stands in the body and in a header without marks.
    soup, notices = s.apply(
        f"<div title=\"header\"><p>QXNOTEAQZ</p></div><p>{s.span(0, 'QXBODYQZ')}"
        " QXNOTEBQZ</p>"
    )
    assert counts(soup, "QXNOTEAQZ", "QXNOTEBQZ") == {"QXNOTEAQZ": 2, "QXNOTEBQZ": 2}
    assert notices == [
        "Fußnoten: 2 Absätze im Export nicht enthalten, aus der Quelle angehängt"
    ]


def test_note_with_lost_start_mark_and_same_text_elsewhere():
    s = (
        Source()
        .body(para("QXBODYQZ"))
        .footnotes([(1, para("QXSAMEQZ") + para("QXTAILQZ"))])
    )
    soup, notices = s.apply(
        f"<div title=\"header\"><p>QXSAMEQZ</p></div><p>{s.span(0, 'QXBODYQZ')}</p>"
        f'<div id="sdfootnote1"><p>QXSAMEQZ{s.end(1)}</p><p>{s.span(2, "QXTAILQZ")}</p>'
        "</div>"
    )
    note = soup.find("div", id="sdfootnote1").get_text()
    assert note.count("QXSAMEQZ") == 1 and count(soup, "QXSAMEQZ") == 2
    assert len(notices) == 1 and notices[0].startswith("Fußnoten: 1 Absatz ohne")


def test_crossing_spans_are_both_unbound():
    s = Source().body(para("QXAQZ") + para("QXBQZ"))
    soup, notices = s.apply(
        f"<p>{s.mark(0)}QXAQZ{s.mark(1)}</p><p>{s.end(0)}QXBQZ{s.end(1)}</p>"
    )
    assert counts(soup, "QXAQZ", "QXBQZ") == {"QXAQZ": 1, "QXBQZ": 1}
    assert len(notices) == 1 and "2 Absätze ohne zuordenbare Herkunft" in notices[0]


def test_unbound_place_never_reaches_into_a_neighbours_span():
    s = Source().body(para("QXAQZ") + para("QXBQZ") + para("QXCQZ"))
    # B lost its end mark; C's span right after it is C's alone.
    soup, notices = s.apply(
        f"<p>{s.span(0, 'QXAQZ')}</p><p>{s.mark(1)}QXBQZ</p><p>{s.span(2, 'QXCQZ')}</p>"
    )
    assert counts(soup, "QXAQZ", "QXBQZ", "QXCQZ") == dict.fromkeys(
        ("QXAQZ", "QXBQZ", "QXCQZ"), 1
    )
    assert soup.find_all("p")[2].get_text() == "QXCQZ"


def test_text_box_with_lost_end_mark_inside_its_anchor():
    box = (
        "<w:r><w:drawing><w:txbxContent>"
        + para("QXBOXQZ")
        + "</w:txbxContent></w:drawing></w:r>"
    )
    s = Source().body(f"<w:p>{run('QXVORQZ')}{box}{run('QXNACHQZ')}</w:p>")
    soup, notices = s.apply(
        f"<p>{s.mark(0)}QXVORQZ<span><p>{s.span(1, 'QXBOXQZ')}</p></span>QXNACHQZ</p>"
    )
    # the anchor's place ends at the next mark that is not one of its boxes
    assert counts(soup, "QXVORQZ", "QXBOXQZ", "QXNACHQZ") == dict.fromkeys(
        ("QXVORQZ", "QXBOXQZ", "QXNACHQZ"), 1
    )
    assert len(notices) == 1


# --------------------------------------------------------------------------
# Sentinels in Writer's export
# --------------------------------------------------------------------------


@pytest.mark.parametrize("displaced", ["w", "wq"])
def test_sentinel_split_by_a_shape_is_restored(displaced):
    # Writer writes an anchored DrawingML shape one character late (h02/c09).
    s = Source().body(para("QXANKERQZ"))
    end = s.end(0)
    soup, notices = s.apply(
        f"<p>{s.mark(0)}QXANKERQZ{displaced}<img src='x.gif'/><span id='Frame1'>"
        f"<p>Rahmen</p></span>{end[len(displaced):]}</p>"
    )
    assert notices == [] and soup.find("img") is not None
    assert soup.find("p").get_text().startswith("QXANKERQZ")
    assert "wq" not in soup.body.get_text() and "test_" not in str(soup)


def test_damaged_sentinel_is_no_mark_and_nothing_of_it_is_shown():
    s = Source().body(para("QXAQZ"))
    damaged = s.end(0)[1:]  # displaced character not in front of it
    soup, notices = s.apply(f"<p>{s.mark(0)}QXAQZ</p><p>{damaged}</p>")
    assert count(soup, "QXAQZ") == 1 and "test_" not in str(soup)
    assert len(notices) == 1


def test_sentinels_are_stripped_from_attributes():
    s = Source().body(para("QXAQZ"))
    soup, notices = s.apply(
        f"<p>{s.span(0, 'QXAQZ')}</p><p><a href='#{s.mark(0)}QXAQZ|outline'>"
        "Verweis</a></p>"
    )
    assert notices == [] and "test_" not in str(soup)


def test_sentinel_runs_cannot_be_hidden_by_a_style():
    provenance = wp.Provenance("wqtest_")
    paragraph = etree.fromstring(f'<w:p xmlns:w="{W}"><w:r><w:t>x</w:t></w:r></w:p>')
    stamper = wp._Stamper(provenance)
    stamper.sentinel(paragraph, "a1")
    stamper.sentinel(paragraph, "z1", at_end=True)
    first, _, last = paragraph
    for node in (first, last):
        assert node.find(f"{{{W}}}rPr/{{{W}}}vanish").get(f"{{{W}}}val") == "0"
    assert first.findtext(f"{{{W}}}t") == "wqtest_a1_"
    assert last.findtext(f"{{{W}}}t") == "wqtest_z1_"
    with pytest.raises(RuntimeError):
        stamper.sentinel(paragraph, "a1")


# --------------------------------------------------------------------------
# Real Writer exports with injected faults (Codex R6 faults/, header_fault/)
# --------------------------------------------------------------------------


def _convert(name, damage=None):
    from src.privacy_service import document_converter as dc

    raw = (KORPUS / name).read_bytes()
    kept = {}
    original = wp.apply_provenance

    def wrapped(soup, body, provenance):
        if damage is not None:
            damage(soup, body, provenance)
        notices = original(soup, body, provenance)
        kept["html"] = BeautifulSoup(str(body), "html.parser")
        return notices

    with patch.object(wp, "apply_provenance", wrapped):
        markdown, meta, _ = dc.convert_docx_bytes(raw)
    return raw, " ".join(rendered_text(markdown).split()), meta, kept["html"]


@writer
def test_page_break_paragraph_with_lost_middle_fragment():
    def damage(soup, body, provenance):
        # only the middle export fragment goes (no claim that Writer does that)
        (node,) = [n for n in body.find_all(string=True) if "QXSECONDQZ" in n]
        node.replace_with(str(node).replace("QXSECONDQZ", ""))

    raw, shown, meta, html = _convert("e01_page_break.docx", damage)
    assert {t: shown.count(t) for t in ("QXFIRSTQZ", "QXSECONDQZ", "QXTHIRDQZ")} == (
        dict.fromkeys(("QXFIRSTQZ", "QXSECONDQZ", "QXTHIRDQZ"), 1)
    )
    assert meta["word_notices"] == [
        "Dokumenttext: 1 Absatz im Export unvollständig, aus der Quelle ersetzt"
    ]
    assert output_regions(html) == source_regions(raw)


@writer
def test_page_break_paragraph_undamaged_spans_three_blocks():
    raw, shown, meta, html = _convert("e01_page_break.docx")
    assert meta["word_notices"] == []
    assert output_regions(html) == source_regions(raw)


@writer
@pytest.mark.parametrize("lost", ["part", "start", "end", "note_start", "note_all"])
def test_six_regions_with_lost_marks_balance_per_region(lost):
    def damage(soup, body, provenance):
        header = [p for p in provenance.parts if p.kind == "header"]
        notes = next(t for t in provenance.texts if t.label == "Fußnoten").paragraphs
        head = next(t for t in provenance.texts if t.name == header[0].name).paragraphs
        names = {
            "part": [header[0].marker],
            "start": [head[0].key],
            "end": [head[-1].end],
            "note_start": [p.key for p in notes],
            "note_all": [k for p in notes for k in (p.key, p.end)],
        }[lost]
        removed = 0
        for anchor in list(body.find_all("a")):
            if anchor.get("name") in {provenance.prefix + n for n in names}:
                anchor.unwrap()
                removed += 1
        for node in list(body.find_all(string=True)):
            text = str(node)
            for name in names:
                if provenance.prefix + name + "_" in text:
                    text = text.replace(provenance.prefix + name + "_", "")
                    removed += 1
            if text != str(node):
                node.replace_with(text)
        assert removed

    raw, shown, meta, html = _convert("e04_same_six_regions.docx", damage)
    assert shown.count("QXEVERYQZ") == 19
    assert output_regions(html) == source_regions(raw)
    if lost != "part":
        notices = meta["word_notices"]
        assert any("Herkunft" in n or "nicht enthalten" in n for n in notices)


@writer
def test_six_regions_lost_continuation_table_comes_back_as_a_table():
    raw, shown, meta, html = _convert("e04_same_six_regions.docx")
    assert shown.count("QXEVERYQZ") == 19
    assert output_regions(html) == source_regions(raw)
    note = html.find("div", id="sdfootnote1")
    assert len(note.find_all("table")) == 2


def test_lost_note_table_inserted_as_source_table():
    cells = "".join(f"<w:tc>{para(f'QXCELL{i}QZ')}</w:tc>" for i in range(2))
    table = f"<w:tbl><w:tr>{cells}</w:tr></w:tbl>"
    raw_root = etree.fromstring(
        f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="1">{para("FN")}{table}'
        "</w:footnote></w:footnotes>"
    )
    s = Source().body(para("QXBODYQZ"))
    stamper = s.stamper
    wp.stamp_part(s.provenance, stamper, "word/footnotes.xml", raw_root)
    wp.record_text(
        s.provenance,
        "word/footnotes.xml",
        raw_root,
        [],
        {"footnote": {"1"}, "endnote": set()},
        stamper,
    )
    soup, notices = s.apply(
        f"<p>{s.span(0, 'QXBODYQZ')}</p>"
        f'<div id="sdfootnote1"><p>{s.span(1, "1 FN")}</p></div>'
    )
    note = soup.find("div", id="sdfootnote1")
    cells = [td.get_text() for td in note.find_all("td")]
    assert cells == ["QXCELL0QZ", "QXCELL1QZ"]
    assert notices == [
        "Fußnoten: 2 Absätze im Export nicht enthalten, an seiner Stelle aus der"
        " Quelle eingefügt"
    ]
    assert "test_" not in str(soup)


def test_header_without_its_part_mark_is_not_appended_twice():
    header = etree.fromstring(f'<w:hdr xmlns:w="{W}">{para("QXHEADQZ")}</w:hdr>')
    s = Source()
    uses = [(1, "default")]
    name = "word/header1.xml"
    wp.stamp_header_footer(s.provenance, s.stamper, name, header, uses)
    wp.record_text(
        s.provenance, name, header, uses, wp.note_references(header), s.stamper
    )
    s.body(para("QXBODYQZ"))
    soup, notices = s.apply(
        f"<div title=\"header\"><p>{s.span(0, 'QXHEADQZ')}</p></div>"
        f"<p>{s.span(1, 'QXBODYQZ')}</p>"
    )
    assert notices == [] and count(soup, "QXHEADQZ") == 1
