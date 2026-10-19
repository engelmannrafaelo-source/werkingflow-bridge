"""Sanitize Word sources before Writer can expose private or external content."""

import io
import posixpath
import shutil
import zipfile
from urllib.parse import urlsplit

from lxml import etree

from .word_symbols import SYMBOL_UNICODE

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"w": W}
EXTERNAL_IMAGE_NOTICE = "[Extern verlinktes Bild nicht geladen]"


def safe_link(url: str) -> bool:
    """Only explicit web/mail URLs and document-local anchors are active."""
    if any(ord(char) < 32 for char in url):
        return False
    return url.startswith("#") or urlsplit(url).scheme.lower() in {
        "http",
        "https",
        "mailto",
    }



# Budgets apply before any member is inflated, independently of compressed size.
MAX_WORD_ENTRIES = 10_000
MAX_WORD_EXPANDED = 1_000 * 1024 * 1024
MAX_WORD_XML_PART = 50 * 1024 * 1024
MAX_WORD_XML_TOTAL = 100 * 1024 * 1024


def checked_word_package(content: bytes):
    """Return a bounded OOXML Word package; reject other ZIP payloads."""
    from .document_converter import DocumentTooLargeError

    try:
        source = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ValueError("Kein Word-Dokument: gültiges OOXML-Paket erforderlich") from exc
    try:
        entries = source.infolist()
        xml_parts = [i for i in entries if i.filename.endswith((".xml", ".rels"))]
        if (len(entries) > MAX_WORD_ENTRIES
                or sum(i.file_size for i in entries) > MAX_WORD_EXPANDED
                or sum(i.file_size for i in xml_parts) > MAX_WORD_XML_TOTAL
                or any(i.file_size > MAX_WORD_XML_PART for i in xml_parts)):
            raise DocumentTooLargeError("Word package exceeds expanded size/entry budget")
        names = source.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Ambiguous Word package: duplicate ZIP entries")
        required = {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
        if not required.issubset(names):
            raise ValueError("Kein Word-Dokument: OOXML Word-Teile fehlen")
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        document = etree.fromstring(source.read("word/document.xml"), parser)
        types = etree.fromstring(source.read("[Content_Types].xml"), parser)
        rels = etree.fromstring(source.read("_rels/.rels"), parser)
        if (document.tag != f"{{{W}}}document"
                or document.find("w:body", NS) is None
                or not any(n.get("PartName") == "/word/document.xml"
                           and n.get("ContentType") ==
                           "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
                           for n in types)
                or not any(n.get("Type", "").endswith("/officeDocument")
                           and n.get("Target", "").lstrip("/") == "word/document.xml"
                           and n.get("TargetMode") != "External" for n in rels)):
            raise ValueError("Kein Word-Dokument: ungültiger OOXML Word-Hauptteil")
        return source
    except BaseException:
        source.close()
        raise


def sanitize_docx(content: bytes) -> bytes:
    """Accept insertions; discard deletions, comments and effectively hidden runs.

    Resolve run/paragraph style inheritance, including docDefaults. External
    images become visible text BEFORE LibreOffice runs, so it cannot fetch them.
    No ZIP members are extracted to the filesystem.
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    with checked_word_package(content) as source:
        parts = {name: source.read(name) for name in source.namelist()
                 if name.endswith((".xml", ".rels"))}
    styles = {}
    defaults = {}
    if "word/styles.xml" in parts:
        root = etree.fromstring(parts["word/styles.xml"], parser)
        styles = {s.get(f"{{{W}}}styleId"): s for s in root.findall("w:style", NS)}
        defaults = _visibility(root.find("w:docDefaults/w:rPrDefault/w:rPr", NS))

    def style_flags(style_id, seen=None):
        seen = set() if seen is None else seen
        if style_id in seen:
            raise ValueError("Cyclic Word style inheritance")
        if style_id not in styles:
            return {}
        seen.add(style_id)
        style = styles[style_id]
        parent = style.find("w:basedOn", NS)
        flags = (
            style_flags(parent.get(f"{{{W}}}val"), seen) if parent is not None else {}
        )
        flags.update(_visibility(style.find("w:rPr", NS)))
        return flags

    default_styles = [
        key for key, s in styles.items() if s.get(f"{{{W}}}default") == "1"
    ]
    for key in default_styles:
        defaults.update(style_flags(key))

    for name, raw in list(parts.items()):
        if not name.startswith("word/") or not name.endswith(".xml"):
            continue
        root = etree.fromstring(raw, parser)
        # Empty comment parts as well as removing references: nothing downstream
        # can accidentally reintroduce comment authors or bodies.
        if root.tag == f"{{{W}}}comments":
            for child in list(root):
                root.remove(child)
        # A row/cell deletion changes structure, not just run visibility.
        for node in root.xpath("//w:tr[w:trPr/w:del] | //w:tc[w:tcPr/w:cellDel]",
                               namespaces=NS):
            node.getparent().remove(node)
        for table in root.findall(".//w:tbl", NS):
            rows = table.findall("w:tr", NS)
            if not rows or any(not row.findall("w:tc", NS) for row in rows):
                raise ValueError("Empty Word table geometry after tracked deletions")
        for node in root.xpath(
            "//w:del | //w:moveFrom | //w:commentReference | //w:commentRangeStart | //w:commentRangeEnd",
            namespaces=NS,
        ):
            node.getparent().remove(node)
        for symbol in root.findall(".//w:sym", NS):
            code = int(symbol.get(f"{{{W}}}char"), 16)
            font = symbol.get(f"{{{W}}}font", "")
            if font.lower() == "symbol":
                value = SYMBOL_UNICODE.get(code - 0xF000 if code >= 0xF000 else code)
            else:
                value = chr(code) if not (0xE000 <= code <= 0xF8FF) else None
            replacement = etree.Element(f"{{{W}}}t")
            replacement.text = value or f"[Symbol aus {font}: U+{code:04X} nicht als Unicode verfügbar]"
            symbol.getparent().replace(symbol, replacement)
        for run in list(root.findall(".//w:r", NS)):
            flags = dict(defaults)
            paragraph = next(
                (p for p in run.iterancestors() if p.tag == f"{{{W}}}p"), None
            )
            if paragraph is not None:
                style = paragraph.find("w:pPr/w:pStyle", NS)
                if style is not None:
                    flags.update(style_flags(style.get(f"{{{W}}}val")))
            style = run.find("w:rPr/w:rStyle", NS)
            if style is not None:
                flags.update(style_flags(style.get(f"{{{W}}}val")))
            flags.update(_visibility(run.find("w:rPr", NS)))
            if any(flags.values()):
                run.getparent().remove(run)
        for node in root.xpath("//w:ins | //w:moveTo", namespaces=NS):
            parent = node.getparent()
            index = parent.index(node)
            for child in list(node):
                parent.insert(index, child)
                index += 1
            parent.remove(node)

        rel_name = posixpath.join(
            posixpath.dirname(name), "_rels", posixpath.basename(name) + ".rels"
        )
        if rel_name in parts:
            rels = etree.fromstring(parts[rel_name], parser)
            blocked_images = set()
            blocked_links = set()
            for rel in list(rels):
                kind, target = rel.get("Type", ""), rel.get("Target", "")
                if kind.endswith("/image") and rel.get("TargetMode") == "External":
                    blocked_images.add(rel.get("Id"))
                    rels.remove(rel)
                elif kind.endswith("/hyperlink") and not safe_link(target):
                    blocked_links.add(rel.get("Id"))
                    rels.remove(rel)
            for node in list(root.iter()):
                refs = {v for k, v in node.attrib.items() if k.startswith(f"{{{R}}}")}
                if refs & blocked_images:
                    # Replace the containing drawing/pict, not a whole run that
                    # may also hold legitimate text.
                    picture = next(
                        (
                            p
                            for p in [node, *node.iterancestors()]
                            if p.tag in {f"{{{W}}}drawing", f"{{{W}}}pict"}
                        ),
                        None,
                    )
                    if picture is None:
                        raise ValueError(
                            "External Word image has unsupported structure"
                        )
                    if picture.getparent() is not None:
                        text = etree.Element(f"{{{W}}}t")
                        text.text = EXTERNAL_IMAGE_NOTICE
                        picture.getparent().replace(picture, text)
                if node.tag == f"{{{W}}}hyperlink" and refs & blocked_links:
                    parent = node.getparent()
                    index = parent.index(node)
                    for child in list(node):
                        parent.insert(index, child)
                        index += 1
                    parent.remove(node)
            parts[rel_name] = etree.tostring(
                rels, xml_declaration=True, encoding="UTF-8"
            )
        parts[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
    output = io.BytesIO()
    with checked_word_package(content) as source, zipfile.ZipFile(
        output, "w", zipfile.ZIP_DEFLATED
    ) as target:
        for info in source.infolist():
            if info.filename in parts:
                target.writestr(info, parts[info.filename])
            else:
                # Binary members stay out of the in-memory XML working set.
                # Stream in bounded chunks, validating ZIP CRC on the way.
                with source.open(info) as reader, target.open(info, "w") as writer:
                    shutil.copyfileobj(reader, writer, length=64 * 1024)
    return output.getvalue()


def _visibility(properties):
    if properties is None:
        return {}
    return {
        key: el.get(f"{{{W}}}val", "true").lower() not in {"0", "false", "off"}
        for key in ("vanish", "webHidden")
        if (el := properties.find(f"w:{key}", NS)) is not None
    }


def restore_table_geometry(soup, content: bytes) -> None:
    """Restore OOXML cell spans; Writer can put a colspan on the wrong cell.

    Pair tables in document order within each Word part, independently of
    dynamic field results and symbol text. Only spanned tables need repair.
    Row/cell counts still guard against shifted data.
    """
    groups = {"body": [], "header": [], "footer": [], "footnotes": [], "endnotes": []}
    part_counts = {key: 0 for key in groups}
    with checked_word_package(content) as source:
        for name in source.namelist():
            if name == "word/document.xml":
                group = "body"
            elif name.startswith("word/header") and name.endswith(".xml"):
                group = "header"
            elif name.startswith("word/footer") and name.endswith(".xml"):
                group = "footer"
            elif name in {"word/footnotes.xml", "word/endnotes.xml"}:
                group = name.split("/")[-1].split(".")[0]
            else:
                continue
            root = etree.fromstring(source.read(name),
                                    etree.XMLParser(resolve_entities=False, no_network=True))
            tables = root.findall(".//w:tbl", NS)
            if tables:
                part_counts[group] += 1
            groups[group].extend(tables)

    html_groups = {key: [] for key in groups}
    for table in soup.find_all("table"):
        group = "body"
        for parent in table.parents:
            if parent.get("title") in {"header", "footer"}:
                group = parent["title"]
                break
            identity = parent.get("id", "")
            if identity.startswith("sdfootnote"):
                group = "footnotes"
                break
            if identity.startswith("sdendnote"):
                group = "endnotes"
                break
        html_groups[group].append(table)
    pairs = []
    for group, sources in groups.items():
        # Ordinary tables need no source matching: Writer handles fields,
        # tabs, protected hyphens and symbols itself.
        if not any(t.xpath(".//w:gridSpan | .//w:vMerge", namespaces=NS) for t in sources):
            continue
        if part_counts[group] > 1:
            raise ValueError("Ambiguous Word table geometry: multiple " + group + " parts")
        exports = html_groups[group]
        if len(sources) != len(exports):
            raise ValueError("Ambiguous Word table geometry: table count changed in " + group)
        pairs.extend(zip(sources, exports))
    for source_table, table in pairs:
        source_rows = source_table.findall("w:tr", NS)
        html_rows = [r for r in table.find_all("tr") if r.find_parent("table") is table]
        if len(source_rows) != len(html_rows):
            raise ValueError("Word table row count changed during export")
        active = {}
        for source_row, html_row in zip(source_rows, html_rows):
            cells = []
            column = 0
            before = source_row.find("w:trPr/w:gridBefore", NS)
            if before is not None and int(before.get(f"{{{W}}}val", "0")):
                raise ValueError("Word table gridBefore is unsupported")
            current = {}
            for cell in source_row.findall("w:tc", NS):
                span_node = cell.find("w:tcPr/w:gridSpan", NS)
                span = (
                    int(span_node.get(f"{{{W}}}val", "1"))
                    if span_node is not None
                    else 1
                )
                merge = cell.find("w:tcPr/w:vMerge", NS)
                if merge is not None and merge.get(f"{{{W}}}val") != "restart":
                    if column not in active or active[column]["colspan"] != span:
                        raise ValueError("Invalid vertical Word table merge")
                    geometry = active[column]
                    geometry["rowspan"] += 1
                    current[column] = geometry
                else:
                    geometry = {"colspan": span, "rowspan": 1}
                    cells.append(geometry)
                    if merge is not None:
                        current[column] = geometry
                column += span
            active = current
            html_cells = html_row.find_all(["td", "th"], recursive=False)
            if len(cells) != len(html_cells):
                raise ValueError("Word table cell count changed during export")
            for geometry, html_cell in zip(cells, html_cells):
                geometry["element"] = html_cell
            # Store references: subsequent rows extend the same geometry dict.
            for geometry in cells:
                html_cell = geometry["element"]
                html_cell.attrs.pop("rowspan", None)
                html_cell.attrs.pop("colspan", None)
                html_cell["_word_geometry"] = geometry
        for cell in table.find_all(["td", "th"]):
            geometry = cell.attrs.pop("_word_geometry", None)
            if geometry:
                for attr in ("rowspan", "colspan"):
                    if geometry[attr] > 1:
                        cell[attr] = str(geometry[attr])
