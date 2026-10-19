"""Element provenance between the Word source and Writer's HTML export.

Writer's HTML is the text carrier (lists, headings, fields, footnotes), the
OOXML source is the authority for table geometry. R3 paired both by element
order, so any structure Writer counts differently (header variants, text
boxes with mc:Fallback copies, content controls, adjacent tables) failed the
whole document. Here every source element carries its own identity instead:
before Writer runs, a bookmark with a per-conversion prefix is written into
the first paragraph of every origin cell and of every header/footer part.
Writer exports bookmarks as ``<a name>``, so each HTML cell names the source
cell it came from. Problems stay local to one table or part.

Identity alone cannot see what Writer never exports (a table in a footnote,
the text of a merged continuation cell). So every source paragraph of a
displayed part is bound to its own span of the export: a start and an end
sentinel — text runs, not bookmarks: Writer moves end-of-paragraph
bookmarks in front of the text and drops bookmarks after text frames, but
exports text where it stands — enclose everything Writer made of it, over
any number of blocks. The span must hold the paragraph's stored text units;
text outside every span is unbound and never counts for any paragraph. An
incomplete span is rewritten whole, a paragraph whose marks are missing or
out of order is replaced at the place the surrounding marks give, one
without a known place is inserted next to its neighbour — always with a
visible notice and named in ``word_notices``, never silently.
"""

from __future__ import annotations

import logging
import posixpath
import re
import secrets
from bisect import bisect_right
from dataclasses import dataclass, field
from urllib.parse import unquote

from lxml import etree

logger = logging.getLogger("privacy-service.word-provenance")

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS = {"w": W, "r": R}

UNCERTAIN_TABLE_NOTICE = (
    "[Tabellenstruktur nicht sicher zuordenbar: Zellen je Zeile in Dokumentreihenfolge,"
    " "
    "ohne Spaltenzuordnung]"
)
VARIANT_LABELS = {"first": "erste Seite", "even": "gerade Seiten", "default": ""}
CONTINUATION_NOTICE = "[Weiterer Text aus einer vertikal verbundenen Folgezelle:]"


def part_target(source: str, target: str, names=None) -> str | None:
    """Resolve an OPC relationship target to a package member name.

    One resolution for every internal relationship: relative to the source
    part, package-absolute (leading slash) and percent-encoded alike. A target
    outside the package resolves to ``None``. With ``names``, the member must
    exist (encoded and decoded spelling are both tried), otherwise ``None``.
    """
    target = target.split("#", 1)[0].replace("\\", "/")
    candidates = []
    for spelling in dict.fromkeys([unquote(target), target]):
        if spelling.startswith("/"):
            path = spelling.lstrip("/")
        else:
            path = posixpath.join(posixpath.dirname(source), spelling)
        path = posixpath.normpath(path) if path else ""
        if not path or path in {".", ".."} or path.startswith(("../", "/")):
            continue
        candidates.append(path)
    if names is None:
        return candidates[0] if candidates else None
    return next((c for c in candidates if c in names), None)


def q(tag: str) -> str:
    return f"{{{W}}}{tag}"


# Containers that Word allows around rows/cells without changing geometry.
_TRANSPARENT = {q("sdt"), q("sdtContent"), q("customXml"), q("smartTag")}


def _children(node, wanted: str):
    """Direct children with tag ``wanted``, looking through content controls."""
    for child in node:
        if child.tag == q(wanted):
            yield child
        elif child.tag in _TRANSPARENT:
            yield from _children(child, wanted)


def _first_paragraph(cell):
    for child in cell:
        if child.tag == q("p"):
            return child
        if child.tag in _TRANSPARENT:
            found = _first_paragraph(child)
            if found is not None:
                return found
    return None


def _val(node, path: str, default: int) -> int:
    found = node.find(path, NS)
    if found is None:
        return default
    try:
        return int(found.get(q("val"), default))
    except ValueError:
        return default


@dataclass
class SourceCell:
    name: str
    colspan: int
    rowspan: int = 1
    node: object = field(default=None, repr=False, compare=False)


@dataclass
class SourceRow:
    grid_before: int
    cells: list[SourceCell]


@dataclass
class SourceTable:
    key: str
    part: str
    rows: list[SourceRow]
    valid: bool
    reason: str = ""
    # (origin cell, continuation cell node) of every valid vertical merge
    continuations: list = field(default_factory=list, repr=False, compare=False)
    node: object = field(default=None, repr=False, compare=False)

    @property
    def spanned(self) -> bool:
        return any(c.colspan > 1 or c.rowspan > 1 for r in self.rows for c in r.cells)


@dataclass
class SourcePart:
    name: str
    kind: str  # header | footer
    marker: str | None
    uses: list[tuple[int, str]]
    html: str


@dataclass
class SourceParagraph:
    """One source paragraph: display text and its stored text units."""

    text: str
    units: list[str]
    key: str | None = None  # start sentinel name; None when not stamped
    end: str | None = None  # end sentinel name, after the paragraph's last run
    parent: str | None = None  # key of the paragraph holding its text box
    # (mc:AlternateContent group, branch index) of every enclosing alternative
    branches: tuple[tuple[str, int], ...] = ()
    table: str | None = None  # key of the outermost source table it stands in


@dataclass
class SourceText:
    """Text ledger of one displayed part."""

    name: str
    kind: str  # body | header | footer | notes
    label: str
    paragraphs: list[SourceParagraph]


@dataclass
class Provenance:
    prefix: str
    tables: dict[str, SourceTable] = field(default_factory=dict)
    parts: list[SourcePart] = field(default_factory=list)
    texts: list[SourceText] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    macros_removed: bool = False
    next_id: int = 1  # first free w:id for new bookmarks

    def cell_pattern(self):
        return re.compile(re.escape(self.prefix) + r"t(\d+)r(\d+)c(\d+)$")


