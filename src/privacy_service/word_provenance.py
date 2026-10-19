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
displayed part carries a bookmark of its own as well, together with its
stored text units. After the export each paragraph is accounted for by its
own occurrence: the text between its bookmark and the next paragraph
bookmark (plus its text boxes) must hold its units, and every character of
the result is consumed by at most one source paragraph. The same text
elsewhere never stands in for it. A paragraph whose occurrence is missing is
inserted whole from the source next to its neighbour, an incomplete one
replaces its own export block — both with a visible notice and named in
``word_notices``, never dropped silently.
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
    key: str | None = None  # bookmark suffix; None when not stamped
    parent: str | None = None  # key of the paragraph holding its text box
    # (mc:AlternateContent group, branch index) of every enclosing alternative
    branches: tuple[tuple[str, int], ...] = ()


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
    return SourceTable(key, part, rows, valid, reason, continuations)


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
        if not self.units:
            return None
        return SourceParagraph(" ".join("".join(self.display).split()), self.units)

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
    for scope in scopes:
        for paragraph in list(scope.iter(q("p"))):
            entry = ledger.paragraph(paragraph)
            if entry is None:
                continue
            entry.branches = _branches(paragraph, groups, name)
            entry.parent = next(
                (keys[a] for a in paragraph.iterancestors() if a in keys), None
            )
            if stamper is not None:
                entry.key = f"s{stamper.next_id}"
                stamper.bookmark(paragraph, entry.key)
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
    box = soup.new_tag("div")
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
    """Rebuild every exported table from its source and append missing parts."""
    notices = list(provenance.notices)
    pattern = provenance.cell_pattern()
    rebuilt: set[str] = set()
    # Presence is a fact of Writer's export; record it before any local
    # rebuild or flattening drops the bookmarks.
    present = set()
    for anchor in body.find_all("a"):
        name = anchor.get("name") or anchor.get("id") or ""
        if name.startswith(provenance.prefix):
            present.add(name[len(provenance.prefix) :])
    # Text is accounted for on Writer's own export, by occurrence; a part
    # appended whole from the source is accounted for as that part.
    appended = {p.name for p in _missing_parts(provenance, present)}
    occurrences, flat = _reconcile(body, provenance, appended)
    keys = {p.key for t in provenance.texts for p in t.paragraphs if p.key}
    replaced: dict[str, int] = {}
    for occurrence in occurrences:
        if occurrence.status == "incomplete" and _replaceable(
            occurrence, flat, keys, provenance.prefix
        ):
            _replace_block(soup, occurrence, provenance.prefix)
            occurrence.replaced = True
            label = occurrence.part.label
            replaced[label] = replaced.get(label, 0) + 1
    for label, count in replaced.items():
        logger.warning(
            "Word paragraph incomplete in Writer export: %s (%d)", label, count
        )
        notices.append(
            f"{label}: {count} {'Absatz' if count == 1 else 'Absätze'} im Export "
            "unvollständig, aus der Quelle ersetzt"
        )
    # Innermost first: a rebuilt inner table moves along with its cell content.
    for table in reversed(body.find_all("table")):
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
    _insert_missing_text(soup, body, occurrences, notices)

    for anchor in body.find_all("a"):
        name = anchor.get("name") or anchor.get("id") or ""
        if name.startswith(provenance.prefix) and not anchor.get("href"):
            anchor.unwrap()
    body.smooth()
    return notices


