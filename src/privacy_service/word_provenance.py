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
"""

from __future__ import annotations

import logging
import posixpath
import re
import secrets
from dataclasses import dataclass, field

from lxml import etree

logger = logging.getLogger("privacy-service.word-provenance")

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS = {"w": W, "r": R}

UNCERTAIN_TABLE_NOTICE = (
    "[Tabellenstruktur nicht sicher zuordenbar: Zellen je Zeile in Dokumentreihenfolge, "
    "ohne Spaltenzuordnung]"
)
VARIANT_LABELS = {"first": "erste Seite", "even": "gerade Seiten", "default": ""}


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
class Provenance:
    prefix: str
    tables: dict[str, SourceTable] = field(default_factory=dict)
    parts: list[SourcePart] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    macros_removed: bool = False

    def cell_pattern(self):
        return re.compile(re.escape(self.prefix) + r"t(\d+)r(\d+)c(\d+)$")


def new_provenance(raw_parts: dict[str, bytes]) -> Provenance:
    while True:
        prefix = "wq" + secrets.token_hex(4) + "_"
        if not any(prefix.encode() in raw for raw in raw_parts.values()):
            return Provenance(prefix)


def table_geometry(table, key: str = "", part: str = "", stamp=None) -> SourceTable:
    """Read OOXML geometry; ``stamp(cell_node, name)`` marks origin cells."""
    rows: list[SourceRow] = []
    active: dict[int, SourceCell] = {}
    valid, reason = True, ""
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
    return SourceTable(key, part, rows, valid, reason)


class _Stamper:
    def __init__(self, provenance: Provenance, next_id: int):
        self.provenance = provenance
        self.next_id = next_id

    def bookmark(self, paragraph, name: str) -> None:
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
        provenance.tables["t" + key] = table_geometry(table, key, name, stamper.cell)


def next_bookmark_id(raw_parts: dict[str, bytes]) -> int:
    ids = [int(m) for raw in raw_parts.values()
           for m in re.findall(rb'w:id="(-?\d+)"', raw)]
    return max([0, *ids]) + 1


# --------------------------------------------------------------------------
# Header/footer variants. Writer's HTML exports one header and one footer.
# --------------------------------------------------------------------------


def header_footer_uses(main_root, main_rels, base: str) -> dict[str, list[tuple[int, str]]]:
    """Map header/footer part names to the (section, type) uses in the body."""
    targets = {}
    for rel in main_rels:
        kind, target = rel.get("Type", ""), rel.get("Target", "")
        if kind.endswith(("/header", "/footer")) and rel.get("TargetMode") != "External":
            targets[rel.get("Id")] = posixpath.normpath(
                target.lstrip("/") if target.startswith("/") else posixpath.join(base, target))
    uses: dict[str, list[tuple[int, str]]] = {}
    for section, sect in enumerate(main_root.iter(q("sectPr")), start=1):
        for ref in [*sect.findall("w:headerReference", NS), *sect.findall("w:footerReference", NS)]:
            target = targets.get(ref.get(f"{{{R}}}id"))
            if target:
                uses.setdefault(target, []).append((section, ref.get(q("type"), "default")))
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


def stamp_header_footer(provenance: Provenance, stamper: _Stamper, name: str, root,
                        uses: list[tuple[int, str]]) -> None:
    kind = "header" if root.tag == q("hdr") else "footer"
    marker = None
    paragraph = next((p for p in root.iter(q("p")) if not _in_fallback(p)), None)
    if paragraph is not None:
        marker = f"p{len(provenance.parts) + 1}"
        stamper.bookmark(paragraph, marker)
    provenance.parts.append(SourcePart(name, kind, marker, uses, render_source_html(root)))


def _in_fallback(node) -> bool:
    return any(a.tag == f"{{{MC}}}Fallback" for a in node.iterancestors())


# --------------------------------------------------------------------------
# Source rendering (for content Writer did not export).
# --------------------------------------------------------------------------


def _paragraph(node, out, blocks) -> None:
    for child in node:
        tag = child.tag
        if tag == f"{{{MC}}}Fallback" or tag in {q("instrText"), q("delText"), q("pPr"), q("rPr")}:
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
    return "".join(etree.tostring(c, method="html", encoding="unicode") for c in container)


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
    return bool(soup_cell.get_text(strip=True)) or soup_cell.find(["img", "table"]) is not None


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
        (t, r), = keys
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
            cells = [m[2] for c in row.find_all(["td", "th"], recursive=False)
                     for m in _own_markers(c, pattern)]
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
                tr.append(soup.new_tag("td", attrs={"colspan": str(source_row.grid_before)}))
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
    # Innermost first: a rebuilt inner table moves along with its cell content.
    for table in reversed(body.find_all("table")):
        has_markers = any(_own_markers(c, pattern) for row in _direct_rows(table)
                          for c in row.find_all(["td", "th"], recursive=False))
        if not has_markers:
            spanned = table.find(lambda tag: tag.name in {"td", "th"} and (
                tag.get("colspan", "1") != "1" or tag.get("rowspan", "1") != "1")) is not None
            if spanned:
                _flatten(soup, table, "Exporttabelle ohne Quellzuordnung", notices)
            continue
        result = _verify(table, provenance, pattern, rebuilt)
        if isinstance(result, str):
            _flatten(soup, table, result, notices)
        else:
            _rebuild(soup, table, result, provenance, pattern, rebuilt)

    present = set()
    for anchor in body.find_all("a"):
        name = anchor.get("name") or anchor.get("id") or ""
        if name.startswith(provenance.prefix):
            present.add(name[len(provenance.prefix):])
    _append_missing_parts(soup, body, provenance, present, notices)

    for anchor in body.find_all("a"):
        name = anchor.get("name") or anchor.get("id") or ""
        if name.startswith(provenance.prefix) and not anchor.get("href"):
            anchor.unwrap()
    body.smooth()
    return notices


def _append_missing_parts(soup, body, provenance, present, notices) -> None:
    from bs4 import BeautifulSoup

    for kind in ("header", "footer"):
        missing = [p for p in provenance.parts if p.kind == kind and p.uses and p.html
                   and p.marker not in present]
        if not missing:
            continue
        exported = body.find("div", attrs={"title": kind})
        anchor = exported
        for part in missing:
            label = part_label(part)
            logger.warning("Word %s not in Writer export, appended from source: %s", kind, part.name)
            notices.append(f"{label} aus der Quelle angehängt")
            box = soup.new_tag("div")
            note = soup.new_tag("p")
            note.string = f"[{label}: im Export nicht enthalten, aus dem Dokument übernommen]"
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