def _scan(raw_parts: dict[str, bytes], prefix: str) -> int | None:
    """Highest w:id in the package, or None if ``prefix`` occurs anywhere.

    Every XML part is parsed in its declared encoding first (UTF-16 as well
    as UTF-8, character references resolved), so names, attribute values
    and text are compared as Writer will read them — no raw byte prefilter.
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    highest = 0
    for name, raw in raw_parts.items():
        if not name.endswith((".xml", ".rels")):
            continue
        for node in etree.fromstring(raw, parser).iter():
            texts = [node.text, node.tail, *node.attrib.values()]
            if any(prefix in t for t in texts if t):
                return None
            value = node.get(q("id"))
            if value is not None and value.lstrip("-").isdigit():
                highest = max(highest, int(value))
    return highest


def new_provenance(raw_parts: dict[str, bytes]) -> Provenance:
    while True:
        prefix = "wq" + secrets.token_hex(4) + "_"
        highest = _scan(raw_parts, prefix)
        if highest is not None:
            return Provenance(prefix, next_id=highest + 1)


def table_geometry(table, key: str = "", part: str = "", stamp=None) -> SourceTable:
    """Read OOXML geometry; ``stamp(cell_node, name)`` marks origin cells."""
    rows: list[SourceRow] = []
    active: dict[int, SourceCell] = {}
    valid, reason = True, ""
    continuations = []
    for r, row in enumerate(_children(table, "tr")):
        column = _val(row, "w:trPr/w:gridBefore", 0)
        source_row = SourceRow(column, [])
        current: dict[int, SourceCell] = {}
        for cell in _children(row, "tc"):
            span = max(1, _val(cell, "w:tcPr/w:gridSpan", 1))
            merge = cell.find("w:tcPr/w:vMerge", NS)
            if merge is not None and merge.get(q("val")) != "restart":
                origin = active.get(column)
                if origin is not None and origin.colspan == span:
                    origin.rowspan += 1
                    current[column] = origin
                    continuations.append((origin, cell))
                    column += span
                    continue
                valid, reason = False, "vertikale Verbindung ohne Ursprungszelle"
            origin = SourceCell(f"t{key}r{r}c{len(source_row.cells)}", span, node=cell)
            source_row.cells.append(origin)
            if merge is not None:
                current[column] = origin
            if stamp is not None:
                stamp(cell, origin.name)
            column += span
        active = current
        rows.append(source_row)
    return SourceTable(key, part, rows, valid, reason, continuations, table)


class _Stamper:
    def __init__(self, provenance: Provenance, next_id: int | None = None):
        self.provenance = provenance
        self.next_id = provenance.next_id if next_id is None else next_id
        self.names: set[str] = set()

    def bookmark(self, paragraph, name: str) -> None:
        if name in self.names:
            raise RuntimeError(f"Provenance bookmark issued twice: {name}")
        self.names.add(name)
        start = etree.Element(q("bookmarkStart"))
        start.set(q("id"), str(self.next_id))
        start.set(q("name"), self.provenance.prefix + name)
        end = etree.Element(q("bookmarkEnd"))
        end.set(q("id"), str(self.next_id))
        self.next_id += 1
        index = 1 if len(paragraph) and paragraph[0].tag == q("pPr") else 0
        paragraph.insert(index, start)
        paragraph.insert(index + 1, end)

    def sentinel(self, paragraph, name: str, at_end: bool = False) -> None:
        """A text run naming the paragraph: Writer exports text where it stands."""
        if name in self.names:
            raise RuntimeError(f"Provenance sentinel issued twice: {name}")
        self.names.add(name)
        run = etree.Element(q("r"))
        properties = etree.SubElement(run, q("rPr"))
        etree.SubElement(properties, q("vanish")).set(q("val"), "0")
        etree.SubElement(properties, q("specVanish")).set(q("val"), "0")
        etree.SubElement(run, q("t")).text = self.provenance.prefix + name + "_"
        if at_end:
            paragraph.append(run)
        else:
            index = 1 if len(paragraph) and paragraph[0].tag == q("pPr") else 0
            paragraph.insert(index, run)

    def cell(self, cell, name: str) -> None:
        paragraph = _first_paragraph(cell)
        if paragraph is not None:
            self.bookmark(paragraph, name)


def stamp_part(provenance: Provenance, stamper: _Stamper, name: str, root) -> None:
    """Give every table in a Word part its own identity (Choice and Fallback alike)."""
    for table in list(root.iter(q("tbl"))):
        key = str(len(provenance.tables) + 1)
        geometry = table_geometry(table, key, name, stamper.cell)
        provenance.tables["t" + key] = geometry
        for origin, cell in geometry.continuations:
            _keep_continuation(provenance, origin.node, cell)


def _keep_continuation(provenance: Provenance, origin, cell) -> None:
    """A continuation cell is no export cell: its stored text moves to the origin."""
    blocks = [c for c in cell if c.tag in {q("p"), q("tbl"), *_TRANSPARENT}]
    if not any(t.text and t.text.strip() for b in blocks for t in b.iter(q("t"))):
        return
    logger.warning("Text in a vertical merge continuation cell kept in its origin cell")
    provenance.notices.append(
        "Text einer vertikal verbundenen Folgezelle in die Ursprungszelle übernommen"
    )
    note = etree.SubElement(origin, q("p"))
    etree.SubElement(etree.SubElement(note, q("r")), q("t")).text = CONTINUATION_NOTICE
    for block in blocks:
        origin.append(block)
    if origin[-1].tag != q("p"):
        etree.SubElement(origin, q("p"))
    etree.SubElement(cell, q("p"))


# --------------------------------------------------------------------------
# Header/footer variants. Writer's HTML exports one header and one footer.
# --------------------------------------------------------------------------


def header_footer_uses(
    main_root, main_rels, main: str
) -> dict[str, list[tuple[int, str]]]:
    """Map header/footer part names to the (section, type) uses in the body."""
    targets = {}
    for rel in main_rels:
        kind, target = rel.get("Type", ""), rel.get("Target", "")
        if (
            kind.endswith(("/header", "/footer"))
            and rel.get("TargetMode") != "External"
        ):
            targets[rel.get("Id")] = part_target(main, target)
    uses: dict[str, list[tuple[int, str]]] = {}
    for section, sect in enumerate(main_root.iter(q("sectPr")), start=1):
        for ref in [
            *sect.findall("w:headerReference", NS),
            *sect.findall("w:footerReference", NS),
        ]:
            target = targets.get(ref.get(f"{{{R}}}id"))
            if target:
                uses.setdefault(target, []).append(
                    (section, ref.get(q("type"), "default"))
                )
    return uses


def part_label(part: SourcePart) -> str:
    base = "Kopfzeile" if part.kind == "header" else "Fußzeile"
    labels = []
    for section, kind in part.uses:
        words = [f"Abschnitt {section}"] if section > 1 else []
        if VARIANT_LABELS.get(kind):
            words.append(VARIANT_LABELS[kind])
        labels.append(", ".join(words) if words else "Standard")
    return f"{base} ({'; '.join(dict.fromkeys(labels))})"


def stamp_header_footer(
    provenance: Provenance,
    stamper: _Stamper,
    name: str,
    root,
    uses: list[tuple[int, str]],
) -> None:
    kind = "header" if root.tag == q("hdr") else "footer"
    marker = None
    paragraph = next((p for p in root.iter(q("p")) if not _in_fallback(p)), None)
    if paragraph is not None:
        marker = f"p{len(provenance.parts) + 1}"
        stamper.bookmark(paragraph, marker)
    provenance.parts.append(
        SourcePart(name, kind, marker, uses, render_source_html(root))
    )


def _in_fallback(node) -> bool:
    return any(a.tag == f"{{{MC}}}Fallback" for a in node.iterancestors())


# --------------------------------------------------------------------------
# Text ledger: what each displayed part says, independent of Writer.
# --------------------------------------------------------------------------

# Zero-width markers: they neither carry text nor separate it.
_SILENT = {
    q(t)
    for t in (
        "pPr",
        "rPr",
        "bookmarkStart",
        "bookmarkEnd",
        "proofErr",
        "permStart",
        "permEnd",
        "lastRenderedPageBreak",
        "instrText",
        "delText",
        "delInstrText",
    )
}
# Containers whose text continues the surrounding text.
_INLINE = {
    q(t)
    for t in (
        "r",
        "hyperlink",
        "smartTag",
        "sdt",
        "sdtContent",
        "customXml",
        "ins",
        "moveTo",
        "dir",
        "bdo",
    )
} | {f"{{{MC}}}AlternateContent", f"{{{MC}}}Choice"}
_OWN_BLOCKS = {q("p"), q("tbl"), q("txbxContent")}


class _Ledger:
    """Units are contiguous stored text, split wherever Writer may insert or
    rewrite something (note marks, fields, pictures, tabs, breaks). Field
    results are skipped: Writer recomputes them (PAGE, DATE, TOC)."""

    def __init__(self):
        self.fields: list[str] = []

    def paragraph(self, node) -> SourceParagraph | None:
        self.units: list[str] = []
        self.current: list[str] = []
        self.display: list[str] = []
        self._walk(node)
        self._flush()
        text = " ".join("".join(self.display).split())
        if not self.units and not text:
            return None
        # A paragraph of field results only has no units to account for,
        # but it is marked as well: its text belongs to it and to nothing else.
        return SourceParagraph(text, self.units)

    def _flush(self) -> None:
        unit = "".join("".join(self.current).split())
        if unit:
            self.units.append(unit)
        self.current = []

    def _walk(self, node) -> None:
        for child in node:
            tag = child.tag
            if tag == f"{{{MC}}}Fallback" or tag in _SILENT or not isinstance(tag, str):
                continue
            if tag in _OWN_BLOCKS:
                self._flush()  # nested text boxes are paragraphs of their own
            elif tag == q("t"):
                text = child.text or ""
                self.display.append(text)
                if self.fields:
                    self._flush()
                else:
                    self.current.append(text)
            elif tag == q("fldChar"):
                self._flush()
                kind = child.get(q("fldCharType"))
                if kind == "begin":
                    self.fields.append("instr")
                elif kind == "separate" and self.fields:
                    self.fields[-1] = "result"
                elif kind == "end" and self.fields:
                    self.fields.pop()
            elif tag == q("fldSimple"):
                self._flush()
                self.fields.append("result")
                self._walk(child)
                self.fields.pop()
                self._flush()
            elif tag in _INLINE:
                self._walk(child)
            else:
                self.display.append(" " if tag in {q("tab"), q("br"), q("cr")} else "")
                self._flush()
                self._walk(child)
                self._flush()


def note_references(main_root) -> dict[str, set[str]]:
    return {
        kind: {r.get(q("id")) for r in main_root.iter(q(f"{kind}Reference"))}
        for kind in ("footnote", "endnote")
    }


def _branches(paragraph, groups: dict, part: str) -> tuple[tuple[str, int], ...]:
    """The mc:AlternateContent branches a paragraph lies in, outermost first."""
    found = []
    node = paragraph
    for ancestor in paragraph.iterancestors():
        if ancestor.tag == f"{{{MC}}}AlternateContent":
            group = groups.setdefault(ancestor, f"{part}#{len(groups) + 1}")
            found.append((group, ancestor.index(node)))
        node = ancestor
    return tuple(reversed(found))


def record_text(
    provenance: Provenance, name: str, root, uses, references, stamper=None
) -> None:
    """Keep the text ledger of a part Word displays (call after all edits).

    With a ``stamper``, every paragraph with text gets its own bookmark, so
    its occurrence in Writer's export can be found by identity. Paragraphs
    of every mc:AlternateContent branch are kept with their branch: Writer
    shows one branch, and the branch it showed is the one that counts.
    """
    ledger = _Ledger()
    tag = root.tag
    if tag == q("document"):
        kind, label, scopes = "body", "Dokumenttext", [root]
    elif tag in {q("hdr"), q("ftr")} and uses:
        kind = "header" if tag == q("hdr") else "footer"
        label, scopes = part_label(SourcePart(name, kind, None, uses, "")), [root]
    elif tag in {q("footnotes"), q("endnotes")}:
        note = "footnote" if tag == q("footnotes") else "endnote"
        kind, label = "notes", "Fußnoten" if note == "footnote" else "Endnoten"
        # separators carry a type and are always shown; others only if referenced
        scopes = [
            n
            for n in root.iter(q(note))
            if n.get(q("type")) or n.get(q("id")) in references[note]
        ]
    else:
        return
    entries: list[SourceParagraph] = []
    keys: dict = {}
    groups: dict = {}
    tables = {id(t.node): k for k, t in provenance.tables.items() if t.node is not None}
    for scope in scopes:
        for paragraph in list(scope.iter(q("p"))):
            entry = ledger.paragraph(paragraph)
            if entry is None:
                continue
            entry.branches = _branches(paragraph, groups, name)
            entry.parent = next(
                (keys[a] for a in paragraph.iterancestors() if a in keys), None
            )
            outer = [a for a in paragraph.iterancestors(q("tbl")) if id(a) in tables]
            entry.table = tables[id(outer[-1])] if outer else None
            if stamper is not None:
                number = stamper.next_id
                stamper.next_id += 1
                entry.key, entry.end = f"a{number}", f"z{number}"
                stamper.sentinel(paragraph, entry.key)
                stamper.sentinel(paragraph, entry.end, at_end=True)
                keys[paragraph] = entry.key
            entries.append(entry)
    if entries:
        provenance.texts.append(SourceText(name, kind, label, entries))


# --------------------------------------------------------------------------
# Source rendering (for content Writer did not export).
# --------------------------------------------------------------------------


def _paragraph(node, out, blocks) -> None:
    for child in node:
        tag = child.tag
        if tag == f"{{{MC}}}Fallback" or tag in {
            q("instrText"),
            q("delText"),
            q("pPr"),
            q("rPr"),
        }:
            continue
        if tag == q("t"):
            _append_text(out, child.text or "")
        elif tag == q("tab"):
            _append_text(out, " ")
        elif tag in {q("br"), q("cr")}:
            etree.SubElement(out, "br")
        elif tag == q("noBreakHyphen"):
            _append_text(out, "‑")
        elif tag == q("txbxContent"):
            blocks.append(child)
        else:
            _paragraph(child, out, blocks)


def _append_text(element, text: str) -> None:
    if len(element):
        element[-1].tail = (element[-1].tail or "") + text
    else:
        element.text = (element.text or "") + text


def _blocks(node, out) -> None:
    for child in node:
        if child.tag == q("p"):
            paragraph = etree.SubElement(out, "p")
            boxes: list = []
            _paragraph(child, paragraph, boxes)
            if not "".join(paragraph.itertext()).strip() and not len(paragraph):
                out.remove(paragraph)
            for box in boxes:
                _blocks(box, out)
        elif child.tag == q("tbl"):
            _table(child, out)
        elif child.tag in _TRANSPARENT:
            _blocks(child, out)


def _table(node, out) -> None:
    geometry = table_geometry(node)
    html = etree.SubElement(out, "table")
    for source_row in geometry.rows:
        tr = etree.SubElement(html, "tr")
        if source_row.grid_before:
            etree.SubElement(tr, "td", colspan=str(source_row.grid_before))
        for source_cell in source_row.cells:
            td = etree.SubElement(tr, "td")
            if geometry.valid:
                for attr in ("colspan", "rowspan"):
                    if getattr(source_cell, attr) > 1:
                        td.set(attr, str(getattr(source_cell, attr)))
            _blocks(source_cell.node, td)


def render_source_html(root) -> str:
    container = etree.Element("div")
    _blocks(root, container)
    if not "".join(container.itertext()).strip():
        return ""
    return "".join(
        etree.tostring(c, method="html", encoding="unicode") for c in container
    )


# --------------------------------------------------------------------------
# HTML side: rebuild tables from source geometry, append missing parts.
# --------------------------------------------------------------------------


def _own_markers(soup_cell, pattern):
    found = []
    for anchor in soup_cell.find_all("a"):
        name = anchor.get("name") or anchor.get("id") or ""
        match = pattern.match(name)
        if match and anchor.find_parent(["td", "th"]) is soup_cell:
            found.append(tuple(int(g) for g in match.groups()))
    return found


def _has_content(soup_cell) -> bool:
    return (
        bool(soup_cell.get_text(strip=True))
        or soup_cell.find(["img", "table"]) is not None
    )


def _direct_rows(table):
    return [r for r in table.find_all("tr") if r.find_parent("table") is table]


def _flatten(soup, table, reason: str, notices: list[str]):
    """Keep every cell's text and images, but claim no column assignment."""
    logger.warning("Word table emitted without geometry: %s", reason)
    notices.append(f"Tabelle ohne Spaltenzuordnung ausgegeben: {reason}")
    # Still cell text, only without geometry: the box says so for the ledger.
    box = soup.new_tag("div", attrs={"data-word-region": "Zelle"})
    note = soup.new_tag("p")
    note.string = UNCERTAIN_TABLE_NOTICE
    box.append(note)
    for row in _direct_rows(table):
        texts, images = [], []
        for cell in row.find_all(["td", "th"], recursive=False):
            images.extend(cell.find_all("img"))
            text = cell.get_text(" ", strip=True)
            if text:
                texts.append(text)
        if not texts and not images:
            continue
        line = soup.new_tag("p")
        line.append(" · ".join(texts))
        for image in images:
            line.append(" ")
            line.append(image.extract())
        box.append(line)
    table.replace_with(box)


