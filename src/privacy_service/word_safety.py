"""Sanitize Word sources before Writer can expose private or external content."""

import io
import posixpath
import zipfile
from urllib.parse import urlsplit

from lxml import etree

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


def sanitize_docx(content: bytes) -> bytes:
    """Accept insertions; discard deletions, comments and effectively hidden runs.

    Resolve run/paragraph style inheritance, including docDefaults. External
    images become visible text BEFORE LibreOffice runs, so it cannot fetch them.
    No ZIP members are extracted to the filesystem.
    """
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    with zipfile.ZipFile(io.BytesIO(content)) as source:
        parts = {name: source.read(name) for name in source.namelist()}
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
        for node in root.xpath(
            "//w:del | //w:moveFrom | //w:commentReference | //w:commentRangeStart | //w:commentRangeEnd",
            namespaces=NS,
        ):
            node.getparent().remove(node)
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
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for name, raw in parts.items():
            target.writestr(name, raw)
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

    Match tables by their text, then pair physical cells within each row.
    Ambiguity or changed cell counts is a loud failure, never a shifted value.
    """
    import re

    def signature(text):
        return re.sub(r"\s+", "", text)

    candidates = {}
    with zipfile.ZipFile(io.BytesIO(content)) as source:
        for name in source.namelist():
            if name.startswith("word/") and name.endswith(".xml"):
                root = etree.fromstring(
                    source.read(name),
                    etree.XMLParser(resolve_entities=False, no_network=True),
                )
                for table in root.findall(".//w:tbl", NS):
                    key = signature(
                        "".join(table.xpath(".//w:t/text()", namespaces=NS))
                    )
                    candidates.setdefault(key, []).append(table)
    for table in soup.find_all("table"):
        key = signature(table.get_text())
        matches = candidates.get(key, [])
        if not matches:
            raise ValueError("Word table geometry cannot be matched to source")
        layouts = {
            tuple(
                tuple(
                    tuple(
                        (
                            name,
                            None
                            if (prop := cell.find(f"w:tcPr/w:{name}", NS)) is None
                            else prop.get(f"{{{W}}}val", "continue"),
                        )
                        for name in ("gridSpan", "vMerge")
                    )
                    for cell in row.findall("w:tc", NS)
                )
                for row in candidate.findall("w:tr", NS)
            )
            for candidate in matches
        }
        if len(layouts) > 1:
            raise ValueError("Ambiguous Word table geometry for identical text")
        source_table = matches.pop(0)
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