def _missing_parts(provenance, present) -> list[SourcePart]:
    return [
        p
        for p in provenance.parts
        if p.uses and p.html and p.marker not in present
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
            box = soup.new_tag("div")
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
    """Where one source paragraph stands in the export, if anywhere."""

    part: SourceText
    paragraph: SourceParagraph
    status: str  # found | missing | incomplete
    block: object = field(default=None, repr=False, compare=False)
    hits: list[tuple[int, int]] = field(default_factory=list, repr=False)
    replaced: bool = False  # incomplete block rewritten in place


class _Flat:
    """Export text without whitespace, with bookmark offsets and string nodes."""

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
        self.used = bytearray(len(self.text))

    def node_at(self, position: int):
        return self.nodes[bisect_right(self.starts, position) - 1]

    def span_of(self, block) -> tuple[int, int] | None:
        found = [
            (self.node_offset[id(n)], len("".join(str(n).split())))
            for n in block.find_all(string=True)
            if id(n) in self.node_offset
        ]
        if not found:
            return None
        return found[0][0], found[-1][0] + found[-1][1]


class _Window:
    """Unconsumed export characters of some spans, searchable as one string."""

    def __init__(self, flat: _Flat, spans: list[tuple[int, int]]):
        self.flat = flat
        pieces: list[str] = []
        self.starts: list[int] = []
        self.globals: list[int] = []
        length = 0
        for start, end in sorted(spans):
            i = start
            while i < end:
                j = flat.used.find(1, i, end)
                stop = end if j < 0 else j
                if stop > i:
                    pieces.append(flat.text[i:stop])
                    self.starts.append(length)
                    self.globals.append(i)
                    length += stop - i
                if j < 0:
                    break
                k = flat.used.find(0, j, end)
                i = end if k < 0 else k
        self.text = "".join(pieces)
        self.taken = bytearray(len(self.text))

    def take(self, units: list[str]) -> tuple[list[tuple[int, int]], int]:
        """Match units in order; every character serves at most one unit."""
        cursor, hits, lost = 0, [], 0
        for unit in units:
            at = self.text.find(unit, cursor)
            while at >= 0 and self.taken.find(1, at, at + len(unit)) >= 0:
                at = self.text.find(unit, at + 1)
            if at < 0:
                lost += 1
                continue
            self.taken[at : at + len(unit)] = b"\x01" * len(unit)
            hits.append((at, at + len(unit)))
            cursor = at + len(unit)
        return hits, lost

    def commit(self, hits: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """Mark hits as consumed in the export; return them as export ranges."""
        ranges = []
        for start, end in hits:
            k = bisect_right(self.starts, start) - 1
            while start < end:
                last = k + 1 == len(self.starts)
                stop = min(end, len(self.text) if last else self.starts[k + 1])
                first = self.globals[k] + start - self.starts[k]
                self.flat.used[first : first + stop - start] = b"\x01" * (stop - start)
                ranges.append((first, first + stop - start))
                start = stop
                k += 1
        return ranges


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


def reconcile_text(body, provenance: Provenance, skipped=()) -> list[Occurrence]:
    return _reconcile(body, provenance, skipped)[0]


def _reconcile(body, provenance: Provenance, skipped=()):
    """Account for every active source paragraph by its own occurrence.

    A stamped paragraph is looked for between its bookmark and the next
    paragraph bookmark, together with the regions of its own text boxes
    (deepest first, so a box consumes its text before the paragraph around
    it). A paragraph whose bookmark is not in the export may only use what
    is left between its present neighbours in the same part. Every export
    character is consumed at most once: text elsewhere never stands in.
    """
    flat = _Flat(body, provenance.prefix)
    entries = [
        (part, paragraph)
        for part in provenance.texts
        if part.name not in skipped
        for paragraph in part.paragraphs
    ]
    every_key = {
        p.key for part in provenance.texts for p in part.paragraphs if p.key
    }
    bounds = [k for k in flat.order if k in every_key]
    region = {
        key: (flat.offsets[key], flat.offsets[bounds[i + 1]])
        if i + 1 < len(bounds)
        else (flat.offsets[key], len(flat.text))
        for i, key in enumerate(bounds)
    }
    # Writer shows one branch of an mc:AlternateContent: the one whose
    # bookmarks it exported (the first, i.e. mc:Choice, if none).
    chosen: dict[str, int] = {}
    for _, paragraph in entries:
        if paragraph.key in region:
            for group, branch in paragraph.branches:
                chosen[group] = min(chosen.get(group, branch), branch)
    entries = [
        (part, p)
        for part, p in entries
        if all(chosen.get(g, 0) == b for g, b in p.branches)
    ]
    parents = {p.key: p.parent for _, p in entries if p.key}
    children: dict[str, list[str]] = {}
    for key, parent in parents.items():
        if parent:
            children.setdefault(parent, []).append(key)
    depths: dict[str, int] = {}

    def depth(key):
        if key not in depths:
            parent = parents.get(key)
            depths[key] = depth(parent) + 1 if parent else 0
        return depths[key]

    def spans(key):
        """Its own region and those of the text boxes it holds."""
        own, stack = [], [key]
        while stack:
            current = stack.pop()
            if current in region:
                own.append(region[current])
            stack.extend(children.get(current, []))
        return own

    result: dict[int, Occurrence] = {}
    present = [(part, p) for part, p in entries if p.key in region]
    for part, paragraph in sorted(present, key=lambda e: -depth(e[1].key)):
        window = _Window(flat, spans(paragraph.key))
        hits, lost = window.take(paragraph.units)
        result[id(paragraph)] = Occurrence(
            part,
            paragraph,
            "incomplete" if lost else "found",
            _block(flat.anchors[paragraph.key], body),
            window.commit(hits),
        )
    # Paragraphs without an exported bookmark: only the text left between
    # their present neighbours of the same part.
    for part in {id(part): part for part, _ in entries}.values():
        own = [p for q, p in entries if q is part]
        after: list[int] = []
        following = len(flat.text)
        for paragraph in reversed(own):
            after.append(following)
            if paragraph.key in region:
                following = flat.offsets[paragraph.key]
        after.reverse()
        windows: dict[tuple[int, int], _Window] = {}
        preceding = 0
        for index, paragraph in enumerate(own):
            if paragraph.key in region:
                preceding = flat.offsets[paragraph.key]
                continue
            limits = (preceding, after[index])
            if limits not in windows:
                windows[limits] = _Window(flat, [limits])
            window = windows[limits]
            hits, lost = window.take(paragraph.units)
            occurrence = Occurrence(part, paragraph, "missing")
            if hits:
                occurrence.hits = window.commit(hits)
                occurrence.status = "incomplete" if lost else "found"
                occurrence.block = _block(flat.node_at(occurrence.hits[0][0]), body)
            result[id(paragraph)] = occurrence
    return [result[id(p)] for _, p in entries], flat


def missing_text(body, provenance: Provenance) -> list[tuple[SourceText, list[str]]]:
    """Source paragraphs whose own occurrence is missing or incomplete."""
    result: dict[int, tuple[SourceText, list[str]]] = {}
    for occurrence in reconcile_text(body, provenance):
        if occurrence.status != "found":
            part = occurrence.part
            result.setdefault(id(part), (part, []))[1].append(
                occurrence.paragraph.text
            )
    return list(result.values())


def _replaceable(occurrence: Occurrence, flat: _Flat, keys: set[str], prefix) -> bool:
    """The export block belongs to this paragraph alone and may be rewritten."""
    block = occurrence.block
    if block is None or block.find("table") is not None:
        return False
    own = occurrence.paragraph.key
    for anchor in block.find_all("a"):
        name = anchor.get("name") or anchor.get("id") or ""
        if name.startswith(prefix) and name[len(prefix) :] in keys - {own}:
            return False
    span = flat.span_of(block)
    if span is None:
        return True
    mine = sum(
        min(end, span[1]) - max(start, span[0])
        for start, end in occurrence.hits
        if start < span[1] and end > span[0]
    )
    return flat.used.count(1, span[0], span[1]) == mine


def _replace_block(soup, occurrence: Occurrence, prefix: str) -> None:
    block = occurrence.block
    keep = [
        tag
        for tag in block.find_all(["a", "img"])
        if tag.name == "img"
        or (tag.get("name") or tag.get("id") or "").startswith(prefix)
    ]
    block.clear()
    for tag in keep:
        block.append(tag.extract())
    block.append(
        f"[{occurrence.part.label}: Absatz im Export unvollständig, aus dem Dokument"
        f" übernommen:] {occurrence.paragraph.text}"
    )


def _attached(node, body) -> bool:
    return node is not None and any(parent is body for parent in node.parents)


def _outside_tables(node):
    tables = [p for p in node.parents if getattr(p, "name", None) == "table"]
    return tables[-1] if tables else node


def _insert_missing_text(soup, body, occurrences, notices) -> None:
    """Insert every lost paragraph whole, next to its nearest exported neighbour."""
    top = body.find("div", attrs={"title": "header"})
    after: dict[int, object] = {}
    by_part: dict[int, list[Occurrence]] = {}
    for occurrence in occurrences:
        by_part.setdefault(id(occurrence.part), []).append(occurrence)
    for own in by_part.values():
        part = own[0].part
        counts = {"missing": 0, "incomplete": 0}
        index = 0
        while index < len(own):
            if own[index].status == "found" or own[index].replaced:
                index += 1
                continue
            kind = own[index].status
            end = index
            while end < len(own) and own[end].status == kind and not own[end].replaced:
                end += 1
            run = own[index:end]
            counts[kind] += len(run)
            logger.warning(
                "Word text not in Writer export, inserted from source: %s (%d, %s)",
                part.name,
                len(run),
                kind,
            )
            box = soup.new_tag("div")
            note = soup.new_tag("p")
            note.string = (
                f"[{part.label}: Text im Export nicht enthalten, "
                "aus dem Dokument übernommen]"
                if kind == "missing"
                else f"[{part.label}: Absatz im Export unvollständig, vollständig "
                "aus dem Dokument übernommen]"
            )
            box.append(note)
            for occurrence in run:
                line = soup.new_tag("p")
                line.string = occurrence.paragraph.text
                box.append(line)
            previous = next(
                (
                    o.block
                    for o in reversed(own[:index])
                    if o.status != "missing" and _attached(o.block, body)
                ),
                None,
            )
            following = next(
                (
                    o.block
                    for o in own[end:]
                    if o.status != "missing" and _attached(o.block, body)
                ),
                None,
            )
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
            index = end
        if counts["missing"]:
            count = counts["missing"]
            notices.append(
                f"{part.label}: {count} {'Absatz' if count == 1 else 'Absätze'} "
                "im Export nicht enthalten, aus der Quelle angehängt"
            )
        if counts["incomplete"]:
            count = counts["incomplete"]
            notices.append(
                f"{part.label}: {count} {'Absatz' if count == 1 else 'Absätze'} "
                "im Export unvollständig, vollständig aus der Quelle angehängt"
            )