def _verify(table, provenance, pattern, rebuilt):
    """Return [(source table, [(source row index, html row)])] or a reason."""
    segments: list[tuple[str, list[tuple[int, object]]]] = []
    for row in _direct_rows(table):
        keys = set()
        for cell in row.find_all(["td", "th"], recursive=False):
            markers = _own_markers(cell, pattern)
            if len(markers) > 1:
                return "eine Exportzelle enthält mehrere Quellzellen"
            if not markers:
                if _has_content(cell):
                    return "Zelle mit Inhalt ohne Quellzuordnung"
                continue
            keys.add(markers[0][:2])
        if not keys:
            continue
        if len(keys) > 1:
            return "eine Exportzeile mischt Quellzeilen"
        ((t, r),) = keys
        key = f"t{t}"
        if segments and segments[-1][0] == key:
            segments[-1][1].append((r, row))
        else:
            segments.append((key, [(r, row)]))
    for key, rows in segments:
        source = provenance.tables.get(key)
        if source is None:
            return "unbekannte Quelltabelle"
        if key in rebuilt or [k for k, _ in segments].count(key) > 1:
            return "Quelltabelle im Export geteilt"
        if not source.valid:
            return source.reason
        expected = [i for i, r in enumerate(source.rows) if r.cells]
        if [r for r, _ in rows] != expected:
            return "Zeilenfolge weicht von der Quelle ab"
        for r, row in rows:
            cells = [
                m[2]
                for c in row.find_all(["td", "th"], recursive=False)
                for m in _own_markers(c, pattern)
            ]
            if cells != list(range(len(source.rows[r].cells))):
                return "Zellfolge weicht von der Quelle ab"
    return segments


def _rebuild(soup, table, segments, provenance, pattern, rebuilt):
    replacements = []
    for key, rows in segments:
        source = provenance.tables[key]
        html_rows = dict(rows)
        new_table = soup.new_tag("table")
        for index, source_row in enumerate(source.rows):
            tr = soup.new_tag("tr")
            new_table.append(tr)
            if source_row.grid_before:
                tr.append(
                    soup.new_tag("td", attrs={"colspan": str(source_row.grid_before)})
                )
            exported = {}
            if index in html_rows:
                for cell in html_rows[index].find_all(["td", "th"], recursive=False):
                    for marker in _own_markers(cell, pattern):
                        exported[marker[2]] = cell
            for c, source_cell in enumerate(source_row.cells):
                old = exported[c]
                td = soup.new_tag(old.name)
                for attr in ("colspan", "rowspan"):
                    if getattr(source_cell, attr) > 1:
                        td[attr] = str(getattr(source_cell, attr))
                for child in list(old.contents):
                    td.append(child.extract())
                tr.append(td)
        rebuilt.add(key)
        replacements.append(new_table)
    table.replace_with(*replacements)


def apply_provenance(soup, body, provenance: Provenance) -> list[str]:
    """Bind the export to its source paragraphs, rebuild every table from its
    source and insert whatever is missing — always visibly."""
    notices = list(provenance.notices)
    pattern = provenance.cell_pattern()
    rebuilt: set[str] = set()
    normalize_sentinels(body, provenance.prefix)
    # Presence is a fact of Writer's export; record it before any local
    # rebuild or flattening drops the marks.
    present = set()
    for anchor in body.find_all("a"):
        name = anchor.get("name") or anchor.get("id") or ""
        if name.startswith(provenance.prefix):
            present.add(name[len(provenance.prefix) :])
    # Text is accounted for on Writer's own export, by span; a part
    # appended whole from the source is accounted for as that part.
    appended = {p.name for p in _missing_parts(provenance, present)}
    plan = _reconcile(body, provenance, appended)
    emptied = _rewrite(soup, body, plan, provenance.prefix, provenance.tables, present)
    counts: dict[str, dict[str, int]] = {}
    for occurrence in plan.occurrences:
        if occurrence.replaced:
            label = occurrence.part.label
            kinds = counts.setdefault(label, {})
            kinds[occurrence.status] = kinds.get(occurrence.status, 0) + 1
    for label, kinds in counts.items():
        for kind, count in kinds.items():
            logger.warning(
                "Word paragraph %s in Writer export, rewritten: %s (%d)",
                kind,
                label,
                count,
            )
            notices.append(f"{label}: {_count(count)} {_REWRITE_NOTICE[kind]}")
    # Innermost first: a rebuilt inner table moves along with its cell content.
    for table in reversed(body.find_all("table")):
        if table.find_parent("table", attrs={_FROM_SOURCE: True}) or table.get(
            _FROM_SOURCE
        ):
            continue
        has_markers = any(
            _own_markers(c, pattern)
            for row in _direct_rows(table)
            for c in row.find_all(["td", "th"], recursive=False)
        )
        if not has_markers:
            spanned = (
                table.find(
                    lambda tag: (
                        tag.name in {"td", "th"}
                        and (
                            tag.get("colspan", "1") != "1"
                            or tag.get("rowspan", "1") != "1"
                        )
                    )
                )
                is not None
            )
            if spanned:
                _flatten(soup, table, "Exporttabelle ohne Quellzuordnung", notices)
            continue
        result = _verify(table, provenance, pattern, rebuilt)
        if isinstance(result, str):
            _flatten(soup, table, result, notices)
        else:
            _rebuild(soup, table, result, provenance, pattern, rebuilt)

    _append_missing_parts(soup, body, provenance, present, notices)
    _insert_missing_text(
        soup, body, plan.occurrences, notices, provenance.tables, present
    )
    _remove_emptied(emptied)
    for table in body.find_all("table", attrs={_FROM_SOURCE: True}):
        del table[_FROM_SOURCE]

    for anchor in body.find_all("a"):
        name = anchor.get("name") or anchor.get("id") or ""
        if name.startswith(provenance.prefix) and not anchor.get("href"):
            anchor.unwrap()
    strip_sentinels(soup, provenance.prefix)
    body.smooth()
    return notices


_REWRITE_NOTICE = {
    "incomplete": "im Export unvollständig, aus der Quelle ersetzt",
    "missing": "im Export nicht enthalten, an seiner Stelle aus der Quelle eingefügt",
    "unbound": "ohne zuordenbare Herkunft im Export, aus der Quelle ersetzt",
}


def _count(count: int) -> str:
    return f"{count} {'Absatz' if count == 1 else 'Absätze'}"


def _remove_emptied(blocks) -> None:
    """Blocks the rewrite left without any content disappear with it, and so
    does an export table whose every text was replaced."""

    def empty(tag) -> bool:
        return not tag.get_text(strip=True) and tag.find(["img", "table"]) is None

    for block in blocks:
        if block.parent is None:
            continue
        table = block.find_parent("table")
        if block.name not in {"td", "th"} and empty(block):
            for anchor in block.find_all("a"):
                block.insert_before(anchor.extract())
            block.decompose()
        if table is not None and table.parent is not None:
            if not table.get_text(strip=True) and table.find("img") is None:
                table.decompose()


def _missing_parts(provenance, present) -> list[SourcePart]:
    """Parts with no mark of their own in the export, neither part nor paragraph."""
    marks: dict[str, set] = {}
    for text in provenance.texts:
        for paragraph in text.paragraphs:
            marks.setdefault(text.name, set()).update({paragraph.key, paragraph.end})
    return [
        p
        for p in provenance.parts
        if p.uses
        and p.html
        and p.marker not in present
        and not (marks.get(p.name, set()) - {None}) & present
    ]


def _append_missing_parts(soup, body, provenance, present, notices) -> None:
    from bs4 import BeautifulSoup

    for kind in ("header", "footer"):
        missing = [p for p in _missing_parts(provenance, present) if p.kind == kind]
        if not missing:
            continue
        exported = body.find("div", attrs={"title": kind})
        anchor = exported
        for part in missing:
            label = part_label(part)
            logger.warning(
                "Word %s not in Writer export, appended from source: %s",
                kind,
                part.name,
            )
            notices.append(f"{label} aus der Quelle angehängt")
            box = soup.new_tag("div", attrs={"title": kind})
            note = soup.new_tag("p")
            note.string = (
                f"[{label}: im Export nicht enthalten, aus dem Dokument übernommen]"
            )
            box.append(note)
            fragment = BeautifulSoup(part.html, "html.parser")
            for child in list(fragment.contents):
                box.append(child.extract())
            if anchor is not None:
                anchor.insert_after(box)
            elif kind == "header":
                body.insert(0, box)
            else:
                body.append(box)
            anchor = box


_BLOCK_TAGS = {
    "p",
    "li",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "td",
    "th",
    "pre",
    "blockquote",
    "dd",
    "dt",
    "caption",
    "div",
}


@dataclass
class Occurrence:
    """Where one source paragraph stands in the export, if anywhere.

    ``found``: both own marks, all units inside the span between them.
    ``incomplete``: own span, units missing — the span is rewritten.
    ``unbound``: a mark is missing or out of order, the place is still
    known from the marks around it — what stands there is rewritten.
    ``missing``: no text of it at a known place — inserted from the source.
    """

    part: SourceText
    paragraph: SourceParagraph
    status: str
    block: object = field(default=None, repr=False, compare=False)
    hits: list[tuple[int, int]] = field(default_factory=list, repr=False)
    replaced: bool = False  # rewritten at its own place


# Tables rendered from the source carry this until reconciling ends: their
# geometry is the source's already, they are no export tables to verify.
_FROM_SOURCE = "data-word-source"
_NOTE_REGION = re.compile(r"sd(?:foot|end)note\d+$")
_NOTE_ANCHORS = {"sdfootnoteanc", "sdendnoteanc", "sdfootnotesym", "sdendnotesym"}


def _region(node, body) -> str:
    """The export container a node stands in: header, footer, one note, body."""
    for parent in node.parents:
        if parent is body or parent is None:
            break
        if parent.name == "div":
            if parent.get("title") in {"header", "footer"}:
                return parent["title"]
            if _NOTE_REGION.match(parent.get("id") or ""):
                return parent["id"]
    return "body"


def normalize_sentinels(body, prefix: str) -> None:
    """Turn the paragraph sentinels Writer exported as text into anchors.

    Writer places an anchored DrawingML shape one character late, so the
    first one or two characters of the sentinel after it end up in front of
    the shape, in the same block. The random prefix stays unique without
    them; the displaced characters are taken back from that block. A
    sentinel that cannot be restored is no mark: its paragraph is then
    reconciled without it, visibly.
    """
    from bs4 import NavigableString, Tag

    pattern = re.compile("(wq|q)?" + re.escape(prefix[2:]) + r"([az]\d+)_")
    seen: list = []  # (text node, its innermost block), document order

    def innermost(node):
        return node.find_parent(list(_BLOCK_TAGS))

    for node in list(body.find_all(string=True)):
        if type(node) is not NavigableString:
            continue
        text = str(node)
        block = innermost(node)
        if prefix[2:] not in text:
            if text.strip():
                seen.append((node, block))
            continue
        pieces: list = []
        cursor = 0
        for match in pattern.finditer(text):
            head = text[cursor : match.start()]
            lost = prefix[: 2 - len(match.group(1) or "")]
            restored = not lost
            if lost and not head.strip():
                if pieces and isinstance(pieces[-1], str) and pieces[-1].endswith(lost):
                    pieces[-1] = pieces[-1][: -len(lost)]
                    restored = True
                else:
                    for i in range(len(seen) - 1, -1, -1):
                        other, other_block = seen[i]
                        if other_block is not block:
                            continue
                        if str(other).rstrip().endswith(lost):
                            kept = NavigableString(str(other).rstrip()[: -len(lost)])
                            other.replace_with(kept)
                            seen[i] = (kept, block)
                            restored = True
                        break
            pieces.append(head)
            if restored:
                pieces.append(Tag(name="a", attrs={"name": prefix + match.group(2)}))
            else:
                logger.warning("Provenance sentinel damaged in Writer export: %s",
                               match.group(2))
                pieces.append(match.group(0))
            cursor = match.end()
        pieces.append(text[cursor:])
        replacement = [
            NavigableString(x) if isinstance(x, str) else x for x in pieces if x != ""
        ]
        node.replace_with(*replacement)
        seen.extend(
            (x, block)
            for x in replacement
            if isinstance(x, NavigableString) and x.strip()
        )


def strip_sentinels(soup, prefix: str) -> None:
    """Nothing of a sentinel reaches the reader, not even in attributes."""
    pattern = re.compile("(?:wq|q)?" + re.escape(prefix[2:]) + r"[az]\d+_")
    for node in list(soup.find_all(string=True)):
        if prefix[2:] in node:
            node.replace_with(pattern.sub("", str(node)))
    for tag in soup.find_all(True):
        for name, value in list(tag.attrs.items()):
            if isinstance(value, str) and prefix[2:] in value:
                tag[name] = pattern.sub("", value)


class _Flat:
    """Export text without whitespace, with mark offsets and string nodes."""

    def __init__(self, body, prefix: str):
        from bs4 import CData, NavigableString, Tag

        pieces: list[str] = []
        self.offsets: dict[str, int] = {}
        self.anchors: dict[str, object] = {}
        self.order: list[str] = []
        self.starts: list[int] = []
        self.nodes: list = []
        self.node_offset: dict[int, int] = {}
        length = 0
        for node in body.descendants:
            if isinstance(node, Tag):
                name = node.get("name") or node.get("id") or ""
                if node.name == "a" and name.startswith(prefix):
                    key = name[len(prefix) :]
                    if key not in self.offsets:
                        self.offsets[key] = length
                        self.anchors[key] = node
                        self.order.append(key)
                continue
            if type(node) not in (NavigableString, CData):
                continue
            piece = "".join(str(node).split())
            if piece:
                self.starts.append(length)
                self.nodes.append(node)
                self.node_offset[id(node)] = length
                pieces.append(piece)
                length += len(piece)
        self.text = "".join(pieces)

    def node_at(self, position: int):
        return self.nodes[bisect_right(self.starts, position) - 1]

    def nodes_in(self, start: int, end: int) -> list:
        first = bisect_right(self.starts, start - 1)
        result = []
        for index in range(first, len(self.nodes)):
            if self.starts[index] >= end:
                break
            result.append(self.nodes[index])
        return result


def _take(text: str, units: list[str]) -> tuple[list[tuple[int, int]], int]:
    """Match units in order within one paragraph's own text."""
    cursor, hits, lost = 0, [], 0
    for unit in units:
        at = text.find(unit, cursor)
        if at < 0:
            lost += 1
            continue
        hits.append((at, at + len(unit)))
        cursor = at + len(unit)
    return hits, lost


def _block(node, body):
    for parent in [node, *node.parents]:
        if parent is body:
            break
        if getattr(parent, "name", None) in _BLOCK_TAGS:
            return parent
    top = node
    while top.parent is not None and top.parent is not body:
        top = top.parent
    return top


@dataclass
class _Plan:
    """What reconciling decided: occurrences and the export edits they need."""

    occurrences: list[Occurrence]
    flat: _Flat
    # (occurrence, anchor to write after, export nodes to drop)
    rewrites: list = field(default_factory=list)
    # (occurrences, anchor next to the box, export nodes to drop, box before it)
    runs: list = field(default_factory=list)
    # (occurrences of a part without any mark, its export remains)
    whole: list = field(default_factory=list)


def reconcile_text(body, provenance: Provenance, skipped=()) -> list[Occurrence]:
    normalize_sentinels(body, provenance.prefix)
    return _reconcile(body, provenance, skipped).occurrences


def _reconcile(body, provenance: Provenance, skipped=()) -> _Plan:
    """Bind every source paragraph to the export span between its own marks.

    Each displayed source paragraph carries a start and an end sentinel. The
    text between them (without the spans of its own text boxes) is the
    paragraph's occurrence, and nothing else is: text outside every span is
    unbound and never counts for any paragraph. A paragraph with both marks
    is checked against its own span only. A paragraph whose marks are
    missing or out of order is never looked for by text: its place is taken
    from the marks around it, and whatever unbound text stands there is
    replaced by the source paragraph, visibly. Without a known place it is
    inserted next to its neighbour.
    """
    flat = _Flat(body, provenance.prefix)
    entries = [
        (part, paragraph)
        for part in provenance.texts
        if part.name not in skipped
        for paragraph in part.paragraphs
    ]
    # Writer shows one branch of an mc:AlternateContent: the one whose
    # marks it exported (the first, i.e. mc:Choice, if none).
    chosen: dict[str, int] = {}
    for _, paragraph in entries:
        if paragraph.key in flat.offsets or paragraph.end in flat.offsets:
            for group, branch in paragraph.branches:
                chosen[group] = min(chosen.get(group, branch), branch)
    entries = [
        (part, p)
        for part, p in entries
        if all(chosen.get(g, 0) == b for g, b in p.branches)
    ]
    of_key = {p.key: p for _, p in entries if p.key}
    of_end = {p.end: p for _, p in entries if p.end}
    # Every paragraph mark in export order, with its (paragraph, is_end).
    marks = []
    for key in flat.order:
        if key in of_key:
            marks.append((key, of_key[key], False))
        elif key in of_end:
            marks.append((key, of_end[key], True))
    index = {key: i for i, (key, _, _) in enumerate(marks)}
    # A span is valid with both marks in order and properly nested.
    valid: set[int] = set()
    stack: list = []
    for key, paragraph, is_end in marks:
        if not is_end:
            if paragraph.end in index and index[paragraph.end] > index[key]:
                stack.append(paragraph)
                valid.add(id(paragraph))
            continue
        if id(paragraph) not in valid or paragraph not in stack:
            continue
        crossing = stack[stack.index(paragraph) :]
        del stack[stack.index(paragraph) :]
        for other in crossing[1:]:
            valid.discard(id(other))
        if len(crossing) > 1:
            valid.discard(id(paragraph))
    # Owner of the text after each mark: the innermost open valid span.
    segments: list[tuple[int, int, object]] = []
    stack = []
    for i, (key, paragraph, is_end) in enumerate(marks):
        if id(paragraph) in valid:
            if is_end:
                stack.pop()
            else:
                stack.append(paragraph)
        start = flat.offsets[key]
        end = flat.offsets[marks[i + 1][0]] if i + 1 < len(marks) else len(flat.text)
        if end > start:
            segments.append((start, end, stack[-1] if stack else None))
    lead = flat.offsets[marks[0][0]] if marks else len(flat.text)
    if lead:
        segments.insert(0, (0, lead, None))
    owned: dict[int, list[tuple[int, int]]] = {}
    for start, end, owner in segments:
        if owner is not None:
            owned.setdefault(id(owner), []).append((start, end))
    segment_starts = [start for start, _, _ in segments]
    regions = [_region(node, body) for node in flat.nodes]
    extent: dict[str, list[int]] = {}
    for node_start, node, region in zip(flat.starts, flat.nodes, regions):
        size = len("".join(str(node).split()))
        if region in extent:
            extent[region][1] = node_start + size
        else:
            extent[region] = [node_start, node_start + size]
    region_of_node = {id(node): region for node, region in zip(flat.nodes, regions)}

    def unowned(start: int, end: int, region) -> list:
        """Export text nodes between two places that belong to no span."""
        inside = region if callable(region) else region.__eq__
        found = []
        i = max(0, bisect_right(segment_starts, start) - 1)
        while i < len(segments) and segments[i][0] < end:
            s, e, owner = segments[i]
            if owner is None and e > start:
                for node in flat.nodes_in(max(s, start), min(e, end)):
                    if inside(region_of_node[id(node)]) and not _protected(node):
                        found.append(node)
            i += 1
        return found

    def mark_region(i: int) -> str:
        return _region(flat.anchors[marks[i][0]], body)

    def at_mark(i: int) -> int:
        return flat.offsets[marks[i][0]]

    def own_marks(paragraph) -> list[int]:
        return [index[k] for k in (paragraph.key, paragraph.end) if k in index]

    plan = _Plan([], flat)
    result: dict[int, Occurrence] = {}
    for part, paragraph in entries:
        if id(paragraph) not in valid:
            continue
        spans = owned.get(id(paragraph), [])
        text = "".join(flat.text[s:e] for s, e in spans)
        hits, lost = _take(text, paragraph.units)
        anchor = flat.anchors[paragraph.key]
        occurrence = Occurrence(
            part, paragraph, "found" if not lost else "incomplete", _block(anchor, body)
        )
        occurrence.hits = spans
        if lost:
            if not hits:
                occurrence.status = "missing"
            nodes = [n for s, e in spans for n in flat.nodes_in(s, e)]
            nodes = [n for n in nodes if not _protected(n)]
            plan.rewrites.append((occurrence, anchor, nodes))
        result[id(paragraph)] = occurrence

    def descendant(paragraph, ancestor) -> bool:
        parent = paragraph.parent
        while parent:
            if parent == ancestor.key:
                return True
            parent = of_key[parent].parent if parent in of_key else None
        return False

    # Paragraphs with a mark but no valid span: the place reaches from each
    # present mark to the nearest mark on either side that is not one of its
    # own text boxes. What is unbound there is taken as its remains: it is
    # replaced by the whole source paragraph, so nothing of it stays twice.
    # Not towards a neighbour without marks that has nothing to deliver
    # (field results only): what is unbound there may be its, and would be
    # lost.
    neighbour: dict[tuple[int, int], object] = {}
    sequence: dict[int, list] = {}
    for part, paragraph in entries:
        sequence.setdefault(id(part), []).append(paragraph)
    for own in sequence.values():
        for i, paragraph in enumerate(own):
            neighbour[(id(paragraph), -1)] = own[i - 1] if i else None
            neighbour[(id(paragraph), 1)] = own[i + 1] if i + 1 < len(own) else None

    def silent(other) -> bool:
        return (
            other is not None
            and not other.units
            and other.key not in index
            and other.end not in index
        )

    for part, paragraph in entries:
        if id(paragraph) in valid:
            continue
        present = [k for k in (paragraph.key, paragraph.end) if k in index]
        if not present:
            continue
        anchor = flat.anchors[present[0]]
        if not paragraph.units:
            # nothing of the source to deliver; what Writer shows stays
            result[id(paragraph)] = Occurrence(
                part, paragraph, "found", _block(anchor, body)
            )
            continue
        nodes = []
        for here in present:
            for step in (1, -1):
                if silent(neighbour[(id(paragraph), step)]):
                    continue
                j = index[here] + step
                while 0 <= j < len(marks) and (
                    marks[j][1] is paragraph or descendant(marks[j][1], paragraph)
                ):
                    j += step
                if step > 0:
                    start = flat.offsets[here]
                    end = at_mark(j) if j < len(marks) else len(flat.text)
                else:
                    start = at_mark(j) if j >= 0 else 0
                    end = flat.offsets[here]
                nodes += unowned(start, end, _region(flat.anchors[here], body))
        occurrence = Occurrence(
            part, paragraph, "unbound" if nodes else "missing", _block(anchor, body)
        )
        unique = list({id(n): n for n in nodes}.values())
        plan.rewrites.append((occurrence, anchor, unique))
        result[id(paragraph)] = occurrence

    # Paragraphs without any mark: their place is the gap between the
    # neighbours' marks, if those stand next to each other in the export.
    by_part: dict[int, list[SourceParagraph]] = {}
    for part, paragraph in entries:
        by_part.setdefault(id(part), []).append(paragraph)
    parts = {id(part): part for part, _ in entries}
    for key, own in by_part.items():
        part = parts[key]
        i = 0
        while i < len(own):
            if id(own[i]) in result:
                i += 1
                continue
            j = i
            while j < len(own) and id(own[j]) not in result:
                j += 1
            run = [
                Occurrence(part, p, "missing" if p.units else "found")
                for p in own[i:j]
            ]
            for occurrence in run:
                result[id(occurrence.paragraph)] = occurrence
            if not any(p.units for p in own[i:j]):
                i = j  # field results only: nothing to deliver, nothing to judge
                continue
            before = own_marks(own[i - 1]) if i else []
            after = own_marks(own[j]) if j < len(own) else []
            place = None
            if before and after:
                lo, hi = max(before), min(after)
                if hi == lo + 1 and mark_region(lo) == mark_region(hi):
                    place = (lo, at_mark(lo), at_mark(hi))
            elif after:
                # first paragraphs of the part: the region begins with them
                hi = min(after)
                region = mark_region(hi)
                if all(mark_region(k) != region for k in range(hi)):
                    place = (-hi - 1, extent.get(region, [0])[0], at_mark(hi))
            elif before:
                lo = max(before)
                region = mark_region(lo)
                if all(mark_region(k) != region for k in range(lo + 1, len(marks))):
                    end = extent.get(region, [0, len(flat.text)])[1]
                    place = (lo, at_mark(lo), max(end, at_mark(lo)))
            else:
                # the whole part without a mark: its regions, if nothing else
                # is marked there, hold only its remains
                inside = _part_regions(part)
                nodes = unowned(0, len(flat.text), inside)
                if nodes and not any(inside(mark_region(k)) for k in range(len(marks))):
                    plan.whole.append((run, nodes))
                    for occurrence in run:
                        if occurrence.paragraph.units:
                            occurrence.status = "unbound"
            if place is not None:
                at, start, end = place
                which = at if at >= 0 else -at - 1
                anchor = flat.anchors[marks[which][0]]
                nodes = unowned(start, end, _region(anchor, body))
                for occurrence in run:
                    if occurrence.paragraph.units:
                        occurrence.status = "unbound" if nodes else "missing"
                plan.runs.append((run, anchor, nodes, at < 0))
            i = j
    plan.occurrences = [result[id(p)] for _, p in entries]
    return plan


def _part_regions(part: SourceText):
    """Which export containers show a part."""
    if part.kind in {"header", "footer"}:
        return part.kind.__eq__
    if part.kind == "notes":
        note = "sdfootnote" if part.label == "Fußnoten" else "sdendnote"
        return re.compile(note + r"\d+$").match
    return "body".__eq__


def _protected(node) -> bool:
    """Export text that is no paragraph text: note numbers Writer generates."""
    return any(
        set(parent.get("class") or []) & _NOTE_ANCHORS
        for parent in node.parents
        if getattr(parent, "name", None) == "a"
    )


def missing_text(body, provenance: Provenance) -> list[tuple[SourceText, list[str]]]:
    """Source paragraphs whose own occurrence is not complete in the export."""
    result: dict[int, tuple[SourceText, list[str]]] = {}
    for occurrence in reconcile_text(body, provenance):
        if occurrence.status != "found":
            part = occurrence.part
            result.setdefault(id(part), (part, []))[1].append(
                occurrence.paragraph.text
            )
    return list(result.values())


_INLINE_NOTICE = {
    "incomplete": "Absatz im Export unvollständig, aus dem Dokument übernommen:",
    "missing": "Text im Export nicht enthalten, aus dem Dokument übernommen:",
    "unbound": "Herkunft im Export nicht zuordenbar, Absatz aus dem Dokument"
    " übernommen:",
}


def _drop(nodes, emptied: list) -> None:
    for node in nodes:
        block = node.find_parent(list(_BLOCK_TAGS))
        node.extract()
        if block is not None:
            emptied.append(block)


def _rewrite(soup, body, plan: _Plan, prefix: str, tables, present) -> list:
    """Replace each paragraph's export by the source, at its own place."""
    emptied: list = []
    for occurrence, anchor, nodes in plan.rewrites:
        _drop(nodes, emptied)
        label = occurrence.part.label
        text = f"[{label}: {_INLINE_NOTICE[occurrence.status]}] "
        text += occurrence.paragraph.text
        if occurrence.paragraph.end == (anchor.get("name") or "")[len(prefix) :]:
            anchor.insert_before(text)
        else:
            anchor.insert_after(text)
        occurrence.replaced = True
    for run, anchor, nodes, before in plan.runs:
        _drop(nodes, emptied)
        status = "unbound" if any(o.status == "unbound" for o in run) else "missing"
        box = _source_box(soup, run, plan, tables, present, status)
        _place_box(box, anchor, body, before)
    for run, nodes in plan.whole:
        container = next(
            (
                tag
                for tag in nodes[0].parents
                if tag is body
                or tag.name == "div"
                and (tag.get("title") or _NOTE_REGION.match(tag.get("id") or ""))
            ),
            body,
        )
        _drop(nodes, emptied)
        box = _source_box(soup, run, plan, tables, None, "unbound")
        container.append(box)
    return emptied


def _source_box(soup, run, plan, tables, present, status):
    box = soup.new_tag("div")
    note = soup.new_tag("p")
    note.string = f"[{run[0].part.label}: " + (
        "Herkunft im Export nicht zuordenbar, aus dem Dokument übernommen]"
        if status == "unbound"
        else "Text im Export nicht enthalten, aus dem Dokument übernommen]"
    )
    box.append(note)
    _lost_content(soup, box, run, plan.occurrences, tables, present)
    for occurrence in run:
        if occurrence.paragraph.units:
            occurrence.replaced = True
        occurrence.block = box
    return box


def _place_box(box, anchor, body, before) -> None:
    block = _block(anchor, body)
    if block.name in {"td", "th"}:
        if before:
            block.insert(0, box)
        else:
            block.append(box)
    elif before:
        block.insert_before(box)
    else:
        block.insert_after(box)


def _attached(node, body) -> bool:
    return node is not None and any(parent is body for parent in node.parents)


def _outside_tables(node):
    tables = [p for p in node.parents if getattr(p, "name", None) == "table"]
    return tables[-1] if tables else node


def _lost_content(soup, box, run, occurrences, tables, present) -> None:
    """The run's paragraphs; a table lost as a whole comes back as that table.

    ``present``: names of the export's marks — a table with an exported cell
    is not lost (None: whatever Writer made of it is being replaced)."""
    from bs4 import BeautifulSoup

    done: set[str] = set()
    members = {id(o) for o in run}
    for occurrence in run:
        key = occurrence.paragraph.table
        whole = (
            key is not None
            and key not in done
            and all(
                id(o) in members
                for o in occurrences
                if o.part is occurrence.part and o.paragraph.table == key
            )
            and not (
                present is not None
                and any(c.name in present for r in tables[key].rows for c in r.cells)
            )
        )
        if whole:
            done.add(key)
            container = etree.Element("div")
            _table(tables[key].node, container)
            for child in container:
                fragment = etree.tostring(child, method="html", encoding="unicode")
                for node in list(BeautifulSoup(fragment, "html.parser").contents):
                    if getattr(node, "name", None) == "table":
                        node[_FROM_SOURCE] = "1"
                    box.append(node.extract())
        elif key not in done:
            line = soup.new_tag("p")
            line.string = occurrence.paragraph.text
            box.append(line)


def _insert_missing_text(soup, body, occurrences, notices, tables, present) -> None:
    """Insert every paragraph without a known place next to its neighbour."""
    top = body.find("div", attrs={"title": "header"})
    after: dict[int, object] = {}
    by_part: dict[int, list[Occurrence]] = {}
    for occurrence in occurrences:
        by_part.setdefault(id(occurrence.part), []).append(occurrence)

    def placed(o) -> bool:
        return o.block is not None and _attached(o.block, body)

    for own in by_part.values():
        part = own[0].part
        missing = 0
        index = 0
        while index < len(own):
            if own[index].status != "missing" or own[index].replaced:
                index += 1
                continue
            end = index
            while end < len(own) and own[end].status == "missing":
                if own[end].replaced:
                    break
                end += 1
            run = own[index:end]
            missing += len(run)
            logger.warning(
                "Word text not in Writer export, inserted from source: %s (%d)",
                part.name,
                len(run),
            )
            box = soup.new_tag("div")
            note = soup.new_tag("p")
            note.string = (
                f"[{part.label}: Text im Export nicht enthalten, "
                "aus dem Dokument übernommen]"
            )
            box.append(note)
            _lost_content(soup, box, run, own, tables, present)
            for occurrence in run:
                occurrence.replaced = True
            previous = next((o.block for o in reversed(own[:index]) if placed(o)), None)
            following = next((o.block for o in own[end:] if placed(o)), None)
            if previous is not None:
                anchor = _outside_tables(previous)
                after.get(id(anchor), anchor).insert_after(box)
                after[id(anchor)] = box
            elif following is not None:
                _outside_tables(following).insert_before(box)
            elif part.kind == "header":
                if top is not None:
                    top.insert_after(box)
                else:
                    body.insert(0, box)
                top = box
            else:
                body.append(box)
            for occurrence in run:
                occurrence.block = box
            index = end
        if missing:
            notices.append(
                f"{part.label}: {_count(missing)} "
                "im Export nicht enthalten, aus der Quelle angehängt"
            )
